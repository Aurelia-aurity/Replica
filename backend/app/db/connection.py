"""DB 연결 풀.

`DATABASE_URL`로 접속한다. 이 연결은 RLS를 우회하므로 백엔드 서버에서만 쓰고,
앱이나 저장소에 값을 노출하지 않는다.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def init_pool(dsn: str | None = None, *, min_size: int = 1, max_size: int | None = None) -> ConnectionPool:
    """연결 풀을 연다. 이미 열려 있으면 기존 풀을 돌려준다.

    FastAPI 시작 시 한 번 호출하면 된다. 호출하지 않아도 첫 쿼리에서 자동으로 연다.
    """
    global _pool
    if _pool is None:
        dsn = dsn or os.environ["DATABASE_URL"]
        if max_size is None:
            max_size = int(os.getenv("DB_POOL_MAX_SIZE", "5"))
        _pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            # Supabase 트랜잭션 모드 풀러(6543)에서도 동작하도록 서버 측 prepared statement를 쓰지 않는다.
            kwargs={"prepare_threshold": None},
            open=True,
        )
    return _pool


def close_pool() -> None:
    """연결 풀을 닫는다. FastAPI 종료 시 호출한다."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """풀에서 연결을 빌린다. 블록이 정상 종료되면 커밋하고, 예외가 나면 롤백한다."""
    with init_pool().connection() as conn:
        yield conn
