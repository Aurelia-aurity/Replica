"""대화방 요청·응답 형식 (contracts/app-backend/api-spec.md '대화방 객체')."""

from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persona_id: UUID
    ai_notice_ack_at: AwareDatetime = Field(description="AI 생성 안내를 확인한 시각 (시간대 포함, 미래 불가)")
    title: str | None = Field(default=None, max_length=200)
    session_id: UUID | None = Field(default=None, description="앱이 미리 만든 id")

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("blank")
        return value


class SessionOut(BaseModel):
    id: UUID
    persona_id: UUID
    title: str | None
    ai_notice_ack_at: datetime
    created_at: datetime
    updated_at: datetime
