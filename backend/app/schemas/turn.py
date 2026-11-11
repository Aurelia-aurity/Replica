"""대화 턴 응답 형식 (contracts/app-backend/api-spec.md '턴 객체').

서버 내부 필드(user_id, request_hash, *_attempt, *_started_at 등)는 여기에 없으므로 응답에 나가지 않는다.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

StageStatus = Literal["pending", "done", "failed"]


class TurnOut(BaseModel):
    turn_id: UUID
    turn_no: int
    input_mode: Literal["text", "voice"]
    question: str | None
    question_status: StageStatus
    answer: str | None
    answer_status: StageStatus | None
    tts_status: Literal["pending", "ready", "failed"] | None
    is_ai_generated: bool
    model_name: str | None
    finish_reason: Literal["stop", "length"] | None = None
    stt_ms: int | None = None
    llm_ms: int | None = None
    tts_ms: int | None = None
    total_ms: int | None = None
    error_message: str | None = None
    created_at: datetime


class TurnList(BaseModel):
    items: list[TurnOut]
    after_turn_no: int
    limit: int
