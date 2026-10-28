"""텍스트 대화 한 턴: DB에 턴을 만들고 → AI를 부르고 → 결과를 저장한다.

규칙의 원본은 contracts/app-backend/api-spec.md `/chat/text`, backend-db 명세 '텍스트 턴의 호출 순서',
backend-ai 명세 '타임아웃과 수동 재시도'다. 단계 처리는 turn_stages.py를 함께 쓴다.
- 같은 turn_id의 재전송: 이미 끝난 답변은 AI를 다시 부르지 않고 저장된 결과(finish_reason 포함)를 돌려준다.
- 실행 중(pending)인 턴: 시간 초과가 확인된 것만 실패 처리하고 새로 시작한다. 아니면 409.
"""

import time

from app.clients.llm import LLMClient
from app.db import UserDatabase
from app.schemas.chat import ChatTextRequest, ChatTextResponse, Timings
from app.services import turn_stages as stages


async def handle_text_turn(user_db: UserDatabase, llm: LLMClient, req: ChatTextRequest) -> ChatTextResponse:
    with stages.turn_errors(req.turn_id):
        return await _run(user_db, llm, req)


async def _run(user_db: UserDatabase, llm: LLMClient, req: ChatTextRequest) -> ChatTextResponse:
    started = time.perf_counter()
    turn = await user_db.create_chat_turn(req.session_id, req.turn_id, input_mode="text", question=req.message)
    turn = await stages.refresh_if_stale(user_db, turn)

    if turn["answer_status"] == "done":
        return _stored_response(turn, started)
    if stages.is_running(turn):
        raise stages.conflict()

    result = await stages.run_llm_stage(user_db, llm, turn, req.message)
    total_ms = stages.elapsed_ms(started)
    await stages.record_total_ms(user_db, req.turn_id, "llm", result.attempt, total_ms)
    return ChatTextResponse(
        turn_id=req.turn_id,
        answer=result.text,
        finish_reason=result.finish_reason,
        model_name=result.model_name,
        timings=Timings(llm_ms=result.llm_ms, total_ms=total_ms),
    )


def _stored_response(turn: dict, started: float) -> ChatTextResponse:
    return ChatTextResponse(
        turn_id=turn["id"],
        answer=turn["answer"],
        finish_reason=stored_finish_reason(turn),
        model_name=turn.get("model_name") or "unknown",
        timings=Timings(llm_ms=turn.get("llm_ms"), total_ms=stages.elapsed_ms(started)),
    )


def stored_finish_reason(turn: dict) -> str:
    """저장된 finish_reason. 컬럼이 생기기 전에 만든 턴은 값이 없어 'stop'으로 본다."""
    return turn.get("finish_reason") or "stop"
