"""Explicit opt-in synthetic two-user integration test. Creates and removes its own fixtures.

Never run on import, never print credentials/personal rows, never call AI or upload files.
Only the Replica development project is allowed. Requires REPLICA_DB_MUTATING_TEST=1.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import secrets
import unittest
from uuid import uuid4

from app.db import (AuthenticationError, ChunkInput, ConflictError, Database, NotFoundError,
                    PermissionDeniedError, Settings, ValidationError)


@unittest.skipUnless(os.environ.get("REPLICA_DB_MUTATING_TEST") == "1", "synthetic live writes disabled")
class FullLiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.run_marker = "replica-integration-" + uuid4().hex
        self.database = Database(Settings.from_env_file(Path(__file__).resolve().parents[1] / ".env"))
        if self.database._settings.url.rstrip("/") != "https://hfmrhmuhcbmskhjagpzg.supabase.co":
            self.fail("Only the Replica development project is allowed")
        self.user_ids = []
        self.addAsyncCleanup(self._cleanup)
        self.users = []
        for index in range(2):
            email = self.run_marker + "-" + str(index) + "@example.com"
            password = secrets.token_urlsafe(48)
            # Admin creation confirms email without sending an email or weakening Auth settings.
            created = await self.database._request("POST", "/auth/v1/admin/users", server=True,
                body={"email": email, "password": password, "email_confirm": True,
                      "app_metadata": {"replica_integration_run": self.run_marker}})
            self.user_ids.append(created["id"])
            auth = await self.database._request("POST", "/auth/v1/token", token="synthetic-login",
                params={"grant_type": "password"}, body={"email": email, "password": password})
            self.users.append(await self.database.for_user(auth["access_token"]))
        self.assertNotEqual(self.users[0].user_id, self.users[1].user_id)

    async def _cleanup(self):
        errors = []
        for uid in self.user_ids:
            try:
                owned = {"user_id": "eq." + uid}
                account = await self.database._request("GET", "/auth/v1/admin/users/" + uid, server=True)
                if account.get("app_metadata", {}).get("replica_integration_run") != self.run_marker:
                    raise AssertionError("Refuse to clean an account outside this test run")
                records = await self.database._request("GET", "/rest/v1/records", server=True,
                    params={**owned, "select": "id"})
                for row in records:
                    job = await self.database._request("POST", "/rest/v1/rpc/request_record_deletion", server=True,
                        body={"p_user_id": uid, "p_record_id": row["id"]})
                    if isinstance(job, list):
                        job = job[0]
                    # This fixture never uploads bytes or calls Storage: no file exists to delete.
                    # A real worker must use Storage APIs, never simulate this acknowledgement.
                    await self.database._request("PATCH", "/rest/v1/deletion_jobs", server=True,
                        params={**owned, "id": "eq." + job["id"]},
                        body={"status": "running", "remaining_steps": ["database"]})
                    await self.database._request("DELETE", "/rest/v1/records", server=True,
                        params={**owned, "id": "eq." + row["id"]})
                for table in ("style_profiles", "chat_sessions", "deletion_jobs", "personas"):
                    await self.database._request("DELETE", "/rest/v1/" + table, server=True, params=owned)
                await self.database._request("DELETE", "/auth/v1/admin/users/" + uid, server=True,
                    body={"should_soft_delete": False})
            except Exception:
                # Fixture IDs/keys/exception details are intentionally not emitted.
                errors.append("synthetic fixture cleanup incomplete")
        if errors:
            self.fail("Cleanup incomplete; inspect test-run-tagged fixtures before any retry")

    async def test_all_functions_and_cross_user_access(self):
        a, b = self.users
        persona = await a.create_persona("합성 통합 시험 대상")
        pid = persona["id"]
        self.assertEqual((await a.get_persona(pid))["id"], pid)
        self.assertTrue(any(row["id"] == pid for row in await a.list_personas()))
        self.assertEqual(await b.list_personas(), [])
        with self.assertRaises(NotFoundError):
            await b.get_persona(pid)
        session = await a.create_session(pid, ai_notice_ack_at=datetime.now(timezone.utc), title="합성 대화")
        sid = session["id"]
        self.assertEqual((await a.get_session(sid))["id"], sid)
        self.assertEqual(len(await a.list_sessions(persona_id=pid)), 1)
        with self.assertRaises(NotFoundError):
            await b.create_session(pid, ai_notice_ack_at=datetime.now(timezone.utc))

        # Text flow, deduplication and persisted finish metadata.
        tid, request_id = uuid4(), uuid4()
        turn = await a.create_chat_turn(sid, tid, input_mode="text", question="합성 질문")
        self.assertEqual((await a.create_chat_turn(sid, tid, input_mode="text", question="합성 질문"))["id"], str(tid))
        with self.assertRaises(ConflictError):
            await a.create_chat_turn(sid, tid, input_mode="text", question="다른 합성 질문")
        claimed = await a.start_chat_stage(tid, "llm", model_name="synthetic-no-ai-call", request_id=request_id)
        with self.assertRaises(ConflictError):
            await a.start_chat_stage(tid, "llm", model_name="synthetic-no-ai-call", request_id=uuid4())
        done = await a.finish_chat_stage(tid, "llm", attempt=claimed["llm_attempt"], ok=True,
            text="합성 답변", elapsed_ms=10, total_ms=20, finish_reason="length", request_id=request_id)
        self.assertEqual((done["finish_reason"], done["total_ms"]), ("length", 20))
        await a.record_turn_total_ms(tid, "llm", attempt=1, total_ms=20)
        with self.assertRaises(ConflictError):
            await a.record_turn_total_ms(tid, "llm", attempt=1, total_ms=30)
        self.assertEqual((await a.get_turn(tid))["answer"], "합성 답변")
        self.assertEqual(len(await a.list_turns(sid)), 1)
        for action in (lambda: b.get_turn(tid), lambda: b.list_turns(sid),
                       lambda: b.start_chat_stage(tid, "tts"), lambda: b.delete_session_rows(sid),
                       lambda: b.record_turn_total_ms(tid, "llm", attempt=1, total_ms=20)):
            with self.assertRaises(NotFoundError):
                await action()
        # Raw RLS and SQL grants, independent of the Python owner pre-check.
        hidden = await self.database._request("GET", "/rest/v1/chat_turns", token=b._token,
            params={"id": "eq." + str(tid), "select": "id"})
        self.assertEqual(hidden, [])
        with self.assertRaises(PermissionDeniedError):
            await self.database._request("POST", "/rest/v1/personas", token=a._token,
                body={"user_id": a.user_id, "name": "forbidden synthetic insert"})
        with self.assertRaises(PermissionDeniedError):
            await self.database._request("POST", "/rest/v1/rpc/record_turn_total_ms", token=a._token,
                body={"p_user_id": a.user_id, "p_turn_id": str(tid), "p_stage": "llm", "p_attempt": 1, "p_total_ms": 20})

        # Voice metadata flow only: synthetic STT/LLM/TTS results, no provider call.
        voice_id = uuid4()
        await a.create_chat_turn(sid, voice_id, input_mode="voice", audio_sha256="a" * 64)
        stt = await a.start_chat_stage(voice_id, "stt")
        await a.finish_chat_stage(voice_id, "stt", attempt=stt["stt_attempt"], ok=True, text="합성 전사", elapsed_ms=2)
        voice_request = uuid4()
        llm = await a.start_chat_stage(voice_id, "llm", model_name="synthetic-no-ai-call", with_audio=True, request_id=voice_request)
        await a.finish_chat_stage(voice_id, "llm", attempt=llm["llm_attempt"], ok=True, text="합성 음성 답변",
            elapsed_ms=3, finish_reason="stop", request_id=voice_request)
        tts = await a.start_chat_stage(voice_id, "tts")
        await a.finish_chat_stage(voice_id, "tts", attempt=tts["tts_attempt"], ok=False,
            elapsed_ms=1, error_code="tts_provider_error", total_ms=9)
        retry = await a.start_chat_stage(voice_id, "tts")
        with self.assertRaises(ConflictError):
            await a.finish_chat_stage(voice_id, "tts", attempt=tts["tts_attempt"], ok=True)
        await a.finish_chat_stage(voice_id, "tts", attempt=retry["tts_attempt"], ok=True, elapsed_ms=1, total_ms=10)
        self.assertEqual((await a.get_turn(voice_id))["answer"], "합성 음성 답변")
        self.assertEqual(await a.get_recent_history(sid, limit=1, before_turn_id=voice_id),
                         [{"user": "합성 질문", "assistant": "합성 답변"}])
        self.assertEqual((await a.get_recent_history(sid, limit=1))[0]["user"], "합성 전사")

        race_id = uuid4()
        await a.create_chat_turn(sid, race_id, input_mode="text", question="합성 동시 실행권 시험")
        results = await asyncio.gather(*[
            a.start_chat_stage(race_id, "llm", model_name="synthetic-no-ai-call", request_id=uuid4())
            for _ in range(2)], return_exceptions=True)
        wins = [result for result in results if isinstance(result, dict)]
        self.assertEqual(len(wins), 1)
        self.assertEqual(sum(isinstance(result, ConflictError) for result in results), 1)
        await a.finish_chat_stage(race_id, "llm", attempt=wins[0]["llm_attempt"], ok=True,
            text="합성 단일 실행 결과", finish_reason="stop", request_id=wins[0]["llm_request_id"])

        pending_id = uuid4()
        await a.create_chat_turn(sid, pending_id, input_mode="text", question="합성 만료 시험")
        pending = await a.start_chat_stage(pending_id, "llm", model_name="synthetic-no-ai-call")
        started = datetime.fromisoformat(pending["llm_started_at"].replace("Z", "+00:00"))
        await asyncio.sleep(0.02)
        self.assertEqual(await a.expire_chat_stages(started_before=started + timedelta(microseconds=1)), 1)
        with self.assertRaises(ConflictError):
            await a.finish_chat_stage(pending_id, "llm", attempt=pending["llm_attempt"], ok=True, text="late synthetic")

        # Source metadata, optimistic edits, confirmation, profile provenance and exact vector search.
        record = await a.reserve_record(pid, kind="text", original_name="synthetic.txt", mime_type="text/plain",
            size_bytes=10, consent_at=datetime.now(timezone.utc), consent_version="integration-test-only")
        rid = record["id"]
        self.assertEqual((await a.get_record(rid))["id"], rid)
        self.assertEqual(len(await a.list_records(persona_id=pid)), 1)
        await a.update_record_status(rid, expected_status="uploading", status="uploaded")
        await a.update_record_status(rid, expected_status="uploaded", status="ready")
        draft = await a.create_text_draft(rid, revision=1, content="합성 개인화 자료")
        version_id = draft["id"]
        timestamp = datetime.fromisoformat(draft["updated_at"].replace("Z", "+00:00"))
        await a.edit_text_draft(version_id, content="수정한 합성 개인화 자료", expected_updated_at=timestamp)
        with self.assertRaises(ConflictError):
            await a.edit_text_draft(version_id, content="늦은 합성 수정", expected_updated_at=timestamp)
        confirmed = await a.confirm_record_text(version_id)
        self.assertEqual((await a.get_text_version(version_id))["status"], "confirmed")
        self.assertEqual(len(await a.list_text_versions(rid)), 1)
        snapshot = await a.get_personalization_input(pid)
        self.assertEqual(len(snapshot["sources"]), 1)
        self.assertIsNone(await a.get_current_style_profile(pid))
        await a.publish_style_profile(pid, source_revision=snapshot["source_revision"],
            content={"tone": "합성 테스트"}, model_name="synthetic-profile", source_ids=[version_id])
        self.assertIsNotNone(await a.get_current_style_profile(pid))
        await a.save_record_chunks(version_id, embedding_model="synthetic-2d", chunks=[ChunkInput(0, confirmed["content"], [1, 0])])
        self.assertEqual(len(await a.search_record_chunks(pid, embedding_model="synthetic-2d", query_embedding=[1, 0])), 1)
        self.assertEqual(await a.search_record_chunks(pid, embedding_model="different-model", query_embedding=[1, 0]), [])
        with self.assertRaises(NotFoundError):
            await b.search_record_chunks(pid, embedding_model="synthetic-2d", query_embedding=[1, 0])
        with self.assertRaises(NotFoundError):
            await b.edit_text_draft(version_id, content="unauthorized", expected_updated_at=timestamp)
        with self.assertRaises(NotFoundError):
            await b.request_record_deletion(rid)
        job = await a.request_record_deletion(rid)
        self.assertEqual((await a.request_record_deletion(rid))["id"], job["id"])
        self.assertEqual((await a.get_deletion_job(job["id"]))["target_id"], rid)
        self.assertEqual(len(await a.list_deletion_jobs()), 1)
        self.assertIsNone(await a.get_current_style_profile(pid))
        self.assertEqual(await a.search_record_chunks(pid, embedding_model="synthetic-2d", query_embedding=[1, 0]), [])
        with self.assertRaises(NotFoundError):
            await a.get_record(rid)
        await a.delete_session_rows(sid)
        self.assertEqual(await a.list_sessions(), [])
        with self.assertRaises(AuthenticationError):
            await self.database.for_user("synthetic-invalid-token")
