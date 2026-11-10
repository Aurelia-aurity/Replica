"""Real API/auth/DB adapters with synthetic Auth/REST responses; no credentials or remote I/O."""

import pytest
from fastapi.testclient import TestClient

from app import db
from app.core import auth
from app.db.transport import Response
from app.main import app

USER_A = "00000000-0000-4000-8000-000000000001"
USER_B = "00000000-0000-4000-8000-000000000002"
PERSONA_A = "00000000-0000-4000-8000-000000000003"
PERSONA_B = "00000000-0000-4000-8000-000000000004"
PRIVATE_MARKER = "synthetic-upstream-private-detail"
SETTINGS = db.Settings("https://example.supabase.co", "sb_publishable_synthetic", "sb_secret_synthetic")


class SyntheticAuthTransport:
    def __init__(self):
        self.calls = []
        self.auth_response = None

    async def request(self, method, path, *, headers, params, body=None):
        self.calls.append({"method": method, "path": path, "headers": dict(headers), "params": dict(params)})
        if path == "/auth/v1/user" and self.auth_response is not None:
            return self.auth_response
        token = headers.get("Authorization")
        assert token in ("Bearer synthetic-a", "Bearer synthetic-b")
        uid, pid = (USER_A, PERSONA_A) if token.endswith("-a") else (USER_B, PERSONA_B)
        if path == "/auth/v1/user":
            return Response(200, {"id": uid, "role": "authenticated", "is_anonymous": False,
                                  "user_metadata": {"user_id": USER_B if uid == USER_A else USER_A}})
        assert method == "GET" and path == "/rest/v1/personas"
        assert params["user_id"] == "eq." + uid
        if params.get("id", "eq." + pid) != "eq." + pid:
            return Response(200, [])
        return Response(200, [{"id": pid, "user_id": uid, "name": "synthetic persona",
                               "status": "active", "data_revision": 0,
                               "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}])


@pytest.fixture
def boundary_client(monkeypatch):
    transport = SyntheticAuthTransport()
    database = db.Database(SETTINGS, transport=transport)
    # Keep get_user_db and Database.for_user active; only replace the network boundary.
    monkeypatch.setattr(auth, "get_database", lambda: database)
    previous = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, transport
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize("header, response, auth_calls", [
    (None, None, 0),
    ("Basic synthetic-a", None, 0),
    ("Bearer   ", None, 0),
    ("Bearer sb_secret_synthetic", None, 0),
    ("Bearer rejected-token", Response(401, {"message": PRIVATE_MARKER}), 1),
    ("Bearer rejected-token", Response(403, {"message": PRIVATE_MARKER}), 1),
    ("Bearer rejected-token", Response(200, {"id": USER_A, "role": "service_role"}), 1),
    ("Bearer rejected-token", Response(200, {"id": USER_A, "role": "authenticated", "is_anonymous": True}), 1),
    ("Bearer rejected-token", Response(200, {"id": "invalid-user-id", "role": "authenticated"}), 1),
])
def test_rejected_auth_never_reaches_application_data(boundary_client, caplog, header, response, auth_calls):
    client, transport = boundary_client
    transport.auth_response = response

    res = client.get("/personas", headers={} if header is None else {"Authorization": header})

    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHENTICATED"
    assert len(transport.calls) == auth_calls
    assert all(call["path"] == "/auth/v1/user" for call in transport.calls)
    for private in (PRIVATE_MARKER, "rejected-token", SETTINGS.secret_key):
        assert private not in res.text and private not in caplog.text


def test_auth_outage_is_503_and_does_not_query_data(boundary_client, caplog):
    client, transport = boundary_client
    transport.auth_response = Response(503, {"message": PRIVATE_MARKER})

    res = client.get("/personas", headers={"Authorization": "Bearer rejected-token"})

    assert res.status_code == 503
    assert res.json()["error"]["code"] == "DB_UNAVAILABLE"
    assert [call["path"] for call in transport.calls] == ["/auth/v1/user"]
    assert PRIVATE_MARKER not in res.text + caplog.text


def test_each_request_uses_auth_id_not_editable_metadata_or_previous_user(boundary_client):
    client, transport = boundary_client
    for token, pid in (("synthetic-a", PERSONA_A), ("synthetic-b", PERSONA_B)):
        res = client.get("/personas", headers={"Authorization": "Bearer " + token})
        assert res.status_code == 200
        assert [item["id"] for item in res.json()["items"]] == [pid]
        assert "user_id" not in res.json()["items"][0]
    assert [call["path"] for call in transport.calls] == ["/auth/v1/user", "/rest/v1/personas"] * 2
    assert [call["params"]["user_id"] for call in transport.calls if call["path"] == "/rest/v1/personas"] == [
        "eq." + USER_A, "eq." + USER_B]
    assert all(call["headers"]["apikey"] == SETTINGS.publishable_key for call in transport.calls)
    assert all(SETTINGS.secret_key not in call["headers"].values() for call in transport.calls)


def test_foreign_persona_lookup_keeps_verified_owner_filter(boundary_client):
    client, transport = boundary_client
    res = client.get("/personas/" + PERSONA_A, headers={"Authorization": "Bearer synthetic-b"})
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"
    assert transport.calls[-1]["params"]["user_id"] == "eq." + USER_B


def test_client_cannot_choose_owner_in_create_request(boundary_client):
    client, transport = boundary_client
    res = client.post("/personas", headers={"Authorization": "Bearer synthetic-a"},
                      json={"name": "synthetic", "user_id": USER_B})
    assert res.status_code == 422
    assert all(call["path"] == "/auth/v1/user" for call in transport.calls)
