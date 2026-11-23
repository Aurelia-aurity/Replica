"""Storage 클라이언트 의존성. 테스트에서는 `app.dependency_overrides`로 가짜를 넣는다."""

from functools import lru_cache

from app.clients.storage import StorageClient, SupabaseStorage
from app.core.auth import load_db_settings


@lru_cache
def get_storage() -> StorageClient:
    return SupabaseStorage(load_db_settings())
