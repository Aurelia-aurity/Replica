"""앱에 보내는 오류 형식.

본문 형식과 code·문구는 contracts/app-backend/api-spec.md 2·3장과 backend-db 명세 2장을 따른다.
오류 본문과 로그에는 질문·답변·토큰·예외 원문을 넣지 않는다.
"""

import logging
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import db

logger = logging.getLogger(__name__)

INTERNAL_MESSAGE = "서버에 문제가 생겼어요. 잠시 후 다시 시도해 주세요."
CONFLICT_MESSAGE = "이미 처리 중이거나 상태가 바뀌었어요. 새로고침 후 다시 시도해 주세요."
INVALID_INPUT_MESSAGE = "입력한 내용을 확인해 주세요."


class AppError(Exception):
    def __init__(
        self,
        http_status: int,
        code: str,
        message: str,
        *,
        retryable: bool,
        stage: str | None = None,
        field: str | None = None,
        turn_id: UUID | str | None = None,
        db_error_code: str | None = None,
    ):
        self.http_status = http_status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.stage = stage
        self.field = field
        self.turn_id = turn_id
        # DB chat_turns.error_message에 기록할 실패 코드 (예: llm_busy). 앱에는 보내지 않는다.
        self.db_error_code = db_error_code
        super().__init__(code)

    def body(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "stage": self.stage,
                "turn_id": None if self.turn_id is None else str(self.turn_id),
                "retryable": self.retryable,
                "field": self.field,
            }
        }


def from_database_error(
    exc: db.DatabaseError, *, field: str | None = None, turn_id: UUID | str | None = None
) -> AppError:
    """DB 모듈 예외를 앱 오류로 바꾼다 (backend-db 명세 2장 표)."""
    common = {"turn_id": turn_id}
    if isinstance(exc, db.AuthenticationError):
        return AppError(401, "UNAUTHENTICATED", "로그인이 필요하거나 만료되었어요. 다시 로그인해 주세요.",
                        retryable=False, **common)
    if isinstance(exc, db.NotFoundError):
        return AppError(404, "NOT_FOUND", "찾을 수 없는 항목이에요. 목록을 새로고침해 주세요.",
                        retryable=False, **common)
    if isinstance(exc, db.ConflictError):
        if exc.code == "22023":  # create_chat_turn: 같은 turn_id에 다른 입력
            return AppError(409, "TURN_ID_CONFLICT", "같은 요청 번호로 다른 내용을 보낼 수 없어요. 새로 전송해 주세요.",
                            retryable=False, **common)
        return AppError(409, "CONFLICT", CONFLICT_MESSAGE, retryable=False, **common)
    if isinstance(exc, db.ValidationError):
        return AppError(422, "INVALID_INPUT", INVALID_INPUT_MESSAGE, retryable=False, field=field, **common)
    if isinstance(exc, db.DatabaseUnavailableError):
        return AppError(503, "DB_UNAVAILABLE", "일시적으로 저장소에 연결할 수 없어요. 잠시 후 다시 시도해 주세요.",
                        retryable=True, **common)
    # PermissionDeniedError 등: 원인은 서버 로그에만 남긴다.
    return AppError(500, "INTERNAL_ERROR", INTERNAL_MESSAGE, retryable=False, **common)


def _request_validation_error(exc: RequestValidationError) -> AppError:
    first = exc.errors()[0] if exc.errors() else {}
    loc = first.get("loc", ())
    field = str(loc[-1]) if len(loc) > 1 else None
    kind = first.get("type", "")
    if field == "message":
        if kind == "string_too_long":
            return AppError(422, "MESSAGE_TOO_LONG", "질문이 너무 길어요. 더 짧게 입력해 주세요.",
                            retryable=False, field="message")
        if kind in ("string_too_short", "missing", "value_error"):
            return AppError(422, "MESSAGE_EMPTY", "메시지를 입력해 주세요.", retryable=False, field="message")
    return AppError(422, "INVALID_INPUT", INVALID_INPUT_MESSAGE, retryable=False, field=field)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.body())

    @app.exception_handler(db.DatabaseError)
    async def handle_database_error(_: Request, exc: db.DatabaseError) -> JSONResponse:
        error = from_database_error(exc)
        if error.http_status >= 500:
            logger.error("DB 오류 type=%s code=%s", type(exc).__name__, exc.code)
        return JSONResponse(status_code=error.http_status, content=error.body())

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        error = _request_validation_error(exc)
        return JSONResponse(status_code=error.http_status, content=error.body())

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # 예외 원문에 입력값이 섞일 수 있어 종류만 남긴다.
        logger.error("처리하지 못한 오류 type=%s", type(exc).__name__)
        error = AppError(500, "INTERNAL_ERROR", INTERNAL_MESSAGE, retryable=False)
        return JSONResponse(status_code=500, content=error.body())
