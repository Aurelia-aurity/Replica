"""POST /chat/voice, POST /chat/turns/{id}/tts: 음성 → STT → LLM → TTS 흐름. 가짜 DB·STT·AI·TTS로 시험한다."""

import base64
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.clients.llm import LLMResult, get_llm_client
from app.clients.stt import get_stt_client
from app.clients.tts import get_tts_client
from app.core.auth import get_user_db
from app.core.config import get_settings
from app.main import app
from tests.fakes import FakeLLM, FakeSTT, FakeTTS, FakeUserDB, busy_error, new_id

AUDIO = b"fake-audio-bytes"


@pytest.fixture
def fake_db():
    fake = FakeUserDB()
    app.dependency_overrides[get_user_db] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def client(fake_db):
    return TestClient(app, raise_server_exceptions=False)


def use(stt=None, llm=None, tts=None):
    stt, llm, tts = stt or FakeSTT(), llm or FakeLLM(), tts or FakeTTS()
    app.dependency_overrides[get_stt_client] = lambda: stt
    app.dependency_overrides[get_llm_client] = lambda: llm
    app.dependency_overrides[get_tts_client] = lambda: tts
    return stt, llm, tts


def send_voice(client, session_id, turn_id=None, audio=AUDIO, filename="a.m4a"):
    return client.post(
        "/chat/voice",
        files={"file": (filename, audio, "audio/mp4")},
        data={"session_id": session_id, "turn_id": turn_id or new_id()},
    )


# --- 정상 흐름 ---


def test_success_runs_all_three_stages_and_stores_results(client, fake_db):
    stt, llm, tts = use(FakeSTT("엄마, 밥 먹었어?"), FakeLLM(LLMResult("그래, 먹었다.", "stop")))
    session_id, turn_id = fake_db.add_session(), new_id()

    res = send_voice(client, session_id, turn_id)

    assert res.status_code == 200
    body = res.json()
    assert body["turn_id"] == turn_id
    assert body["transcript"] == "엄마, 밥 먹었어?" and body["answer"] == "그래, 먹었다."
    assert body["is_ai_generated"] is True and body["finish_reason"] == "stop"
    assert body["model_name"] == "test-model"
    assert body["tts_status"] == "ready" and body["tts_error"] is None and body["audio_format"] == "wav"
    assert base64.b64decode(body["audio_base64"]) == b"RIFFfake"
    assert all(body["timings"][k] is not None for k in ("stt_ms", "llm_ms", "tts_ms", "total_ms"))

    stored = fake_db.turns[turn_id]
    assert (stored["question"], stored["answer"]) == ("엄마, 밥 먹었어?", "그래, 먹었다.")
    assert (stored["question_status"], stored["answer_status"], stored["tts_status"]) == ("done", "done", "ready")
    assert stored["audio_requested"] is True
    assert fake_db.calls == [
        ("start", "stt"), ("finish", "stt", True), ("start", "llm"), ("finish", "llm", True),
        ("start", "tts"), ("finish", "tts", True), ("total", "tts"),
    ]
    assert stored["total_ms"] == body["timings"]["total_ms"]  # 전체 시간은 마지막 단계(TTS)에 기록한다
    assert stored["finish_reason"] == "stop" and stored["llm_request_id"] == llm.calls[0]["request_id"]
    assert stt.calls[0]["size"] == len(AUDIO) and llm.calls[0]["message"] == "엄마, 밥 먹었어?"
    assert tts.calls == ["그래, 먹었다."]


def test_previous_turns_become_history_for_voice_too(client, fake_db):
    _, llm, _ = use()
    session_id = fake_db.add_session()
    fake_db.add_old_turn(session_id, "지난 질문", "지난 답")

    assert send_voice(client, session_id).status_code == 200

    assert [(t.role, t.content) for t in llm.calls[0]["history"]] == [("user", "지난 질문"), ("assistant", "지난 답")]


def test_length_finish_reason_is_passed_through(client, fake_db):
    use(llm=FakeLLM(LLMResult("길어서 잘린 답", "length")))
    assert send_voice(client, fake_db.add_session()).json()["finish_reason"] == "length"


# --- TTS 실패: 텍스트는 유지 ---


def test_tts_failure_keeps_text_and_returns_200_then_dedicated_retry_works(client, fake_db):
    use(tts=FakeTTS(RuntimeError("provider 비밀 내용"), (b"RIFFok", "wav")))
    session_id, turn_id = fake_db.add_session(), new_id()

    res = send_voice(client, session_id, turn_id)

    assert res.status_code == 200
    body = res.json()
    assert body["tts_status"] == "failed" and body["audio_base64"] is None and body["audio_format"] is None
    assert body["tts_error"] == {"code": "TTS_FAILED", "message": "음성 합성에 실패했어요. 답변 글은 그대로 볼 수 있어요."}
    assert body["answer"] and body["transcript"]
    assert "비밀" not in res.text
    stored = fake_db.turns[turn_id]
    assert (stored["answer_status"], stored["tts_status"], stored["error_message"]) == ("done", "failed", "tts_failed")

    retry = client.post(f"/chat/turns/{turn_id}/tts")

    assert retry.status_code == 200
    assert retry.json()["tts_status"] == "ready" and base64.b64decode(retry.json()["audio_base64"]) == b"RIFFok"
    assert fake_db.turns[turn_id]["tts_attempt"] == 2


# --- STT·LLM 실패는 단계가 표시된 오류 ---


def test_stt_failure_is_502_with_stage_and_retry_restarts_stt(client, fake_db):
    stt, llm, _ = use(FakeSTT(RuntimeError("boom"), "다시 말한 질문"))
    session_id, turn_id = fake_db.add_session(), new_id()

    first = send_voice(client, session_id, turn_id)

    assert first.status_code == 502
    assert first.json()["error"] == {
        "code": "STT_FAILED", "message": "음성을 글자로 바꾸지 못했어요. 다시 시도해 주세요.",
        "stage": "stt", "turn_id": turn_id, "retryable": True, "field": None,
    }
    assert (fake_db.turns[turn_id]["question_status"], fake_db.turns[turn_id]["error_message"]) == ("failed", "stt_failed")
    assert llm.calls == []  # STT가 실패하면 AI를 부르지 않는다

    second = send_voice(client, session_id, turn_id)

    assert second.status_code == 200 and second.json()["transcript"] == "다시 말한 질문"
    assert fake_db.turns[turn_id]["stt_attempt"] == 2


def test_stt_timeout_is_recorded_as_timeout(client, fake_db):
    use(stt=FakeSTT(TimeoutError()))
    session_id, turn_id = fake_db.add_session(), new_id()

    res = send_voice(client, session_id, turn_id)

    assert res.status_code == 502 and res.json()["error"]["code"] == "STT_FAILED"
    assert fake_db.turns[turn_id]["error_message"] == "stt_timeout"


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_transcript_is_422_and_ai_is_not_called(client, fake_db, text):
    _, llm, _ = use(stt=FakeSTT(text))
    session_id, turn_id = fake_db.add_session(), new_id()

    res = send_voice(client, session_id, turn_id)

    assert res.status_code == 422
    assert (res.json()["error"]["code"], res.json()["error"]["stage"], res.json()["error"]["retryable"]) == (
        "EMPTY_TRANSCRIPT", "stt", True)
    assert llm.calls == [] and fake_db.turns[turn_id]["error_message"] == "stt_empty"


def test_llm_failure_keeps_transcript_and_retry_skips_stt(client, fake_db):
    stt, llm, tts = use(llm=FakeLLM(busy_error(), LLMResult("두 번째 답", "stop")))
    session_id, turn_id = fake_db.add_session(), new_id()

    first = send_voice(client, session_id, turn_id)

    assert first.status_code == 429
    assert (first.json()["error"]["code"], first.json()["error"]["stage"]) == ("AI_BUSY", "llm")
    assert tts.calls == []
    stored = fake_db.turns[turn_id]
    assert (stored["question_status"], stored["answer_status"], stored["error_message"]) == ("done", "failed", "llm_busy")

    second = send_voice(client, session_id, turn_id)

    assert second.status_code == 200 and second.json()["answer"] == "두 번째 답"
    assert len(stt.calls) == 1  # 이미 끝난 STT는 다시 하지 않는다
    assert fake_db.turns[turn_id]["llm_attempt"] == 2


# --- 재전송 ---


def test_resend_of_finished_turn_reuses_text_and_resynthesizes_audio(client, fake_db):
    stt, llm, tts = use(tts=FakeTTS((b"RIFFone", "wav"), (b"RIFFtwo", "wav")))
    session_id, turn_id = fake_db.add_session(), new_id()
    first = send_voice(client, session_id, turn_id).json()

    again = send_voice(client, session_id, turn_id)

    assert again.status_code == 200
    body = again.json()
    assert (body["transcript"], body["answer"]) == (first["transcript"], first["answer"])
    assert body["tts_status"] == "ready" and base64.b64decode(body["audio_base64"]) == b"RIFFtwo"
    assert len(stt.calls) == 1 and len(llm.calls) == 1 and len(tts.calls) == 2
    stored = fake_db.turns[turn_id]
    assert (stored["tts_attempt"], stored["tts_status"]) == (1, "ready")  # DB 상태는 그대로


def test_resend_with_failing_resynthesis_reports_tts_failed_without_touching_db(client, fake_db):
    use(tts=FakeTTS((b"RIFFone", "wav"), RuntimeError("down")))
    session_id, turn_id = fake_db.add_session(), new_id()
    send_voice(client, session_id, turn_id)

    again = send_voice(client, session_id, turn_id)

    assert again.status_code == 200
    assert again.json()["tts_status"] == "failed" and again.json()["audio_base64"] is None
    assert fake_db.turns[turn_id]["tts_status"] == "ready"


def test_same_turn_id_with_different_audio_is_rejected(client, fake_db):
    use()
    session_id, turn_id = fake_db.add_session(), new_id()
    send_voice(client, session_id, turn_id, audio=b"first")

    res = send_voice(client, session_id, turn_id, audio=b"second")

    assert res.status_code == 409 and res.json()["error"]["code"] == "TURN_ID_CONFLICT"


def test_stage_still_running_within_time_limit_is_a_conflict(client, fake_db):
    stt, _, _ = use()
    session_id, turn_id = fake_db.add_session(), new_id()
    send_voice(client, session_id, turn_id)
    fake_db.turns[turn_id].update(answer_status="pending", answer=None, llm_started_at=datetime.now(timezone.utc))

    res = send_voice(client, session_id, turn_id)

    assert res.status_code == 409 and res.json()["error"]["code"] == "CONFLICT"
    assert len(stt.calls) == 1


def test_timed_out_stt_is_failed_and_retried(client, fake_db):
    stt, _, _ = use()
    session_id, turn_id = fake_db.add_session(), new_id()
    send_voice(client, session_id, turn_id)
    fake_db.turns[turn_id].update(
        question_status="pending", question=None, answer_status=None, answer=None, tts_status=None,
        stt_started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )

    res = send_voice(client, session_id, turn_id)

    assert res.status_code == 200 and fake_db.turns[turn_id]["stt_attempt"] == 2 and len(stt.calls) == 2


# --- 업로드·입력·인증 ---


def test_unsupported_extension_is_415(client, fake_db):
    use()
    res = send_voice(client, fake_db.add_session(), filename="notes.txt")
    assert res.status_code == 415
    error = res.json()["error"]
    assert error["code"] == "UNSUPPORTED_AUDIO_FORMAT" and error["retryable"] is False
    assert error["message"].startswith("지원하지 않는 음성 형식입니다(.txt). 지원 형식: .aac, .m4a, .mp3")


def test_missing_extension_message(client, fake_db):
    use()
    res = send_voice(client, fake_db.add_session(), filename="recording")
    assert res.status_code == 415 and "(확장자 없음)" in res.json()["error"]["message"]


def test_empty_file_is_400(client, fake_db):
    use()
    res = send_voice(client, fake_db.add_session(), audio=b"")
    assert res.status_code == 400
    assert (res.json()["error"]["code"], res.json()["error"]["message"]) == ("EMPTY_FILE", "빈 파일입니다.")


def test_too_large_file_is_413_and_no_turn_is_created(client, fake_db, monkeypatch):
    use()
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    res = send_voice(client, fake_db.add_session(), audio=b"x" * (1024 * 1024 + 1))
    assert res.status_code == 413
    assert (res.json()["error"]["code"], res.json()["error"]["message"]) == ("FILE_TOO_LARGE", "파일이 너무 큽니다. 최대 1MB")
    assert fake_db.turns == {}


def test_invalid_session_id_and_missing_file_report_field(client, fake_db):
    use()
    bad_id = client.post("/chat/voice", files={"file": ("a.m4a", AUDIO, "audio/mp4")},
                         data={"session_id": "nope", "turn_id": new_id()})
    no_file = client.post("/chat/voice", data={"session_id": new_id(), "turn_id": new_id()})
    assert bad_id.status_code == no_file.status_code == 422
    assert bad_id.json()["error"]["field"] == "session_id" and no_file.json()["error"]["field"] == "file"


def test_unknown_session_is_404_with_turn_id(client, fake_db):
    use()
    turn_id = new_id()
    res = send_voice(client, new_id(), turn_id)
    assert res.status_code == 404 and res.json()["error"]["turn_id"] == turn_id


def test_voice_requires_login():
    app.dependency_overrides.clear()
    res = TestClient(app).post(
        "/chat/voice", files={"file": ("a.m4a", AUDIO, "audio/mp4")}, data={"session_id": new_id(), "turn_id": new_id()}
    )
    assert res.status_code == 401 and res.json()["error"]["code"] == "UNAUTHENTICATED"


# --- POST /chat/turns/{turn_id}/tts ---


def _finished_text_turn(client, fake_db) -> str:
    use()
    turn_id = new_id()
    body = {"session_id": fake_db.add_session(), "turn_id": turn_id, "message": "안녕"}
    assert client.post("/chat/text", json=body).status_code == 200
    return turn_id


def test_turn_tts_synthesizes_a_finished_text_turn_once(client, fake_db):
    turn_id = _finished_text_turn(client, fake_db)

    res = client.post(f"/chat/turns/{turn_id}/tts")

    assert res.status_code == 200
    body = res.json()
    assert (body["turn_id"], body["tts_status"], body["audio_format"]) == (turn_id, "ready", "wav")
    assert base64.b64decode(body["audio_base64"]) == b"RIFFfake"
    assert set(body["timings"]) == {"tts_ms", "total_ms"}
    assert fake_db.turns[turn_id]["tts_status"] == "ready"

    again = client.post(f"/chat/turns/{turn_id}/tts")
    assert again.status_code == 409 and again.json()["error"]["code"] == "CONFLICT"


def test_turn_tts_failure_is_502_then_retry_succeeds(client, fake_db):
    turn_id = _finished_text_turn(client, fake_db)
    use(tts=FakeTTS(RuntimeError("x"), (b"RIFFok", "wav")))

    first = client.post(f"/chat/turns/{turn_id}/tts")

    assert first.status_code == 502
    assert first.json()["error"] == {
        "code": "TTS_FAILED", "message": "음성 합성에 실패했어요. 답변 글은 그대로 볼 수 있어요.",
        "stage": "tts", "turn_id": turn_id, "retryable": True, "field": None,
    }
    assert fake_db.turns[turn_id]["tts_status"] == "failed"
    assert fake_db.turns[turn_id]["answer"]  # 답변 텍스트는 그대로

    assert client.post(f"/chat/turns/{turn_id}/tts").status_code == 200
    assert fake_db.turns[turn_id]["tts_attempt"] == 2


def test_turn_tts_needs_a_finished_answer(client, fake_db):
    use()
    turn = FakeUserDB.add_session(fake_db)
    fake_db.add_old_turn(turn, "질문", None)  # 답변 실패 턴
    turn_id = next(iter(fake_db.turns))

    res = client.post(f"/chat/turns/{turn_id}/tts")

    assert res.status_code == 409 and res.json()["error"]["code"] == "CONFLICT"


def test_turn_tts_still_running_is_409_and_timed_out_is_retried(client, fake_db):
    turn_id = _finished_text_turn(client, fake_db)
    fake_db.turns[turn_id].update(tts_status="pending", tts_attempt=1, tts_started_at=datetime.now(timezone.utc))
    assert client.post(f"/chat/turns/{turn_id}/tts").status_code == 409

    fake_db.turns[turn_id]["tts_started_at"] = datetime.now(timezone.utc) - timedelta(minutes=10)
    res = client.post(f"/chat/turns/{turn_id}/tts")

    assert res.status_code == 200 and fake_db.turns[turn_id]["tts_attempt"] == 2


def test_turn_tts_unknown_turn_and_login(client, fake_db):
    use()
    assert client.post(f"/chat/turns/{new_id()}/tts").status_code == 404
    app.dependency_overrides.clear()
    assert TestClient(app).post(f"/chat/turns/{new_id()}/tts").status_code == 401


def test_resend_replays_stored_finish_reason(client, fake_db):
    use(llm=FakeLLM(LLMResult("길어서 잘린 답", "length")))
    session_id, turn_id = fake_db.add_session(), new_id()
    send_voice(client, session_id, turn_id)

    assert send_voice(client, session_id, turn_id).json()["finish_reason"] == "length"
    assert fake_db.turns[turn_id]["finish_reason"] == "length"


def test_total_ms_goes_to_tts_attempt_even_when_tts_fails(client, fake_db):
    use(tts=FakeTTS(RuntimeError("down")))
    session_id, turn_id = fake_db.add_session(), new_id()

    body = send_voice(client, session_id, turn_id).json()

    assert body["tts_status"] == "failed"
    assert fake_db.turns[turn_id]["total_ms"] == body["timings"]["total_ms"]
    assert ("total", "tts") in fake_db.calls


def test_dedicated_tts_endpoint_records_total_ms(client, fake_db):
    turn_id = _finished_text_turn(client, fake_db)
    body = client.post(f"/chat/turns/{turn_id}/tts").json()
    assert fake_db.turns[turn_id]["total_ms"] == body["timings"]["total_ms"]
