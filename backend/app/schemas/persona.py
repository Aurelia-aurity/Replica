"""대화 상대 요청·응답 형식 (contracts/app-backend/api-spec.md '대화 상대 객체').

user_id 등 앱에 보내지 않는 내부 필드는 응답 모델에 두지 않는다.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PersonaCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, examples=["엄마"])
    persona_id: UUID | None = Field(default=None, description="앱이 미리 만든 id (쓰기 결과가 불확실할 때 재조회용)")

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank")
        return value


class PersonaOut(BaseModel):
    id: UUID
    name: str
    status: Literal["active", "deleting"]
    data_revision: int
    created_at: datetime
    updated_at: datetime


class PersonaList(BaseModel):
    items: list[PersonaOut]
    limit: int
    offset: int
