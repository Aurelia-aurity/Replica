"""Framework-neutral adapter for Ysj00000's backend-db draft. No raw errors escape."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from .errors import (AuthenticationError, ConflictError, DatabaseError, DatabaseUnavailableError,
                     NotFoundError, ValidationError)

MESSAGES = {
    "INVALID_INPUT": "입력한 내용을 확인해 주세요.",
    "INTERNAL_ERROR": "서버에 문제가 생겼어요. 잠시 후 다시 시도해 주세요.",
    "TURN_ID_CONFLICT": "같은 요청 번호로 다른 내용을 보낼 수 없어요. 새로 전송해 주세요.",
    "UNAUTHENTICATED": "로그인이 필요하거나 만료되었어요. 다시 로그인해 주세요.",
    "NOT_FOUND": "찾을 수 없는 항목이에요. 목록을 새로고침해 주세요.",
    "CONFLICT": "이미 처리 중이거나 상태가 바뀌었어요. 새로고침 후 다시 시도해 주세요.",
    "EDIT_CONFLICT": "다른 곳에서 먼저 수정되었거나 이미 확정된 내용이에요. 최신 내용을 불러와 확인해 주세요.",
    "DB_UNAVAILABLE": "일시적으로 저장소에 연결할 수 없어요. 잠시 후 다시 시도해 주세요.",
}
_FIELDS = {"name", "title", "persona_id", "session_id", "turn_id", "record_id", "text_version_id",
           "content", "revision", "limit", "offset", "after_turn_no", "ai_notice_ack_at", "message",
           "original_name", "mime_type", "size_bytes", "consent_at", "consent_version"}


@dataclass(frozen=True)
class HttpDatabaseError:
    status_code: int
    body: dict
    # Server-only guidance; not serialized into body or treated as an automatic retry.
    outcome_unknown: bool = False


def to_http_error(error: DatabaseError, *, operation: str | None = None,
                  input_source: Literal["client", "server"] = "server",
                  field: str | None = None, stage: str | None = None,
                  turn_id: str | UUID | None = None) -> HttpDatabaseError:
    """Use trusted operation/context, never values copied from an upstream error message."""
    status, code, retryable = 500, "INTERNAL_ERROR", False
    if isinstance(error, ValidationError) and input_source == "client":
        status, code = 422, "INVALID_INPUT"
    elif isinstance(error, AuthenticationError):
        status, code = 401, "UNAUTHENTICATED"
    elif isinstance(error, NotFoundError):
        status, code = 404, "NOT_FOUND"
    elif isinstance(error, ConflictError):
        status, code = 409, "CONFLICT"
        if operation == "create_chat_turn" and error.code == "22023":
            code = "TURN_ID_CONFLICT"
        elif operation == "edit_text_draft":
            code = "EDIT_CONFLICT"
    elif isinstance(error, DatabaseUnavailableError):
        status, code, retryable = 503, "DB_UNAVAILABLE", True
    try:
        safe_turn_id = None if turn_id is None else str(UUID(str(turn_id)))
    except (TypeError, ValueError, AttributeError):
        safe_turn_id = None
    return HttpDatabaseError(status, {"error": {"code": code, "message": MESSAGES[code],
        "stage": stage if stage in ("stt", "llm", "tts") else None, "turn_id": safe_turn_id,
        "retryable": retryable, "field": field if code == "INVALID_INPUT" and field in _FIELDS else None}},
        outcome_unknown=error.outcome_unknown)
