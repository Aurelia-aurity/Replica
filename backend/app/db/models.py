"""DB 행을 담는 데이터 클래스. 필드 이름은 테이블 컬럼 이름과 같다."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class ChatSession:
    id: UUID
    user_id: UUID
    persona_id: UUID
    ai_notice_ack_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class ChatTurn:
    """질문 하나와 응답 하나. `answer`는 화면에 항상 "AI 생성"으로 표시한다(FR-08)."""

    id: UUID
    session_id: UUID
    user_id: UUID
    input_mode: str              # text · voice
    question: str | None
    question_status: str         # pending · done · failed (입력·STT)
    answer: str | None
    answer_status: str | None    # None · pending · done · failed (LLM)
    tts_status: str | None       # None · pending · ready · failed (TTS)
    model_name: str | None
    stt_ms: int | None
    llm_ms: int | None
    tts_ms: int | None
    total_ms: int | None
    error_message: str | None
    created_at: datetime


@dataclass(frozen=True)
class TurnResults:
    """대표 시나리오 실행 결과 (수행계획서 2.7절: 10회 중 9회 이상 정상 완료)."""

    total: int
    completed: int
    avg_stt_ms: int | None
    avg_llm_ms: int | None
    avg_tts_ms: int | None
    avg_total_ms: int | None
    turns: list[ChatTurn] = field(default_factory=list)
