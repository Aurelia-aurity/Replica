"""환경변수(.env) 기반 설정.

값은 코드에 직접 쓰지 않고 .env 또는 서버 환경변수로 주입한다.
API 키 같은 비밀값은 .env에만 두고 저장소에 올리지 않는다(NFR-03).
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
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
    # LLM_MODE=remote일 때만 쓴다. 시간 제한 수치는 GPU 측정과 합의 전이라 기본값을 두지 않는다.
    llm_server_url: str | None = None
    llm_server_token: SecretStr | None = None  # 서비스 인증 방식은 #14 확인 후 확정. 지금은 Bearer로 보낸다.
    llm_timeout_seconds: float | None = Field(default=None, gt=0)
    # DB에 기록하는 답변 모델 라벨. 서비스 모델은 미확정이며 Qwen/Qwen3-8B는 #77 결과의 조건부 기준선이다.
    # 배포 라벨이 다르면 LLM_MODEL_NAME으로 바꾼다. 코드·.env.example·README의 기본값은 같아야 한다(#65).
    llm_model_name: str = "Qwen/Qwen3-8B"
    stt_provider: str = "mock"
    tts_provider: str = "mock"
    # 개발용 /stt, /tts 점검 라우트. 기본은 꺼짐(404). 켜도 APP_ENV=prod에서는 열리지 않고,
    # APP_ENV=dev에서는 로그인(Bearer)이 필요하다. 외부 음성 공급자 비용·키가 걸린 경로라서 기본을 닫아 둔다(#67 N1).
    dev_speech_routes: bool = False

    # 음성 업로드 최대 크기(MB). Cloudflare 무료 플랜 요청 한도(약 100MB)보다 작게 둔다.
    max_upload_mb: int = 25


@lru_cache
def get_settings() -> Settings:
    return Settings()
