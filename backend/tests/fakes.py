"""테스트용 가짜 DB·AI·STT·TTS. DB 모듈(app.db.UserDatabase)의 동작 규칙을 메모리에서 흉내 낸다.

턴 규칙은 infra의 SQL 함수(create_chat_turn, start_chat_stage, finish_chat_stage, expire_chat_stages)를 따른다.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from app import db
from app.clients.llm import LLMResult
from app.core.errors import AppError

STAGE_FIELDS = {
    "stt": ("question_status", "stt_attempt", "stt_started_at", "stt_ms"),
    "llm": ("answer_status", "llm_attempt", "llm_started_at", "llm_ms"),
    "tts": ("tts_status", "tts_attempt", "tts_started_at", "tts_ms"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def new_id() -> str:
    return str(UUID(int=uuid4().int))


class FakeUserDB:
    def __init__(self) -> None:
        self.personas: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.turns: dict[str, dict] = {}
        self._turn_seq = 0
        self.list_turns_calls = 0
        self.rows_read = 0  # list_turns가 돌려준 행 수 합계 (얼마나 읽었는지 확인용)
        self.calls: list[tuple] = []  # (함수, 단계) 호출 기록

    # --- 대화 상대·대화방 ---
    async def create_persona(self, name, *, persona_id=None):
        pid = str(persona_id or uuid4())
        if pid in self.personas:
            raise db.ConflictError(code="23505")
        now = _iso(_now())
        row = {"id": pid, "user_id": "u-secret", "name": name, "status": "active", "data_revision": 0,
               "created_at": now, "updated_at": now}
        self.personas[pid] = row
        return dict(row)

    async def get_persona(self, persona_id):
        row = self.personas.get(str(persona_id))
        if row is None:
            raise db.NotFoundError(code="P0002")
        return dict(row)

    async def list_personas(self, *, limit=50, offset=0):
        rows = sorted(self.personas.values(), key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in rows[offset:offset + limit]]

    async def create_session(self, persona_id, *, ai_notice_ack_at, title=None, session_id=None):
        await self.get_persona(persona_id)
        if ai_notice_ack_at > _now():
            raise db.ValidationError()
        sid = str(session_id or uuid4())
        now = _iso(_now())
        row = {"id": sid, "user_id": "u-secret", "persona_id": str(persona_id), "title": title,
               "ai_notice_ack_at": _iso(ai_notice_ack_at), "created_at": now, "updated_at": now}
        self.sessions[sid] = row
        return dict(row)

    # --- 대화 턴 ---
    async def create_chat_turn(self, session_id, turn_id, *, input_mode, question=None, audio_sha256=None):
        sid, tid = str(session_id), str(turn_id)
        if sid not in self.sessions:
            raise db.NotFoundError(code="P0002")
        fingerprint = (input_mode, question if input_mode == "text" else audio_sha256)
        existing = self.turns.get(tid)
        if existing is not None:
            if existing["session_id"] != sid or existing["_fingerprint"] != fingerprint:
                raise db.ConflictError(code="22023")
            return self._public(existing)
        self._turn_seq += 1
        row = {"id": tid, "session_id": sid, "user_id": "u-secret", "turn_no": self._turn_seq,
               "input_mode": input_mode, "question": question if input_mode == "text" else None,
               "question_status": "done" if input_mode == "text" else "pending",
               "answer": None, "answer_status": None, "tts_status": None, "model_name": None,
               "audio_requested": False, "error_message": None, "_fingerprint": fingerprint,
               "stt_attempt": 0, "llm_attempt": 0, "tts_attempt": 0,
               "stt_started_at": None, "llm_started_at": None, "tts_started_at": None,
               "stt_ms": None, "llm_ms": None, "tts_ms": None, "total_ms": None,
               "finish_reason": None, "llm_request_id": None}
        self.turns[tid] = row
        return self._public(row)

    async def start_chat_stage(self, turn_id, stage, *, model_name=None, with_audio=False, request_id=None):
        self.calls.append(("start", stage))
        row = self._turn(turn_id)
        if request_id is not None:
            if stage != "llm":
                raise db.ValidationError()
            if any(r["llm_request_id"] == str(request_id) for r in self.turns.values()):
                raise db.ConflictError(code="23505")  # 같은 요청 ID를 다른 시도가 이미 쓰고 있다(고유 인덱스)
        if stage == "stt":
            ok = (row["input_mode"] == "voice" and row["answer_status"] is None
                  and (row["question_status"] == "failed"
                       or (row["question_status"] == "pending" and row["stt_attempt"] == 0)))
            if not ok:
                raise db.ConflictError(code="55000")
            row.update(question_status="pending", question=None, stt_attempt=row["stt_attempt"] + 1,
                       stt_started_at=_now(), stt_ms=None, error_message=None, total_ms=None)
        elif stage == "llm":
            if not model_name:
                raise db.ValidationError()
            if row["question_status"] != "done" or row["answer_status"] not in (None, "failed"):
                raise db.ConflictError(code="55000")
            row.update(answer_status="pending", answer=None, llm_attempt=row["llm_attempt"] + 1,
                       llm_started_at=_now(), model_name=model_name, llm_ms=None, tts_status=None, tts_ms=None,
                       tts_started_at=None, audio_requested=with_audio, error_message=None, total_ms=None,
                       finish_reason=None, llm_request_id=None if request_id is None else str(request_id))
        elif stage == "tts":
            if row["answer_status"] != "done" or row["tts_status"] not in (None, "failed"):
                raise db.ConflictError(code="55000")
            row.update(tts_status="pending", audio_requested=True, tts_attempt=row["tts_attempt"] + 1,
                       tts_started_at=_now(), tts_ms=None, error_message=None, total_ms=None)
        else:
            raise db.ValidationError()
        return self._public(row)

    async def finish_chat_stage(self, turn_id, stage, *, attempt, ok, text=None, elapsed_ms=None, error_code=None,
                                total_ms=None, finish_reason=None, request_id=None):
        self.calls.append(("finish", stage, ok))
        row = self._turn(turn_id)
        if finish_reason is not None and (stage != "llm" or not ok or finish_reason not in ("stop", "length")):
            raise db.ValidationError()
        if request_id is not None and stage != "llm":
            raise db.ValidationError()
        v2 = total_ms is not None or finish_reason is not None or request_id is not None
        if stage == "llm" and request_id is None and row["llm_request_id"] is not None:
            raise db.ConflictError(code="55000")  # 연결된 시도는 기존 완료 경로도 거부한다
        if v2 and stage == "llm" and request_id is not None and str(request_id) != row["llm_request_id"]:
            raise db.ConflictError(code="55000")
        status_key, attempt_key, _, ms_key = STAGE_FIELDS[stage]
        if row[status_key] != "pending" or row[attempt_key] != attempt:
            raise db.ConflictError(code="55000")
        if ok and stage in ("stt", "llm") and not (text and text.strip()):
            raise db.ValidationError()
        done = {"stt": "done", "llm": "done", "tts": "ready"}[stage]
        row[status_key] = done if ok else "failed"
        row[ms_key] = elapsed_ms
        row["error_message"] = None if ok else (error_code or f"{stage}_failed")
        if ok and stage == "stt":
            row["question"] = text
        if ok and stage == "llm":
            row["answer"] = text
        if v2:
            row["total_ms"] = total_ms
            if stage == "llm":
                row["finish_reason"] = finish_reason
        return self._public(row)

    async def record_turn_total_ms(self, turn_id, stage, *, attempt, total_ms):
        self.calls.append(("total", stage))
        row = self._turn(turn_id)
        status_key, attempt_key, _, _ = STAGE_FIELDS[stage]
        pending = "pending" in (row["question_status"], row["answer_status"], row["tts_status"])
        stale = (
            row[attempt_key] != attempt or row[status_key] not in ("done", "ready", "failed") or pending
            or (stage == "stt" and row["llm_attempt"] > 0) or (stage == "llm" and row["tts_attempt"] > 0)
            or (row["total_ms"] is not None and row["total_ms"] != total_ms)
        )
        if stale:
            raise db.ConflictError(code="55000")
        row["total_ms"] = total_ms
        return self._public(row)

    async def expire_chat_stages(self, *, started_before):
        count = 0
        for row in self.turns.values():
            for stage, (status_key, _, started_key, _) in STAGE_FIELDS.items():
                if row[status_key] == "pending" and row[started_key] is not None and row[started_key] < started_before:
                    row[status_key] = "failed"
                    row["error_message"] = f"{stage}_timeout"
                    count += 1
        return count

    async def get_turn(self, turn_id):
        return self._public(self._turn(turn_id))

    async def list_turns(self, session_id, *, after_turn_no=0, limit=50):
        if str(session_id) not in self.sessions:
            raise db.NotFoundError(code="P0002")
        rows = [r for r in self.turns.values() if r["session_id"] == str(session_id) and r["turn_no"] > after_turn_no]
        result = [self._public(r) for r in sorted(rows, key=lambda r: r["turn_no"])[:limit]]
        self.list_turns_calls += 1
        self.rows_read += len(result)
        return result

    async def get_recent_history(self, session_id, *, limit=20, before_turn_id=None):
        """DB 모듈과 같은 규칙: 끝난 쌍만, 현재 턴보다 앞선 것만, 최근 limit개를 오래된 순으로."""
        if str(session_id) not in self.sessions:
            raise db.NotFoundError(code="P0002")
        rows = [r for r in self.turns.values() if r["session_id"] == str(session_id)
                and r["question_status"] == "done" and r["answer_status"] == "done"]
        if before_turn_id is not None:
            current = self._turn(before_turn_id)
            rows = [r for r in rows if r["turn_no"] < current["turn_no"]]
        rows = sorted(rows, key=lambda r: r["turn_no"], reverse=True)[:limit]
        self.list_turns_calls += 1
        self.rows_read += len(rows)
        return [{"user": r["question"], "assistant": r["answer"]} for r in reversed(rows)]

    def _turn(self, turn_id) -> dict:
        row = self.turns.get(str(turn_id))
        if row is None:
            raise db.NotFoundError(code="P0002")
        return row

    @staticmethod
    def _public(row: dict) -> dict:
        return {k: v for k, v in row.items() if not k.startswith("_")}

    # --- 시험 도우미 ---
    def add_old_turn(self, session_id: str, question: str, answer: str | None, *, skipped_numbers: int = 0) -> None:
        """이미 끝난(answer가 있으면 done, 없으면 failed) 턴을 넣는다. turn_no는 모든 대화방이 공유해서 건너뛴다."""
        self._turn_seq += 1 + skipped_numbers
        tid = new_id()
        self.turns[tid] = {
            "id": tid, "session_id": session_id, "user_id": "u-secret", "turn_no": self._turn_seq,
            "input_mode": "text", "question": question, "question_status": "done",
            "answer": answer, "answer_status": "done" if answer else "failed", "tts_status": None,
            "model_name": "test-model", "audio_requested": False, "_fingerprint": ("text", question),
            "stt_attempt": 0, "llm_attempt": 1, "tts_attempt": 0,
            "stt_started_at": None, "llm_started_at": None, "tts_started_at": None,
            "stt_ms": None, "llm_ms": 10, "tts_ms": None, "total_ms": None,
            "finish_reason": "stop" if answer else None, "llm_request_id": None,
            "error_message": None if answer else "llm_failed",
        }

    def add_session(self) -> str:
        persona_id = str(uuid4())
        self.personas[persona_id] = {"id": persona_id, "name": "엄마", "status": "active"}
        session_id = str(uuid4())
        self.sessions[session_id] = {"id": session_id, "persona_id": persona_id}
        return session_id


class FakeLLM:
    """차례대로 준비한 결과(LLMResult 또는 예외)를 돌려준다."""

    name = "test-model"

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes) or [LLMResult("안녕, 반갑다.", "stop")]
        self.calls: list[dict] = []

    async def generate(self, message, history, persona_id, *, request_id=None):
        self.calls.append({"message": message, "history": list(history), "persona_id": persona_id,
                           "request_id": request_id})
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSTT:
    """차례대로 준비한 전사문(str) 또는 예외를 돌려준다."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes) or ["오늘 날씨 어때?"]
        self.calls: list[dict] = []

    async def transcribe(self, audio, filename, content_type):
        self.calls.append({"size": len(audio), "filename": filename, "content_type": content_type})
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeTTS:
    """차례대로 준비한 (바이트, 형식) 또는 예외를 돌려준다."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes) or [(b"RIFFfake", "wav")]
        self.calls: list[str] = []

    async def synthesize(self, text):
        self.calls.append(text)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def busy_error() -> AppError:
    return AppError(429, "AI_BUSY", "지금 다른 요청을 처리하고 있어요. 잠시 후 다시 시도해 주세요.",
                    retryable=True, stage="llm", db_error_code="llm_busy")
