"""음성 파일 업로드 검증 (UC-01 예외 흐름: 지원하지 않는 형식이면 안내 후 중단).

오류 code·문구는 contracts/app-backend/api-spec.md 2장을 따른다.
"""

import os

from fastapi import UploadFile

from app.core.config import get_settings
from app.core.errors import AppError

# 웹앱은 브라우저 녹음 결과(webm, ogg, mp4)도 올라오므로 포함한다.
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".webm", ".ogg", ".mp4", ".aac"}


async def read_audio(file: UploadFile) -> bytes:
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise AppError(
            415,
            "UNSUPPORTED_AUDIO_FORMAT",
            f"지원하지 않는 음성 형식입니다({ext or '확장자 없음'}). 지원 형식: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
            retryable=False,
            field="file",
        )

    limit_mb = get_settings().max_upload_mb
    data = await file.read(limit_mb * 1024 * 1024 + 1)  # 한도보다 1바이트만 더 읽어 초과 여부를 확인
    if len(data) > limit_mb * 1024 * 1024:
        raise AppError(413, "FILE_TOO_LARGE", f"파일이 너무 큽니다. 최대 {limit_mb}MB", retryable=False, field="file")
    if not data:
        raise AppError(400, "EMPTY_FILE", "빈 파일입니다.", retryable=False, field="file")
    return data
