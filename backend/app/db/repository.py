"""User-scoped database functions. Construct via Database.for_user(), per request."""

from __future__ import annotations

import json
import math
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Sequence
from uuid import UUID, uuid4

from .errors import (
    AuthenticationError, ConflictError, DatabaseError, DatabaseUnavailableError,
    NotFoundError, PermissionDeniedError, ValidationError,
)
from .transport import Settings, Transport, UrllibTransport

Row = dict[str, Any]
Identifier = UUID | str
Stage = Literal["stt", "llm", "tts"]


def _id(value: Identifier) -> str:
    try:
        if not isinstance(value, (str, UUID)):
            raise ValueError()
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError() from None


def _text(value: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValidationError()
    if max_length is not None and len(value) > max_length:
        raise ValidationError()
    return value


def _integer(value: int, *, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationError()
    return value


def _time(value: datetime, *, not_future: bool = False) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError()
    if not_future and value > datetime.now(timezone.utc):
        raise ValidationError()
    return value.astimezone(timezone.utc).isoformat()


def _vector(value: Sequence[float]) -> str:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not 1 <= len(value) <= 16000:
        raise ValidationError()
    numbers = []
    for x in value:
        if isinstance(x, bool) or not isinstance(x, (float, int)):
            raise ValidationError()
        try:
            # pgvector stores float32, so check nonzero/finite after that conversion.
            number = struct.unpack("f", struct.pack("f", float(x)))[0]
        except (ValueError, OverflowError, struct.error):
            raise ValidationError() from None
        if not math.isfinite(number):
            raise ValidationError()
        numbers.append(number)
    if not any(x != 0 for x in numbers):
        raise ValidationError()
    return json.dumps(numbers, allow_nan=False, separators=(",", ":"))


def _page(limit: int, offset: int = 0) -> dict[str, str]:
    return {"limit": str(_integer(limit, minimum=1, maximum=100)), "offset": str(_integer(offset))}


def _rows(data: Any) -> list[Row]:
    if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
        raise DatabaseError(code="invalid_response")
    return data


def _one(data: Any, *, empty_error: type[DatabaseError] = NotFoundError) -> Row:
    if isinstance(data, dict):
        return data
    rows = _rows(data)
    if not rows:
        raise empty_error()
    if len(rows) != 1:
        raise DatabaseError(code="invalid_response")
    return rows[0]


@dataclass(frozen=True)
class ChunkInput:
    chunk_no: int
    content: str
    embedding: Sequence[float]


class Database:
    """Reusable transport/config factory. Does not store any current user token."""

    def __init__(self, settings: Settings, *, transport: Transport | None = None):
        self._settings = settings
        self._transport = transport if transport is not None else UrllibTransport(settings)

    async def _request(self, method: str, path: str, *, token: str | None = None,
                       server: bool = False, params: Mapping[str, str] | None = None,
                       body: Any = None) -> Any:
        key = self._settings.secret_key if server else self._settings.publishable_key
        headers = {"apikey": key}
        if not server:
            if token is None:
                raise AuthenticationError()
            headers["Authorization"] = "Bearer " + token
        elif not key.startswith("sb_secret_"):
            # Only legacy service_role keys are JWTs. New secret keys belong in apikey only.
            headers["Authorization"] = "Bearer " + key
        if method in ("POST", "PATCH", "DELETE") and path.startswith("/rest/v1/"):
            headers["Prefer"] = "return=representation"
        response = await self._transport.request(method, path, headers=headers,
                                                  params=params or {}, body=body)
        if 200 <= response.status < 300:
            return response.data
        data = response.data if isinstance(response.data, dict) else {}
        upstream_code = data.get("code")
        code = upstream_code if isinstance(upstream_code, str) and re.fullmatch(r"[A-Z0-9]{5}|PGRST[0-9]{3}", upstream_code) else None
        if response.status == 401 or path == "/auth/v1/user" and response.status in (400, 403, 422):
            raise AuthenticationError(code=code)
        if response.status in (429, 502, 503, 504) or response.status >= 500:
            raise DatabaseUnavailableError(code=code, outcome_unknown=method != "GET")
        if code == "P0002":
            raise NotFoundError(code=code)
        if code in ("55000", "23505", "23503", "40001", "40P01"):
            raise ConflictError(code=code)
        if code in ("23514", "23502") or code is not None and code.startswith("22"):
            raise ValidationError(code=code)
        if response.status == 403 or code == "42501":
            raise PermissionDeniedError(code=code)
        raise DatabaseError(code=code)

    async def for_user(self, access_token: str) -> UserDatabase:
        if (not isinstance(access_token, str) or not access_token or not access_token.isascii() or len(access_token) > 16384
                or any(c.isspace() for c in access_token) or access_token.startswith("sb_")):
            raise AuthenticationError()
        try:
            data = await self._request("GET", "/auth/v1/user", token=access_token)
            # Auth responds with a user object, not client-editable user_metadata claims.
            if not isinstance(data, dict) or data.get("role") != "authenticated" or data.get("is_anonymous") is True:
                raise AuthenticationError()
            user_id = _id(data.get("id"))
        except (ValidationError, PermissionDeniedError):
            raise AuthenticationError() from None
        return UserDatabase(self, user_id, access_token)


class UserDatabase:
    """Request-scoped. Never accept user_id from an HTTP request or retain across requests."""

    def __init__(self, database: Database, user_id: str, token: str):
        self._database, self._user_id, self._token = database, user_id, token

    @property
    def user_id(self) -> str:
        return self._user_id

    async def _select(self, table: str, **filters: str) -> list[Row]:
        data = await self._database._request("GET", "/rest/v1/" + table, token=self._token,
            params={"select": "*", **filters, "user_id": "eq." + self._user_id})
        rows = _rows(data)
        if any(row.get("user_id") != self._user_id for row in rows):
            raise DatabaseError(code="owner_mismatch")
        return rows

    async def _get(self, table: str, identifier: Identifier) -> Row:
        return _one(await self._select(table, id="eq." + _id(identifier), limit="1"))

    async def _insert(self, table: str, payload: Row) -> Row:
        return self._owned_one(await self._database._request("POST", "/rest/v1/" + table, server=True,
            body={**payload, "user_id": self._user_id}))

    def _owned_one(self, data: Any, *, empty_error: type[DatabaseError] = NotFoundError) -> Row:
        row = _one(data, empty_error=empty_error)
        if row.get("user_id") != self._user_id:
            raise DatabaseError(code="owner_mismatch")
        return row

    async def _update(self, table: str, identifier: Identifier, payload: Row, **conditions: str) -> Row:
        return self._owned_one(await self._database._request("PATCH", "/rest/v1/" + table, server=True,
            params={**conditions, "id": "eq." + _id(identifier), "user_id": "eq." + self._user_id},
            body=payload), empty_error=ConflictError)

    async def _rpc(self, name: str, **params: Any) -> Any:
        try:
            return await self._database._request("POST", "/rest/v1/rpc/" + name, server=True,
                body={**params, "p_user_id": self._user_id})
        except ValidationError as error:
            if name == "create_chat_turn" and error.code == "22023":
                # All input is validated locally; this SQL error denotes ID/payload reuse.
                raise ConflictError(code=error.code) from None
            raise

    async def create_persona(self, name: str, *, persona_id: Identifier | None = None) -> Row:
        return await self._insert("personas", {"id": _id(uuid4() if persona_id is None else persona_id), "name": _text(name)})

    async def get_persona(self, persona_id: Identifier) -> Row:
        return await self._get("personas", persona_id)

    async def list_personas(self, *, limit: int = 50, offset: int = 0) -> list[Row]:
        return await self._select("personas", **_page(limit, offset), order="created_at.desc,id.desc")

    async def create_session(self, persona_id: Identifier, *, ai_notice_ack_at: datetime,
                             title: str | None = None, session_id: Identifier | None = None) -> Row:
        payload = {"id": _id(uuid4() if session_id is None else session_id), "persona_id": _id(persona_id),
                   "ai_notice_ack_at": _time(ai_notice_ack_at, not_future=True),
                   "title": None if title is None else _text(title, max_length=200)}
        await self.get_persona(persona_id)
        return await self._insert("chat_sessions", payload)

    async def get_session(self, session_id: Identifier) -> Row:
        return await self._get("chat_sessions", session_id)

    async def list_sessions(self, *, persona_id: Identifier | None = None,
                            limit: int = 50, offset: int = 0) -> list[Row]:
        filters = {} if persona_id is None else {"persona_id": "eq." + _id(persona_id)}
        return await self._select("chat_sessions", **filters, **_page(limit, offset), order="created_at.desc,id.desc")

    async def delete_session_rows(self, session_id: Identifier) -> None:
        await self.get_session(session_id)
        data = await self._database._request("DELETE", "/rest/v1/chat_sessions", server=True,
            params={"id": "eq." + _id(session_id), "user_id": "eq." + self._user_id})
        self._owned_one(data, empty_error=ConflictError)

    async def get_turn(self, turn_id: Identifier) -> Row:
        return await self._get("chat_turns", turn_id)

    async def list_turns(self, session_id: Identifier, *, after_turn_no: int = 0, limit: int = 50) -> list[Row]:
        filters = {"session_id": "eq." + _id(session_id),
                   "turn_no": "gt." + str(_integer(after_turn_no, maximum=2**63 - 1)),
                   **_page(limit), "order": "turn_no.asc"}
        await self.get_session(session_id)
        return await self._select("chat_turns", **filters)

    async def create_chat_turn(self, session_id: Identifier, turn_id: Identifier, *,
                               input_mode: Literal["text", "voice"], question: str | None = None,
                               audio_sha256: str | None = None) -> Row:
        session, turn = _id(session_id), _id(turn_id)
        if input_mode == "text":
            _text(question)
            if audio_sha256 is not None:
                raise ValidationError()
        elif input_mode == "voice":
            if question is not None or not isinstance(audio_sha256, str) or not re.fullmatch("[0-9a-f]{64}", audio_sha256):
                raise ValidationError()
        else:
            raise ValidationError()
        await self.get_session(session)
        return self._owned_one(await self._rpc("create_chat_turn", p_session_id=session, p_turn_id=turn,
            p_input_mode=input_mode, p_question=question, p_audio_sha256=audio_sha256))

    async def start_chat_stage(self, turn_id: Identifier, stage: Stage, *,
                               model_name: str | None = None, with_audio: bool = False) -> Row:
        identifier = _id(turn_id)
        if stage not in ("stt", "llm", "tts") or type(with_audio) is not bool:
            raise ValidationError()
        if stage == "llm":
            _text(model_name)
        elif model_name is not None:
            raise ValidationError()
        await self.get_turn(identifier)
        return self._owned_one(await self._rpc("start_chat_stage", p_turn_id=identifier, p_stage=stage,
            p_model_name=model_name, p_with_audio=with_audio))

    async def finish_chat_stage(self, turn_id: Identifier, stage: Stage, *, attempt: int,
                                ok: bool, text: str | None = None, elapsed_ms: int | None = None,
                                error_code: str | None = None) -> Row:
        identifier = _id(turn_id)
        _integer(attempt, minimum=1)
        if stage not in ("stt", "llm", "tts") or type(ok) is not bool:
            raise ValidationError()
        if elapsed_ms is not None:
            _integer(elapsed_ms)
        if ok and stage in ("stt", "llm"):
            _text(text)
        if error_code is not None and (not isinstance(error_code, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", error_code)):
            raise ValidationError()
        await self.get_turn(identifier)
        return self._owned_one(await self._rpc("finish_chat_stage", p_turn_id=identifier, p_stage=stage,
            p_attempt=attempt, p_ok=ok, p_text=text, p_ms=elapsed_ms, p_error_code=error_code))

    async def expire_chat_stages(self, *, started_before: datetime) -> int:
        result = await self._rpc("expire_chat_stages", p_started_before=_time(started_before, not_future=True))
        if type(result) is not int or result < 0:
            raise DatabaseError(code="invalid_response")
        return result

    async def reserve_record(self, persona_id: Identifier, *, kind: Literal["text", "audio"],
                             original_name: str, mime_type: str, size_bytes: int,
                             consent_at: datetime, consent_version: str,
                             record_id: Identifier | None = None) -> Row:
        pid, rid = _id(persona_id), _id(uuid4() if record_id is None else record_id)
        allowed = {"text": ("text/plain",), "audio": ("audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4", "audio/x-m4a")}
        if not isinstance(kind, str) or kind not in allowed or mime_type not in allowed[kind]:
            raise ValidationError()
        payload = {"id": rid, "persona_id": pid, "kind": kind,
            "original_name": _text(original_name, max_length=255), "mime_type": mime_type,
            "size_bytes": _integer(size_bytes, maximum=2**63 - 1),
            "object_path": self._user_id + "/" + rid + "/original",
            "consent_at": _time(consent_at, not_future=True), "consent_version": _text(consent_version, max_length=100),
            "consent_scopes": ["personalization"], "status": "uploading"}
        await self.get_persona(pid)
        return await self._insert("records", payload)

    async def get_record(self, record_id: Identifier) -> Row:
        return await self._get("records", record_id)

    async def list_records(self, *, persona_id: Identifier | None = None,
                           limit: int = 50, offset: int = 0) -> list[Row]:
        filters = {} if persona_id is None else {"persona_id": "eq." + _id(persona_id)}
        return await self._select("records", **filters, **_page(limit, offset), order="created_at.desc,id.desc")

    async def update_record_status(self, record_id: Identifier, *, expected_status: str,
                                   status: str, error_code: str | None = None) -> Row:
        transitions = {"uploading": ("uploaded", "failed"), "uploaded": ("processing", "ready", "failed"),
                       "processing": ("ready", "failed"), "failed": ("uploading", "processing")}
        if not isinstance(expected_status, str) or not isinstance(status, str) or status not in transitions.get(expected_status, ()):
            raise ValidationError()
        if error_code is not None and (not isinstance(error_code, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", error_code)):
            raise ValidationError()
        record = await self.get_record(record_id)
        if record["status"] != expected_status:
            raise ConflictError()
        return await self._update("records", record_id,
            {"status": status, "error_code": error_code if status == "failed" else None}, status="eq." + expected_status)

    async def create_text_draft(self, record_id: Identifier, *, revision: int, content: str,
                                text_version_id: Identifier | None = None) -> Row:
        payload = {"id": _id(uuid4() if text_version_id is None else text_version_id), "record_id": _id(record_id),
                   "revision": _integer(revision, minimum=1), "content": _text(content), "status": "draft"}
        record = await self.get_record(record_id)
        payload["persona_id"] = record["persona_id"]
        return await self._insert("record_text_versions", payload)

    async def get_text_version(self, text_version_id: Identifier) -> Row:
        return await self._get("record_text_versions", text_version_id)

    async def list_text_versions(self, record_id: Identifier) -> list[Row]:
        await self.get_record(record_id)
        return await self._all("record_text_versions", record_id="eq." + _id(record_id), order="revision.asc")

    async def _all(self, table: str, **filters: str) -> list[Row]:
        result: list[Row] = []
        while True:
            page = await self._select(table, **filters, **_page(100, len(result)))
            result.extend(page)
            if len(page) < 100:
                return result

    async def edit_text_draft(self, text_version_id: Identifier, *, content: str,
                              expected_updated_at: datetime) -> Row:
        text, timestamp = _text(content), _time(expected_updated_at)
        version = await self.get_text_version(text_version_id)
        if version["status"] != "draft":
            raise ConflictError()
        return await self._update("record_text_versions", text_version_id, {"content": text},
                                  status="eq.draft", updated_at="eq." + timestamp)

    async def confirm_record_text(self, text_version_id: Identifier) -> Row:
        await self.get_text_version(text_version_id)
        return self._owned_one(await self._rpc("confirm_record_text", p_text_version_id=_id(text_version_id)))

    async def get_personalization_input(self, persona_id: Identifier) -> Row:
        persona = await self.get_persona(persona_id)
        sources = await self._all("record_text_versions", persona_id="eq." + _id(persona_id),
            status="eq.confirmed", select="*,records!inner(status)", **{"records.status": "eq.ready"}, order="id.asc")
        return {"source_revision": persona["data_revision"], "sources": sources}

    async def get_current_style_profile(self, persona_id: Identifier) -> Row | None:
        await self.get_persona(persona_id)
        rows = await self._select("style_profiles", persona_id="eq." + _id(persona_id), status="eq.ready", limit="1")
        return rows[0] if rows else None

    async def publish_style_profile(self, persona_id: Identifier, *, source_revision: int,
                                    content: dict, model_name: str, source_ids: Sequence[Identifier]) -> Row:
        pid, revision, model = _id(persona_id), _integer(source_revision, maximum=2**63 - 1), _text(model_name)
        if not isinstance(content, dict) or not content or not isinstance(source_ids, Sequence) or isinstance(source_ids, (str, bytes)):
            raise ValidationError()
        try:
            json.dumps(content, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise ValidationError() from None
        ids = list(dict.fromkeys(_id(i) for i in source_ids))
        if not ids:
            raise ValidationError()
        await self.get_persona(pid)
        return self._owned_one(await self._rpc("publish_style_profile", p_persona_id=pid,
            p_source_revision=revision, p_content=content, p_model_name=model, p_source_ids=ids))

    async def save_record_chunks(self, text_version_id: Identifier, *, embedding_model: str,
                                 chunks: Sequence[ChunkInput]) -> list[Row]:
        tid, model = _id(text_version_id), _text(embedding_model)
        if not isinstance(chunks, Sequence) or not 1 <= len(chunks) <= 100:
            raise ValidationError()
        payload, numbers, dimensions = [], set(), set()
        for chunk in chunks:
            if not isinstance(chunk, ChunkInput):
                raise ValidationError()
            number, content, vector = _integer(chunk.chunk_no), _text(chunk.content), _vector(chunk.embedding)
            if number in numbers:
                raise ValidationError()
            numbers.add(number)
            dimensions.add(len(chunk.embedding))
            payload.append({"user_id": self._user_id, "text_version_id": tid, "chunk_no": number,
                "content": content, "embedding": vector, "embedding_model": model,
                "embedding_dimensions": len(chunk.embedding)})
        if len(dimensions) != 1:
            raise ValidationError()
        version = await self.get_text_version(tid)
        record = await self.get_record(version["record_id"])
        if version["status"] != "confirmed" or record["status"] != "ready":
            raise ConflictError()
        for row in payload:
            row["persona_id"] = version["persona_id"]
        rows = _rows(await self._database._request("POST", "/rest/v1/record_chunks", server=True, body=payload))
        if len(rows) != len(payload) or any(row.get("user_id") != self._user_id for row in rows):
            raise DatabaseError(code="invalid_response")
        return rows

    async def search_record_chunks(self, persona_id: Identifier, *, embedding_model: str,
                                   query_embedding: Sequence[float], limit: int = 10,
                                   min_similarity: float = 0.5) -> list[Row]:
        pid, model, vector = _id(persona_id), _text(embedding_model), _vector(query_embedding)
        _integer(limit, minimum=1, maximum=50)
        if (isinstance(min_similarity, bool) or not isinstance(min_similarity, (int, float))
                or not math.isfinite(min_similarity) or not -1 <= min_similarity <= 1):
            raise ValidationError()
        await self.get_persona(pid)
        return _rows(await self._rpc("search_record_chunks", p_persona_id=pid, p_embedding_model=model,
            p_query_embedding=vector, p_limit=limit, p_min_similarity=min_similarity))

    async def request_record_deletion(self, record_id: Identifier) -> Row:
        rid = _id(record_id)
        # Repeat calls must still reach the idempotent RPC after RLS hides the record.
        # The SQL function checks p_user_id itself and only queues deletion.
        return self._owned_one(await self._rpc("request_record_deletion", p_record_id=rid))

    async def get_deletion_job(self, job_id: Identifier) -> Row:
        return await self._get("deletion_jobs", job_id)

    async def list_deletion_jobs(self, *, limit: int = 50, offset: int = 0) -> list[Row]:
        return await self._select("deletion_jobs", **_page(limit, offset), order="created_at.desc,id.desc")
