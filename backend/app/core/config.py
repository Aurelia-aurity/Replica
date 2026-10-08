"""환경변수(.env) 기반 설정.

값은 코드에 직접 쓰지 않고 .env 또는 서버 환경변수로 주입한다.
API 키 같은 비밀값은 .env에만 두고 저장소에 올리지 않는다(NFR-03).
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Replica API"
    app_version: str = "0.1.0"
    app_env: Literal["local", "dev", "prod"] = "local"
    log_level: str = "INFO"

    # 웹앱은 브라우저가 API를 호출하므로 CORS가 적용된다. 배포 시 "*" 대신 웹앱 도메인만 허용한다.
    cors_origins: list[str] = ["*"]

    # 외부 연동 방식. 값만 바꿔 mock ↔ 실제 구현을 교체한다(NFR-05).
    llm_mode: Literal["mock", "remote"] = "mock"
    stt_provider: str = "mock"
    tts_provider: str = "mock"

    # 음성 업로드 최대 크기(MB). Cloudflare 무료 플랜 요청 한도(약 100MB)보다 작게 둔다.
    max_upload_mb: int = 25


@lru_cache
def get_settings() -> Settings:
    return Settings()
