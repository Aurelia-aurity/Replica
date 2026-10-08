"""음성 파일 업로드 검증 (UC-01 예외 흐름: 지원하지 않는 형식이면 안내 후 중단)."""

import os

from fastapi import HTTPException, UploadFile

from app.core.config import get_settings

# 웹앱은 브라우저 녹음 결과(webm, ogg, mp4)도 올라오므로 포함한다.
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".webm", ".ogg", ".mp4", ".aac"}


async def read_audio(file: UploadFile) -> bytes:
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"지원하지 않는 음성 형식입니다({ext or '확장자 없음'}). 지원 형식: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)  # 한도보다 1바이트만 더 읽어 초과 여부를 확인
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"파일이 너무 큽니다. 최대 {get_settings().max_upload_mb}MB")
    if not data:
        raise HTTPException(status_code=400, detail="빈 파일입니다.")
    return data
