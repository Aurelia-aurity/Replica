"""POST·GET /personas, POST /sessions."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.auth import get_user_db
from app.main import app
from tests.fakes import FakeUserDB, new_id


@pytest.fixture
def fake_db():
    fake = FakeUserDB()
    app.dependency_overrides[get_user_db] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def client(fake_db):
    return TestClient(app, raise_server_exceptions=False)


def now_iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


# --- 대화 상대 ---


def test_create_persona_returns_object_without_internal_fields(client):
    res = client.post("/personas", json={"name": "엄마"})
    assert res.status_code == 201
    body = res.json()
    assert set(body) == {"id", "name", "status", "data_revision", "created_at", "updated_at"}
    assert body["name"] == "엄마" and body["status"] == "active" and body["data_revision"] == 0


def test_create_persona_with_client_id_and_duplicate_is_conflict(client):
    persona_id = new_id()
    assert client.post("/personas", json={"name": "엄마", "persona_id": persona_id}).json()["id"] == persona_id
    again = client.post("/personas", json={"name": "엄마", "persona_id": persona_id})
    assert again.status_code == 409 and again.json()["error"]["code"] == "CONFLICT"


@pytest.mark.parametrize("name", ["", "   "])
def test_blank_persona_name_is_rejected(client, name):
    res = client.post("/personas", json={"name": name})
    assert res.status_code == 422
    assert (res.json()["error"]["code"], res.json()["error"]["field"]) == ("INVALID_INPUT", "name")


def test_list_and_get_persona(client):
    first = client.post("/personas", json={"name": "엄마"}).json()
    client.post("/personas", json={"name": "아빠"})

    listed = client.get("/personas").json()
    assert (listed["limit"], listed["offset"], len(listed["items"])) == (50, 0, 2)
    assert client.get(f"/personas/{first['id']}").json()["name"] == "엄마"


def test_get_unknown_persona_is_404(client):
    res = client.get(f"/personas/{new_id()}")
    assert res.status_code == 404 and res.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("query, field", [("limit=101", "limit"), ("limit=0", "limit"), ("offset=-1", "offset")])
def test_list_paging_limits(client, query, field):
    res = client.get(f"/personas?{query}")
    assert res.status_code == 422 and res.json()["error"]["field"] == field


# --- 대화방 ---


def test_create_session(client):
    persona = client.post("/personas", json={"name": "엄마"}).json()
    res = client.post(
        "/sessions",
        json={"persona_id": persona["id"], "ai_notice_ack_at": now_iso(timedelta(seconds=-5)), "title": "첫 대화"},
    )
    assert res.status_code == 201
    body = res.json()
    assert set(body) == {"id", "persona_id", "title", "ai_notice_ack_at", "created_at", "updated_at"}
    assert body["persona_id"] == persona["id"] and body["title"] == "첫 대화"


def test_session_for_unknown_persona_is_404(client):
    res = client.post("/sessions", json={"persona_id": new_id(), "ai_notice_ack_at": now_iso(timedelta(seconds=-5))})
    assert res.status_code == 404 and res.json()["error"]["code"] == "NOT_FOUND"


def test_future_ack_time_is_rejected(client):
    persona = client.post("/personas", json={"name": "엄마"}).json()
    res = client.post("/sessions", json={"persona_id": persona["id"], "ai_notice_ack_at": now_iso(timedelta(hours=1))})
    assert res.status_code == 422
    assert (res.json()["error"]["code"], res.json()["error"]["field"]) == ("INVALID_INPUT", "ai_notice_ack_at")


@pytest.mark.parametrize(
    "ack, field",
    [("2026-10-27T10:00:00", "ai_notice_ack_at"), ("not-a-date", "ai_notice_ack_at")],
)
def test_ack_time_needs_timezone(client, ack, field):
    persona = client.post("/personas", json={"name": "엄마"}).json()
    res = client.post("/sessions", json={"persona_id": persona["id"], "ai_notice_ack_at": ack})
    assert res.status_code == 422 and res.json()["error"]["field"] == field


def test_session_title_limits(client):
    persona = client.post("/personas", json={"name": "엄마"}).json()
    base = {"persona_id": persona["id"], "ai_notice_ack_at": now_iso(timedelta(seconds=-5))}
    assert client.post("/sessions", json={**base, "title": "가" * 200}).status_code == 201
    too_long = client.post("/sessions", json={**base, "title": "가" * 201})
    blank = client.post("/sessions", json={**base, "title": "  "})
    assert too_long.status_code == blank.status_code == 422
    assert too_long.json()["error"]["field"] == blank.json()["error"]["field"] == "title"
