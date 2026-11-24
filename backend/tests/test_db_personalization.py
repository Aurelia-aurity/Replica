"""Check the new public Python entry points against actual SQL argument names."""
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import Database, ValidationError
from app.db.transport import Response
from tests.test_db import FakeTransport, SETTINGS, U1, P, R, V, T


class PersonalizationRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = FakeTransport()
        self.user = await Database(SETTINGS, transport=self.transport).for_user("token-a")
        self.transport.calls.clear()

    async def test_sql_parameter_names_and_verified_owner(self):
        runtime = {"model": "r1", "tokenizer": "r1", "template": "r1"}
        calls = [
            lambda: self.user.update_persona_addresses(P, fields={"display_name": "synthetic"}),
            lambda: self.user.confirm_record_text_v2(V, source_format="plain", units=[{"start": 0, "end": 1}], speakers={}),
            lambda: self.user.capture_personalization_snapshot(P, snapshot_id=R, masking_policy_version="synthetic"),
            lambda: self.user.advance_personalization_source(P, expected_stamp=R),
            lambda: self.user.start_profile_analysis(R, operation_id=V, deadline=datetime.now(timezone.utc) + timedelta(minutes=1), runtime=runtime),
            lambda: self.user.start_profile_analysis_node(V, attempt=1, node_id=T, request_id=R, kind="analyze", ranges=[], children=[]),
            lambda: self.user.finish_profile_analysis_node(V, attempt=1, node_id=T, request_id=R, runtime=runtime, status="done", content={"tone": "synthetic"}, model_name="test"),
            lambda: self.user.publish_analysis_profile(V, attempt=1, root_node_id=T),
            lambda: self.user.get_profile_analysis_result(V, attempt=1),
            lambda: self.user.start_record_index_build(R, build_id=V, embedding_model="test", embedding_revision="r1", dimensions=2),
            lambda: self.user.append_record_index_chunks(V, chunks=[{"id": T, "chunk_no": 0, "content": "text", "coverage": [], "embedding": [1, 0]}]),
            lambda: self.user.activate_record_index_build(V),
            lambda: self.user.fail_record_index_build(V),
            lambda: self.user.read_persona_context(P, embedding_model="test", embedding_revision="r1", query_embedding=[1, 0]),
            lambda: self.user.get_record_reflection(R),
            lambda: self.user.bind_persona_chat_turn(T, request_id=R, snapshot_id=V, build_id=P, profile_id=P),
            lambda: self.user.check_persona_chat_turn(T, request_id=R, attempt=1),
            lambda: self.user.finish_persona_chat_turn(T, request_id=R, attempt=1, ok=True, text="synthetic"),
        ]
        for name, value in {
            "advance_personalization_source": P,
            "append_record_index_chunks": 1,
            "get_record_reflection": "pending",
            "check_persona_chat_turn": True,
        }.items():
            self.transport.responses[("POST", "/rest/v1/rpc/" + name)] = [Response(200, value)]
        root = Path(__file__).resolve().parents[2] / "infra" / "supabase"
        sql = (root / "record_metadata.sql").read_text(encoding="utf-8")
        sql += (root / "20261124043921_personalization_lifecycle.sql").read_text(encoding="utf-8")
        signatures = dict(re.findall(r"create function public\.(\w+)\((.*?)\)\s*returns", sql, re.S))
        for call in calls:
            await call()
            request = self.transport.calls[-1]
            name = request["path"].rsplit("/", 1)[1]
            expected = set(re.findall(r"\b(p_\w+)\s", signatures[name]))
            self.assertEqual(set(request["body"]), expected, name)
            self.assertEqual(request["body"]["p_user_id"], U1)
            self.assertEqual(request["headers"]["apikey"], SETTINGS.secret_key)
            self.assertNotIn("Authorization", request["headers"])
        self.assertEqual(len(self.transport.calls), len(calls))

    async def test_speaker_sqlstates_remain_validation_errors_on_http_500(self):
        for code in ("P1201", "P1202", "P1203"):
            self.transport.responses[("POST", "/rest/v1/rpc/confirm_record_text_v2")] = [
                Response(500, {"code": code, "message": "private source detail"})]
            with self.assertRaises(ValidationError) as caught:
                await self.user.confirm_record_text_v2(V, source_format="kakao",
                    units=[{"start": 0, "end": 1, "speaker": "synthetic"}], speakers={})
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("private source", str(caught.exception))

    async def test_nonfinite_json_and_invalid_vector_are_rejected_before_writes(self):
        with self.assertRaises(ValidationError):
            await self.user.start_profile_analysis(R, operation_id=V,
                deadline=datetime.now(timezone.utc), runtime={"model": float("nan")})
        with self.assertRaises(ValidationError):
            await self.user.append_record_index_chunks(V,
                chunks=[{"id": T, "chunk_no": 0, "content": "text", "embedding": [0, 0]}])
        with self.assertRaises(ValidationError):
            await self.user.read_persona_context(P, embedding_model="test", embedding_revision="r1", query_embedding=[1, 0], limit=11)
        with self.assertRaises(ValidationError):
            await self.user.finish_persona_chat_turn(T, request_id=R, attempt=1, ok=False,
                error_code={"unexpected": "private"})
        self.assertEqual(self.transport.calls, [])
