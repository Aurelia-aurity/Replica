"""LLM 호출 클라이언트. LLM_MODE 설정으로 mock과 실제 서버를 교체한다(NFR-05).

remote는 AI 서버의 `POST /v1/baseline/generate`를 부른다. 요청·응답·오류 규격의 원본은
contracts/baseline(PR #29, 담당자 합의 전 초안)이고, 앱에 보내는 오류 변환은
contracts/app-backend/api-spec.md 3장을 따른다.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, ValidationError

from app.core.config import get_settings
from app.core.errors import AppError
from app.schemas.chat import ChatTurn

logger = logging.getLogger(__name__)

GENERATE_PATH = "/v1/baseline/generate"
MAX_HISTORY_PAIRS = 20  # 계약의 이력 상한. 넘으면 오래된 쌍부터 뺀다.
MAX_TEXT_CHARS = 16000  # 계약: 질문·이력의 각 문자열은 16000 Unicode code point 이하
MAX_BODY_BYTES = 262144  # 계약: HTTP 원시 본문 256 KiB
DEFAULT_MAX_NEW_TOKENS = 160  # 계약이 제안한 백엔드 기본값(1~1024)

_RETRY_LATER = "답변을 만들지 못했어요. 잠시 후 다시 시도해 주세요."
_UNAVAILABLE = "AI 서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요."
_TIMEOUT = "답변을 만드는 데 시간이 오래 걸리고 있어요. 잠시 후 다시 시도해 주세요."

# AI error.code -> (AI가 보내야 하는 HTTP, 앱 HTTP, 앱 code, 앱 message, retryable, DB 기록 error_code)
_AI_ERRORS = {
    "INVALID_REQUEST": (400, 502, "AI_FAILED", _RETRY_LATER, False, "llm_invalid_request"),
    "SERVICE_UNAUTHENTICATED": (401, 503, "AI_UNAVAILABLE", _UNAVAILABLE, False, "llm_service_auth"),
    "SERVICE_FORBIDDEN": (403, 503, "AI_UNAVAILABLE", _UNAVAILABLE, False, "llm_service_auth"),
    "PAYLOAD_TOO_LARGE": (413, 502, "AI_FAILED", _RETRY_LATER, False, "llm_payload_too_large"),
    "CONTEXT_LIMIT_EXCEEDED": (
        422, 422, "MESSAGE_TOO_LONG", "질문이 너무 길어요. 더 짧게 입력해 주세요.", False, "llm_context_limit",
    ),
    "MODEL_NOT_READY": (
        503, 503, "AI_NOT_READY", "AI를 준비하고 있어요. 잠시 후 다시 시도해 주세요.", True, "llm_not_ready",
    ),
    "REQUEST_IN_PROGRESS": (
        409, 409, "CONFLICT", "이미 처리 중이거나 상태가 바뀌었어요. 새로고침 후 다시 시도해 주세요.", False,
        "llm_in_progress",
    ),
    "AI_BUSY": (
        429, 429, "AI_BUSY", "지금 다른 요청을 처리하고 있어요. 잠시 후 다시 시도해 주세요.", True, "llm_busy",
    ),
    "INFERENCE_DEADLINE_EXCEEDED": (504, 504, "AI_TIMEOUT", _TIMEOUT, True, "llm_deadline"),
    "INFERENCE_FAILED": (500, 502, "AI_FAILED", _RETRY_LATER, True, "llm_failed"),
}
# 요청 ID를 안전하게 얻지 못할 수 있어 request_id가 null이어도 되는 AI 오류(계약 4장)
_NULL_ID_ALLOWED = {"INVALID_REQUEST", "SERVICE_UNAUTHENTICATED", "SERVICE_FORBIDDEN", "PAYLOAD_TOO_LARGE"}


@dataclass(frozen=True)
class LLMResult:
    text: str
    finish_reason: str  # "stop" 또는 "length" (둘 다 성공)


class LLMClient(Protocol):
    name: str

    async def generate(
        self, message: str, history: list[ChatTurn], persona_id: str | None, *, request_id: str | None = None
    ) -> LLMResult:
        """request_id는 이 호출 시도의 AI 요청 ID다. 백엔드가 DB에 먼저 기록한 값을 넘기며, 없으면 새로 만든다."""
        ...


class MockLLMClient:
    name = "mock-llm"

    async def generate(
        self, message: str, history: list[ChatTurn], persona_id: str | None, *, request_id: str | None = None
    ) -> LLMResult:
        return LLMResult(f"(Mock 응답) 받은 메시지: {message}", "stop")


def to_history_pairs(history: list[ChatTurn]) -> list[dict[str, str]]:
    """앱의 role/content 목록을 AI 계약의 완결 대화 쌍으로 바꾼다.

    user 바로 뒤에 assistant가 오는 경우만 한 쌍으로 인정한다. 짝이 없거나 공백뿐인 값, 한쪽이
    16000자를 넘는 쌍은 뺀다. 20쌍을 넘으면 오래된 쌍부터 버린다.
    """
    pairs: list[dict[str, str]] = []
    pending: str | None = None
    for turn in history:
        if turn.role == "user":
            pending = turn.content
        elif pending is not None:
            usable = (
                pending.strip() and turn.content.strip()
                and len(pending) <= MAX_TEXT_CHARS and len(turn.content) <= MAX_TEXT_CHARS
            )
            if usable:
                pairs.append({"user": pending, "assistant": turn.content})
            pending = None
    return pairs[-MAX_HISTORY_PAIRS:]


class _Context(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    used_history_turns: StrictInt
    dropped_history_turns: StrictInt


class _Success(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str
    message: str
    finish_reason: str
    ai_generated: StrictBool
    context: _Context


class _ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str
    message: str


class _ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str | None
    error: _ErrorDetail


def _protocol_error(request_id: str, reason: str) -> AppError:
    """응답이 계약과 다르다: 결과 UNKNOWN. 자동 재시도하지 않는다."""
    logger.error("AI 응답이 계약과 다릅니다 request_id=%s reason=%s", request_id, reason)
    return AppError(502, "AI_FAILED", _RETRY_LATER, retryable=True, stage="llm", db_error_code="llm_protocol_error")


class RemoteLLMClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        *,
        token: str | None = None,
        model_name: str = "Qwen2.5-7B-Instruct",
        max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._url = base_url.rstrip("/") + GENERATE_PATH
        self._timeout = timeout_seconds
        self._token = token
        self._max_new_tokens = max_new_tokens
        self._transport = transport
        self.name = model_name

    async def generate(
        self, message: str, history: list[ChatTurn], persona_id: str | None, *, request_id: str | None = None
    ) -> LLMResult:
        # persona_id는 baseline 요청에 넣지 않는다(개인화는 후속 단계).
        request_id = str(uuid.UUID(str(request_id))) if request_id is not None else str(uuid.uuid4())
        pairs = to_history_pairs(history)
        body, sent_pairs = self._serialize(request_id, message, pairs)
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            # 전체 대기시간은 한 번만 건다. 실패해도 재전송하지 않는다.
            async with asyncio.timeout(self._timeout):
                async with httpx.AsyncClient(transport=self._transport, timeout=None) as http:
                    res = await http.post(self._url, content=body, headers=headers)
        except TimeoutError:
            logger.warning("AI 호출 시간 초과 request_id=%s", request_id)
            raise AppError(504, "AI_TIMEOUT", _TIMEOUT, retryable=True, stage="llm",
                           db_error_code="llm_timeout") from None
        except httpx.ConnectError:
            logger.warning("AI 서버 연결 실패 request_id=%s", request_id)
            raise AppError(503, "AI_UNAVAILABLE", _UNAVAILABLE, retryable=True, stage="llm",
                           db_error_code="llm_unreachable") from None
        except httpx.HTTPError:
            # 연결 끊김 등: 결과 UNKNOWN. AI 504를 만들어 귀속하지 않는다.
            logger.warning("AI 호출 중 연결이 끊김 request_id=%s", request_id)
            raise AppError(504, "AI_TIMEOUT", _TIMEOUT, retryable=True, stage="llm",
                           db_error_code="llm_timeout") from None

        if res.status_code == 200:
            return self._parse_success(res, request_id, sent_pairs)
        raise self._parse_error(res, request_id)

    def _serialize(self, request_id: str, message: str, pairs: list[dict[str, str]]) -> tuple[bytes, int]:
        """실제 전송할 본문을 만들고 256 KiB 이하인지 확인한다. (본문, 실제로 보낸 이력 쌍 수)를 돌려준다.

        넘으면 가장 오래된 이력 쌍부터 뺀다(이력은 문맥일 뿐이다). 현재 질문은 줄이지 않는다.
        응답 context의 합은 '제출한' 쌍 수와 같아야 하므로 실제로 보낸 쌍 수로 검증한다.
        """
        kept = list(pairs)
        while True:
            body = json.dumps(
                {"request_id": request_id, "message": message, "history": kept,
                 "max_new_tokens": self._max_new_tokens},
                ensure_ascii=False,
            ).encode("utf-8")
            if len(body) <= MAX_BODY_BYTES:
                if len(kept) != len(pairs):
                    logger.info("본문 크기 한도로 이력 %d쌍 제외", len(pairs) - len(kept))
                return body, len(kept)
            if not kept:
                raise AppError(502, "AI_FAILED", _RETRY_LATER, retryable=False, stage="llm",
                               db_error_code="llm_payload_too_large")
            kept.pop(0)

    def _parse_success(self, res: httpx.Response, request_id: str, sent_pairs: int) -> LLMResult:
        try:
            body = _Success.model_validate_json(res.content)
        except ValidationError:
            raise _protocol_error(request_id, "success schema") from None
        if body.request_id != request_id:
            raise _protocol_error(request_id, "request_id mismatch")
        if not body.message.strip():
            raise _protocol_error(request_id, "blank message")
        if body.finish_reason not in ("stop", "length"):
            raise _protocol_error(request_id, "finish_reason")
        if body.ai_generated is not True:
            raise _protocol_error(request_id, "ai_generated")
        ctx = body.context
        if min(ctx.used_history_turns, ctx.dropped_history_turns) < 0 or (
            ctx.used_history_turns + ctx.dropped_history_turns != sent_pairs
        ):
            raise _protocol_error(request_id, "context sum")
        return LLMResult(body.message, body.finish_reason)

    def _parse_error(self, res: httpx.Response, request_id: str) -> AppError:
        try:
            body = _ErrorBody.model_validate_json(res.content)
        except ValidationError:
            return _protocol_error(request_id, f"error body (http {res.status_code})")
        known = _AI_ERRORS.get(body.error.code)
        if known is None or known[0] != res.status_code:
            return _protocol_error(request_id, f"unknown code/http {body.error.code} {res.status_code}")
        if body.request_id != request_id and not (
            body.request_id is None and body.error.code in _NULL_ID_ALLOWED
        ):
            return _protocol_error(request_id, "error request_id mismatch")
        _, app_http, app_code, app_message, retryable, db_code = known
        logger.warning("AI 오류 request_id=%s ai_code=%s", request_id, body.error.code)
        return AppError(app_http, app_code, app_message, retryable=retryable, stage="llm", db_error_code=db_code)


def get_llm_client() -> LLMClient:
    settings = get_settings()
    if settings.llm_mode == "mock":
        return MockLLMClient()
    if not settings.llm_server_url or settings.llm_timeout_seconds is None:
        raise RuntimeError("LLM_MODE=remote 에는 LLM_SERVER_URL과 LLM_TIMEOUT_SECONDS가 필요합니다.")
    token = settings.llm_server_token.get_secret_value() if settings.llm_server_token else None
    return RemoteLLMClient(
        settings.llm_server_url,
        settings.llm_timeout_seconds,
        token=token,
        model_name=settings.llm_model_name,
    )
