"""Offline contract tests; no real user, key, DB or Storage is modified."""

import asyncio
import json
import inspect
import re
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID
from unittest.mock import patch
from urllib.error import URLError

from app.db import (
    AuthenticationError, ChunkInput, ConflictError, Database, DatabaseError,
    DatabaseUnavailableError, NotFoundError, PermissionDeniedError, Settings, UserDatabase, ValidationError,
)
from app.db.transport import Response, UrllibTransport

U1 = "00000000-0000-4000-8000-000000000001"
U2 = "00000000-0000-4000-8000-000000000002"
P = "00000000-0000-4000-8000-000000000003"
S = "00000000-0000-4000-8000-000000000004"
R = "00000000-0000-4000-8000-000000000005"
V = "00000000-0000-4000-8000-000000000006"
T = "00000000-0000-4000-8000-000000000007"
TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
SETTINGS = Settings("https://example.supabase.co", "sb_publishable_fake", "sb_secret_fake")


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.text_status = "draft"
        self.record_status = "ready"

    async def request(self, method, path, *, headers, params, body=None):
        self.calls.append({"method": method, "path": path, "headers": dict(headers),
                           "params": dict(params), "body": body})
        queued = self.responses.get((method, path), [])
        if queued:
            response = queued.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        if path == "/auth/v1/user":
            user = U2 if headers["Authorization"] == "Bearer token-b" else U1
            return Response(200, {"id": user, "role": "authenticated", "is_anonymous": False})
        uid = params.get("user_id", "eq." + U1)[3:]
        row = {"id": params.get("id", "eq." + P)[3:], "user_id": uid,
               "persona_id": P, "record_id": R, "data_revision": 3,
               "status": "active", "updated_at": TIME.isoformat(), "llm_attempt": 1}
        if path.endswith("records"):
            row["status"] = self.record_status
        if path.endswith("record_text_versions"):
            row["status"] = self.text_status
        if path.endswith("expire_chat_stages"):
            return Response(200, 1)
        if path.endswith("search_record_chunks"):
            return Response(200, [])
        if method == "POST" and "/rpc/" not in path:
            return Response(201, body if isinstance(body, list) else [{**row, **body}])
        return Response(200, [row])


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = FakeTransport()
        self.database = Database(SETTINGS, transport=self.transport)
        self.user = await self.database.for_user("token-a")
        self.transport.calls.clear()

    async def test_auth_is_server_verified(self):
        self.transport.responses[("GET", "/auth/v1/user")] = [Response(200, {
            "id": U1, "role": "authenticated", "user_metadata": {"user_id": U2}})]
        user = await self.database.for_user("token-a")
        self.assertEqual(user.user_id, U1)
        self.assertEqual(self.transport.calls[-1]["headers"]["apikey"], SETTINGS.publishable_key)

    async def test_invalid_or_anonymous_auth_rejected(self):
        for data in ({"id": U1, "role": "service_role"}, {"id": U1, "role": "authenticated", "is_anonymous": True},
                     {"id": "bad", "role": "authenticated"}, None):
            with self.subTest(data=data):
                self.transport.responses[("GET", "/auth/v1/user")] = [Response(200, data)]
                with self.assertRaises(AuthenticationError):
                    await self.database.for_user("token-a")
        for token in (None, "", "token\nleak", "sb_secret_fake", "x" * 16385):
            with self.subTest(token_type=type(token)):
                with self.assertRaises(AuthenticationError):
                    await self.database.for_user(token)

    async def test_two_request_contexts_do_not_share_tokens(self):
        a, b = await asyncio.gather(self.database.for_user("token-a"), self.database.for_user("token-b"))
        await asyncio.gather(a.list_personas(), b.list_personas())
        reads = self.transport.calls[-2:]
        self.assertEqual({r["headers"]["Authorization"] for r in reads}, {"Bearer token-a", "Bearer token-b"})
        self.assertEqual({r["params"]["user_id"] for r in reads}, {"eq." + U1, "eq." + U2})

    async def test_modern_server_key_not_used_as_bearer(self):
        await self.user.create_persona("test", persona_id=P)
        request = self.transport.calls[-1]
        self.assertEqual(request["headers"]["apikey"], SETTINGS.secret_key)
        self.assertNotIn("Authorization", request["headers"])
        self.assertEqual(request["body"]["user_id"], U1)

    async def test_legacy_keys_supported(self):
        db = Database(Settings(SETTINGS.url, "fake-anon-jwt", "fake-service-jwt"), transport=self.transport)
        user = await db.for_user("token-a")
        await user.create_persona("test")
        self.assertEqual(self.transport.calls[-1]["headers"]["Authorization"], "Bearer fake-service-jwt")

    async def test_all_reads_include_owner_filter(self):
        calls = [self.user.list_personas(), self.user.get_persona(P), self.user.get_session(S),
                 self.user.list_sessions(persona_id=P), self.user.get_turn(T), self.user.list_turns(S),
                 self.user.get_record(R), self.user.list_records(persona_id=P), self.user.get_text_version(V),
                 self.user.list_text_versions(R), self.user.get_current_style_profile(P),
                 self.user.get_deletion_job(T), self.user.list_deletion_jobs()]
        for call in calls:
            await call
        for request in self.transport.calls:
            self.assertEqual(request["method"], "GET")
            self.assertEqual(request["params"]["user_id"], "eq." + U1)
            self.assertEqual(request["headers"]["Authorization"], "Bearer token-a")

    async def test_missing_or_cross_owner_is_unavailable(self):
        self.transport.responses[("GET", "/rest/v1/personas")] = [Response(200, [])]
        with self.assertRaises(NotFoundError):
            await self.user.get_persona(P)
        self.transport.responses[("GET", "/rest/v1/personas")] = [Response(200, [{"id": P, "user_id": U2}])]
        with self.assertRaises(DatabaseError):
            await self.user.get_persona(P)

    async def test_invisible_parent_prevents_server_write(self):
        self.transport.responses[("GET", "/rest/v1/personas")] = [Response(200, [])]
        with self.assertRaises(NotFoundError):
            await self.user.create_session(P, ai_notice_ack_at=TIME)
        self.assertFalse(any(c["method"] == "POST" for c in self.transport.calls))

    async def test_uuid_and_blank_input_validation(self):
        for value in ("", "not-uuid", 0, [], None):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    await self.user.get_persona(value)
        with self.assertRaises(ValidationError):
            await self.user.create_persona("test", persona_id="")
        with self.assertRaises(ValidationError):
            await self.user.create_persona(" \t")
        row = await self.user.create_persona("test", persona_id=UUID(P))
        self.assertEqual(row["id"], P)

    async def test_pagination_and_turn_cursor(self):
        await self.user.list_records(limit=20, offset=40)
        self.assertEqual(self.transport.calls[-1]["params"]["order"], "created_at.desc,id.desc")
        self.assertEqual(self.transport.calls[-1]["params"]["offset"], "40")
        await self.user.list_turns(S, after_turn_no=123, limit=10)
        self.assertEqual(self.transport.calls[-1]["params"]["turn_no"], "gt.123")
        self.assertEqual(self.transport.calls[-1]["params"]["order"], "turn_no.asc")
        for limit in (0, 101, True):
            with self.assertRaises(ValidationError):
                await self.user.list_personas(limit=limit)

    async def test_reservation_has_owned_immutable_path(self):
        row = await self.user.reserve_record(P, kind="text", original_name="test.txt", mime_type="text/plain",
            size_bytes=5, consent_at=TIME, consent_version="v1", record_id=R)
        self.assertEqual(row["object_path"], U1 + "/" + R + "/original")
        self.assertEqual(row["status"], "uploading")
        self.assertEqual(row["consent_scopes"], ["personalization"])
        with self.assertRaises(ValidationError):
            await self.user.reserve_record(P, kind="text", original_name="x", mime_type="audio/wav",
                size_bytes=5, consent_at=TIME, consent_version="v1")

    async def test_status_compare_and_swap(self):
        self.transport.record_status = "uploaded"
        await self.user.update_record_status(R, expected_status="uploaded", status="processing")
        call = self.transport.calls[-1]
        self.assertEqual(call["params"], {"status": "eq.uploaded", "id": "eq." + R, "user_id": "eq." + U1})
        with self.assertRaises(ConflictError):
            await self.user.update_record_status(R, expected_status="uploading", status="uploaded")
        with self.assertRaises(ValidationError):
            await self.user.update_record_status(R, expected_status="ready", status="deleting")
        self.transport.responses[("PATCH", "/rest/v1/records")] = [Response(200, [])]
        with self.assertRaises(ConflictError):
            await self.user.update_record_status(R, expected_status="uploaded", status="ready")

    async def test_draft_create_and_optimistic_edit(self):
        await self.user.create_text_draft(R, revision=1, content="draft", text_version_id=V)
        self.assertEqual(self.transport.calls[-1]["body"]["persona_id"], P)
        await self.user.edit_text_draft(V, content="edited", expected_updated_at=TIME)
        params = self.transport.calls[-1]["params"]
        self.assertEqual(params["updated_at"], "eq." + TIME.isoformat())
        self.assertEqual(params["status"], "eq.draft")
        self.transport.text_status = "confirmed"
        with self.assertRaises(ConflictError):
            await self.user.edit_text_draft(V, content="bad", expected_updated_at=TIME)

    async def test_naive_times_and_future_cutoff_rejected(self):
        with self.assertRaises(ValidationError):
            await self.user.create_session(P, ai_notice_ack_at=datetime(2026, 1, 1))
        with self.assertRaises(ValidationError):
            await self.user.expire_chat_stages(started_before=datetime(2999, 1, 1, tzinfo=timezone.utc))

    async def test_rpc_parameter_names_match_applied_sql(self):
        await self.user.create_chat_turn(S, T, input_mode="text", question="hello")
        await self.user.start_chat_stage(T, "llm", model_name="test", with_audio=True)
        await self.user.finish_chat_stage(T, "llm", attempt=2, ok=True, text="answer", elapsed_ms=15)
        await self.user.expire_chat_stages(started_before=TIME)
        await self.user.confirm_record_text(V)
        await self.user.publish_style_profile(P, source_revision=3, content={"tone": "test"}, model_name="test", source_ids=[V, V])
        await self.user.search_record_chunks(P, embedding_model="test", query_embedding=[1, 0])
        await self.user.request_record_deletion(R)
        sql = (Path(__file__).resolve().parents[2] / "infra/supabase/redesign.sql").read_text(encoding="utf-8")
        declarations = dict(re.findall(r"create function public\.(\w+)\((.*?)\)\s*returns", sql, re.S))
        calls = [c for c in self.transport.calls if "/rpc/" in c["path"]]
        self.assertEqual(len(calls), 8)
        for call in calls:
            name = call["path"].split("/")[-1]
            expected = set(re.findall(r"\b(p_\w+)\s", declarations[name]))
            self.assertEqual(set(call["body"]), expected, name)
            self.assertEqual(call["body"]["p_user_id"], U1)
        self.assertEqual(calls[2]["body"]["p_attempt"], 2)
        self.assertEqual(calls[2]["body"]["p_ms"], 15)
        self.assertEqual(calls[5]["body"]["p_source_ids"], [V])

    async def test_stage_inputs_validated_before_network(self):
        operations = [self.user.create_chat_turn(S, T, input_mode="text", question=""),
            self.user.create_chat_turn(S, T, input_mode="voice", audio_sha256="bad"),
            self.user.start_chat_stage(T, "llm"), self.user.start_chat_stage(T, "unknown"),
            self.user.finish_chat_stage(T, "llm", attempt=0, ok=True, text="x"),
            self.user.finish_chat_stage(T, "llm", attempt=1, ok=True, text=""),
            self.user.finish_chat_stage(T, "tts", attempt=1, ok=False, error_code="private user text"),
            self.user.finish_chat_stage(T, "tts", attempt=1, ok=True, elapsed_ms=-1)]
        for call in operations:
            with self.assertRaises(ValidationError):
                await call
        self.assertEqual(self.transport.calls, [])

    async def test_voice_digest_and_failure_rpc_preserve_attempt(self):
        await self.user.create_chat_turn(S, T, input_mode="voice", audio_sha256="a" * 64)
        self.assertEqual(self.transport.calls[-1]["body"]["p_question"], None)
        await self.user.finish_chat_stage(T, "tts", attempt=2, ok=False, error_code="tts_timeout")
        self.assertEqual(self.transport.calls[-1]["body"]["p_attempt"], 2)
        self.assertFalse(self.transport.calls[-1]["body"]["p_ok"])

    async def test_personalization_collects_current_sources_and_pages(self):
        row = {"id": V, "user_id": U1, "status": "confirmed"}
        self.transport.responses[("GET", "/rest/v1/record_text_versions")] = [Response(200, [row] * 100), Response(200, [row])]
        result = await self.user.get_personalization_input(P)
        self.assertEqual(result["source_revision"], 3)
        self.assertEqual(len(result["sources"]), 101)
        requests = [c for c in self.transport.calls if c["path"].endswith("record_text_versions")]
        self.assertEqual(requests[0]["params"]["status"], "eq.confirmed")
        self.assertEqual(requests[0]["params"]["records.status"], "eq.ready")
        self.assertEqual(requests[1]["params"]["offset"], "100")

    async def test_empty_profile_is_none(self):
        self.transport.responses[("GET", "/rest/v1/style_profiles")] = [Response(200, [])]
        self.assertIsNone(await self.user.get_current_style_profile(P))

    async def test_invalid_profile_payloads(self):
        for content, ids in (({}, [V]), ({"x": float("nan")}, [V]), ({"x": "a"}, [])):
            with self.assertRaises(ValidationError):
                await self.user.publish_style_profile(P, source_revision=3, content=content, model_name="m", source_ids=ids)

    async def test_chunks_insert_atomic_batch(self):
        self.transport.text_status = "confirmed"
        chunks = [ChunkInput(0, "a", [1, 0]), ChunkInput(1, "b", [0, 1])]
        result = await self.user.save_record_chunks(V, embedding_model="m", chunks=chunks)
        self.assertEqual(len(result), 2)
        writes = [c for c in self.transport.calls if c["method"] == "POST"]
        self.assertEqual(len(writes), 1)
        for row in writes[0]["body"]:
            self.assertEqual(row["user_id"], U1)
            self.assertEqual(row["persona_id"], P)
            self.assertEqual(row["embedding_dimensions"], 2)
            self.assertIsInstance(row["embedding"], str)

    async def test_chunks_reject_draft_duplicate_dimension_mix(self):
        with self.assertRaises(ConflictError):
            await self.user.save_record_chunks(V, embedding_model="m", chunks=[ChunkInput(0, "a", [1, 0])])
        for chunks in ([ChunkInput(0, "a", [1]), ChunkInput(0, "b", [1])],
                       [ChunkInput(0, "a", [1]), ChunkInput(1, "b", [1, 0])], []):
            with self.assertRaises(ValidationError):
                await self.user.save_record_chunks(V, embedding_model="m", chunks=chunks)

    async def test_bad_vectors_and_similarity(self):
        for vector in ([], [0, 0], [float("inf")], [float("nan")], [True], "[1,0]", ["1"], [1e100], [1e-100]):
            with self.assertRaises(ValidationError):
                await self.user.search_record_chunks(P, embedding_model="m", query_embedding=vector)
        with self.assertRaises(ValidationError):
            await self.user.search_record_chunks(P, embedding_model="m", query_embedding=[1], min_similarity=2)

    async def test_deletion_repeat_can_reach_rpc_for_hidden_record(self):
        await self.user.request_record_deletion(R)
        await self.user.request_record_deletion(R)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertTrue(all(c["path"].endswith("request_record_deletion") for c in self.transport.calls))

    async def test_session_delete_always_has_owner_filter(self):
        await self.user.delete_session_rows(S)
        self.assertEqual(self.transport.calls[-1]["params"], {"id": "eq." + S, "user_id": "eq." + U1})

    async def test_sql_error_mapping_and_no_private_messages(self):
        for status, code, expected in ((400, "55000", ConflictError), (409, "23505", ConflictError),
             (409, "23503", ConflictError), (400, "22023", ValidationError), (400, "23514", ValidationError),
             (400, "P0002", NotFoundError), (403, "42501", PermissionDeniedError),
             (401, "PGRST301", AuthenticationError), (503, None, DatabaseUnavailableError),
             (404, "PGRST202", DatabaseError), (400, "xx_secret_fake", DatabaseError)):
            with self.subTest(code=code):
                self.transport.responses[("POST", "/rest/v1/personas")] = [Response(status, {
                    "code": code, "message": "private text and sb_secret_fake", "details": "token-a"})]
                with self.assertRaises(expected) as caught:
                    await self.user.create_persona("x")
                self.assertNotIn("sb_secret", str(caught.exception))
                self.assertNotIn("token-a", repr(caught.exception))
                self.assertNotEqual(caught.exception.code, "xx_secret_fake")

    async def test_id_payload_reuse_is_conflict(self):
        self.transport.responses[("POST", "/rest/v1/rpc/create_chat_turn")] = [Response(400, {"code": "22023"})]
        with self.assertRaises(ConflictError):
            await self.user.create_chat_turn(S, T, input_mode="text", question="x")

    async def test_network_failure_has_no_automatic_write_retry(self):
        self.transport.responses[("POST", "/rest/v1/personas")] = [DatabaseUnavailableError(outcome_unknown=True)]
        with self.assertRaises(DatabaseUnavailableError) as caught:
            await self.user.create_persona("x")
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(len(self.transport.calls), 1)

    async def test_malformed_success_is_not_silently_accepted(self):
        self.transport.responses[("GET", "/rest/v1/personas")] = [Response(200, "not rows")]
        with self.assertRaises(DatabaseError):
            await self.user.list_personas()


class SettingsTests(unittest.TestCase):
    def test_document_lists_every_public_user_function(self):
        document = (Path(__file__).resolve().parents[2] / "docs/architecture/db-python-api.md").read_text(encoding="utf-8")
        documented = set(re.findall(r"^\| ([a-z][a-z_]*) \|", document, re.M))
        actual = {name for name, method in inspect.getmembers(UserDatabase, inspect.iscoroutinefunction)
                  if not name.startswith("_")}
        self.assertEqual(actual, documented)
        for name in actual:
            self.assertNotIn("user_id", inspect.signature(getattr(UserDatabase, name)).parameters)

    def test_environment_and_secret_repr(self):
        env = {"SUPABASE_URL": SETTINGS.url, "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_fake",
               "SUPABASE_SECRET_KEY": "sb_secret_fake"}
        settings = Settings.from_env(env)
        self.assertNotIn("fake", repr(settings))
        self.assertEqual(settings.timeout_seconds, 10)
        with self.assertRaises(ValidationError):
            Settings.from_env({})
        with self.assertRaises(ValidationError):
            Settings.from_env({**env, "SUPABASE_DB_TIMEOUT_SECONDS": "bad-secret-value"})

    def test_insecure_or_credential_url_rejected(self):
        for url in ("http://example.com", "https://user:pass@example.com", "https://example.com?token=x",
                    "https://example.com/#secret", "https://example.com/subpath"):
            with self.assertRaises(ValidationError):
                Settings(url, "a", "b")
        with self.assertRaises(ValidationError):
            Settings(SETTINGS.url, "sb_secret_not_publishable", "sb_secret_fake")


class TransportWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_network_failure_does_not_expose_url_or_key(self):
        transport = UrllibTransport(SETTINGS)
        with patch("app.db.transport.build_opener") as opener:
            opener.return_value.open.side_effect = URLError("private token-a sb_secret_fake")
            with self.assertRaises(DatabaseUnavailableError) as caught:
                await transport.request("POST", "/rest/v1/personas", headers={"apikey": "fake"}, params={}, body={"name": "x"})
            self.assertTrue(caught.exception.outcome_unknown)
            self.assertNotIn("sb_secret", str(caught.exception))
            self.assertEqual(opener.return_value.open.call_count, 1)

    async def test_json_query_headers_and_redirect_blocking(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                received.append((self.path, self.headers.get("Authorization")))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/must-not-receive-key")
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((self.path, json.loads(body), self.headers.get("apikey")))
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b'[{"ok":true}]')

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            transport = UrllibTransport(Settings("http://127.0.0.1:" + str(server.server_port), "a", "b", allow_local_http=True))
            result = await transport.request("GET", "/rest/v1/records", headers={"Authorization": "Bearer fake"},
                params={"user_id": "eq." + U1, "select": "*,records!inner(status)"})
            self.assertEqual(result.data, {"ok": True})
            query = parse_qs(urlsplit(received[-1][0]).query)
            self.assertEqual(query["user_id"], ["eq." + U1])
            self.assertEqual(query["select"], ["*,records!inner(status)"])
            result = await transport.request("POST", "/rest/v1/rpc/test", headers={"apikey": "fake"}, params={}, body={"text": "한글"})
            self.assertEqual(result.status, 201)
            self.assertEqual(received[-1][1], {"text": "한글"})
            result = await transport.request("GET", "/redirect", headers={"apikey": "fake"}, params={})
            self.assertEqual(result.status, 302)
            self.assertFalse(any(item[0] == "/must-not-receive-key" for item in received))
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
