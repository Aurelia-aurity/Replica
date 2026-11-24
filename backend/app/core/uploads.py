"""음성 파일 업로드 검증 (UC-01 예외 흐름: 지원하지 않는 형식이면 안내 후 중단).

오류 code·문구는 contracts/app-backend/api-spec.md 2장을 따른다.
"""

import os

from fastapi import UploadFile

from app.core.config import get_settings
from app.core.errors import AppError

# 웹앱은 브라우저 녹음 결과(webm, ogg, mp4)도 올라오므로 포함한다.
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".webm", ".ogg", ".mp4", ".aac"}

# 일부 클라이언트는 content-type을 application/octet-stream으로 보내므로 허용한다(내용은 시그니처로 확인).
ALLOWED_CONTENT_TYPES = {"video/mp4", "video/webm", "application/octet-stream"}


def _mime_allowed(content_type: str | None) -> bool:
    base = (content_type or "").split(";")[0].strip().lower()
    return not base or base.startswith("audio/") or base in ALLOWED_CONTENT_TYPES


def _signature_matches(ext: str, data: bytes) -> bool:
    """확장자별 파일 머리 시그니처 확인. 디코딩까지는 하지 않는다(공급자가 디코딩 오류를 돌려준다)."""
    head = data[:16]
    if ext == ".wav":
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if ext == ".mp3":
        return head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if ext in (".m4a", ".mp4"):
        return head[4:8] == b"ftyp"
    if ext == ".webm":
        return head[:4] == b"\x1a\x45\xdf\xa3"
    if ext == ".ogg":
        return head[:4] == b"OggS"
    if ext == ".aac":  # ADTS 프레임 동기, ID3 태그, ADIF
        return head[:3] == b"ID3" or head[:4] == b"ADIF" or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xF0 == 0xF0)
    return False


def _unsupported(message: str) -> AppError:
    return AppError(415, "UNSUPPORTED_AUDIO_FORMAT", message, retryable=False, field="file")


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
    if not _mime_allowed(file.content_type):
        raise _unsupported("음성 파일이 아닙니다. 녹음 파일을 다시 선택해 주세요.")

    data = await file.read(limit_mb * 1024 * 1024 + 1)  # 한도보다 1바이트만 더 읽어 초과 여부를 확인
    if len(data) > limit_mb * 1024 * 1024:
        raise AppError(413, "FILE_TOO_LARGE", f"파일이 너무 큽니다. 최대 {limit_mb}MB", retryable=False, field="file")
    if not data:
        raise AppError(400, "EMPTY_FILE", "빈 파일입니다.", retryable=False, field="file")
    if not _signature_matches(ext, data):
        raise _unsupported(f"파일 내용이 {ext} 음성 형식과 맞지 않습니다. 녹음 파일을 다시 선택해 주세요.")
    return data
