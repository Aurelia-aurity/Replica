"""기록 업로드 예약·완료 알림·목록·상세 (contracts/app-backend/api-spec.md '기록' 장, 고도화 1 · #9)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app import db
from app.clients.storage import StorageClient
from app.core.auth import get_user_db
from app.core.storage import get_storage
from app.schemas.record import RecordCreate, RecordList, RecordOut, RecordReserved, UploadedBody
from app.services import records as service

router = APIRouter(prefix="/records", tags=["records"])


def _public(row: dict) -> dict:
    return db.to_public_row("record", row)


def _visible(row: dict) -> dict:
    if row.get("status") == "deleting":  # 삭제 중인 기록은 목록·상세에서 숨긴다.
        raise db.NotFoundError()
    return row


@router.post("", response_model=RecordReserved, status_code=201, summary="기록 업로드 예약")
async def reserve_record(
    req: RecordCreate,
    response: Response,
    user_db: db.UserDatabase = Depends(get_user_db),
    storage: StorageClient = Depends(get_storage),
):
    row, upload, created = await service.reserve(user_db, storage, req)
    if not created:
        response.status_code = 200  # 같은 record_id·같은 본문의 재예약: 기록은 그대로 두고 새 업로드 정보만 준다.
    return {
        **_public(row),
        "upload": {"url": upload.url, "method": "PUT", "headers": dict(upload.headers),
                   "expires_at": upload.expires_at},
    }


@router.post("/{record_id}/uploaded", response_model=RecordOut, summary="업로드 완료 알림")
async def record_uploaded(
    record_id: UUID,
    body: UploadedBody | None = None,
    user_db: db.UserDatabase = Depends(get_user_db),
    storage: StorageClient = Depends(get_storage),
):
    return _public(await service.mark_uploaded(user_db, storage, record_id, body))


@router.get("", response_model=RecordList, summary="기록 목록")
async def list_records(
    persona_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user_db: db.UserDatabase = Depends(get_user_db),
):
    rows = await user_db.list_records(persona_id=persona_id, limit=limit, offset=offset)
    return {"items": [_public(row) for row in rows], "limit": limit, "offset": offset}


@router.get("/{record_id}", response_model=RecordOut, summary="기록 상세")
async def get_record(record_id: UUID, user_db: db.UserDatabase = Depends(get_user_db)):
    return _public(_visible(await user_db.get_record(record_id)))
