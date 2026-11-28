"""기록 업로드 예약·완료 확인 (contracts/app-backend/api-spec.md 'POST /records', 'POST /records/{id}/uploaded').

예약 단계에는 파일이 없으므로 화자 값은 형식만 검사하고, 파일에 있는지·카카오톡 판정·화자 수는 파일을 올린 뒤
`/uploaded`에서 검사한다. 카카오톡 기록은 화자가 1명이어도 그 화자를 대화 상대로 확인한 `target_speaker`가 있어야 완료된다. 질문·본문·화자 이름은 로그에 남기지 않는다.
"""

import asyncio
import logging
from datetime import datetime, timezone

from app import db
from app.clients.storage import SignedUpload, StorageClient, StorageUnavailable
from app.core.config import get_settings
from app.core.errors import CONFLICT_MESSAGE, INVALID_INPUT_MESSAGE, AppError
from app.db.repository import CONSENT_CLOCK_TOLERANCE, RECORD_MIME_TYPES
from app.schemas.record import RecordCreate, UploadedBody
from app.services.audio_signature import HEAD_BYTES, signature_matches
from app.services.kakao import MAX_SPEAKER_NAME, NoChatMessages, SpeakerNameTooLong, TooManySpeakers, analyze_text

logger = logging.getLogger(__name__)

SPEAKER_MAX = MAX_SPEAKER_NAME  # 분석이 화자로 세는 이름의 길이 한도와 같다
# 서버 판정에 따른 화자 오류는 기록의 error_code에 저장해, 앱을 다시 열어도 '확인 필요' 기록임을 알 수 있게 한다.
_SPEAKER_STATES = {"SPEAKER_REQUIRED": "speaker_required", "SPEAKER_NOT_FOUND": "speaker_not_found",
                   "SPEAKER_NOT_ALLOWED": "speaker_not_allowed"}
_PROCESSED = ("uploaded", "processing", "ready")  # `/uploaded`를 이미 처리한 뒤의 상태(재호출은 멱등)


def max_upload_bytes() -> int:
    return get_settings().max_upload_mb * 1024 * 1024


def _invalid(field: str | None, message: str = INVALID_INPUT_MESSAGE) -> AppError:
    return AppError(422, "INVALID_INPUT", message, retryable=False, field=field)


def _speaker_error(code: str, field: str, message: str, speakers: list[str]) -> AppError:
    """화자 값을 고쳐 `/uploaded`를 다시 부르면 복구되는 오류. 서버가 본 화자 목록을 함께 준다."""
    return AppError(422, code, message, retryable=True, field=field, speakers=speakers)


def _storage_down() -> AppError:
    return AppError(503, "DB_UNAVAILABLE", "일시적으로 저장소에 연결할 수 없어요. 잠시 후 다시 시도해 주세요.",
                    retryable=True)


def clean_speakers(kind: str, target: str | None, provider: str | None) -> tuple[str | None, str | None]:
    """형식만 검사한다. 앞뒤 공백을 제거하고, `provider_speaker`만 올 수 없고, 둘이 오면 서로 달라야 한다.

    `target_speaker`만 오는 것은 화자가 1명뿐인 기록에서 그 화자를 대화 상대로 확인한 경우다. 화자 수와 파일 속 존재는
    파일을 올린 뒤(`mark_uploaded`) 검사한다.
    """
    if target is None and provider is None:
        return None, None
    if kind != "text":
        raise _invalid("target_speaker", "음성 기록에는 화자를 선택할 수 없어요.")
    if target is None:
        raise _invalid("target_speaker", "대화 상대를 선택해 주세요.")
    target = target.strip()
    values = [("target_speaker", target)]
    if provider is not None:
        provider = provider.strip()
        values.append(("provider_speaker", provider))
    for name, value in values:
        if not 1 <= len(value) <= SPEAKER_MAX:
            raise _invalid(name, "화자 이름을 확인해 주세요.")
    if provider is not None and target == provider:
        raise _invalid("provider_speaker", "대화 상대와 제공한 사람은 서로 달라야 해요.")
    return target, provider


def _check_kakao_speakers(speakers: list[str], target: str | None, provider: str | None) -> None:
    """카카오톡 기록은 화자가 1명이어도 대화 상대 확인(`target_speaker`)이 필요하다. 2명 이상이면 제공자도 필요하다.

    1명뿐인 기록의 제공자는 파일에 없으므로 추정하지 않고, 보내면 거절한다(`null`로 지우면 된다).
    """
    if target is None:
        raise _speaker_error("SPEAKER_REQUIRED", "target_speaker",
                             "이 화자가 대화 상대인지 확인해 주세요." if len(speakers) < 2
                             else "대화 상대와 제공한 사람을 선택해 주세요.", speakers)
    if target not in speakers:
        raise _speaker_error("SPEAKER_NOT_FOUND", "target_speaker", "선택한 사람이 파일에 없어요. 다시 선택해 주세요.", speakers)
    if len(speakers) < 2:
        if provider is not None:
            raise _speaker_error("SPEAKER_NOT_ALLOWED", "provider_speaker",
                                 "화자가 한 명뿐인 기록에는 제공한 사람을 선택할 수 없어요.", speakers)
        return
    if provider is None:
        raise _speaker_error("SPEAKER_REQUIRED", "provider_speaker", "이 기록을 제공한 사람을 선택해 주세요.", speakers)
    if provider not in speakers:
        raise _speaker_error("SPEAKER_NOT_FOUND", "provider_speaker", "선택한 사람이 파일에 없어요. 다시 선택해 주세요.", speakers)


def _same_instant(left: object, right: datetime) -> bool:
    try:
        return datetime.fromisoformat(str(left)) == right
    except ValueError:
        return False


def _read_at(record: dict) -> datetime:
    """검사를 시작하기 전에 읽은 기록의 수정 시각. 저장할 때 이 값과 같아야 한다(오래된 결과가 최신 상태를 덮지 않게)."""
    try:
        return datetime.fromisoformat(str(record["updated_at"]))
    except (KeyError, ValueError):
        raise AppError(500, "INTERNAL_ERROR", "서버에 문제가 생겼어요. 잠시 후 다시 시도해 주세요.", retryable=False) from None


def _same_reservation(row: dict, req: RecordCreate) -> bool:
    """화자 값을 뺀 필드가 같은지. 화자 값은 `uploading`인 동안 재예약으로 바꿀 수 있다."""
    return (
        row.get("persona_id") == str(req.persona_id)
        and row.get("kind") == req.kind
        and row.get("original_name") == req.original_name
        and row.get("mime_type") == req.mime_type
        and row.get("size_bytes") == req.size_bytes
        and row.get("consent_version") == req.consent_version
        and _same_instant(row.get("consent_at"), req.consent_at)
    )


def _speaker_choice_conflicts(record: dict, body: UploadedBody | None) -> bool:
    """이미 처리된 기록에 다시 온 `/uploaded`의 화자 선택이 저장된 확정값과 모순되는지.

    본문이 없거나 화자 키를 생략하면(확정값 유지) 모순이 아니다. 키를 보냈으면 앞뒤 공백을 뺀 값이 저장값과 같아야 하고,
    명시적 `null`은 '없음'이라 저장값이 있으면 모순이다. 모순을 200으로 돌려주면 호출한 쪽이 자기가 고른 화자가
    저장된 것으로 오인하므로(확정 화자는 이후 사실·말투 추출의 기준), 호출한 쪽은 409를 받는다.
    """
    if body is None:
        return False
    for key in ("target_speaker", "provider_speaker"):
        if key in body.model_fields_set:
            value = getattr(body, key)
            if isinstance(value, str):
                value = value.strip()
            if value != record.get(key):
                return True
    return False


async def _issue_upload(storage: StorageClient, row: dict) -> SignedUpload:
    try:
        return await storage.create_upload(row["object_path"], mime_type=row["mime_type"])
    except StorageUnavailable:
        raise _storage_down() from None


async def _original_exists(storage: StorageClient, row: dict) -> bool:
    try:
        return await storage.object_size(row["object_path"]) is not None
    except StorageUnavailable:
        raise _storage_down() from None


async def reserve(user_db: db.UserDatabase, storage: StorageClient, req: RecordCreate) -> tuple[dict, SignedUpload | None, bool]:
    """(기록 행, 서명 업로드 정보, 새로 만들었는지). 같은 `record_id`·같은 본문의 재예약이면 새로 만들지 않는다.

    재예약인데 원본이 이미 Storage에 있으면 서명 주소를 새로 발급하지 않고 `None`을 돌려준다. Storage는 이미 있는 객체의
    서명 주소 발급을 거절하고(400 KeyAlreadyExists, #120 실측) 원본은 덮어쓸 수 없으므로, 앱은 올리지 않고 `/uploaded`를 부른다.
    """
    limit = max_upload_bytes()
    if req.size_bytes > limit:
        raise AppError(413, "FILE_TOO_LARGE", f"파일이 너무 커요. 최대 {limit // (1024 * 1024)}MB까지 올릴 수 있어요.",
                       retryable=False, field="size_bytes")
    if req.mime_type not in RECORD_MIME_TYPES[req.kind]:
        raise _invalid("mime_type", "지원하지 않는 파일 형식이에요.")
    # 기기 시계가 서버보다 조금 앞서도 받는다(같은 허용 오차를 DB 모듈도 쓴다). 그보다 먼 미래만 거절한다.
    if req.consent_at > datetime.now(timezone.utc) + CONSENT_CLOCK_TOLERANCE:
        raise _invalid("consent_at", "동의한 시각을 확인해 주세요. 기기의 날짜와 시간이 맞는지 확인해 주세요.")
    target, provider = clean_speakers(req.kind, req.target_speaker, req.provider_speaker)

    try:
        row = await user_db.reserve_record(
            req.persona_id, kind=req.kind, original_name=req.original_name, mime_type=req.mime_type,
            size_bytes=req.size_bytes, consent_at=req.consent_at, consent_version=req.consent_version,
            record_id=req.record_id, target_speaker=target, provider_speaker=provider)
        created = True
    except db.ConflictError as exc:
        if req.record_id is None or exc.code != "23505":
            raise
        row = await user_db.get_record(req.record_id)
        if row.get("status") != "uploading" or not _same_reservation(row, req):
            raise AppError(409, "CONFLICT", CONFLICT_MESSAGE, retryable=False) from None
        if (row.get("target_speaker"), row.get("provider_speaker")) != (target, provider) or row.get("error_code"):
            # 재예약은 새 확인 시도다. 요청의 화자 값이 새 값이고(생략하면 없음), 남아 있던 `speaker_*` 오류도 지운다.
            # 두 키를 모두 보내 이전 값을 확실히 바꾸고, 같은 화자로 다시 예약해도 오류는 지워진다.
            row = await user_db.update_record_upload(
                req.record_id, expected_updated_at=_read_at(row), status="uploading",
                speakers={"target_speaker": target, "provider_speaker": provider})
        created = False
        if await _original_exists(storage, row):
            return row, None, False
    return row, await _issue_upload(storage, row), created


async def _remember_speaker_state(user_db: db.UserDatabase, record: dict, code: str) -> None:
    """화자 오류 사유를 기록의 error_code에 저장한다. 저장에 실패해도 앱에는 원래의 화자 오류를 돌려준다."""
    state = _SPEAKER_STATES.get(code)
    if state is None or record.get("error_code") == state:
        return
    try:
        await user_db.update_record_upload(
            record["id"], expected_updated_at=_read_at(record), status="uploading", speakers={}, error_code=state)
    except db.DatabaseError:  # 그사이 기록이 바뀌어 충돌(55000)한 경우도 포함한다. 최신 상태를 덮지 않는다.
        logger.warning("화자 확인 대기 상태를 저장하지 못했습니다.")


async def mark_uploaded(user_db: db.UserDatabase, storage: StorageClient, record_id, body: UploadedBody | None) -> dict:
    record = await user_db.get_record(record_id)
    status = record.get("status")
    if status == "deleting":
        raise db.NotFoundError()  # 삭제 중인 기록은 목록·상세처럼 없는 기록이다(실제 조회는 RLS가 숨겨 이미 404).
    if status in _PROCESSED:
        # 응답을 받지 못해 다시 호출한 경우: 현재 기록을 그대로 돌려준다. 다만 이미 확정된 화자와 다른 선택을 보냈다면
        # 성공 재시도가 아니므로 거절한다(본문이 없거나 같은 값이면 200).
        if _speaker_choice_conflicts(record, body):
            raise AppError(409, "CONFLICT", CONFLICT_MESSAGE, retryable=False)
        return record
    if status != "uploading":
        raise AppError(409, "CONFLICT", CONFLICT_MESSAGE, retryable=False)

    path, kind = record["object_path"], record["kind"]
    try:
        size = await storage.object_size(path)
    except StorageUnavailable:
        raise _storage_down() from None
    if size is None:
        raise AppError(409, "UPLOAD_MISSING", "업로드가 끝나지 않았어요. 파일을 다시 올려 주세요.", retryable=True)
    if size != record.get("size_bytes"):
        # 원본은 덮어쓸 수 없어 같은 기록으로는 복구되지 않는다(기록을 지우고 새로 예약).
        raise AppError(409, "UPLOAD_SIZE_MISMATCH", "올린 파일의 크기가 예약한 크기와 달라요. 기록을 지우고 다시 올려 주세요.",
                       retryable=False)

    target, provider = record.get("target_speaker"), record.get("provider_speaker")
    if body is not None:
        # 키를 생략하면 예약 때 값을 유지하고, 값을 보내면 덮어쓰고, 명시적 null은 지운다.
        sent = body.model_fields_set
        if "target_speaker" in sent:
            target = body.target_speaker
        if "provider_speaker" in sent:
            provider = body.provider_speaker
        if sent & {"target_speaker", "provider_speaker"}:
            target, provider = clean_speakers(kind, target, provider)

    if kind == "text":
        try:
            data = await storage.read_object(path, max_bytes=size)
        except StorageUnavailable:
            raise _storage_down() from None
        if b"\x00" in data or data.isspace():
            raise _invalid(None, "파일 내용을 읽을 수 없어요. 다른 파일을 올려 주세요.")
        try:
            # 최대 25MiB를 줄 단위로 훑으므로 이벤트 루프를 막지 않게 스레드에서 돌린다. UTF-8로 읽히지 않으면
            # 디코딩 오류가 나며, 파일 복사본을 따로 만들지 않고 같은 순회에서 검사한다.
            analysis = await asyncio.to_thread(analyze_text, data)
        except (UnicodeDecodeError, TooManySpeakers):
            # UTF-8이 아니거나, 서로 다른 화자가 너무 많아(대화 내보내기가 아님) 읽을 수 없는 파일이다.
            raise _invalid(None, "파일 내용을 읽을 수 없어요. 다른 파일을 올려 주세요.") from None
        except NoChatMessages:
            raise _invalid(None, "대화 내용을 찾을 수 없어요. 파일을 확인해 주세요.") from None
        except SpeakerNameTooLong:
            # 화자 줄이 인식됐는데 이름이 한도를 넘는다. 이름을 자르거나 일반 텍스트로 바꾸지 않고 거절한다.
            raise _invalid(None, f"대화에 {SPEAKER_MAX}자가 넘는 화자 이름이 있어요. 파일을 확인해 주세요.") from None
        try:
            if analysis.kakao:
                _check_kakao_speakers(analysis.speakers, target, provider)
            elif target is not None or provider is not None:
                raise _speaker_error("SPEAKER_NOT_ALLOWED", "target_speaker", "이 파일은 화자를 선택할 필요가 없어요.", [])
        except AppError as exc:
            await _remember_speaker_state(user_db, record, exc.code)
            raise
    else:
        # 음성 파일은 앞 몇 바이트만 읽어 예약한 형식과 맞는지 본다. 맞지 않으면 기록을 지우고 새로 예약해야 한다.
        try:
            head = await storage.read_head(path, size=HEAD_BYTES)
        except StorageUnavailable:
            raise _storage_down() from None
        if not signature_matches(record["mime_type"], head):
            raise _invalid(None, "음성 파일 내용이 선택한 형식과 맞지 않아요. 다른 파일을 올려 주세요.")

    # 검증을 통과한 최종 화자 값·`uploaded`·오류 해제를 한 번에 저장한다(중간에 끊겨도 반쪽만 저장되지 않는다).
    # 두 키를 모두 보내므로 저장되는 값은 방금 검증한 값과 같다.
    try:
        return await user_db.update_record_upload(
            record_id, expected_updated_at=_read_at(record), status="uploaded",
            speakers={"target_speaker": target, "provider_speaker": provider})
    except db.ConflictError:
        # 같은 호출이 동시에 와서 다른 요청이 먼저 처리했다면(응답 유실 뒤 재호출 등) 현재 기록을 돌려준다.
        current = await user_db.get_record(record_id)
        if current.get("status") in _PROCESSED:
            # 다른 요청이 다른 화자로 먼저 확정했다면 그 결과를 성공 재시도로 돌려주지 않는다.
            if _speaker_choice_conflicts(current, body):
                raise AppError(409, "CONFLICT", CONFLICT_MESSAGE, retryable=False) from None
            return current
        raise
