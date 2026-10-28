from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str
    env: str
    llm_mode: str
    stt_provider: str
    tts_provider: str
    server_time: datetime


@router.get("/health", response_model=HealthResponse, summary="서버 상태 확인")
def health() -> HealthResponse:
    """서버가 살아 있는지와 현재 연동 모드(mock/실제)를 알려준다."""
    s = get_settings()
    return HealthResponse(
        status="ok",
        app=s.app_name,
        version=s.app_version,
        env=s.app_env,
        llm_mode=s.llm_mode,
        stt_provider=s.stt_provider,
        tts_provider=s.tts_provider,
        server_time=datetime.now(timezone.utc),
    )
