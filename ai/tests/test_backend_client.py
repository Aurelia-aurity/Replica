"""CPU-only integration: real backend client and loopback AI HTTP handler.

Requires backend/requirements-dev.txt; run with pytest, not unittest discovery.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from test_server import Engine, REQUEST_ID, serving
import server


def test_backend_accepts_success_with_trimmed_history():
    import anyio
    from app.clients.llm import RemoteLLMClient
    from app.schemas.chat import ChatTurn

    # Given: one history pair that must be dropped, and length is still success.
    engine = Engine(context_limit=180)
    engine.reason = "length"
    history = [
        ChatTurn(role="user", content="u" * 30),
        ChatTurn(role="assistant", content="a" * 30),
    ]
    with serving(server.Service(engine)) as port:
        client = RemoteLLMClient(f"http://127.0.0.1:{port}", 5)

        async def generate():
            return await client.generate("question", history, None, request_id=REQUEST_ID)

        # When: the real client serializes and sends over HTTP to the real handler.
        result = anyio.run(generate)

    # Then: the backend has validated the UUID echo, context sum and success fields.
    assert (result.text, result.finish_reason) == (engine.answer, "length")
    assert engine.calls == 1
    assert [m["role"] for m in engine.tokenizer.calls[-1][0]] == ["system", "user"]


def test_backend_maps_not_ready_from_server():
    _assert_error(server.Service(), 503, "AI_NOT_READY", "llm_not_ready")


def test_backend_maps_context_limit_from_server():
    _assert_error(server.Service(Engine(context_limit=1)), 422, "MESSAGE_TOO_LONG", "llm_context_limit")


def test_backend_maps_inference_failure_from_server():
    engine = Engine()
    engine.failure = True
    _assert_error(server.Service(engine), 502, "AI_FAILED", "llm_failed")


def test_backend_accepts_null_id_auth_rejection_from_server():
    _assert_error(server.Service(auth=lambda headers: 401), 503, "AI_UNAVAILABLE", "llm_service_auth")


def _assert_error(service: server.Service, status: int, code: str, db_code: str) -> None:
    import anyio
    import pytest
    from app.clients.llm import RemoteLLMClient
    from app.core.errors import AppError

    # Given: an actual server in the stated failure condition.
    with serving(service) as port:
        client = RemoteLLMClient(f"http://127.0.0.1:{port}", 5)

        async def generate():
            return await client.generate("question", [], None, request_id=REQUEST_ID)

        # When: the backend receives the real server's error envelope.
        with pytest.raises(AppError) as raised:
            anyio.run(generate)

    # Then: code/status/ID validation passed, rather than becoming protocol failure.
    error = raised.value
    assert (error.http_status, error.code, error.db_error_code) == (status, code, db_code)
