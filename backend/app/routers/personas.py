from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.auth import get_user_db
from app.db import UserDatabase
from app.schemas.persona import PersonaCreate, PersonaList, PersonaOut

router = APIRouter(prefix="/personas", tags=["personas"])


@router.post("", response_model=PersonaOut, status_code=201, summary="대화 상대 생성")
async def create_persona(req: PersonaCreate, user_db: UserDatabase = Depends(get_user_db)):
    return await user_db.create_persona(req.name, persona_id=req.persona_id)


@router.get("", response_model=PersonaList, summary="대화 상대 목록")
async def list_personas(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user_db: UserDatabase = Depends(get_user_db),
):
    items = await user_db.list_personas(limit=limit, offset=offset)
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/{persona_id}", response_model=PersonaOut, summary="대화 상대 상세")
async def get_persona(persona_id: UUID, user_db: UserDatabase = Depends(get_user_db)):
    return await user_db.get_persona(persona_id)
