"""POST /records, POST /records/{id}/uploaded, GET /records (contracts/app-backend/api-spec.md '기록' 장)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.auth import get_user_db
from app.core.storage import get_storage
from app.main import app
from tests.fakes import FakeStorage, FakeUserDB, new_id

KAKAO = "[엄마] [오후 3:12] 밥 먹었니\n[나] [오후 3:13] 응\n[엄마] [오후 3:14] 다행이다\n".encode()
ONE_SPEAKER = "[엄마] [오후 3:12] 안녕\n[엄마] [오후 3:13] 잘 지내니\n".encode()
PLAIN = "오늘은 날씨가 좋았다.\n산책을 했다.\n".encode()
PUBLIC_FIELDS = {"id", "persona_id", "kind", "original_name", "mime_type", "size_bytes", "status", "consent_at",
                 "consent_version", "consent_scopes", "error_code", "target_speaker", "provider_speaker",
                 "created_at", "updated_at"}


@pytest.fixture
def fake_db():
    fake = FakeUserDB()
    app.dependency_overrides[get_user_db] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def storage():
    fake = FakeStorage()
    app.dependency_overrides[get_storage] = lambda: fake
    yield fake


@pytest.fixture
def client(fake_db, storage):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def persona(client):
    return client.post("/personas", json={"name": "엄마"}).json()["id"]


def body(persona, **over):
    base = {"persona_id": persona, "kind": "text", "original_name": "chat.txt", "mime_type": "text/plain",
            "size_bytes": len(KAKAO), "consent_at": datetime.now(timezone.utc).isoformat(), "consent_version": "v1"}
    return {**base, **over}


def reserve(client, persona, **over):
    return client.post("/records", json=body(persona, **over))


def put_file(storage, fake_db, record_id, data):
    storage.files[fake_db.records[record_id]["object_path"]] = data


def error(res):
    return res.json()["error"]


# --- 예약 ---


def test_reserve_returns_record_and_signed_upload_without_internal_fields(client, fake_db, storage, persona):
    res = reserve(client, persona)
    assert res.status_code == 201
    out = res.json()
    assert set(out) == PUBLIC_FIELDS | {"upload"}
    assert out["status"] == "uploading" and out["consent_scopes"] == ["personalization"]
    assert out["upload"]["method"] == "PUT" and out["upload"]["headers"] == {"Content-Type": "text/plain"}
    assert out["upload"]["url"].startswith("https://storage.test/") and out["upload"]["expires_at"]
    assert storage.upload_calls == [(fake_db.records[out["id"]]["object_path"], "text/plain")]


def test_reserve_unknown_persona_is_404(client):
    res = reserve(client, new_id())
    assert res.status_code == 404 and error(res)["code"] == "NOT_FOUND"


def test_too_large_file_is_413_and_nothing_is_saved(client, fake_db, persona):
    res = reserve(client, persona, size_bytes=25 * 1024 * 1024 + 1)
    assert res.status_code == 413
    assert (error(res)["code"], error(res)["field"]) == ("FILE_TOO_LARGE", "size_bytes")
    assert fake_db.records == {}
    assert reserve(client, persona, size_bytes=25 * 1024 * 1024).status_code == 201


@pytest.mark.parametrize("kind, mime", [("text", "audio/wav"), ("audio", "text/plain"), ("audio", "audio/ogg")])
def test_mime_type_must_match_kind(client, persona, kind, mime):
    res = reserve(client, persona, kind=kind, mime_type=mime)
    assert res.status_code == 422 and (error(res)["code"], error(res)["field"]) == ("INVALID_INPUT", "mime_type")


def test_consent_time_needs_a_timezone(client, persona):
    res = reserve(client, persona, consent_at="2026-11-15T10:00:00")
    assert res.status_code == 422 and error(res)["field"] == "consent_at"


def test_unknown_fields_are_rejected(client, persona):
    res = client.post("/records", json={**body(persona), "user_id": new_id()})
    assert res.status_code == 422 and error(res)["code"] == "INVALID_INPUT"


# --- 예약: 화자 형식 ---


def test_speakers_are_trimmed_and_stored(client, persona):
    res = reserve(client, persona, target_speaker="  엄마 ", provider_speaker="나")
    assert res.status_code == 201
    assert (res.json()["target_speaker"], res.json()["provider_speaker"]) == ("엄마", "나")


@pytest.mark.parametrize("over, field", [
    ({"provider_speaker": "나"}, "target_speaker"),
    ({"target_speaker": "엄마", "provider_speaker": "엄마"}, "provider_speaker"),
    ({"target_speaker": "엄마", "provider_speaker": " 엄마 "}, "provider_speaker"),
    ({"target_speaker": "   ", "provider_speaker": "나"}, "target_speaker"),
    ({"target_speaker": "가" * 101, "provider_speaker": "나"}, "target_speaker"),
])
def test_speaker_format_errors_name_the_field(client, persona, over, field):
    res = reserve(client, persona, **over)
    assert res.status_code == 422 and (error(res)["code"], error(res)["field"]) == ("INVALID_INPUT", field)


def test_audio_records_cannot_carry_speakers(client, persona):
    res = reserve(client, persona, kind="audio", mime_type="audio/wav", target_speaker="엄마", provider_speaker="나")
    assert res.status_code == 422 and error(res)["field"] == "target_speaker"


# --- 재예약 ---


def test_same_record_and_body_is_a_rereservation_with_a_new_upload(client, fake_db, storage, persona):
    record_id = new_id()
    payload = body(persona, record_id=record_id)
    first = client.post("/records", json=payload)
    again = client.post("/records", json=payload)
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["id"] == record_id and len(fake_db.records) == 1
    assert again.json()["upload"]["url"] != first.json()["upload"]["url"]


def test_rereservation_with_a_different_body_or_after_upload_is_409(client, fake_db, storage, persona):
    record_id = new_id()
    assert client.post("/records", json=body(persona, record_id=record_id)).status_code == 201
    other = client.post("/records", json=body(persona, record_id=record_id, original_name="other.txt"))
    assert other.status_code == 409 and error(other)["code"] == "CONFLICT"

    put_file(storage, fake_db, record_id, KAKAO)
    assert client.post(f"/records/{record_id}/uploaded", json={"target_speaker": "엄마", "provider_speaker": "나"}
                       ).status_code == 200
    late = client.post("/records", json=body(persona, record_id=record_id, target_speaker="엄마", provider_speaker="나"))
    assert late.status_code == 409


def test_storage_failure_at_reservation_keeps_the_row_and_retry_succeeds(client, fake_db, storage, persona):
    record_id = new_id()
    payload = body(persona, record_id=record_id)  # 같은 본문을 다시 보낸다(consent_at 포함)
    storage.down = True
    res = client.post("/records", json=payload)
    assert res.status_code == 503 and (error(res)["code"], error(res)["retryable"]) == ("DB_UNAVAILABLE", True)
    assert fake_db.records[record_id]["status"] == "uploading"

    storage.down = False
    retry = client.post("/records", json=payload)
    assert retry.status_code == 200 and retry.json()["upload"]["url"]


# --- 업로드 완료 알림 ---


def reserved(client, persona, **over):
    return reserve(client, persona, **over).json()["id"]


def test_uploaded_without_the_file_or_with_a_different_size_is_409(client, fake_db, storage, persona):
    record_id = reserved(client, persona, target_speaker="엄마", provider_speaker="나")
    res = client.post(f"/records/{record_id}/uploaded")
    assert res.status_code == 409 and (error(res)["code"], error(res)["retryable"]) == ("UPLOAD_MISSING", True)

    storage.files[fake_db.records[record_id]["object_path"]] = KAKAO[:-1]
    res = client.post(f"/records/{record_id}/uploaded")
    # 올린 객체는 덮어쓸 수 없으므로 같은 기록으로는 복구되지 않는다(기록을 지우고 새로 예약).
    assert res.status_code == 409 and (error(res)["code"], error(res)["retryable"]) == ("UPLOAD_SIZE_MISMATCH", False)
    assert fake_db.records[record_id]["status"] == "uploading"


def test_uploaded_marks_the_record_and_repeating_the_call_is_idempotent(client, fake_db, storage, persona):
    record_id = reserved(client, persona, size_bytes=len(PLAIN))
    put_file(storage, fake_db, record_id, PLAIN)
    first = client.post(f"/records/{record_id}/uploaded")
    again = client.post(f"/records/{record_id}/uploaded")
    assert first.status_code == 200 and first.json()["status"] == "uploaded"
    assert again.status_code == 200 and again.json()["status"] == "uploaded"
    assert set(first.json()) == PUBLIC_FIELDS


def test_kakao_export_needs_both_speakers_and_the_record_stays_uploading_on_422(client, fake_db, storage, persona):
    record_id = reserved(client, persona)
    put_file(storage, fake_db, record_id, KAKAO)
    res = client.post(f"/records/{record_id}/uploaded")
    assert res.status_code == 422 and (error(res)["code"], error(res)["field"]) == ("SPEAKER_REQUIRED", "target_speaker")
    assert fake_db.records[record_id]["status"] == "uploading"

    fixed = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": " 엄마", "provider_speaker": "나"})
    assert fixed.status_code == 200 and fixed.json()["status"] == "uploaded"
    assert (fixed.json()["target_speaker"], fixed.json()["provider_speaker"]) == ("엄마", "나")
    assert len(storage.upload_calls) == 1  # 파일을 다시 올릴 필요가 없다(재예약 없음)


def test_selected_speaker_must_exist_in_the_file(client, fake_db, storage, persona):
    record_id = reserved(client, persona, target_speaker="할머니", provider_speaker="나")
    put_file(storage, fake_db, record_id, KAKAO)
    res = client.post(f"/records/{record_id}/uploaded")
    assert res.status_code == 422 and error(res)["field"] == "target_speaker"

    res = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": "엄마", "provider_speaker": "아빠"})
    assert res.status_code == 422 and error(res)["field"] == "provider_speaker"
    assert fake_db.records[record_id]["status"] == "uploading"


def test_speakers_sent_with_the_reservation_are_checked_against_the_file(client, fake_db, storage, persona):
    record_id = reserved(client, persona, target_speaker="엄마", provider_speaker="나")
    put_file(storage, fake_db, record_id, KAKAO)
    res = client.post(f"/records/{record_id}/uploaded")
    assert res.status_code == 200 and res.json()["target_speaker"] == "엄마"


def test_speakers_are_rejected_for_a_file_that_is_not_a_kakao_export(client, fake_db, storage, persona):
    record_id = reserved(client, persona, size_bytes=len(PLAIN), target_speaker="엄마", provider_speaker="나")
    put_file(storage, fake_db, record_id, PLAIN)
    res = client.post(f"/records/{record_id}/uploaded")
    assert res.status_code == 422 and error(res)["field"] == "target_speaker"
    assert fake_db.records[record_id]["status"] == "uploading"


# --- 화자가 1명뿐인 카카오톡 기록: 대화 상대 확인 ---


def single(client, persona, **over):
    return reserved(client, persona, size_bytes=len(ONE_SPEAKER), **over)


def test_single_speaker_needs_the_target_confirmation_and_stays_uploading_until_then(client, fake_db, storage, persona):
    record_id = single(client, persona)
    put_file(storage, fake_db, record_id, ONE_SPEAKER)
    res = client.post(f"/records/{record_id}/uploaded")
    assert res.status_code == 422 and (error(res)["code"], error(res)["field"]) == ("SPEAKER_REQUIRED", "target_speaker")
    assert fake_db.records[record_id]["status"] == "uploading"  # 확인 전에는 색인·분석이 시작될 수 없다
    assert fake_db.records[record_id]["target_speaker"] is None


def test_single_speaker_is_completed_by_confirming_only_the_target(client, fake_db, storage, persona):
    record_id = single(client, persona)
    put_file(storage, fake_db, record_id, ONE_SPEAKER)
    res = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": " 엄마 "})
    assert res.status_code == 200 and res.json()["status"] == "uploaded"
    assert (res.json()["target_speaker"], res.json()["provider_speaker"]) == ("엄마", None)
    assert len(storage.upload_calls) == 1  # 파일을 다시 올리지 않았다


def test_single_speaker_confirmation_sent_with_the_reservation_is_checked_against_the_file(client, fake_db, storage, persona):
    record_id = single(client, persona, target_speaker="엄마")
    put_file(storage, fake_db, record_id, ONE_SPEAKER)
    res = client.post(f"/records/{record_id}/uploaded")
    assert res.status_code == 200 and res.json()["target_speaker"] == "엄마"


def test_single_speaker_confirmation_must_name_the_speaker_in_the_file(client, fake_db, storage, persona):
    record_id = single(client, persona)
    put_file(storage, fake_db, record_id, ONE_SPEAKER)
    res = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": "할머니"})
    assert res.status_code == 422 and error(res)["field"] == "target_speaker"
    assert fake_db.records[record_id]["status"] == "uploading"


def test_the_provider_is_never_guessed_for_a_single_speaker_file(client, fake_db, storage, persona):
    record_id = single(client, persona)
    put_file(storage, fake_db, record_id, ONE_SPEAKER)
    res = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": "엄마", "provider_speaker": "나"})
    assert res.status_code == 422 and error(res)["field"] == "provider_speaker"
    assert fake_db.records[record_id]["status"] == "uploading"


def test_two_speakers_still_need_both_values(client, fake_db, storage, persona):
    record_id = reserved(client, persona, target_speaker="엄마")  # 예약 단계는 target만으로 통과(파일을 아직 모름)
    put_file(storage, fake_db, record_id, KAKAO)
    res = client.post(f"/records/{record_id}/uploaded")
    assert res.status_code == 422 and error(res)["field"] == "provider_speaker"
    done = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": "엄마", "provider_speaker": "나"})
    assert done.status_code == 200 and done.json()["provider_speaker"] == "나"


def test_resent_speakers_replace_the_reserved_ones(client, fake_db, storage, persona):
    record_id = reserved(client, persona, target_speaker="나", provider_speaker="엄마")
    put_file(storage, fake_db, record_id, KAKAO)
    res = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": "엄마", "provider_speaker": "나"})
    assert res.status_code == 200
    assert (res.json()["target_speaker"], res.json()["provider_speaker"]) == ("엄마", "나")


@pytest.mark.parametrize("data", [b"\x00\x01\x02binary", b"   \n  ", "안녕 반가워".encode("cp949") + b"\n"])
def test_unreadable_text_is_422_without_a_field(client, fake_db, storage, persona, data):
    record_id = reserved(client, persona, size_bytes=len(data))
    put_file(storage, fake_db, record_id, data)
    res = client.post(f"/records/{record_id}/uploaded")
    assert res.status_code == 422 and (error(res)["code"], error(res)["field"]) == ("INVALID_INPUT", None)


def test_a_file_with_too_many_distinct_speakers_is_unreadable_and_saves_no_speaker_state(client, fake_db, storage, persona):
    from app.services.kakao import MAX_SPEAKERS

    data = "".join(f"[u{n}] [오후 3:12] 안녕\n" for n in range(MAX_SPEAKERS + 1)).encode()
    record_id = reserved(client, persona, size_bytes=len(data))
    put_file(storage, fake_db, record_id, data)
    res = client.post(f"/records/{record_id}/uploaded")
    assert res.status_code == 422 and (error(res)["code"], error(res)["field"]) == ("INVALID_INPUT", None)
    assert "speakers" not in error(res)  # 수천 명의 목록을 응답에 싣지 않는다
    assert (fake_db.records[record_id]["status"], fake_db.records[record_id]["error_code"]) == ("uploading", None)


def test_audio_upload_is_checked_by_size_only_and_rejects_speakers(client, fake_db, storage, persona):
    record_id = reserved(client, persona, kind="audio", mime_type="audio/wav", size_bytes=44)
    put_file(storage, fake_db, record_id, b"RIFF" + b"\x00" * 40)
    bad = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": "엄마", "provider_speaker": "나"})
    assert bad.status_code == 422 and error(bad)["field"] == "target_speaker"
    ok = client.post(f"/records/{record_id}/uploaded")
    assert ok.status_code == 200 and ok.json()["status"] == "uploaded"


def test_storage_failure_at_uploaded_is_503_and_the_record_stays_uploading(client, fake_db, storage, persona):
    record_id = reserved(client, persona, size_bytes=len(PLAIN))
    put_file(storage, fake_db, record_id, PLAIN)
    storage.down = True
    res = client.post(f"/records/{record_id}/uploaded")
    assert res.status_code == 503 and error(res)["code"] == "DB_UNAVAILABLE"
    assert fake_db.records[record_id]["status"] == "uploading"


@pytest.mark.parametrize("status", ["failed", "deleting"])
def test_uploaded_is_409_for_failed_or_deleting_records(client, fake_db, persona, status):
    record_id = reserved(client, persona)
    fake_db.records[record_id]["status"] = status
    assert client.post(f"/records/{record_id}/uploaded").status_code == 409


def test_uploaded_for_an_unknown_record_is_404(client):
    assert client.post(f"/records/{new_id()}/uploaded").status_code == 404


def test_unknown_body_fields_on_uploaded_are_rejected(client, persona):
    record_id = reserved(client, persona)
    res = client.post(f"/records/{record_id}/uploaded", json={"status": "ready"})
    assert res.status_code == 422


# --- 목록·상세 ---


def test_list_is_newest_first_filtered_and_hides_deleting_records(client, fake_db, persona):
    other = client.post("/personas", json={"name": "아빠"}).json()["id"]
    a, b = reserved(client, persona, record_id=new_id()), reserved(client, other, record_id=new_id())
    c = reserved(client, persona, record_id=new_id())
    fake_db.records[b]["status"] = "deleting"
    for n, rid in enumerate((a, b, c)):
        fake_db.records[rid]["created_at"] = f"2026-11-15T10:00:0{n}+00:00"

    listed = client.get("/records").json()
    assert [r["id"] for r in listed["items"]] == [c, a] and (listed["limit"], listed["offset"]) == (50, 0)
    assert all(set(item) == PUBLIC_FIELDS for item in listed["items"])
    assert [r["id"] for r in client.get(f"/records?persona_id={other}").json()["items"]] == []
    assert [r["id"] for r in client.get(f"/records?persona_id={persona}&limit=1&offset=1").json()["items"]] == [a]


def test_get_record_and_hidden_cases(client, fake_db, persona):
    record_id = reserved(client, persona)
    assert client.get(f"/records/{record_id}").json()["id"] == record_id
    assert client.get(f"/records/{new_id()}").status_code == 404
    fake_db.records[record_id]["status"] = "deleting"
    assert client.get(f"/records/{record_id}").status_code == 404


@pytest.mark.parametrize("query, field", [("limit=101", "limit"), ("limit=0", "limit"), ("offset=-1", "offset"),
                                          ("persona_id=nope", "persona_id")])
def test_list_query_validation(client, query, field):
    res = client.get(f"/records?{query}")
    assert res.status_code == 422 and error(res)["field"] == field


def test_a_zero_byte_reservation_is_rejected_with_its_field(client, persona):
    for kind, mime in (("text", "text/plain"), ("audio", "audio/wav")):
        res = reserve(client, persona, kind=kind, mime_type=mime, size_bytes=0)
        assert res.status_code == 422 and (error(res)["code"], error(res)["field"]) == ("INVALID_INPUT", "size_bytes")


def test_a_future_consent_time_is_rejected_with_its_field(client, fake_db, persona):
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    res = reserve(client, persona, consent_at=future)
    assert res.status_code == 422 and (error(res)["code"], error(res)["field"]) == ("INVALID_INPUT", "consent_at")
    assert fake_db.records == {}


def test_a_concurrent_call_that_finished_first_is_answered_with_the_current_record(client, fake_db, storage, persona):
    record_id = reserved(client, persona, size_bytes=len(PLAIN))
    put_file(storage, fake_db, record_id, PLAIN)
    original = fake_db.update_record_upload

    async def lose_the_race(*args, **kwargs):
        await original(*args, **kwargs)  # 다른 요청이 먼저 `uploaded`로 바꿈
        return await original(*args, **kwargs)  # 이쪽은 수정 시각·상태가 이미 바뀌어 충돌

    fake_db.update_record_upload = lose_the_race
    res = client.post(f"/records/{record_id}/uploaded")
    assert res.status_code == 200 and res.json()["status"] == "uploaded"


def test_a_conflict_that_is_not_a_finished_record_stays_409(client, fake_db, storage, persona):
    record_id = reserved(client, persona, size_bytes=len(PLAIN))
    put_file(storage, fake_db, record_id, PLAIN)

    async def always_conflict(*args, **kwargs):
        from app import db
        raise db.ConflictError()

    fake_db.update_record_upload = always_conflict
    assert client.post(f"/records/{record_id}/uploaded").status_code == 409


# --- 화자 오류 코드와 서버가 본 화자 목록(speakers), null 해제 (#117) ---


def test_speaker_errors_carry_the_speakers_the_server_found(client, fake_db, storage, persona):
    record_id = reserved(client, persona)
    put_file(storage, fake_db, record_id, KAKAO)
    res = client.post(f"/records/{record_id}/uploaded")
    err = error(res)
    assert (err["code"], err["retryable"], err["field"]) == ("SPEAKER_REQUIRED", True, "target_speaker")
    assert err["speakers"] == ["엄마", "나"]  # 메시지가 많은 순

    res = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": "할머니", "provider_speaker": "나"})
    assert (error(res)["code"], error(res)["field"], error(res)["speakers"]) == ("SPEAKER_NOT_FOUND", "target_speaker", ["엄마", "나"])

    res = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": "엄마"})
    assert (error(res)["code"], error(res)["field"]) == ("SPEAKER_REQUIRED", "provider_speaker")


def test_the_app_can_relist_choices_when_it_counted_one_speaker_but_the_server_found_two(client, fake_db, storage, persona):
    record_id = reserved(client, persona, target_speaker="엄마")  # 앱은 1명으로 보고 확인값만 보냄
    put_file(storage, fake_db, record_id, KAKAO)
    res = client.post(f"/records/{record_id}/uploaded")
    assert (error(res)["code"], error(res)["field"], error(res)["speakers"]) == ("SPEAKER_REQUIRED", "provider_speaker", ["엄마", "나"])
    done = client.post(f"/records/{record_id}/uploaded", json={"provider_speaker": "나"})  # 생략한 target은 유지
    assert done.status_code == 200 and (done.json()["target_speaker"], done.json()["provider_speaker"]) == ("엄마", "나")


def test_not_allowed_has_an_empty_speaker_list_for_a_file_without_speakers_and_can_be_cleared_with_null(client, fake_db, storage, persona):
    record_id = reserved(client, persona, size_bytes=len(PLAIN), target_speaker="엄마", provider_speaker="나")
    put_file(storage, fake_db, record_id, PLAIN)
    res = client.post(f"/records/{record_id}/uploaded")
    assert (error(res)["code"], error(res)["field"], error(res)["speakers"]) == ("SPEAKER_NOT_ALLOWED", "target_speaker", [])

    cleared = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": None, "provider_speaker": None})
    assert cleared.status_code == 200 and cleared.json()["status"] == "uploaded"
    assert (cleared.json()["target_speaker"], cleared.json()["provider_speaker"]) == (None, None)
    assert len(storage.upload_calls) == 1  # 파일을 다시 올리지 않았다


def test_a_provider_sent_for_a_single_speaker_file_is_cleared_with_null_and_the_target_is_kept(client, fake_db, storage, persona):
    record_id = single(client, persona, target_speaker="엄마", provider_speaker="나")
    put_file(storage, fake_db, record_id, ONE_SPEAKER)
    res = client.post(f"/records/{record_id}/uploaded")
    err = error(res)
    assert (err["code"], err["field"], err["speakers"]) == ("SPEAKER_NOT_ALLOWED", "provider_speaker", ["엄마"])
    done = client.post(f"/records/{record_id}/uploaded", json={"provider_speaker": None})
    assert done.status_code == 200 and (done.json()["target_speaker"], done.json()["provider_speaker"]) == ("엄마", None)


def test_omitted_keys_and_an_empty_body_keep_the_reserved_values(client, fake_db, storage, persona):
    record_id = reserved(client, persona, target_speaker="엄마", provider_speaker="나")
    put_file(storage, fake_db, record_id, KAKAO)
    res = client.post(f"/records/{record_id}/uploaded", json={})
    assert res.status_code == 200 and (res.json()["target_speaker"], res.json()["provider_speaker"]) == ("엄마", "나")


def test_clearing_only_the_target_leaves_a_provider_alone_and_is_a_format_error(client, fake_db, storage, persona):
    record_id = reserved(client, persona, target_speaker="엄마", provider_speaker="나")
    put_file(storage, fake_db, record_id, KAKAO)
    res = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": None})
    assert res.status_code == 422 and (error(res)["code"], error(res)["field"]) == ("INVALID_INPUT", "target_speaker")
    assert "speakers" not in error(res)  # 화자 오류(SPEAKER_*)가 아니라 형식 오류


def test_other_errors_have_no_speakers_key(client, fake_db, storage, persona):
    record_id = reserved(client, persona)
    res = client.post(f"/records/{record_id}/uploaded")  # 파일 없음(409)
    assert res.status_code == 409 and "speakers" not in error(res)
    bad = client.post("/records", json={**body(persona), "size_bytes": 25 * 1024 * 1024 + 1})
    assert bad.status_code == 413 and "speakers" not in error(bad)


def test_the_final_values_must_not_name_the_same_person_twice(client, fake_db, storage, persona):
    record_id = reserved(client, persona, target_speaker="엄마", provider_speaker="나")  # 예약 때는 서로 다름
    put_file(storage, fake_db, record_id, KAKAO)
    res = client.post(f"/records/{record_id}/uploaded", json={"provider_speaker": "엄마"})  # 합치면 엄마/엄마
    assert res.status_code == 422
    assert (error(res)["code"], error(res)["field"]) == ("INVALID_INPUT", "provider_speaker")
    assert "speakers" not in error(res)  # 서버 판정(SPEAKER_*)이 아니라 형식 오류
    assert fake_db.records[record_id]["status"] == "uploading"
    assert fake_db.records[record_id]["provider_speaker"] == "나"  # 거절된 값은 저장되지 않는다
    assert len(storage.upload_calls) == 1

    fixed = client.post(f"/records/{record_id}/uploaded", json={"provider_speaker": " 나 "})  # 파일을 다시 올리지 않고 복구
    assert fixed.status_code == 200 and (fixed.json()["target_speaker"], fixed.json()["provider_speaker"]) == ("엄마", "나")


# --- 재예약의 화자 갱신, 화자 확인 대기 error_code (#117) ---


def test_a_rereservation_may_change_the_speakers_while_uploading(client, fake_db, storage, persona):
    record_id = new_id()
    base = body(persona, record_id=record_id)  # 화자를 뺀 필드는 같게 다시 보낸다
    assert client.post("/records", json={**base, "target_speaker": "엄마", "provider_speaker": "나"}).status_code == 201
    swapped = client.post("/records", json={**base, "target_speaker": "나", "provider_speaker": "엄마"})
    assert swapped.status_code == 200
    assert (swapped.json()["target_speaker"], swapped.json()["provider_speaker"]) == ("나", "엄마")
    assert len(fake_db.records) == 1 and len(storage.upload_calls) == 2

    omitted = client.post("/records", json=base)  # 생략하면 없음(`/uploaded`의 '생략은 유지'와 다름)
    assert omitted.status_code == 200
    assert (omitted.json()["target_speaker"], omitted.json()["provider_speaker"]) == (None, None)

    other = client.post("/records", json={**base, "original_name": "other.txt"})  # 화자 외 필드가 다르면 409
    assert other.status_code == 409 and error(other)["code"] == "CONFLICT"


def test_a_speaker_error_is_remembered_in_error_code_and_cleared_when_it_passes(client, fake_db, storage, persona):
    record_id = reserved(client, persona)
    put_file(storage, fake_db, record_id, KAKAO)
    assert client.post(f"/records/{record_id}/uploaded").status_code == 422

    detail = client.get(f"/records/{record_id}").json()  # 앱을 닫았다 다시 열어도 보인다
    assert (detail["status"], detail["error_code"]) == ("uploading", "speaker_required")
    assert client.get("/records").json()["items"][0]["error_code"] == "speaker_required"

    again = client.post(f"/records/{record_id}/uploaded")  # 본문 없이 다시 호출하면 같은 오류와 speakers
    assert error(again)["code"] == "SPEAKER_REQUIRED" and error(again)["speakers"] == ["엄마", "나"]

    wrong = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": "할머니", "provider_speaker": "나"})
    assert error(wrong)["code"] == "SPEAKER_NOT_FOUND"
    assert client.get(f"/records/{record_id}").json()["error_code"] == "speaker_not_found"

    done = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": "엄마", "provider_speaker": "나"})
    assert done.status_code == 200 and (done.json()["status"], done.json()["error_code"]) == ("uploaded", None)


def test_a_not_allowed_speaker_error_is_remembered_too(client, fake_db, storage, persona):
    record_id = reserved(client, persona, size_bytes=len(PLAIN), target_speaker="엄마", provider_speaker="나")
    put_file(storage, fake_db, record_id, PLAIN)
    assert error(client.post(f"/records/{record_id}/uploaded"))["code"] == "SPEAKER_NOT_ALLOWED"
    assert client.get(f"/records/{record_id}").json()["error_code"] == "speaker_not_allowed"


def test_format_errors_and_other_failures_are_not_remembered(client, fake_db, storage, persona):
    record_id = reserved(client, persona, target_speaker="엄마", provider_speaker="나")
    put_file(storage, fake_db, record_id, KAKAO)
    res = client.post(f"/records/{record_id}/uploaded", json={"provider_speaker": "엄마"})  # 동일인 중복(형식 오류)
    assert error(res)["code"] == "INVALID_INPUT"
    assert fake_db.records[record_id]["error_code"] is None

    other = reserved(client, persona)
    assert client.post(f"/records/{other}/uploaded").status_code == 409  # 원본 없음
    assert fake_db.records[other]["error_code"] is None


def test_failing_to_remember_the_state_does_not_hide_the_speaker_error(client, fake_db, storage, persona):
    from app import db

    record_id = reserved(client, persona)
    put_file(storage, fake_db, record_id, KAKAO)
    original = fake_db.update_record_upload

    async def failing(*args, **kwargs):
        if kwargs.get("status") == "uploading":
            raise db.DatabaseUnavailableError()
        return await original(*args, **kwargs)

    fake_db.update_record_upload = failing
    res = client.post(f"/records/{record_id}/uploaded")
    assert res.status_code == 422 and error(res)["code"] == "SPEAKER_REQUIRED" and error(res)["speakers"] == ["엄마", "나"]
    assert fake_db.records[record_id]["error_code"] is None


# --- 저장은 SQL RPC `update_record_upload`(#124)로: 읽은 시점의 수정 시각과 함께, 한 번에 (#121) ---


def test_the_final_values_are_saved_in_one_call_with_the_time_the_record_was_read(client, fake_db, storage, persona):
    record_id = reserved(client, persona)
    put_file(storage, fake_db, record_id, KAKAO)
    res = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": " 엄마", "provider_speaker": "나"})
    assert res.status_code == 200 and res.json()["status"] == "uploaded"
    # 오류를 남기지 않고, 두 화자 키를 모두 명시해 방금 검증한 값을 그대로 저장한다.
    assert fake_db.upload_updates == [{"status": "uploaded", "error_code": None,
                                       "speakers": {"target_speaker": "엄마", "provider_speaker": "나"}}]


def test_a_speaker_error_is_saved_without_touching_the_stored_speakers(client, fake_db, storage, persona):
    record_id = reserved(client, persona)
    put_file(storage, fake_db, record_id, KAKAO)
    assert client.post(f"/records/{record_id}/uploaded", json={"target_speaker": "할머니", "provider_speaker": "나"}).status_code == 422
    assert fake_db.upload_updates == [{"status": "uploading", "speakers": {}, "error_code": "speaker_not_found"}]
    stored = fake_db.records[record_id]
    assert (stored["target_speaker"], stored["provider_speaker"], stored["error_code"]) == (None, None, "speaker_not_found")


def test_a_rereservation_sends_both_speaker_keys_and_clears_the_pending_error(client, fake_db, storage, persona):
    record_id = reserved(client, persona, size_bytes=len(PLAIN), target_speaker="엄마", provider_speaker="나")
    put_file(storage, fake_db, record_id, PLAIN)
    assert client.post(f"/records/{record_id}/uploaded").status_code == 422  # SPEAKER_NOT_ALLOWED가 error_code에 남음
    assert fake_db.records[record_id]["error_code"] == "speaker_not_allowed"
    fake_db.upload_updates.clear()
    res = reserve(client, persona, record_id=record_id, size_bytes=len(PLAIN), consent_at=fake_db.records[record_id]["consent_at"])
    assert res.status_code == 200
    assert fake_db.upload_updates == [{"status": "uploading", "error_code": None,
                                       "speakers": {"target_speaker": None, "provider_speaker": None}}]
    assert fake_db.records[record_id]["error_code"] is None


def test_a_result_that_is_older_than_the_latest_selection_does_not_overwrite_it(client, fake_db, storage, persona):
    record_id = reserved(client, persona)
    put_file(storage, fake_db, record_id, KAKAO)
    original = storage.read_object

    async def another_request_changes_the_selection(*args, **kwargs):
        row = fake_db.records[record_id]
        row.update(target_speaker="나", provider_speaker="엄마")  # 파일을 읽는 동안 다른 요청이 선택을 바꿈
        fake_db._touch(row)
        return await original(*args, **kwargs)

    storage.read_object = another_request_changes_the_selection
    res = client.post(f"/records/{record_id}/uploaded", json={"target_speaker": "엄마", "provider_speaker": "나"})
    assert res.status_code == 409  # 오래된 검증 결과로 덮어쓰지 않는다
    stored = fake_db.records[record_id]
    assert (stored["status"], stored["target_speaker"], stored["provider_speaker"]) == ("uploading", "나", "엄마")
