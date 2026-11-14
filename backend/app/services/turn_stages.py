"""대화 턴의 단계(STT·LLM·TTS) 처리: 텍스트 대화와 음성 대화가 함께 쓴다.

규칙의 원본은 backend-db 명세 '호출 규칙', backend-ai 명세 '타임아웃과 수동 재시도'다.
- `start_chat_stage`로 실행권을 얻은 요청만 외부(STT·AI·TTS)를 부른다. 자동 재시도는 TTS에만 있다(첫 시도 + 재시도 2번, 앱 대기 3분 안).
- AI 요청 ID는 호출 전에 만들어 DB(`start_chat_stage`)에 먼저 기록하고, 같은 ID로 AI를 부르고, 결과를 저장할 때 확인한다.
- 외부 호출이 실패하면 그 시도 번호로 실패를 기록한다. 질문·답변·예외 원문은 기록하지 않는다.
- 실행 중(pending)인 단계는 제한 시간을 넘긴 것만 실패로 닫고, 아니면 409로 거절한다. TTS는 앱 대기 시간(180초)까지 닫지 않는다.
"""

import asyncio
import base64
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app import db
from app.clients.llm import MAX_HISTORY_PAIRS, MAX_TEXT_CHARS, LLMClient
from app.clients.stt import STTClient
from app.clients.tts import TTSClient
from app.core.config import get_settings
from app.core.errors import CONFLICT_MESSAGE, MESSAGE_TOO_LONG_MESSAGE, AppError, from_database_error
from app.db import UserDatabase
from app.schemas.chat import ChatTurn

logger = logging.getLogger(__name__)

# 시간 제한 수치는 아직 합의 전이다. LLM_TIMEOUT_SECONDS가 없을 때(mock 모드)만 쓰는 임시 값.
FALLBACK_STALE_SECONDS = 30.0

# TTS 자동 재시도: 팀 회의 결정(앱은 응답을 최대 3분 기다리고, 그 안에 첫 시도 + 재시도 2번). 여유 시간은 임시 값이다.
APP_WAIT_SECONDS = 180.0
RESPONSE_MARGIN_SECONDS = 10.0
TTS_MAX_RETRIES = 2
FIRST_TRY_MIN_SECONDS = 10.0  # 앞 단계가 오래 걸려 남은 시간이 없어도 첫 시도가 기다리는 최소 시간
TTS_STALE_SECONDS = APP_WAIT_SECONDS  # 진행 중인 TTS는 앱이 기다리는 시간 안에는 멈춘 것으로 보지 않는다

STT_FAILED_MESSAGE = "음성을 글자로 바꾸지 못했어요. 다시 시도해 주세요."
EMPTY_TRANSCRIPT_MESSAGE = "음성을 인식하지 못했어요. 다시 말씀해 주세요."
TTS_FAILED_MESSAGE = "음성 합성에 실패했어요. 답변 글은 그대로 볼 수 있으니, 잠시 후 다시 시도해 주세요."


def elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


@contextmanager
def turn_errors(turn_id: UUID):
    """DB 예외와 앱 오류에 turn_id를 붙여 앱이 재시도·재조회에 쓸 수 있게 한다."""
    try:
        yield
    except db.DatabaseError as exc:
        raise from_database_error(exc, turn_id=turn_id) from None
    except AppError as exc:
        exc.turn_id = turn_id
        raise


# --- 실행 중인 단계 판정 ---


def is_running(turn: dict) -> bool:
    """어느 단계든 지금 실행 중인지. 질문이 pending이어도 STT를 시작한 적이 없으면 실행 중이 아니다."""
    stt_running = turn["question_status"] == "pending" and (turn.get("stt_attempt") or 0) > 0
    return stt_running or turn["answer_status"] == "pending" or turn.get("tts_status") == "pending"


async def refresh_if_stale(user_db: UserDatabase, turn: dict) -> dict:
    """실행 중인 단계가 제한 시간을 넘겼으면 실패로 닫고 턴을 다시 읽는다. 제한 안이면 그대로 둔다.

    STT·LLM은 LLM_TIMEOUT_SECONDS(없으면 30초), TTS는 앱 대기 시간(TTS_STALE_SECONDS)을 기준으로 한다.
    TTS는 자동 재시도로 더 오래 걸릴 수 있기 때문이다. 이 호출은 어느 단계의 요청에서든 사용자의 모든 턴을
    대상으로 하므로 TTS 기준을 항상 함께 넘겨, 진행 중인 TTS가 다른 요청에 의해 닫히지 않게 한다.
    """
    if not is_running(turn):
        return turn
    seconds = get_settings().llm_timeout_seconds or FALLBACK_STALE_SECONDS
    now = datetime.now(timezone.utc)
    await user_db.expire_chat_stages(
        started_before=now - timedelta(seconds=seconds),
        tts_started_before=now - timedelta(seconds=TTS_STALE_SECONDS),
    )
    return await user_db.get_turn(turn["id"])


def conflict() -> AppError:
    return AppError(409, "CONFLICT", CONFLICT_MESSAGE, retryable=False)


async def record_failure(
    user_db: UserDatabase, turn_id: UUID | str, stage: str, attempt: int, error_code: str, ms: int,
    *, request_id: UUID | str | None = None,
) -> None:
    try:
        await user_db.finish_chat_stage(turn_id, stage, attempt=attempt, ok=False, elapsed_ms=ms,
                                       error_code=error_code, request_id=request_id)
    except db.DatabaseError as exc:
        # 이미 새 시도가 이어받았거나 DB가 불안정하다. 원래 오류를 앱에 돌려주는 일이 우선이다.
        logger.warning("실패 기록 저장 실패 stage=%s type=%s turn_id=%s", stage, type(exc).__name__, turn_id)


async def record_total_ms(user_db: UserDatabase, turn_id: UUID | str, stage: str, attempt: int, total_ms: int) -> None:
    """이번 요청이 처리한 전체 시간을 마지막으로 끝난 단계에 기록한다. 실패해도 응답에는 영향을 주지 않는다."""
    if attempt < 1:
        return
    try:
        await user_db.record_turn_total_ms(turn_id, stage, attempt=attempt, total_ms=total_ms)
    except db.DatabaseError as exc:
        logger.warning("전체 시간 기록 실패 stage=%s type=%s turn_id=%s", stage, type(exc).__name__, turn_id)


# --- STT 단계 ---


async def run_stt_stage(
    user_db: UserDatabase, stt: STTClient, turn_id: UUID | str, audio: bytes, filename: str, content_type: str | None
) -> tuple[str, int]:
    """STT를 한 번 호출해 전사문을 저장한다. (전사문, 걸린 ms)를 돌려준다."""
    claimed = await user_db.start_chat_stage(turn_id, "stt")
    attempt = claimed["stt_attempt"]
    started = time.perf_counter()
    try:
        text = await stt.transcribe(audio, filename, content_type)
    except TimeoutError:
        await record_failure(user_db, turn_id, "stt", attempt, "stt_timeout", elapsed_ms(started))
        raise AppError(502, "STT_FAILED", STT_FAILED_MESSAGE, retryable=True, stage="stt") from None
    except Exception:
        await record_failure(user_db, turn_id, "stt", attempt, "stt_failed", elapsed_ms(started))
        raise AppError(502, "STT_FAILED", STT_FAILED_MESSAGE, retryable=True, stage="stt") from None
    ms = elapsed_ms(started)
    if not text or not text.strip():
        # 무음 녹음 등: AI를 부르기 전에 막는다.
        await record_failure(user_db, turn_id, "stt", attempt, "stt_empty", ms)
        raise AppError(422, "EMPTY_TRANSCRIPT", EMPTY_TRANSCRIPT_MESSAGE, retryable=True, stage="stt")
    saved = await user_db.finish_chat_stage(turn_id, "stt", attempt=attempt, ok=True, text=text, elapsed_ms=ms)
    return saved["question"], ms


# --- LLM 단계 ---


@dataclass(frozen=True)
class LlmStageResult:
    text: str
    finish_reason: str
    model_name: str
    llm_ms: int
    attempt: int


async def run_llm_stage(
    user_db: UserDatabase, llm: LLMClient, turn: dict, question: str, *, with_audio: bool = False
) -> LlmStageResult:
    """AI를 한 번 호출해 답변을 저장한다. 이력은 이 대화방의 최근 완결 20쌍이다.

    실행권을 잡기 전에 끝낼 수 있는 일(질문 길이 검사, 이력 조회)은 모두 먼저 한다. 여기서 실패하면 잡아 둔
    실행권이 없어, 앱이 같은 turn_id로 바로 다시 보낼 수 있고 AI는 한 번도 불리지 않는다.
    """
    if len(question) > MAX_TEXT_CHARS:
        # 음성 전사문처럼 앱이 길이를 막지 못한 질문. 자르지 않고 거절한다. 같은 음성으로는 다시 보내도 같은 결과다.
        raise AppError(422, "MESSAGE_TOO_LONG", MESSAGE_TOO_LONG_MESSAGE, retryable=False, stage="llm")
    history = await recent_history(user_db, turn["session_id"], current_turn_id=turn["id"])

    request_id = uuid4()  # 이 시도의 AI 요청 ID. DB에 먼저 기록하고 같은 값으로 AI를 부른다.
    claimed = await user_db.start_chat_stage(
        turn["id"], "llm", model_name=llm.name, with_audio=with_audio, request_id=request_id
    )
    attempt = claimed["llm_attempt"]

    started = time.perf_counter()
    try:
        result = await llm.generate(question, history, None, request_id=str(request_id))
    except AppError as exc:
        await record_failure(user_db, turn["id"], "llm", attempt, exc.db_error_code or "llm_failed",
                             elapsed_ms(started), request_id=request_id)
        raise
    except Exception:
        await record_failure(user_db, turn["id"], "llm", attempt, "llm_failed", elapsed_ms(started),
                             request_id=request_id)
        raise
    ms = elapsed_ms(started)

    saved = await user_db.finish_chat_stage(
        turn["id"], "llm", attempt=attempt, ok=True, text=result.text, elapsed_ms=ms,
        finish_reason=result.finish_reason, request_id=request_id,
    )
    return LlmStageResult(saved["answer"], result.finish_reason, saved["model_name"] or llm.name, ms, attempt)


async def recent_history(user_db: UserDatabase, session_id: UUID | str, *, current_turn_id: UUID | str) -> list[ChatTurn]:
    """대화방의 최근 완결 질문·답변 20쌍(현재 턴 제외)을 오래된 순 role/content 목록으로 돌려준다.

    계약(#29): 최근 완결 쌍만, 20쌍까지. 실패·진행 중인 턴은 쌍이 아니다. 최근 N개 조회는 DB 모듈이 한다.
    """
    pairs = await user_db.get_recent_history(session_id, limit=MAX_HISTORY_PAIRS, before_turn_id=current_turn_id)
    history: list[ChatTurn] = []
    for pair in pairs:
        history.append(ChatTurn(role="user", content=pair["user"]))
        history.append(ChatTurn(role="assistant", content=pair["assistant"]))
    return history


# --- TTS 단계 ---


@dataclass(frozen=True)
class TtsStageResult:
    ok: bool
    audio_base64: str | None
    audio_format: str | None
    tts_ms: int
    attempt: int = 0  # DB 시도 번호. DB를 거치지 않은 재합성이면 0


async def synthesize(tts: TTSClient, text: str, *, request_started: float | None = None) -> TtsStageResult:
    """TTS를 호출해 base64 음성으로 바꾼다. 실패하면 ok=False로 돌려준다. DB 상태는 건드리지 않는다.

    첫 시도는 항상 한다. 실패하면 최대 TTS_MAX_RETRIES번 다시 부르며, 앱이 기다리는 시간(요청 시작 기준 APP_WAIT_SECONDS)
    안에서만 다시 시도하고 남은 시간이 직전 시도에 걸린 시간보다 짧으면 다시 부르지 않는다. 재시도는 남은 시간을 넘기지
    않고, 이미 시간이 지났어도 첫 시도는 FIRST_TRY_MIN_SECONDS까지 기다린다.
    """
    started = time.perf_counter()
    deadline = (started if request_started is None else request_started) + APP_WAIT_SECONDS - RESPONSE_MARGIN_SECONDS
    last_took = 0.0
    for retry in range(TTS_MAX_RETRIES + 1):
        remaining = deadline - time.perf_counter()
        if retry and (remaining <= 0 or remaining < last_took):
            break
        tried = time.perf_counter()
        try:
            async with asyncio.timeout(max(remaining, FIRST_TRY_MIN_SECONDS) if not retry else remaining):
                audio, fmt = await tts.synthesize(text)
        except Exception:  # 시간 초과 포함. 예외 원문은 남기지 않는다.
            last_took = time.perf_counter() - tried
            logger.warning("TTS 시도 실패 retry=%d", retry)
            continue
        return TtsStageResult(True, base64.b64encode(audio).decode("ascii"), fmt, elapsed_ms(started))
    return TtsStageResult(False, None, None, elapsed_ms(started))


async def run_tts_stage(
    user_db: UserDatabase, tts: TTSClient, turn_id: UUID | str, text: str, *, request_started: float | None = None
) -> TtsStageResult:
    """실행권을 얻은 뒤 TTS를 부른다(실패 시 시간 안에서 자동 재시도). 실패해도 예외 대신 ok=False로 돌려준다(답변 텍스트는 그대로 남는다)."""
    claimed = await user_db.start_chat_stage(turn_id, "tts")
    attempt = claimed["tts_attempt"]
    result = await synthesize(tts, text, request_started=request_started)
    if not result.ok:
        await record_failure(user_db, turn_id, "tts", attempt, "tts_failed", result.tts_ms)
    else:
        await user_db.finish_chat_stage(turn_id, "tts", attempt=attempt, ok=True, elapsed_ms=result.tts_ms)
    return TtsStageResult(result.ok, result.audio_base64, result.audio_format, result.tts_ms, attempt)


def tts_failed_error() -> AppError:
    return AppError(502, "TTS_FAILED", TTS_FAILED_MESSAGE, retryable=True, stage="tts")
