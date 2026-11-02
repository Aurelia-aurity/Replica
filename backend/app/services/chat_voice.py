"""음성 대화 한 턴: 음성 → STT → LLM → TTS. 단계마다 DB에 시도 번호와 결과를 저장한다.

규칙의 원본은 contracts/app-backend/api-spec.md `/chat/voice`·`/chat/turns/{turn_id}/tts`, backend-db 명세
'음성 턴의 호출 순서'다. 단계 처리는 turn_stages.py를 함께 쓴다.
- 같은 turn_id의 재전송: 끝난 단계는 건너뛰고, 실패했거나 시작 전인 단계부터 새 시도로 이어간다.
- STT·LLM 실패는 오류(stage 포함)로 응답한다. TTS 실패는 전사문·답변을 유지한 채 200으로 응답한다.
- 응답 음성은 저장하지 않는다. TTS까지 끝난 턴을 다시 보내면 답변 텍스트로 음성을 다시 합성해 돌려준다
  (DB 상태는 바꾸지 않는다).
"""

import hashlib
import time
from uuid import UUID

from app.clients.llm import LLMClient
from app.clients.stt import STTClient
from app.clients.tts import TTSClient
from app.db import UserDatabase
from app.schemas.chat import ChatVoiceResponse, Timings, TtsError, TurnTtsResponse, TurnTtsTimings
from app.services import turn_stages as stages
from app.services.chat_text import stored_finish_reason


async def handle_voice_turn(
    user_db: UserDatabase,
    stt: STTClient,
    llm: LLMClient,
    tts: TTSClient,
    *,
    session_id: UUID,
    turn_id: UUID,
    audio: bytes,
    filename: str,
    content_type: str | None,
) -> ChatVoiceResponse:
    with stages.turn_errors(turn_id):
        return await _run(user_db, stt, llm, tts, session_id, turn_id, audio, filename, content_type)


async def _run(user_db, stt, llm, tts, session_id, turn_id, audio, filename, content_type) -> ChatVoiceResponse:
    started = time.perf_counter()
    # 같은 turn_id에 다른 음성을 보내면 해시가 달라 TURN_ID_CONFLICT가 된다.
    turn = await user_db.create_chat_turn(
        session_id, turn_id, input_mode="voice", audio_sha256=hashlib.sha256(audio).hexdigest()
    )
    turn = await stages.refresh_if_stale(user_db, turn)
    if stages.is_running(turn):
        raise stages.conflict()

    # 1) STT: 끝났으면 저장된 전사문을 쓴다.
    stt_ms = turn.get("stt_ms")
    if turn["question_status"] == "done":
        transcript = turn["question"]
    else:
        transcript, stt_ms = await stages.run_stt_stage(user_db, stt, turn_id, audio, filename, content_type)

    # 2) LLM: 끝났으면 저장된 답변을 쓴다. 새로 실행하면 DB가 TTS 상태를 비운다.
    llm_attempt = 0
    if turn["answer_status"] != "done":
        result = await stages.run_llm_stage(user_db, llm, turn, transcript, with_audio=True)
        answer, finish_reason, model_name = result.text, result.finish_reason, result.model_name
        llm_ms, llm_attempt, tts_status = result.llm_ms, result.attempt, None
    else:
        answer, finish_reason = turn["answer"], stored_finish_reason(turn)
        model_name = turn.get("model_name") or llm.name
        llm_ms, tts_status = turn.get("llm_ms"), turn.get("tts_status")

    # 3) TTS: 실패해도 텍스트는 유지한다. 음성은 저장하지 않아 이미 끝난 턴은 다시 합성만 한다(DB는 그대로).
    if tts_status == "ready":
        speech = await stages.synthesize(tts, answer)
        tts_ms = speech.tts_ms
    else:
        speech = await stages.run_tts_stage(user_db, tts, turn_id, answer)
        tts_ms = speech.tts_ms

    total_ms = stages.elapsed_ms(started)
    # 이번 요청이 새로 실행한 마지막 단계에 전체 시간을 기록한다. TTS를 실행했으면 TTS, 아니면 LLM.
    if speech.attempt:
        await stages.record_total_ms(user_db, turn_id, "tts", speech.attempt, total_ms)
    elif llm_attempt:
        await stages.record_total_ms(user_db, turn_id, "llm", llm_attempt, total_ms)

    tts_error = None if speech.ok else TtsError(code="TTS_FAILED", message=stages.TTS_FAILED_MESSAGE)
    return ChatVoiceResponse(
        turn_id=turn_id,
        transcript=transcript,
        answer=answer,
        finish_reason=finish_reason,
        model_name=model_name,
        tts_status="ready" if speech.ok else "failed",
        audio_base64=speech.audio_base64,
        audio_format=speech.audio_format,
        tts_error=tts_error,
        timings=Timings(stt_ms=stt_ms, llm_ms=llm_ms, tts_ms=tts_ms, total_ms=total_ms),
    )


async def handle_turn_tts(user_db: UserDatabase, tts: TTSClient, turn_id: UUID) -> TurnTtsResponse:
    """완료된 답변을 음성으로 합성한다(텍스트 턴의 '음성 재생', TTS 실패 뒤 재시도).

    합성 중이거나 이미 완료된 턴, 답변이 아직 없는 턴은 409다. 합성이 실패하면 502 TTS_FAILED.
    """
    with stages.turn_errors(turn_id):
        started = time.perf_counter()
        turn = await user_db.get_turn(turn_id)
        turn = await stages.refresh_if_stale(user_db, turn)
        # 상태 조건(답변 완료, tts_status 없음 또는 failed)은 start_chat_stage가 검사해 409로 돌려준다.
        speech = await stages.run_tts_stage(user_db, tts, turn_id, turn["answer"] or "")
        total_ms = stages.elapsed_ms(started)
        await stages.record_total_ms(user_db, turn_id, "tts", speech.attempt, total_ms)
        if not speech.ok:
            raise stages.tts_failed_error()
        return TurnTtsResponse(
            turn_id=turn_id,
            tts_status="ready",
            audio_base64=speech.audio_base64,
            audio_format=speech.audio_format,
            timings=TurnTtsTimings(tts_ms=speech.tts_ms, total_ms=total_ms),
        )
