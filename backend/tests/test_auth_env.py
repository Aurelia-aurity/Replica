"""get_database(): SUPABASE_* 설정을 `.env` 파일과 환경변수에서 읽는다 (pydantic-settings는 .env를 os.environ에 넣지 않는다)."""

import pytest

from app import db
from app.core import auth

KEYS = ("SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_SECRET_KEY", "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_DB_TIMEOUT_SECONDS")
ENV_FILE = (
    "APP_ENV=local\n"
    'CORS_ORIGINS=["*"]\n'
    "SUPABASE_URL=https://example.supabase.co\n"
    "SUPABASE_PUBLISHABLE_KEY=sb_publishable_file\n"
    "SUPABASE_SECRET_KEY=sb_secret_file\n"
)


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # .env는 실행 폴더 기준
    auth.get_database.cache_clear()
    yield tmp_path
    auth.get_database.cache_clear()


def test_values_in_env_file_are_used(clean):
    (clean / ".env").write_text(ENV_FILE, encoding="utf-8")
    settings = auth.load_db_settings()
    assert settings.url == "https://example.supabase.co"
    assert settings.publishable_key == "sb_publishable_file" and settings.secret_key == "sb_secret_file"
    assert isinstance(auth.get_database(), db.Database)


def test_real_environment_variables_win_over_the_file(clean, monkeypatch):
    (clean / ".env").write_text(ENV_FILE, encoding="utf-8")
    monkeypatch.setenv("SUPABASE_URL", "https://other.supabase.co")
    assert auth.load_db_settings().url == "https://other.supabase.co"


def test_environment_variables_work_without_a_file(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://env.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_env")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_env")
    assert auth.load_db_settings().url == "https://env.supabase.co"


def test_missing_settings_is_a_server_configuration_error():
    with pytest.raises(RuntimeError):
        auth.get_database()
