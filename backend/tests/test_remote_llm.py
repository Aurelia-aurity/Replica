"""RemoteLLMClient 테스트. 가짜 AI 서버(httpx.MockTransport)로 contracts/baseline 초안의 동작을 확인한다."""

import asyncio
import json

import httpx
import pytest

from app.clients.llm import (
    MAX_BODY_BYTES, MAX_HISTORY_PAIRS, MAX_TEXT_CHARS, LLMResult, RemoteLLMClient, to_history_pairs,
)
from app.core.errors import AppError
from app.schemas.chat import ChatTurn


def ok_body(request: dict, **override) -> dict:
    body = {
        "request_id": request["request_id"],
        "message": "작은 목표부터 시작해 봐.",
        "finish_reason": "stop",
        "ai_generated": True,
        "context": {"used_history_turns": len(request["history"]), "dropped_history_turns": 0},
    }
    body.update(override)
    return body


def error_body(request: dict, code: str, request_id="same") -> dict:
    rid = request["request_id"] if request_id == "same" else request_id
    return {"request_id": rid, "error": {"code": code, "message": "안전한 안내문"}}


def make_client(handler, **kwargs) -> RemoteLLMClient:
    return RemoteLLMClient(
        "http://ai.test", 5, transport=httpx.MockTransport(handler), **kwargs
    )


def call_full(client: RemoteLLMClient, message="안녕", history=None) -> LLMResult:
    return asyncio.run(client.generate(message, history or [], None))


def call(client: RemoteLLMClient, message="안녕", history=None) -> str:
    return call_full(client, message, history).text


def raised(client: RemoteLLMClient, **kwargs) -> AppError:
    with pytest.raises(AppError) as info:
        call(client, **kwargs)
    return info.value


# --- 요청 만들기 ---


def test_request_follows_contract_and_returns_message():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_body(seen["body"]))

    client = make_client(handler, token="secret-token")
    history = [ChatTurn(role="user", content="q1"), ChatTurn(role="assistant", content="a1")]
    assert call(client, "q2", history) == "작은 목표부터 시작해 봐."

    assert seen["url"] == "http://ai.test/v1/baseline/generate"
    assert seen["auth"] == "Bearer secret-token"
    body = seen["body"]
    assert set(body) == {"request_id", "message", "history", "max_new_tokens"}
    assert len(body["request_id"]) == 36
    assert body["message"] == "q2"
    assert body["history"] == [{"user": "q1", "assistant": "a1"}]
    assert body["max_new_tokens"] == 160


def test_each_call_gets_new_request_id():
    ids = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        ids.append(body["request_id"])
        return httpx.Response(200, json=ok_body(body))

    client = make_client(handler)
    call(client)
    call(client)
    assert ids[0] != ids[1]


def test_history_pairs_keep_only_complete_pairs_and_last_20():
    turns = [
        ChatTurn(role="assistant", content="짝 없는 답변"),
        ChatTurn(role="user", content="u0"),
        ChatTurn(role="assistant", content="a0"),
        ChatTurn(role="user", content="  "),
        ChatTurn(role="assistant", content="공백 질문의 답변"),
        ChatTurn(role="user", content="끝나지 않은 질문"),
    ]
    assert to_history_pairs(turns) == [{"user": "u0", "assistant": "a0"}]

    many = []
    for i in range(MAX_HISTORY_PAIRS + 5):
        many += [ChatTurn(role="user", content=f"u{i}"), ChatTurn(role="assistant", content=f"a{i}")]
    pairs = to_history_pairs(many)
    assert len(pairs) == MAX_HISTORY_PAIRS
    assert pairs[0] == {"user": "u5", "assistant": "a5"}  # 오래된 쌍부터 버림


# --- 성공 응답 검증 ---


def test_length_finish_is_success():
    client = make_client(lambda r: httpx.Response(200, json=ok_body(json.loads(r.content), finish_reason="length")))
    assert call(client) == "작은 목표부터 시작해 봐."


@pytest.mark.parametrize(
    "override",
    [
        {"request_id": "22222222-2222-4222-8222-222222222222"},
        {"message": "   "},
        {"finish_reason": "content_filter"},
        {"ai_generated": False},
        {"context": {"used_history_turns": 3, "dropped_history_turns": 0}},  # 합이 제출한 쌍 수와 다름
        {"context": {"used_history_turns": -1, "dropped_history_turns": 1}},
        {"extra": "필드"},
    ],
)
def test_invalid_success_body_is_protocol_failure(override):
    client = make_client(lambda r: httpx.Response(200, json=ok_body(json.loads(r.content), **override)))
    err = raised(client)
    assert (err.http_status, err.code, err.retryable) == (502, "AI_FAILED", True)


def test_non_json_success_is_protocol_failure():
    err = raised(make_client(lambda r: httpx.Response(200, content=b"<html>")))
    assert err.code == "AI_FAILED"


# --- AI 오류 -> 앱 오류 (api-spec 3장 ②) ---


@pytest.mark.parametrize(
    "ai_http, ai_code, app_http, app_code, retryable",
    [
        (400, "INVALID_REQUEST", 502, "AI_FAILED", False),
        (401, "SERVICE_UNAUTHENTICATED", 503, "AI_UNAVAILABLE", False),
        (403, "SERVICE_FORBIDDEN", 503, "AI_UNAVAILABLE", False),
        (413, "PAYLOAD_TOO_LARGE", 502, "AI_FAILED", False),
        (422, "CONTEXT_LIMIT_EXCEEDED", 422, "MESSAGE_TOO_LONG", False),
        (503, "MODEL_NOT_READY", 503, "AI_NOT_READY", True),
        (409, "REQUEST_IN_PROGRESS", 409, "CONFLICT", False),
        (429, "AI_BUSY", 429, "AI_BUSY", True),
        (504, "INFERENCE_DEADLINE_EXCEEDED", 504, "AI_TIMEOUT", True),
        (500, "INFERENCE_FAILED", 502, "AI_FAILED", True),
    ],
)
def test_ai_error_mapping(ai_http, ai_code, app_http, app_code, retryable):
    client = make_client(lambda r: httpx.Response(ai_http, json=error_body(json.loads(r.content), ai_code)))
    err = raised(client)
    assert (err.http_status, err.code, err.retryable, err.stage) == (app_http, app_code, retryable, "llm")


def test_null_request_id_allowed_only_where_contract_allows():
    allowed = make_client(
        lambda r: httpx.Response(401, json=error_body(json.loads(r.content), "SERVICE_UNAUTHENTICATED", None))
    )
    assert raised(allowed).code == "AI_UNAVAILABLE"

    not_allowed = make_client(lambda r: httpx.Response(429, json=error_body(json.loads(r.content), "AI_BUSY", None)))
    assert raised(not_allowed).code == "AI_FAILED"  # 프로토콜 실패로 취급


def test_error_with_other_request_id_is_protocol_failure():
    client = make_client(
        lambda r: httpx.Response(
            429, json=error_body(json.loads(r.content), "AI_BUSY", "22222222-2222-4222-8222-222222222222")
        )
    )
    assert raised(client).code == "AI_FAILED"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, json={"request_id": None, "error": {"code": "NOT_IN_CONTRACT", "message": "x"}}),
        httpx.Response(500, content=b"Internal Server Error"),
        httpx.Response(502, json={"detail": "bad gateway"}),
    ],
)
def test_unknown_error_shape_is_protocol_failure(response):
    err = raised(make_client(lambda r: response))
    assert (err.http_status, err.code) == (502, "AI_FAILED")


def test_http_and_code_must_match():
    client = make_client(lambda r: httpx.Response(500, json=error_body(json.loads(r.content), "AI_BUSY")))
    assert raised(client).code == "AI_FAILED"


# --- 전송 실패: 결과 UNKNOWN, 재전송 없음 ---


def test_connection_refused_is_unavailable_and_not_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("refused")

    err = raised(make_client(handler))
    assert (err.http_status, err.code, err.retryable) == (503, "AI_UNAVAILABLE", True)
    assert len(calls) == 1


def test_dropped_connection_is_timeout_unknown_and_not_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadError("connection reset")

    err = raised(make_client(handler))
    assert (err.http_status, err.code) == (504, "AI_TIMEOUT")
    assert len(calls) == 1


def test_total_timeout_is_ai_timeout():
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200)

    client = RemoteLLMClient("http://ai.test", 0.05, transport=httpx.MockTransport(slow))
    err = raised(client)
    assert (err.http_status, err.code, err.retryable) == (504, "AI_TIMEOUT", True)


# --- 한도: 본문 256 KiB, 문자열 16000자 ---


def _pair(i: int, size: int) -> list[ChatTurn]:
    label = str(i)
    return [ChatTurn(role="user", content="가" * size), ChatTurn(role="assistant", content=label + "나" * (size - len(label)))]


def test_pair_with_text_over_16000_chars_is_excluded():
    turns = _pair(0, MAX_TEXT_CHARS + 1) + _pair(1, 10)
    pairs = to_history_pairs(turns)
    assert len(pairs) == 1 and pairs[0]["assistant"].startswith("1")


def test_oversized_body_drops_oldest_pairs_until_it_fits():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["size"] = len(request.content)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_body(seen["body"]))

    history = []
    for i in range(MAX_HISTORY_PAIRS):  # 한국어 16000자 x 40개는 UTF-8로 약 1.9MB
        history += _pair(i, MAX_TEXT_CHARS)
    call(make_client(handler), "안녕", history)

    assert seen["size"] <= MAX_BODY_BYTES
    kept = seen["body"]["history"]
    assert 0 < len(kept) < MAX_HISTORY_PAIRS
    assert kept[-1]["assistant"].startswith(str(MAX_HISTORY_PAIRS - 1))  # 최근 쪽이 남는다


def test_request_content_type_is_json():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["type"] = request.headers["content-type"]
        return httpx.Response(200, json=ok_body(json.loads(request.content)))

    call(make_client(handler))
    assert seen["type"] == "application/json"


def test_finish_reason_is_returned():
    client = make_client(lambda r: httpx.Response(200, json=ok_body(json.loads(r.content), finish_reason="length")))
    assert call_full(client).finish_reason == "length"


def test_errors_carry_db_error_codes():
    cases = [
        (429, "AI_BUSY", "llm_busy"),
        (504, "INFERENCE_DEADLINE_EXCEEDED", "llm_deadline"),
        (500, "INFERENCE_FAILED", "llm_failed"),
        (503, "MODEL_NOT_READY", "llm_not_ready"),
    ]
    for status, code, db_code in cases:
        client = make_client(lambda r, s=status, c=code: httpx.Response(s, json=error_body(json.loads(r.content), c)))
        assert raised(client).db_error_code == db_code


def test_given_request_id_is_sent_as_is_and_checked_in_the_response():
    given = "33333333-3333-4333-8333-333333333333"
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["id"] = json.loads(request.content)["request_id"]
        return httpx.Response(200, json=ok_body(json.loads(request.content)))

    client = make_client(handler)
    result = asyncio.run(client.generate("안녕", [], None, request_id=given))

    assert seen["id"] == given and result.text


def test_response_with_other_request_id_than_given_is_protocol_failure():
    client = make_client(lambda r: httpx.Response(200, json=ok_body(json.loads(r.content), request_id="x" * 36)))
    with pytest.raises(AppError) as info:
        asyncio.run(client.generate("안녕", [], None, request_id="33333333-3333-4333-8333-333333333333"))
    assert info.value.code == "AI_FAILED"
