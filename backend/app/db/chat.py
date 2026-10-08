"""대화방·턴 저장·조회 함수.

대화·음성 엔드포인트는 SQL 대신 이 함수들을 호출한다.

공통 규칙
- 첫 인자는 항상 `user_id`다. baseline에서는 엔드포인트가 `.env`의 `BASELINE_USER_ID`를,
  고도화 1부터는 로그인 토큰에서 꺼낸 값을 넘긴다. 함수는 바뀌지 않는다.
- 대상이 없거나, 다른 사용자 소유이거나, 지금 상태에서 할 수 없는 변경이면 `None`을 돌려준다.
  예외를 던지지 않으므로 호출하는 쪽이 `None`을 404나 409로 바꿔 응답한다.
- 잘못된 인자 값(input_mode, stage 등)은 `ValueError`를 던진다.

한 턴의 순서 (텍스트)
    create_turn → start_answer → complete_answer | fail_turn("llm") → record_total_ms
한 턴의 순서 (음성)
    create_turn → set_question | fail_turn("stt") → start_answer(with_audio=True)
    → complete_answer | fail_turn("llm") → set_tts_result → record_total_ms
"""

from typing import Any
from uuid import UUID

from psycopg.rows import class_row, dict_row

from . import queries
from .connection import connection
from .models import ChatSession, ChatTurn, TurnResults

INPUT_MODES = ("text", "voice")
FAIL_STAGES = {"stt": queries.FAIL_STT, "llm": queries.FAIL_LLM}


def _one_session(sql: str, params: dict[str, Any]) -> ChatSession | None:
    with connection() as conn, conn.cursor(row_factory=class_row(ChatSession)) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _one_turn(sql: str, params: dict[str, Any]) -> ChatTurn | None:
    with connection() as conn, conn.cursor(row_factory=class_row(ChatTurn)) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _turns(sql: str, params: dict[str, Any]) -> list[ChatTurn]:
    with connection() as conn, conn.cursor(row_factory=class_row(ChatTurn)) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _check_ms(name: str, value: int | None) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{name}는 0 이상이어야 합니다: {value}")


# 대화방 ---------------------------------------------------------------------


def create_session(user_id: UUID, persona_id: UUID) -> ChatSession | None:
    """새 대화방을 만든다. 페르소나가 이 사용자 소유가 아니면 None."""
    return _one_session(queries.CREATE_SESSION, {"user_id": user_id, "persona_id": persona_id})


def ack_ai_notice(user_id: UUID, session_id: UUID) -> ChatSession | None:
    """대화 시작 전 AI 생성 안내를 확인한 시각을 기록한다(FR-08). 처음 확인한 시각을 유지한다."""
    return _one_session(queries.ACK_AI_NOTICE, {"user_id": user_id, "session_id": session_id})


# 턴 -------------------------------------------------------------------------


def create_turn(
    user_id: UUID,
    session_id: UUID,
    turn_id: UUID,
    input_mode: str,
    question: str | None = None,
) -> ChatTurn | None:
    """질문을 받아 턴을 만든다.

    `turn_id`는 앱이 전송마다 만든 uuid다. 같은 id로 다시 호출하면 새로 만들지 않고
    기존 턴을 돌려준다. 호출하는 쪽은 돌려받은 턴의 상태를 보고 실패한 단계부터 다시 실행한다.
    텍스트 턴은 질문이 바로 완료(done)되고, 음성 턴은 STT 결과를 기다린다(pending).
    대화방이 이 사용자 소유가 아니면 None.
    """
    if input_mode not in INPUT_MODES:
        raise ValueError(f"input_mode는 {INPUT_MODES} 중 하나여야 합니다: {input_mode!r}")
    if input_mode == "text":
        if not question or not question.strip():
            raise ValueError("텍스트 턴에는 비어 있지 않은 질문이 필요합니다.")
        question_status = "done"
    else:
        question, question_status = None, "pending"

    params = {
        "user_id": user_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "input_mode": input_mode,
        "question": question,
        "question_status": question_status,
    }
    with connection() as conn:
        conn.execute(queries.INSERT_TURN, params)
        with conn.cursor(row_factory=class_row(ChatTurn)) as cur:
            cur.execute(queries.GET_TURN, params)
            return cur.fetchone()


def get_turn(user_id: UUID, turn_id: UUID) -> ChatTurn | None:
    """턴 하나를 조회한다."""
    return _one_turn(queries.GET_TURN, {"user_id": user_id, "turn_id": turn_id})


def set_question(user_id: UUID, turn_id: UUID, text: str, stt_ms: int | None = None) -> ChatTurn | None:
    """음성 턴에 STT 전사문을 넣고 질문을 완료한다. STT 재시도 후에도 같은 함수를 쓴다."""
    if not text or not text.strip():
        raise ValueError("전사문이 비어 있습니다. 실패로 기록하려면 fail_turn(..., 'stt', ...)를 쓰세요.")
    _check_ms("stt_ms", stt_ms)
    return _one_turn(
        queries.SET_QUESTION,
        {"user_id": user_id, "turn_id": turn_id, "text": text, "stt_ms": stt_ms},
    )


def start_answer(
    user_id: UUID, turn_id: UUID, model_name: str | None, with_audio: bool = False
) -> ChatTurn | None:
    """LLM 호출 직전에 응답을 시작 상태로 만든다. LLM 재시도에도 같은 함수를 쓴다.

    질문이 완료되지 않았거나, 응답이 이미 진행 중이거나 완료된 턴이면 None.
    `with_audio=True`면 음성 상태도 pending으로 만든다.
    """
    return _one_turn(
        queries.START_ANSWER,
        {"user_id": user_id, "turn_id": turn_id, "model_name": model_name, "with_audio": with_audio},
    )


def complete_answer(user_id: UUID, turn_id: UUID, text: str, llm_ms: int | None = None) -> ChatTurn | None:
    """LLM 응답을 저장한다. 응답이 진행 중(pending)인 턴만 갱신한다."""
    if not text or not text.strip():
        raise ValueError("응답이 비어 있습니다. 실패로 기록하려면 fail_turn(..., 'llm', ...)를 쓰세요.")
    _check_ms("llm_ms", llm_ms)
    return _one_turn(
        queries.COMPLETE_ANSWER,
        {"user_id": user_id, "turn_id": turn_id, "text": text, "llm_ms": llm_ms},
    )


def start_tts(user_id: UUID, turn_id: UUID) -> ChatTurn | None:
    """완료된 응답의 음성 합성을 시작한다.

    TTS 재시도, 그리고 텍스트로 대화한 뒤 음성을 요청할 때(UC-02 "원하면 음성 재생") 쓴다.
    응답이 완료되지 않았거나 이미 합성 중·완료면 None.
    """
    return _one_turn(queries.START_TTS, {"user_id": user_id, "turn_id": turn_id})


def set_tts_result(
    user_id: UUID, turn_id: UUID, ok: bool, tts_ms: int | None = None, error: str | None = None
) -> ChatTurn | None:
    """음성 합성 결과를 기록한다. 실패해도 응답 텍스트는 그대로 남는다(#7)."""
    _check_ms("tts_ms", tts_ms)
    return _one_turn(
        queries.SET_TTS_RESULT,
        {"user_id": user_id, "turn_id": turn_id, "ok": ok, "tts_ms": tts_ms, "error": error},
    )


def fail_turn(user_id: UUID, turn_id: UUID, stage: str, error: str) -> ChatTurn | None:
    """STT 또는 LLM 단계의 실패를 기록한다. `stage`는 "stt" 또는 "llm".

    해당 단계가 진행 중(pending)일 때만 실패로 바꾼다. TTS 실패는 set_tts_result를 쓴다.
    """
    sql = FAIL_STAGES.get(stage)
    if sql is None:
        raise ValueError(f"stage는 {tuple(FAIL_STAGES)} 중 하나여야 합니다: {stage!r}")
    return _one_turn(sql, {"user_id": user_id, "turn_id": turn_id, "error": error})


def record_total_ms(user_id: UUID, turn_id: UUID, total_ms: int) -> ChatTurn | None:
    """서버가 질문을 받은 때부터 응답이 준비될 때까지 걸린 시간을 기록한다(NFR-01)."""
    _check_ms("total_ms", total_ms)
    return _one_turn(queries.RECORD_TOTAL_MS, {"user_id": user_id, "turn_id": turn_id, "total_ms": total_ms})


# 조회 ------------------------------------------------------------------------


def get_context(user_id: UUID, session_id: UUID, limit: int = 10) -> list[ChatTurn]:
    """LLM 프롬프트에 넣을 최근 턴(응답 완료)을 오래된 순으로 돌려준다."""
    if limit < 1:
        raise ValueError(f"limit은 1 이상이어야 합니다: {limit}")
    return _turns(queries.GET_CONTEXT, {"user_id": user_id, "session_id": session_id, "limit": limit})


def list_turns(user_id: UUID, session_id: UUID) -> list[ChatTurn]:
    """대화방의 턴 전체를 순서대로 돌려준다. 앱은 한 턴을 질문·응답 말풍선 두 개로 그린다."""
    return _turns(queries.LIST_TURNS, {"user_id": user_id, "session_id": session_id})


def get_turn_results(user_id: UUID, session_id: UUID) -> TurnResults:
    """대화방의 실행 결과 요약과 턴 목록. 대표 시나리오 10회 측정에 쓴다(2.7절, NFR-01)."""
    params = {"user_id": user_id, "session_id": session_id}
    with connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(queries.SUMMARIZE_TURNS, params)
        summary = cur.fetchone()
    return TurnResults(**summary, turns=list_turns(user_id, session_id))
