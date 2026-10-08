"""프론트와 주고받는 요청·응답 형식.

Mock을 실제 구현으로 바꿔도 이 형식은 유지한다. 프론트는 Swagger(/docs)의 이 모델을 보고 개발한다.
Request는 frontend -> backend, Response는 backend -> frontend를 의미한다.
"""

from typing import Literal

from pydantic import BaseModel, Field


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
    message: str = Field(min_length=1, max_length=2000, examples=["엄마, 오늘 뭐 했어?"])
    persona_id: str | None = Field(default=None, description="대화할 페르소나 id (인증·DB 연동 전에는 생략 가능)")
    history: list[ChatTurn] = Field(default_factory=list, description="이전 대화 (최근순 아님, 오래된 것부터)")


class ChatTextResponse(BaseModel):
    answer: str
    is_ai_generated: Literal[True] = True
    model_name: str
    timings: Timings


class ChatVoiceResponse(BaseModel):
    transcript: str = Field(description="사용자 음성을 STT로 바꾼 텍스트")
    answer: str
    is_ai_generated: Literal[True] = True
    model_name: str
    audio_base64: str = Field(description="응답 음성(TTS)을 base64로 인코딩한 값")
    audio_format: str = Field(description="audio_base64의 형식 (예: wav, mp3)")
    timings: Timings


class SttResponse(BaseModel):
    text: str
    timings: Timings


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000, examples=["안녕, 밥은 먹었니?"])
