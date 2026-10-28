"""요청의 `Authorization: Bearer <Supabase JWT>`로 사용자를 확인한다.

사용자 id는 토큰을 Supabase Auth 서버로 확인해서만 얻는다. 앱이 보낸 user_id는 받지 않는다.
토큰은 로그·예외 문자열·반환값에 넣지 않는다.
"""

import os
from functools import lru_cache
from pathlib import Path

from fastapi import Header

from app import db

ENV_FILE = Path(".env")  # core/config.py(pydantic-settings)가 읽는 파일과 같은 위치(실행 폴더)


def load_db_settings() -> db.Settings:
    """SUPABASE_* 설정을 읽는다. `.env`가 있으면 파일 값을 쓰고, 실제 환경변수가 있으면 그 값이 우선한다.

    pydantic-settings는 `.env` 값을 os.environ에 넣지 않으므로 DB 모듈이 직접 읽어야 한다.
    """
    if ENV_FILE.is_file():
        return db.Settings.from_env_file(ENV_FILE, override=os.environ)
    return db.Settings.from_env()


@lru_cache
def get_database() -> db.Database:
    try:
        return db.Database(load_db_settings())
    except db.ValidationError:
        # SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUPABASE_SECRET_KEY가 없거나 잘못된 서버 설정 문제다.
        raise RuntimeError("Supabase 환경변수 설정을 확인하세요.") from None


async def get_user_db(authorization: str | None = Header(default=None)) -> db.UserDatabase:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise db.AuthenticationError()
    return await get_database().for_user(token.strip())
