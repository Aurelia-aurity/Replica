"""프론트와 주고받는 요청·응답 형식.

Mock을 실제 구현으로 바꿔도 이 형식은 유지한다. 프론트는 Swagger(/docs)의 이 모델을 보고 개발한다.
Request는 frontend -> backend, Response는 backend -> frontend를 의미한다.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Timings(BaseModel):
    """구간별 소요 시간(ms). 해당 구간을 거치지 않았으면 null."""

    stt_ms: int | None = None
    llm_ms: int | None = None
    tts_ms: int | None = None
    total_ms: int


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatTextRequest(BaseModel):
    """텍스트 대화 요청. 이력은 백엔드가 DB에서 조회하므로 앱이 보내지 않는다(persona_id, history 없음)."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID = Field(description="대화방 id")
    turn_id: UUID = Field(description="앱이 전송마다 만드는 id. 같은 질문을 재전송할 때는 같은 값을 유지")
    message: str = Field(min_length=1, max_length=2000, examples=["엄마, 오늘 뭐 했어?"])

    @field_validator("message")
    @classmethod
    def message_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank")
        return value


class ChatTextResponse(BaseModel):
    turn_id: UUID
    answer: str
    is_ai_generated: Literal[True] = True
    finish_reason: Literal["stop", "length"]
    model_name: str
    timings: Timings


class TtsError(BaseModel):
    code: str
    message: str


class ChatVoiceResponse(BaseModel):
    """음성 대화 응답. TTS가 실패해도 전사문과 답변 텍스트는 유지하고 200으로 응답한다."""

    turn_id: UUID
    transcript: str = Field(description="사용자 음성을 STT로 바꾼 텍스트")
    answer: str
    is_ai_generated: Literal[True] = True
    finish_reason: Literal["stop", "length"]
    model_name: str
    tts_status: Literal["ready", "failed"]
    audio_base64: str | None = Field(default=None, description="응답 음성(TTS)을 base64로 인코딩한 값. 실패 시 null")
    audio_format: str | None = Field(default=None, description="audio_base64의 형식 (예: wav, mp3). 실패 시 null")
    tts_error: TtsError | None = Field(default=None, description="TTS 실패 시에만")
    timings: Timings


class TurnTtsTimings(BaseModel):
    tts_ms: int
    total_ms: int


class TurnTtsResponse(BaseModel):
    """완료된 답변을 음성으로 합성한 결과. 실패하면 이 응답 대신 502 TTS_FAILED 오류가 간다."""

    turn_id: UUID
    tts_status: Literal["ready"]
    audio_base64: str
    audio_format: str
    timings: TurnTtsTimings


class SttResponse(BaseModel):
    text: str
    timings: Timings


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000, examples=["안녕, 밥은 먹었니?"])
