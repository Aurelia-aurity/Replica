"""Offline integration-boundary regression tests. No real credentials required."""
import json
import ast
import inspect
import re
import tempfile
import unittest
from pathlib import Path

from app.db import (AuthenticationError, ConflictError, Database, DatabaseError, UserDatabase,
                    DatabaseUnavailableError, NotFoundError, PermissionDeniedError,
                    Settings, ValidationError, to_http_error, to_public_row)
from app.db.transport import Response
from test_db import FakeTransport, SETTINGS, U1, S, T, P


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = FakeTransport()
        self.user = await Database(SETTINGS, transport=self.transport).for_user("token-a")
        self.transport.calls.clear()

    async def test_latest_history_queries_descending_and_returns_complete_pairs_ascending(self):
        self.transport.responses[("GET", "/rest/v1/chat_turns")] = [
            Response(200, [{"id": T, "user_id": U1, "session_id": S, "turn_no": 9}]),
            Response(200, [{"user_id": U1, "question": "new", "answer": "new answer"},
                           {"user_id": U1, "question": "old", "answer": "old answer"}])]
        history = await self.user.get_recent_history(S, limit=2, before_turn_id=T)
        self.assertEqual(history, [{"user": "old", "assistant": "old answer"},
                                   {"user": "new", "assistant": "new answer"}])
        call = self.transport.calls[-1]
        self.assertEqual(call["headers"]["Authorization"], "Bearer token-a")
        self.assertEqual(call["params"], {"select": "*", "session_id": "eq." + S,
            "user_id": "eq." + U1, "question_status": "eq.done", "answer_status": "eq.done",
            "order": "turn_no.desc", "limit": "2", "turn_no": "lt.9"})

    async def test_history_rejects_wrong_session_and_bad_limit(self):
        self.transport.responses[("GET", "/rest/v1/chat_turns")] = [Response(200, [
            {"id": T, "user_id": U1, "session_id": P, "turn_no": 9}])]
        with self.assertRaises(NotFoundError):
            await self.user.get_recent_history(S, before_turn_id=T)
        for limit in (0, 21, True, "2"):
            with self.assertRaises(ValidationError):
                await self.user.get_recent_history(S, limit=limit)

    async def test_v2_contract_and_owner_are_injected(self):
        await self.user.start_chat_stage(T, "llm", model_name="known-before-call", request_id=P)
        await self.user.finish_chat_stage(T, "llm", attempt=1, ok=True, text="answer",
            elapsed_ms=3, total_ms=5, finish_reason="length", request_id=P)
        await self.user.record_turn_total_ms(T, "llm", attempt=1, total_ms=5)
        calls = [c for c in self.transport.calls if "/rpc/" in c["path"]]
        self.assertEqual([c["path"].split("/")[-1] for c in calls],
                         ["start_chat_stage_v2", "finish_chat_stage_v2", "record_turn_total_ms"])
        sql = (Path(__file__).resolve().parents[2] / "infra/supabase/weekend_integration.sql").read_text(encoding="utf-8")
        import re
        declarations = dict(re.findall(r"create function public\.(\w+)\((.*?)\)\s*returns", sql, re.S))
        for call in calls:
            expected = set(re.findall(r"\b(p_\w+)\s", declarations[call["path"].split("/")[-1]]))
            self.assertEqual(set(call["body"]), expected)
            self.assertEqual(call["body"]["p_user_id"], U1)
        self.assertEqual(calls[1]["body"]["p_finish_reason"], "length")

    async def test_metadata_validation(self):
        for kwargs in ({"finish_reason": "bad"}, {"total_ms": -1}, {"request_id": "bad"}):
            with self.assertRaises(ValidationError):
                await self.user.finish_chat_stage(T, "llm", attempt=1, ok=True, text="ok", **kwargs)
        with self.assertRaises(ValidationError):
            await self.user.start_chat_stage(T, "tts", request_id=P)
        with self.assertRaises(ValidationError):
            await self.user.finish_chat_stage(T, "tts", attempt=1, ok=True, finish_reason="stop")
        with self.assertRaises(ValidationError):
            await self.user.finish_chat_stage(T, "llm", attempt=1, ok=False, finish_reason="stop")

    async def test_postgrest_55000_http_500_is_conflict_not_outage(self):
        self.transport.responses[("POST", "/rest/v1/rpc/start_chat_stage_v2")] = [
            Response(500, {"code": "55000", "message": "never expose upstream detail"})]
        with self.assertRaises(ConflictError) as caught:
            await self.user.start_chat_stage(T, "llm", model_name="synthetic", request_id=P)
        self.assertEqual(caught.exception.code, "55000")
        self.assertFalse(caught.exception.outcome_unknown)


class ErrorAdapterTests(unittest.TestCase):
    def test_allowlist_projection_drops_current_and_future_internal_fields(self):
        private = {"id": T, "answer": "answer", "finish_reason": "length", "user_id": U1,
                   "llm_request_id": P, "request_hash": "private", "llm_attempt": 1,
                   "manifest": {"path": "private"}, "future_secret": "private"}
        projected = to_public_row("turn", private)
        self.assertEqual(projected, {"turn_id": T, "answer": "answer", "is_ai_generated": True, "finish_reason": "length"})
        self.assertNotIn("private", json.dumps(projected))
        for kind in ("persona", "session", "record", "text_version", "style_profile", "deletion_job"):
            self.assertNotIn("user_id", to_public_row(kind, private))
        with self.assertRaises(ValidationError):
            to_public_row("unknown", private)

    def test_draft_error_mapping(self):
        cases = [(AuthenticationError(), 401, "UNAUTHENTICATED"),
                 (NotFoundError(), 404, "NOT_FOUND"), (ConflictError(), 409, "CONFLICT"),
                 (PermissionDeniedError(), 500, "INTERNAL_ERROR"),
                 (DatabaseError(code="private"), 500, "INTERNAL_ERROR"),
                 (DatabaseUnavailableError(outcome_unknown=True), 503, "DB_UNAVAILABLE")]
        for error, status, code in cases:
            mapped = to_http_error(error)
            self.assertEqual((mapped.status_code, mapped.body["error"]["code"]), (status, code))
            self.assertNotIn("private", json.dumps(mapped.body))
        self.assertTrue(to_http_error(cases[-1][0]).outcome_unknown)

    def test_context_controls_input_and_conflict_mapping(self):
        self.assertEqual(to_http_error(ValidationError()).status_code, 500)
        self.assertEqual(to_http_error(ValidationError(), input_source="client", field="message").status_code, 422)
        self.assertEqual(to_http_error(ConflictError(code="22023"), operation="create_chat_turn").body["error"]["code"], "TURN_ID_CONFLICT")
        self.assertEqual(to_http_error(ConflictError(), operation="edit_text_draft").body["error"]["code"], "EDIT_CONFLICT")
        result = to_http_error(ValidationError(), input_source="client", field="secret-value", stage="secret-value", turn_id="secret-value")
        self.assertNotIn("secret-value", json.dumps(result.body))
        self.assertEqual(set(result.body["error"]), {"code", "message", "stage", "turn_id", "retryable", "field"})


class EnvFileTests(unittest.TestCase):
    def test_teammate_draft_all_thirty_functions_and_arguments_remain_supported(self):
        doc = (Path(__file__).resolve().parents[2] / "contracts/backend-db/api-spec.md").read_text(encoding="utf-8")
        seen = set()
        for line in doc.splitlines():
            match = re.match(r"^\| `([a-z][a-z_]*)` \|", line)
            if match is None or not re.search(r"\| `(?:Row|list\[Row\]|dict|None|int)(?:`| )", line):
                continue
            name = match[1]
            if not hasattr(UserDatabase, name):
                self.fail("Missing draft function: " + name)
            params = inspect.signature(getattr(UserDatabase, name)).parameters
            expected = re.findall(r"`([a-z][a-z_]*)\s*:", line)
            self.assertTrue(set(expected) <= set(params), name)
            for param in params.values():
                if param.name not in {"self", *expected}:
                    self.assertNotEqual(param.default, inspect.Parameter.empty, name)
            seen.add(name)
        self.assertEqual(len(seen), 30)

    def test_live_scenario_exercises_every_public_user_function(self):
        source = (Path(__file__).parent / "test_db_full_live.py").read_text(encoding="utf-8")
        calls = {node.func.attr for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and isinstance(node.func.value, ast.Name) and node.func.value.id == "a"}
        actual = {name for name, method in inspect.getmembers(UserDatabase, inspect.iscoroutinefunction)
                  if not name.startswith("_")}
        self.assertEqual(calls, actual)

    def test_explicit_loader_without_shell_expansion_or_global_changes(self):
        # A literal local config, never a shell script; fixtures contain fake keys only.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('SUPABASE_URL=https://example.supabase.co\n'
                'export SUPABASE_PUBLISHABLE_KEY="sb_publishable_fake" # local\n'
                "SUPABASE_SECRET_KEY='sb_secret_fake'\nSUPABASE_DB_TIMEOUT_SECONDS=4 # timeout\n", encoding="utf-8")
            settings = Settings.from_env_file(path)
            self.assertEqual(settings.timeout_seconds, 4)
            self.assertNotIn("fake", repr(settings))
            self.assertEqual(Settings.from_env_file(path, override={"SUPABASE_DB_TIMEOUT_SECONDS": "5"}).timeout_seconds, 5)
            path.write_text('SUPABASE_SECRET_KEY="sensitive-value\n', encoding="utf-8")
            with self.assertRaises(ValidationError) as caught:
                Settings.from_env_file(path)
            self.assertNotIn("sensitive", str(caught.exception))
            with self.assertRaises(ValidationError):
                Settings.from_env_file(Path(directory) / "absent")
