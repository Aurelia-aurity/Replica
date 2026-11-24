"""Offline regression for timeout attribution and manual resend through /chat/text."""

import json
from typing import Literal, assert_never
from uuid import UUID

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from app.clients.llm import get_llm_client
from app.core.config import Settings
from app.main import app
from tests.fakes import FakeUserDB, new_id
from tests.test_chat_text import fake_db as fake_db
from tests.test_remote_llm import error_body, make_client, ok_body


@pytest.mark.parametrize(
    ("failure", "diagnostic"),
    [
        pytest.param("ai-deadline", "llm_deadline", id="correlated-ai-504"),
        pytest.param("lost-response", "llm_timeout", id="lost-response"),
        pytest.param("wait-timeout", "llm_timeout", id="wait-timeout"),
    ],
)
def test_timeout_attribution_and_manual_resend(
    fake_db: FakeUserDB,
    monkeypatch: pytest.MonkeyPatch,
    failure: Literal["ai-deadline", "lost-response", "wait-timeout"],
    diagnostic: str,
) -> None:
    sent_ids: list[str] = []
    answer = "다시 보낸 질문에 대한 답"
    turn_id = new_id()
    payload = {"session_id": fake_db.add_session(), "turn_id": turn_id, "message": "안녕"}

    async def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent_ids.append(str(UUID(body["request_id"])))
        if len(sent_ids) == 1:
            match failure:
                case "ai-deadline":
                    return httpx.Response(504, json=error_body(body, "INFERENCE_DEADLINE_EXCEEDED"))
                case "lost-response":
                    raise httpx.ReadError("synthetic lost response", request=request)
                case "wait-timeout":
                    # If the client timer is broken, return success so the 504 assertion fails.
                    with anyio.move_on_after(1):
                        await anyio.Event().wait()
                case unreachable:
                    assert_never(unreachable)
        return httpx.Response(200, json=ok_body(body, message=answer))

    llm = make_client(handle)
    if failure == "wait-timeout":
        monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "0.05")
        # make_client supplies the mock transport and otherwise uses a fixed 5 s timeout.
        monkeypatch.setattr(llm, "_timeout", Settings().llm_timeout_seconds)
    app.dependency_overrides[get_llm_client] = lambda: llm

    with TestClient(app) as client:
        first = client.post("/chat/text", json=payload)

        assert len(sent_ids) == 1
        assert first.status_code == 504
        error = first.json()["error"]
        assert error["code"] == "AI_TIMEOUT"
        assert error["stage"] == "llm"
        assert error["turn_id"] == turn_id
        assert error["retryable"] is True
        failed = fake_db.turns[turn_id]
        assert (failed["answer_status"], failed["error_message"], failed["llm_attempt"]) == (
            "failed", diagnostic, 1
        )
        assert failed["llm_request_id"] == sent_ids[0]

        retry = client.post("/chat/text", json=payload)

    assert len(sent_ids) == 2
    assert sent_ids[1] != sent_ids[0]
    assert retry.status_code == 200
    assert retry.json()["turn_id"] == turn_id
    assert retry.json()["answer"] == answer
    stored = fake_db.turns[turn_id]
    assert (stored["answer_status"], stored["answer"], stored["llm_attempt"]) == ("done", answer, 2)
    assert stored["llm_request_id"] == sent_ids[1]
