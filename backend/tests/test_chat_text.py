"""POST /chat/text: 턴 저장 → AI 호출 → 결과 저장, 재시도, 오류 변환. 가짜 DB·AI로 시험한다."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import db
from app.clients.llm import LLMResult, get_llm_client
from app.core import auth
from app.core.auth import get_user_db
from app.main import app
from tests.fakes import FakeLLM, FakeUserDB, busy_error, new_id


@pytest.fixture
def fake_db():
    fake = FakeUserDB()
    app.dependency_overrides[get_user_db] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


def use_llm(llm: FakeLLM) -> FakeLLM:
    app.dependency_overrides[get_llm_client] = lambda: llm
    return llm


@pytest.fixture
def client(fake_db):
    return TestClient(app, raise_server_exceptions=False)


def send(client, session_id, turn_id=None, message="엄마, 오늘 뭐 했어?", **extra):
    body = {"session_id": session_id, "turn_id": turn_id or new_id(), "message": message, **extra}
    return client.post("/chat/text", json=body)


# --- 정상 흐름 ---


def test_success_returns_spec_fields_and_stores_result(client, fake_db):
    llm = use_llm(FakeLLM(LLMResult("밥 먹었지.", "stop")))
    session_id, turn_id = fake_db.add_session(), new_id()

    res = send(client, session_id, turn_id)

    assert res.status_code == 200
    body = res.json()
    assert body["turn_id"] == turn_id
    assert body["answer"] == "밥 먹었지."
    assert body["is_ai_generated"] is True
    assert body["finish_reason"] == "stop"
    assert body["model_name"] == "test-model"
    assert body["timings"]["llm_ms"] is not None and body["timings"]["stt_ms"] is None
    stored = fake_db.turns[turn_id]
    assert (stored["answer_status"], stored["answer"], stored["llm_attempt"]) == ("done", "밥 먹었지.", 1)
    assert len(llm.calls) == 1 and llm.calls[0]["history"] == [] and llm.calls[0]["persona_id"] is None


def test_length_finish_reason_is_passed_through(client, fake_db):
    use_llm(FakeLLM(LLMResult("답변이 길어서", "length")))
    assert send(client, fake_db.add_session()).json()["finish_reason"] == "length"


def test_previous_finished_turns_become_history_and_current_turn_is_excluded(client, fake_db):
    llm = use_llm(FakeLLM(LLMResult("첫 답", "stop"), LLMResult("둘째 답", "stop")))
    session_id = fake_db.add_session()

    send(client, session_id, message="첫 질문")
    send(client, session_id, message="둘째 질문")

    history = [(t.role, t.content) for t in llm.calls[1]["history"]]
    assert history == [("user", "첫 질문"), ("assistant", "첫 답")]


def _sent_history(llm: FakeLLM) -> list[tuple[str, str]]:
    turns = llm.calls[-1]["history"]
    return [(turns[i].content, turns[i + 1].content) for i in range(0, len(turns), 2)]


def test_history_is_limited_to_last_20_pairs_and_does_not_read_every_turn(client, fake_db):
    llm = use_llm(FakeLLM())
    session_id = fake_db.add_session()
    for i in range(300):
        fake_db.add_old_turn(session_id, f"질문{i}", f"답{i}")

    assert send(client, session_id).status_code == 200

    assert _sent_history(llm) == [(f"질문{i}", f"답{i}") for i in range(280, 300)]
    assert fake_db.rows_read == 20  # DB가 최근 20쌍만 돌려준다. 300개를 다 읽지 않는다.


def test_history_does_not_depend_on_how_sparse_turn_numbers_are(client, fake_db):
    llm = use_llm(FakeLLM())
    session_id = fake_db.add_session()
    for i in range(30):  # 다른 대화방이 번호를 많이 써도 최근 20쌍은 그대로 나온다
        fake_db.add_old_turn(session_id, f"질문{i}", f"답{i}", skipped_numbers=60)

    assert send(client, session_id).status_code == 200

    assert _sent_history(llm) == [(f"질문{i}", f"답{i}") for i in range(10, 30)]
    assert fake_db.rows_read == 20


def test_history_skips_failed_turns_and_still_fills_20_pairs(client, fake_db):
    llm = use_llm(FakeLLM())
    session_id = fake_db.add_session()
    for i in range(45):
        fake_db.add_old_turn(session_id, f"질문{i}", None if i % 3 == 0 else f"답{i}")

    assert send(client, session_id).status_code == 200

    done = [i for i in range(45) if i % 3 != 0]
    assert _sent_history(llm) == [(f"질문{i}", f"답{i}") for i in done[-20:]]


def test_short_session_sends_all_its_pairs(client, fake_db):
    llm = use_llm(FakeLLM())
    session_id = fake_db.add_session()
    for i in range(3):
        fake_db.add_old_turn(session_id, f"질문{i}", f"답{i}")

    send(client, session_id)

    assert _sent_history(llm) == [(f"질문{i}", f"답{i}") for i in range(3)]


def test_failed_turn_is_not_used_as_history(client, fake_db):
    llm = use_llm(FakeLLM(busy_error(), LLMResult("성공", "stop")))
    session_id = fake_db.add_session()

    send(client, session_id, message="실패할 질문")
    send(client, session_id, message="다음 질문")

    assert llm.calls[1]["history"] == []


# --- 같은 turn_id 재전송 / 수동 재시도 ---


def test_resend_of_finished_turn_returns_stored_answer_without_calling_ai(client, fake_db):
    llm = use_llm(FakeLLM(LLMResult("저장될 답", "stop")))
    session_id, turn_id = fake_db.add_session(), new_id()
    send(client, session_id, turn_id)

    again = send(client, session_id, turn_id)

    assert again.status_code == 200 and again.json()["answer"] == "저장될 답"
    assert again.json()["model_name"] == "test-model"
    assert len(llm.calls) == 1


def test_same_turn_id_with_different_message_is_rejected(client, fake_db):
    use_llm(FakeLLM())
    session_id, turn_id = fake_db.add_session(), new_id()
    send(client, session_id, turn_id, message="원래 질문")

    res = send(client, session_id, turn_id, message="다른 질문")

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "TURN_ID_CONFLICT"


def test_turn_still_running_within_time_limit_is_a_conflict_and_ai_is_not_called(client, fake_db):
    llm = use_llm(FakeLLM())
    session_id, turn_id = fake_db.add_session(), new_id()
    send(client, session_id, turn_id)
    fake_db.turns[turn_id].update(answer_status="pending", answer=None, llm_started_at=datetime.now(timezone.utc))

    res = send(client, session_id, turn_id)

    assert res.status_code == 409 and res.json()["error"]["code"] == "CONFLICT"
    assert res.json()["error"]["turn_id"] == turn_id
    assert len(llm.calls) == 1  # 첫 전송의 호출뿐


def test_timed_out_pending_turn_is_failed_and_retried_with_new_attempt(client, fake_db):
    llm = use_llm(FakeLLM(LLMResult("재시도 답", "stop")))
    session_id, turn_id = fake_db.add_session(), new_id()
    send(client, session_id, turn_id)
    fake_db.turns[turn_id].update(
        answer_status="pending", answer=None, llm_started_at=datetime.now(timezone.utc) - timedelta(minutes=10)
    )

    res = send(client, session_id, turn_id)

    assert res.status_code == 200 and res.json()["answer"] == "재시도 답"
    stored = fake_db.turns[turn_id]
    assert stored["llm_attempt"] == 2 and stored["answer_status"] == "done"
    assert len(llm.calls) == 2


def test_ai_error_is_recorded_then_manual_retry_uses_new_attempt(client, fake_db):
    use_llm(FakeLLM(busy_error(), LLMResult("두 번째엔 성공", "stop")))
    session_id, turn_id = fake_db.add_session(), new_id()

    first = send(client, session_id, turn_id)

    assert first.status_code == 429
    assert first.json()["error"] == {
        "code": "AI_BUSY", "message": "지금 다른 요청을 처리하고 있어요. 잠시 후 다시 시도해 주세요.",
        "stage": "llm", "turn_id": turn_id, "retryable": True, "field": None,
    }
    failed = fake_db.turns[turn_id]
    assert (failed["answer_status"], failed["error_message"], failed["llm_attempt"]) == ("failed", "llm_busy", 1)

    second = send(client, session_id, turn_id)

    assert second.status_code == 200 and second.json()["answer"] == "두 번째엔 성공"
    assert fake_db.turns[turn_id]["llm_attempt"] == 2


def test_unexpected_ai_failure_is_recorded_and_returns_internal_error(client, fake_db):
    use_llm(FakeLLM(RuntimeError("boom 비밀 내용")))
    session_id, turn_id = fake_db.add_session(), new_id()

    res = send(client, session_id, turn_id)

    assert res.status_code == 500 and res.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "비밀" not in res.text
    assert fake_db.turns[turn_id]["error_message"] == "llm_failed"


@pytest.mark.parametrize("failure, expected_status", [(busy_error(), 429), (RuntimeError("synthetic failure"), 500)])
def test_failed_generation_keeps_its_request_id_and_retry_gets_a_new_one(client, fake_db, failure, expected_status):
    llm = use_llm(FakeLLM(failure, LLMResult("재시도 성공", "stop")))
    session_id, turn_id = fake_db.add_session(), new_id()

    first = send(client, session_id, turn_id)

    assert first.status_code == expected_status
    first_request_id = llm.calls[0]["request_id"]
    assert fake_db.turns[turn_id]["answer_status"] == "failed"
    assert fake_db.turns[turn_id]["llm_request_id"] == first_request_id
    assert len(llm.calls) == 1

    second = send(client, session_id, turn_id)

    assert second.status_code == 200
    assert fake_db.turns[turn_id]["llm_attempt"] == 2
    assert llm.calls[1]["request_id"] != first_request_id
    assert fake_db.turns[turn_id]["llm_request_id"] == llm.calls[1]["request_id"]


# --- 입력 검증 / 인증 / 존재하지 않는 대화방 ---


@pytest.mark.parametrize(
    "message, code",
    [("", "MESSAGE_EMPTY"), ("   ", "MESSAGE_EMPTY"), ("가" * 2001, "MESSAGE_TOO_LONG")],
)
def test_message_validation(client, fake_db, message, code):
    use_llm(FakeLLM())
    res = send(client, fake_db.add_session(), message=message)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == code and res.json()["error"]["field"] == "message"


def test_message_of_exactly_2000_chars_is_accepted(client, fake_db):
    use_llm(FakeLLM())
    assert send(client, fake_db.add_session(), message="가" * 2000).status_code == 200


def test_client_cannot_send_history_or_persona(client, fake_db):
    use_llm(FakeLLM())
    res = send(client, fake_db.add_session(), history=[{"role": "user", "content": "x"}])
    assert res.status_code == 422 and res.json()["error"]["code"] == "INVALID_INPUT"


def test_invalid_uuid_reports_field(client, fake_db):
    use_llm(FakeLLM())
    res = send(client, "not-a-uuid")
    assert res.status_code == 422
    assert (res.json()["error"]["code"], res.json()["error"]["field"]) == ("INVALID_INPUT", "session_id")


def test_unknown_session_is_not_found_with_turn_id(client, fake_db):
    use_llm(FakeLLM())
    turn_id = new_id()
    res = send(client, new_id(), turn_id)
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND" and res.json()["error"]["turn_id"] == turn_id


def test_database_outage_is_503_and_retryable(client, fake_db, monkeypatch):
    use_llm(FakeLLM())

    async def down(*args, **kwargs):
        raise db.DatabaseUnavailableError(outcome_unknown=True)

    monkeypatch.setattr(fake_db, "create_chat_turn", down)
    res = send(client, fake_db.add_session())
    assert res.status_code == 503
    assert (res.json()["error"]["code"], res.json()["error"]["retryable"]) == ("DB_UNAVAILABLE", True)


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "Bearer   "])
def test_missing_or_malformed_authorization_is_401(header):
    app.dependency_overrides.clear()
    headers = {} if header is None else {"Authorization": header}
    res = TestClient(app).post(
        "/chat/text", headers=headers, json={"session_id": new_id(), "turn_id": new_id(), "message": "안녕"}
    )
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHENTICATED"


def test_bearer_token_is_verified_through_database_module(monkeypatch):
    app.dependency_overrides.clear()
    seen = {}

    class FakeDatabase:
        async def for_user(self, token):
            seen["token"] = token
            return FakeUserDB()

    monkeypatch.setattr(auth, "get_database", lambda: FakeDatabase())
    res = TestClient(app).get("/personas", headers={"Authorization": "Bearer abc.def.ghi"})

    assert res.status_code == 200
    assert seen["token"] == "abc.def.ghi"  # 'Bearer ' 접두어는 떼고 넘긴다


# --- DB 연결: AI 요청 ID, finish_reason, 전체 시간 (PR #61) ---


def test_ai_request_id_is_recorded_in_db_before_the_call_and_is_the_one_sent_to_ai(client, fake_db):
    llm = use_llm(FakeLLM())
    session_id, turn_id = fake_db.add_session(), new_id()

    send(client, session_id, turn_id)

    sent_id = llm.calls[0]["request_id"]
    assert sent_id and fake_db.turns[turn_id]["llm_request_id"] == sent_id
    assert str(UUID(sent_id)) == sent_id


def test_manual_retry_uses_a_new_ai_request_id(client, fake_db):
    llm = use_llm(FakeLLM(busy_error(), LLMResult("두 번째엔 성공", "stop")))
    session_id, turn_id = fake_db.add_session(), new_id()

    send(client, session_id, turn_id)
    send(client, session_id, turn_id)

    first, second = llm.calls[0]["request_id"], llm.calls[1]["request_id"]
    assert first != second
    assert fake_db.turns[turn_id]["llm_request_id"] == second  # 새 시도의 ID만 남는다


def test_finish_reason_is_stored_and_replayed(client, fake_db):
    use_llm(FakeLLM(LLMResult("길어서 잘린 답", "length")))
    session_id, turn_id = fake_db.add_session(), new_id()

    first = send(client, session_id, turn_id)
    again = send(client, session_id, turn_id)

    assert first.json()["finish_reason"] == "length"
    assert fake_db.turns[turn_id]["finish_reason"] == "length"
    assert again.json()["finish_reason"] == "length"  # 저장된 값을 그대로 돌려준다


def test_turn_without_stored_finish_reason_replays_as_stop(client, fake_db):
    use_llm(FakeLLM())
    session_id, turn_id = fake_db.add_session(), new_id()
    send(client, session_id, turn_id)
    fake_db.turns[turn_id]["finish_reason"] = None  # 컬럼이 생기기 전에 만든 턴

    assert send(client, session_id, turn_id).json()["finish_reason"] == "stop"


def test_failed_attempt_stores_no_finish_reason(client, fake_db):
    use_llm(FakeLLM(busy_error()))
    session_id, turn_id = fake_db.add_session(), new_id()

    send(client, session_id, turn_id)

    assert fake_db.turns[turn_id]["finish_reason"] is None


def test_total_ms_is_recorded_on_the_last_stage_and_matches_the_response(client, fake_db):
    use_llm(FakeLLM())
    session_id, turn_id = fake_db.add_session(), new_id()

    body = send(client, session_id, turn_id).json()

    assert fake_db.turns[turn_id]["total_ms"] == body["timings"]["total_ms"]
    assert ("total", "llm") in fake_db.calls


def test_failing_total_ms_record_does_not_break_the_response(client, fake_db, monkeypatch):
    use_llm(FakeLLM())

    async def stale(*args, **kwargs):
        raise db.ConflictError(code="55000")

    monkeypatch.setattr(fake_db, "record_turn_total_ms", stale)
    assert send(client, fake_db.add_session()).status_code == 200
