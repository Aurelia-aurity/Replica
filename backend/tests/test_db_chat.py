"""대화방·턴 저장 함수 테스트 (로컬 Supabase 필요, conftest.py 참고)."""

from uuid import uuid4

import pytest
from app.db import chat

from .conftest import PERSONA_B, USER_A, USER_B

# 대화방 ---------------------------------------------------------------------


def test_create_session_rejects_other_users_persona():
    assert chat.create_session(USER_A, PERSONA_B) is None


def test_ack_ai_notice_keeps_first_time(session_a):
    first = chat.ack_ai_notice(USER_A, session_a.id)
    second = chat.ack_ai_notice(USER_A, session_a.id)
    assert first.ai_notice_ack_at is not None
    assert second.ai_notice_ack_at == first.ai_notice_ack_at


# 정상 흐름 -------------------------------------------------------------------


def test_text_turn_flow(session_a):
    turn_id = uuid4()
    turn = chat.create_turn(USER_A, session_a.id, turn_id, "text", "오늘 뭐 했어?")
    assert (turn.question_status, turn.answer_status, turn.tts_status) == ("done", None, None)

    turn = chat.start_answer(USER_A, turn_id, "qwen2.5-7b")
    assert turn.answer_status == "pending" and turn.tts_status is None

    turn = chat.complete_answer(USER_A, turn_id, "산책 다녀왔어.", llm_ms=900)
    turn = chat.record_total_ms(USER_A, turn_id, 1200)
    assert (turn.answer, turn.answer_status, turn.llm_ms, turn.total_ms) == ("산책 다녀왔어.", "done", 900, 1200)


def test_voice_turn_flow(session_a):
    turn_id = uuid4()
    turn = chat.create_turn(USER_A, session_a.id, turn_id, "voice")
    assert (turn.question, turn.question_status) == (None, "pending")

    chat.set_question(USER_A, turn_id, "밥은 먹었어?", stt_ms=500)
    turn = chat.start_answer(USER_A, turn_id, "qwen2.5-7b", with_audio=True)
    assert turn.tts_status == "pending"

    chat.complete_answer(USER_A, turn_id, "응, 먹었어.", llm_ms=900)
    turn = chat.set_tts_result(USER_A, turn_id, ok=True, tts_ms=300)
    assert (turn.question_status, turn.answer_status, turn.tts_status) == ("done", "done", "ready")
    assert (turn.stt_ms, turn.llm_ms, turn.tts_ms) == (500, 900, 300)


def test_tts_failure_keeps_answer_and_can_retry(session_a):
    turn_id = uuid4()
    chat.create_turn(USER_A, session_a.id, turn_id, "text", "안녕")
    chat.start_answer(USER_A, turn_id, "m", with_audio=True)
    chat.complete_answer(USER_A, turn_id, "안녕!")

    turn = chat.set_tts_result(USER_A, turn_id, ok=False, error="TTS timeout")
    assert (turn.answer, turn.tts_status, turn.error_message) == ("안녕!", "failed", "TTS timeout")

    turn = chat.start_tts(USER_A, turn_id)
    assert turn.tts_status == "pending" and turn.error_message is None
    assert chat.start_tts(USER_A, turn_id) is None  # 이미 합성 중


def test_text_turn_can_request_audio_later(session_a):
    turn_id = uuid4()
    chat.create_turn(USER_A, session_a.id, turn_id, "text", "안녕")
    chat.start_answer(USER_A, turn_id, "m")
    assert chat.start_tts(USER_A, turn_id) is None  # 응답 완료 전
    chat.complete_answer(USER_A, turn_id, "안녕!")
    assert chat.start_tts(USER_A, turn_id).tts_status == "pending"


# 재전송·재시도 (#6) -------------------------------------------------------------


def test_resend_same_turn_id_keeps_one_row(session_a):
    turn_id = uuid4()
    first = chat.create_turn(USER_A, session_a.id, turn_id, "text", "안녕")
    again = chat.create_turn(USER_A, session_a.id, turn_id, "text", "안녕")
    assert again.id == first.id
    assert len(chat.list_turns(USER_A, session_a.id)) == 1


def test_resend_returns_current_state(session_a):
    turn_id = uuid4()
    chat.create_turn(USER_A, session_a.id, turn_id, "voice")
    chat.fail_turn(USER_A, turn_id, "stt", "STT 오류")
    turn = chat.create_turn(USER_A, session_a.id, turn_id, "voice")
    assert turn.question_status == "failed"  # 호출하는 쪽이 STT부터 다시 실행


def test_stt_retry_after_failure(session_a):
    turn_id = uuid4()
    chat.create_turn(USER_A, session_a.id, turn_id, "voice")
    turn = chat.fail_turn(USER_A, turn_id, "stt", "STT 오류")
    assert (turn.question_status, turn.error_message) == ("failed", "STT 오류")

    turn = chat.set_question(USER_A, turn_id, "다시 말한 질문", stt_ms=400)
    assert (turn.question_status, turn.error_message) == ("done", None)


def test_llm_retry_updates_same_turn(session_a):
    turn_id = uuid4()
    chat.create_turn(USER_A, session_a.id, turn_id, "text", "안녕")
    chat.start_answer(USER_A, turn_id, "m", with_audio=True)

    turn = chat.fail_turn(USER_A, turn_id, "llm", "LLM timeout")
    assert (turn.answer_status, turn.tts_status) == ("failed", None)

    turn = chat.start_answer(USER_A, turn_id, "m", with_audio=True)
    assert (turn.answer_status, turn.tts_status, turn.error_message) == ("pending", "pending", None)
    chat.complete_answer(USER_A, turn_id, "안녕!")
    assert len(chat.list_turns(USER_A, session_a.id)) == 1


# 상태 순서 ------------------------------------------------------------------


def test_cannot_start_answer_before_question_done(session_a):
    turn_id = uuid4()
    chat.create_turn(USER_A, session_a.id, turn_id, "voice")
    assert chat.start_answer(USER_A, turn_id, "m") is None


def test_cannot_restart_pending_or_done_answer(session_a):
    turn_id = uuid4()
    chat.create_turn(USER_A, session_a.id, turn_id, "text", "안녕")
    chat.start_answer(USER_A, turn_id, "m")
    assert chat.start_answer(USER_A, turn_id, "m") is None  # 진행 중
    chat.complete_answer(USER_A, turn_id, "안녕!")
    assert chat.start_answer(USER_A, turn_id, "m") is None  # 완료


def test_cannot_change_question_after_answer_started(session_a):
    turn_id = uuid4()
    chat.create_turn(USER_A, session_a.id, turn_id, "voice")
    chat.set_question(USER_A, turn_id, "질문")
    chat.start_answer(USER_A, turn_id, "m")
    assert chat.set_question(USER_A, turn_id, "바뀐 질문") is None


def test_set_question_only_for_voice(session_a):
    turn_id = uuid4()
    chat.create_turn(USER_A, session_a.id, turn_id, "text", "안녕")
    assert chat.set_question(USER_A, turn_id, "바뀐 질문") is None


def test_complete_answer_requires_pending(session_a):
    turn_id = uuid4()
    chat.create_turn(USER_A, session_a.id, turn_id, "text", "안녕")
    assert chat.complete_answer(USER_A, turn_id, "응답") is None


# 입력 검증 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "args"),
    [
        (chat.create_turn, (USER_A, uuid4(), uuid4(), "video")),
        (chat.create_turn, (USER_A, uuid4(), uuid4(), "text", "   ")),
        (chat.set_question, (USER_A, uuid4(), "")),
        (chat.complete_answer, (USER_A, uuid4(), "")),
        (chat.fail_turn, (USER_A, uuid4(), "tts", "x")),
        (chat.record_total_ms, (USER_A, uuid4(), -1)),
        (chat.get_context, (USER_A, uuid4(), 0)),
    ],
)
def test_invalid_arguments_raise(call, args):
    with pytest.raises(ValueError):
        call(*args)


# 사용자 분리 (NFR-02) -----------------------------------------------------------


def test_other_user_cannot_touch_turn(session_a):
    turn_id = uuid4()
    chat.create_turn(USER_A, session_a.id, turn_id, "voice")

    assert chat.get_turn(USER_B, turn_id) is None
    assert chat.ack_ai_notice(USER_B, session_a.id) is None
    assert chat.set_question(USER_B, turn_id, "가로챈 질문") is None
    assert chat.fail_turn(USER_B, turn_id, "stt", "x") is None
    assert chat.record_total_ms(USER_B, turn_id, 1) is None
    assert chat.list_turns(USER_B, session_a.id) == []
    assert chat.get_context(USER_B, session_a.id) == []
    assert chat.get_turn_results(USER_B, session_a.id).total == 0

    turn = chat.get_turn(USER_A, turn_id)
    assert (turn.question, turn.question_status, turn.total_ms) == (None, "pending", None)


def test_other_user_cannot_add_turn_to_session(session_a):
    turn_id = uuid4()
    assert chat.create_turn(USER_B, session_a.id, turn_id, "text", "안녕") is None
    assert chat.list_turns(USER_A, session_a.id) == []


def test_other_user_cannot_hijack_turn_id(session_a, cleanup_sessions):
    turn_id = uuid4()
    chat.create_turn(USER_A, session_a.id, turn_id, "text", "A의 질문")
    session_b = chat.create_session(USER_B, PERSONA_B)
    cleanup_sessions.append(session_b.id)

    assert chat.create_turn(USER_B, session_b.id, turn_id, "text", "B의 질문") is None
    assert chat.get_turn(USER_A, turn_id).question == "A의 질문"


# 조회 ------------------------------------------------------------------------


def _done_turn(session_id, question, answer):
    turn_id = uuid4()
    chat.create_turn(USER_A, session_id, turn_id, "text", question)
    chat.start_answer(USER_A, turn_id, "m")
    chat.complete_answer(USER_A, turn_id, answer)
    return turn_id


def test_get_context_returns_recent_done_turns_in_order(session_a):
    for i in range(3):
        _done_turn(session_a.id, f"질문{i}", f"응답{i}")
    chat.create_turn(USER_A, session_a.id, uuid4(), "text", "응답 전 질문")

    context = chat.get_context(USER_A, session_a.id, limit=2)
    assert [t.question for t in context] == ["질문1", "질문2"]


def test_get_turn_results_counts_completed(session_a):
    _done_turn(session_a.id, "질문", "응답")

    tts_failed = uuid4()
    chat.create_turn(USER_A, session_a.id, tts_failed, "text", "음성 질문")
    chat.start_answer(USER_A, tts_failed, "m", with_audio=True)
    chat.complete_answer(USER_A, tts_failed, "응답")
    chat.set_tts_result(USER_A, tts_failed, ok=False, error="TTS 오류")

    chat.create_turn(USER_A, session_a.id, uuid4(), "voice")

    results = chat.get_turn_results(USER_A, session_a.id)
    assert (results.total, results.completed, len(results.turns)) == (3, 1, 3)
