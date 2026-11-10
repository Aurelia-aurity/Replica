"""요청 본문 크기 제한 (#67 N2).

multipart 파서가 본문을 다 받기 전에 끊기 위해 ASGI 단계에서 확인한다.
- Content-Length가 한도를 넘으면 본문을 읽지 않고 413으로 응답한다.
- Content-Length가 없거나 거짓이어도(청크 전송 등) 읽은 바이트가 한도를 넘으면 413으로 끊는다.
음성 업로드 경로는 `MAX_UPLOAD_MB` + multipart 머리글 여유분, 그 외 경로는 JSON 한도(1MiB)를 쓴다.
"""

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.errors import AppError

UPLOAD_PATHS = {"/stt", "/chat/voice"}
MULTIPART_OVERHEAD = 1024 * 1024  # multipart 경계·머리글·다른 폼 필드 여유
JSON_LIMIT = 1024 * 1024  # 질문은 최대 16,000자(UTF-8 약 48KB)라 1MiB면 충분하다


class BodyTooLarge(HTTPException):
    """요청 본문이 한도를 넘었다. 오류 봉투 변환은 core/errors.py가 한다."""

    def __init__(self, upload: bool):
        super().__init__(status_code=413)
        self.upload = upload


def limit_for(path: str) -> tuple[int, bool]:
    if path in UPLOAD_PATHS:
        return get_settings().max_upload_mb * 1024 * 1024 + MULTIPART_OVERHEAD, True
    return JSON_LIMIT, False


def too_large_error(upload: bool) -> AppError:
    if upload:
        return AppError(413, "FILE_TOO_LARGE", f"파일이 너무 큽니다. 최대 {get_settings().max_upload_mb}MB",
                        retryable=False, field="file")
    return AppError(413, "REQUEST_TOO_LARGE", "요청이 너무 커요. 내용을 줄여 다시 시도해 주세요.", retryable=False)


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        limit, upload = limit_for(scope["path"])
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            error = too_large_error(upload)
            await JSONResponse(status_code=413, content=error.body())(scope, receive, send)
            return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge(upload)
            return message

        await self.app(scope, limited_receive, send)
