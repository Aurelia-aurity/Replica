"""GET /sessions, GET /sessions/{id}, GET /sessions/{id}/turns, DELETE /sessions/{id} (#12). 가짜 DB로 시험한다."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db
from app.clients.llm import LLMResult, get_llm_client
from app.core.auth import get_user_db
from app.main import app
from tests.fakes import FakeLLM, FakeUserDB, new_id

SESSION_KEYS = {"id", "persona_id", "title", "ai_notice_ack_at", "created_at", "updated_at"}
TURN_KEYS = {
    "turn_id", "turn_no", "input_mode", "question", "question_status", "answer", "answer_status", "tts_status",
    "is_ai_generated", "model_name", "finish_reason", "stt_ms", "llm_ms", "tts_ms", "total_ms", "error_message",
    "created_at",
}
INTERNAL = {"user_id", "request_hash", "audio_requested", "stt_attempt", "llm_attempt", "tts_attempt",
            "stt_started_at", "llm_started_at", "tts_started_at", "llm_request_id", "session_id"}


@pytest.fixture
def fake_db():
    fake = FakeUserDB()
    app.dependency_overrides[get_user_db] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def client(fake_db):
    return TestClient(app, raise_server_exceptions=False)


def make_persona(client, name="엄마") -> str:
    return client.post("/personas", json={"name": name}).json()["id"]


def make_session(client, persona_id, title=None) -> str:
    ack = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    res = client.post("/sessions", json={"persona_id": persona_id, "ai_notice_ack_at": ack, "title": title})
    assert res.status_code == 201
    return res.json()["id"]


def error_of(res) -> dict:
    return res.json()["error"]


# --- 목록 ---


def test_empty_list_has_defaults(client):
    assert client.get("/sessions").json() == {"items": [], "limit": 50, "offset": 0}


def test_list_is_newest_first_with_only_contract_fields(client):
    persona = make_persona(client)
    first, second, third = (make_session(client, persona, title=t) for t in ("a", "b", "c"))

    items = client.get("/sessions").json()["items"]

    assert [item["id"] for item in items] == [third, second, first]
    assert all(set(item) == SESSION_KEYS for item in items)


def test_list_paging_and_persona_filter(client):
    mom, dad = make_persona(client, "엄마"), make_persona(client, "아빠")
    mom_sessions = [make_session(client, mom) for _ in range(3)]
    make_session(client, dad)

    page = client.get("/sessions", params={"persona_id": mom, "limit": 2, "offset": 1}).json()

    assert (page["limit"], page["offset"]) == (2, 1)
    assert [item["id"] for item in page["items"]] == [mom_sessions[1], mom_sessions[0]]
    assert len(client.get("/sessions").json()["items"]) == 4


@pytest.mark.parametrize("query, field", [("limit=0", "limit"), ("limit=101", "limit"), ("offset=-1", "offset"),
                                           ("persona_id=not-a-uuid", "persona_id")])
def test_list_rejects_bad_query(client, query, field):
    res = client.get(f"/sessions?{query}")
    assert res.status_code == 422 and error_of(res)["code"] == "INVALID_INPUT" and error_of(res)["field"] == field


# --- 상세 ---


def test_get_session_returns_the_session_object(client):
    persona = make_persona(client)
    session_id = make_session(client, persona, title="첫 대화")

    body = client.get(f"/sessions/{session_id}").json()

    assert set(body) == SESSION_KEYS and body["id"] == session_id and body["title"] == "첫 대화"


def test_get_unknown_session_is_404_with_the_error_envelope(client):
    res = client.get(f"/sessions/{new_id()}")
    assert res.status_code == 404 and error_of(res)["code"] == "NOT_FOUND" and error_of(res)["retryable"] is False


def test_get_session_with_a_malformed_id_is_422(client):
    res = client.get("/sessions/not-a-uuid")
    assert res.status_code == 422 and error_of(res)["code"] == "INVALID_INPUT"


# --- 턴 목록 ---


def test_turns_are_ascending_and_hide_internal_fields(client, fake_db):
    session_id = make_session(client, make_persona(client))
    fake_db.add_old_turn(session_id, "첫 질문", "첫 답")
    fake_db.add_old_turn(session_id, "둘째 질문", "둘째 답")

    body = client.get(f"/sessions/{session_id}/turns").json()

    assert (body["after_turn_no"], body["limit"]) == (0, 50)
    assert [t["question"] for t in body["items"]] == ["첫 질문", "둘째 질문"]
    assert [t["turn_no"] for t in body["items"]] == sorted(t["turn_no"] for t in body["items"])
    for turn in body["items"]:
        assert set(turn) == TURN_KEYS
        assert not INTERNAL & set(turn)
        assert turn["is_ai_generated"] is True and turn["finish_reason"] == "stop"
        assert turn["input_mode"] == "text" and turn["answer_status"] == "done"


def test_failed_turn_keeps_the_safe_error_code_and_is_not_marked_ai_generated(client, fake_db):
    session_id = make_session(client, make_persona(client))
    fake_db.add_old_turn(session_id, "답을 못 받은 질문", None)

    turn = client.get(f"/sessions/{session_id}/turns").json()["items"][0]

    assert turn["answer"] is None and turn["answer_status"] == "failed"
    assert turn["error_message"] == "llm_failed" and turn["is_ai_generated"] is False


def test_turns_cursor_and_limit(client, fake_db):
    session_id = make_session(client, make_persona(client))
    for index in range(5):
        fake_db.add_old_turn(session_id, f"질문 {index}", f"답 {index}", skipped_numbers=index % 2)  # 번호에 빈 칸

    first = client.get(f"/sessions/{session_id}/turns", params={"limit": 2}).json()
    last_no = first["items"][-1]["turn_no"]
    rest = client.get(f"/sessions/{session_id}/turns", params={"after_turn_no": last_no, "limit": 100}).json()

    assert [t["question"] for t in first["items"]] == ["질문 0", "질문 1"]
    assert [t["question"] for t in rest["items"]] == ["질문 2", "질문 3", "질문 4"]
    assert rest["after_turn_no"] == last_no and rest["limit"] == 100


def test_turns_of_other_sessions_are_not_mixed_in(client, fake_db):
    persona = make_persona(client)
    mine, other = make_session(client, persona), make_session(client, persona)
    fake_db.add_old_turn(mine, "내 질문", "내 답")
    fake_db.add_old_turn(other, "남의 질문", "남의 답")

    assert [t["question"] for t in client.get(f"/sessions/{mine}/turns").json()["items"]] == ["내 질문"]


def test_turns_of_an_unknown_session_is_404(client):
    res = client.get(f"/sessions/{new_id()}/turns")
    assert res.status_code == 404 and error_of(res)["code"] == "NOT_FOUND"


@pytest.mark.parametrize("query, field", [("limit=0", "limit"), ("limit=101", "limit"), ("after_turn_no=-1", "after_turn_no")])
def test_turns_reject_bad_query(client, fake_db, query, field):
    session_id = make_session(client, make_persona(client))
    res = client.get(f"/sessions/{session_id}/turns?{query}")
    assert res.status_code == 422 and error_of(res)["field"] == field


def test_a_turn_made_by_chat_text_shows_up_with_its_answer(client, fake_db):
    app.dependency_overrides[get_llm_client] = lambda: FakeLLM(LLMResult("밥 먹었지.", "stop"))
    session_id, turn_id = make_session(client, make_persona(client)), new_id()
    assert client.post("/chat/text", json={"session_id": session_id, "turn_id": turn_id, "message": "뭐 했어?"}).status_code == 200

    turn = client.get(f"/sessions/{session_id}/turns").json()["items"][0]

    assert turn["turn_id"] == turn_id and turn["question"] == "뭐 했어?" and turn["answer"] == "밥 먹었지."
    assert turn["is_ai_generated"] is True and turn["answer_status"] == "done"


# --- 삭제 ---


def test_delete_removes_the_session_and_its_turns_only(client, fake_db):
    persona = make_persona(client)
    gone, kept = make_session(client, persona), make_session(client, persona)
    fake_db.add_old_turn(gone, "지울 질문", "지울 답")
    fake_db.add_old_turn(kept, "남길 질문", "남길 답")

    res = client.delete(f"/sessions/{gone}")

    assert res.status_code == 204 and res.content == b""
    assert client.get(f"/sessions/{gone}").status_code == 404
    assert client.get(f"/sessions/{gone}/turns").status_code == 404
    assert [t["question"] for t in client.get(f"/sessions/{kept}/turns").json()["items"]] == ["남길 질문"]
    assert [s["id"] for s in client.get("/sessions").json()["items"]] == [kept]


def test_delete_twice_or_unknown_is_404(client):
    session_id = make_session(client, make_persona(client))
    assert client.delete(f"/sessions/{session_id}").status_code == 204
    again = client.delete(f"/sessions/{session_id}")
    assert again.status_code == 404 and error_of(again)["code"] == "NOT_FOUND"
    assert client.delete(f"/sessions/{new_id()}").status_code == 404


# --- 인증 ---


@pytest.mark.parametrize("method, path", [
    ("GET", "/sessions"), ("GET", f"/sessions/{new_id()}"), ("GET", f"/sessions/{new_id()}/turns"),
    ("DELETE", f"/sessions/{new_id()}"),
])
def test_login_is_required(method, path):
    app.dependency_overrides.clear()
    res = TestClient(app, raise_server_exceptions=False).request(method, path)
    assert res.status_code == 401 and error_of(res)["code"] == "UNAUTHENTICATED"

# --- 조회할 때 제한 시간을 넘긴 pending 단계 정리 ---


def pending_llm_turn(fake_db, session_id, age_seconds) -> str:
    """LLM 단계가 age_seconds 전에 시작된 채 멈춰 있는 턴을 만든다."""
    turn_id = new_id()

    async def make():
        await fake_db.create_chat_turn(session_id, turn_id, input_mode="text", question="진행 중이던 질문")
        await fake_db.start_chat_stage(turn_id, "llm", model_name="test-model")

    asyncio.run(make())
    fake_db.turns[turn_id]["llm_started_at"] = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return turn_id


def test_listing_closes_a_pending_stage_that_outlived_its_limit(client, fake_db):
    session_id = make_session(client, make_persona(client))
    turn_id = pending_llm_turn(fake_db, session_id, age_seconds=100)

    turn = client.get(f"/sessions/{session_id}/turns").json()["items"][0]

    assert turn["turn_id"] == turn_id
    assert turn["answer_status"] == "failed" and turn["error_message"] == "llm_timeout" and turn["answer"] is None


def test_listing_keeps_a_pending_stage_that_is_still_within_its_limit(client, fake_db):
    session_id = make_session(client, make_persona(client))
    pending_llm_turn(fake_db, session_id, age_seconds=5)

    turn = client.get(f"/sessions/{session_id}/turns").json()["items"][0]

    assert turn["answer_status"] == "pending" and turn["error_message"] is None


def test_listing_without_a_running_stage_does_not_touch_the_expiry(client, fake_db, monkeypatch):
    session_id = make_session(client, make_persona(client))
    fake_db.add_old_turn(session_id, "끝난 질문", "끝난 답")
    calls = []

    async def spy(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(fake_db, "expire_chat_stages", spy)
    assert client.get(f"/sessions/{session_id}/turns").status_code == 200
    assert calls == []


def test_listing_still_works_when_closing_the_stale_stage_fails(client, fake_db, monkeypatch):
    session_id = make_session(client, make_persona(client))
    pending_llm_turn(fake_db, session_id, age_seconds=100)

    async def broken(**kwargs):
        raise db.DatabaseUnavailableError()

    monkeypatch.setattr(fake_db, "expire_chat_stages", broken)
    res = client.get(f"/sessions/{session_id}/turns")

    assert res.status_code == 200 and res.json()["items"][0]["answer_status"] == "pending"
