import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app import db
from app.core.auth import get_user_db
from app.core.errors import INVALID_INPUT_MESSAGE, AppError
from app.db import UserDatabase
from app.schemas.session import SessionCreate, SessionList, SessionOut
from app.schemas.turn import TurnList
from app.services import turn_stages as stages

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionOut, status_code=201, summary="대화방 생성")
async def create_session(req: SessionCreate, user_db: UserDatabase = Depends(get_user_db)):
    try:
        return await user_db.create_session(
            req.persona_id, ai_notice_ack_at=req.ai_notice_ack_at, title=req.title, session_id=req.session_id
        )
    except db.ValidationError:
        # 형식은 요청 검증에서 이미 확인했으므로 남는 경우는 확인 시각이 미래인 것이다.
        raise AppError(422, "INVALID_INPUT", INVALID_INPUT_MESSAGE, retryable=False, field="ai_notice_ack_at") from None


@router.get("", response_model=SessionList, summary="대화 이력 목록")
async def list_sessions(
    persona_id: UUID | None = Query(default=None, description="특정 대화 상대의 대화방만"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user_db: UserDatabase = Depends(get_user_db),
):
    items = await user_db.list_sessions(persona_id=persona_id, limit=limit, offset=offset)
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/{session_id}", response_model=SessionOut, summary="대화방 상세")
async def get_session(session_id: UUID, user_db: UserDatabase = Depends(get_user_db)):
    return await user_db.get_session(session_id)


@router.get("/{session_id}/turns", response_model=TurnList, summary="대화방의 턴 목록 (이어 보기)")
async def list_turns(
    session_id: UUID,
    after_turn_no: int = Query(default=0, ge=0, description="이 번호보다 큰 턴만 조회"),
    limit: int = Query(default=50, ge=1, le=100),
    user_db: UserDatabase = Depends(get_user_db),
):
    rows = await user_db.list_turns(session_id, after_turn_no=after_turn_no, limit=limit)
    running = next((row for row in rows if stages.is_running(row)), None)
    if running is not None:
        # 서버가 요청 도중 멈춰 pending으로 남은 단계는 같은 턴에 새 요청이 와야 닫힌다. 앱은 pending인 동안
        # 입력을 막고 이 목록만 다시 조회하므로, 조회할 때도 제한 시간을 넘긴 단계를 먼저 정리한다.
        # 기준은 요청 경로와 같은 refresh_if_stale을 그대로 쓴다. 정리에 실패해도 조회는 막지 않는다.
        try:
            await stages.refresh_if_stale(user_db, running)
            rows = await user_db.list_turns(session_id, after_turn_no=after_turn_no, limit=limit)
        except db.DatabaseError as exc:
            logger.warning("pending 단계 정리 실패 type=%s session_id=%s", type(exc).__name__, session_id)
    return {"items": [db.to_public_row("turn", row) for row in rows], "after_turn_no": after_turn_no, "limit": limit}


@router.delete("/{session_id}", status_code=204, summary="대화 삭제 (DB 행만)")
async def delete_session(session_id: UUID, user_db: UserDatabase = Depends(get_user_db)):
    await user_db.delete_session_rows(session_id)
    return Response(status_code=204)
