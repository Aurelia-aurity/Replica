import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.body_limit import BodyLimitMiddleware
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.routers import chat, health, personas, sessions, speech

settings = get_settings()

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="레플리카(인간 복제 보험 서비스) 백엔드 API. 모든 AI 응답은 AI 생성 콘텐츠입니다.",
)

# 본문 크기 확인은 파서가 읽기 전에 한도를 넘는 본문을 끊는다. add_middleware는 나중에 추가한 것이 바깥이므로
# CORS를 나중에 추가해, 선언된 크기 초과로 즉시 돌려주는 413에도 CORS 헤더가 붙게 한다(브라우저가 오류 본문을 읽는다).
app.add_middleware(BodyLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_error_handlers(app)

app.include_router(health.router)
app.include_router(personas.router)
app.include_router(sessions.router)
app.include_router(chat.router)
app.include_router(speech.router)
