"""기록 업로드·조회 요청·응답 형식 (contracts/app-backend/api-spec.md '기록 객체', 'POST /records').

user_id·object_path 등 앱에 보내지 않는 내부 필드는 응답 모델에 두지 않는다.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RecordCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persona_id: UUID
    kind: Literal["text", "audio"]
    original_name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1)
    consent_at: datetime
    consent_version: str = Field(min_length=1, max_length=100)
    # 카카오톡 내보내기에서 화자가 2명 이상일 때만 둘 다 보낸다. 형식 검사는 서비스에서 한다(필드 이름을 오류에 담기 위해).
    target_speaker: str | None = None
    provider_speaker: str | None = None
    record_id: UUID | None = Field(default=None, description="앱이 미리 만든 id (재예약·재조회용)")

    @field_validator("consent_at")
    @classmethod
    def consent_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone required")
        return value


class UploadedBody(BaseModel):
    """`/uploaded`에서 화자 선택값을 다시 보낼 때(예약 때 보내지 않았거나 업로드 뒤 검증에서 거절됐을 때)."""

    model_config = ConfigDict(extra="forbid")

    target_speaker: str | None = None
    provider_speaker: str | None = None


class RecordOut(BaseModel):
    id: UUID
    persona_id: UUID
    kind: Literal["text", "audio"]
    original_name: str
    mime_type: str
    size_bytes: int | None = None
    status: Literal["uploading", "uploaded", "processing", "ready", "failed", "deleting"]
    consent_at: datetime | None = None
    consent_version: str | None = None
    consent_scopes: list[str] = []
    error_code: str | None = None
    target_speaker: str | None = None
    provider_speaker: str | None = None
    created_at: datetime
    updated_at: datetime


class UploadOut(BaseModel):
    url: str
    method: Literal["PUT"] = "PUT"
    headers: dict[str, str]
    expires_at: datetime


class RecordReserved(RecordOut):
    upload: UploadOut | None  # 원본이 이미 올라가 있는 재예약이면 None


class RecordList(BaseModel):
    items: list[RecordOut]
    limit: int
    offset: int
