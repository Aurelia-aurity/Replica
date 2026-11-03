from fastapi import APIRouter, Depends

from app import db
from app.core.auth import get_user_db
from app.core.errors import INVALID_INPUT_MESSAGE, AppError
from app.db import UserDatabase
from app.schemas.session import SessionCreate, SessionOut

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
