"""DB 테스트 공통 설정.

로컬 Supabase(`infra/supabase`에서 `supabase start`)에 시드가 들어간 상태를 전제로 한다.
`TEST_DATABASE_URL`(환경 변수 또는 backend/.env)이 없으면 DB 테스트를 건너뛴다.
테스트가 행을 만들고 지우므로 원격 프로젝트 주소를 넣지 않는다.

각 테스트는 자기가 만든 대화방만 쓰고, 끝나면 그 대화방을 지운다(턴은 CASCADE로 함께 삭제).
"""

import os
from pathlib import Path
from uuid import UUID

import pytest
from app.db import close_pool, init_pool
from app.db.connection import connection as db_connection

USER_A = UUID("a0000000-0000-4000-8000-000000000001")
USER_B = UUID("a0000000-0000-4000-8000-000000000002")
PERSONA_A = UUID("b0000000-0000-4000-8000-000000000001")
PERSONA_B = UUID("b0000000-0000-4000-8000-000000000002")


def _test_dsn() -> str | None:
    """환경 변수, 없으면 backend/.env에서 TEST_DATABASE_URL을 읽는다."""
    if os.getenv("TEST_DATABASE_URL"):
        return os.environ["TEST_DATABASE_URL"]
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "TEST_DATABASE_URL":
                return value.strip() or None
    return None


@pytest.fixture(scope="session", autouse=True)
def db_pool():
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL이 없어 DB 테스트를 건너뜁니다.", allow_module_level=True)
    init_pool(dsn)
    yield
    close_pool()


@pytest.fixture
def cleanup_sessions():
    created: list[UUID] = []
    yield created
    if created:
        with db_connection() as conn:
            conn.execute("delete from public.chat_sessions where id = any(%s)", (created,))


@pytest.fixture
def session_a(cleanup_sessions):
    from app.db import chat

    session = chat.create_session(USER_A, PERSONA_A)
    assert session is not None
    cleanup_sessions.append(session.id)
    return session
