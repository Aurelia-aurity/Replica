"""Synthetic observability repros; NOT captured outputs from GPU smoke35890."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from persona_adapter import PersonaInference
import server
from test_persona import PATH
from test_server import Engine, call, request as baseline_request, serving
from test_smoke_persona import smoke


def smoke_request(history=False):
    data = smoke.persona_payload(evidence=history)
    if history:
        data["history"] = [{"user": "안녕.", "assistant": "안녕하세요."}]
    else:
        data["message"] = "안녕!"
    return data


class PersonaDiagnosticTests(unittest.TestCase):
    def test_original_smoke_request_shapes_succeed_with_valid_synthetic_outputs(self):
        for history in (False, True):
            # Given: same smoke request construction, NOT a claimed real GPU reply.
            data = smoke_request(history)
            engine = Engine()
            engine.answer = json.dumps({
                "message": "합성 응답", "evidence_used": [1] if history else []})
            with self.subTest(history=history), serving(server.Service(
                    engine, persona_generator=PersonaInference(engine, "test"))) as port:
                # When:
                status, body = call(port, data, path=PATH)
            # Then:
            self.assertEqual(status, 200)
            self.assertEqual(body["context"], {
                "used_history_turns": int(history), "dropped_history_turns": 0})
            self.assertEqual(engine.calls, 1)

    def test_persona_500_records_type_and_stack_without_private_values(self):
        for history in (False, True):
            for phase in ("prepare", "decode", "parse", "response"):
                # Given: distinct injected failures at each real500 boundary.
                secret = "PRIVATE_REQUEST_AND_EXCEPTION_VALUE"
                data = smoke_request(history)
                data["message"] = secret
                engine = Engine()
                engine.answer = '{"message":"a","evidence_used":[]}'
                adapter = PersonaInference(engine, "test")
                target, method = {
                    "prepare": (engine.tokenizer, "apply_chat_template"),
                    "decode": (engine, "generate"),
                    "parse": (engine, "generate"),
                    "response": (server.persona_contract, "success_body"),
                }[phase]
                if phase == "parse":
                    engine.answer = secret  # JSONDecodeError retains raw .doc.
                    failure = patch.object(engine, "failure", False)
                    expected_type = "JSONDecodeError"
                else:
                    failure = patch.object(target, method, side_effect=RuntimeError(secret))
                    expected_type = "RuntimeError"
                with self.subTest(history=history, phase=phase), failure, serving(
                        server.Service(engine, persona_generator=adapter)) as port:
                    # When:
                    with self.assertLogs(server.LOGGER, level="ERROR") as logs:
                        status, body = call(port, data, path=PATH)
                    # Then: safe response and useful, value-free stack metadata.
                    self.assertEqual((status, body), (
                        500, server.error_body(500, data["request_id"])))
                    self.assertEqual(len(logs.records), 1)
                    record = logs.records[0]
                    self.assertIsNone(record.exc_info)
                    message = record.getMessage()
                    self.assertNotIn(secret, message)
                    self.assertNotIn(str(smoke.Path.home()), message)
                    self.assertIn(f"exception_type={expected_type}", message)
                    frames = json.loads(message.split(" stack=", 1)[1])
                    self.assertTrue(frames)
                    self.assertTrue(all(set(frame) == {"file", "function", "line"} for frame in frames))
                    self.assertTrue(all(type(frame["line"]) is int for frame in frames))
                    self.assertTrue(any(frame["function"] in ("_generate", "do_POST") for frame in frames))

    def test_baseline_500_keeps_existing_logging_and_response(self):
        # Given:
        engine = Engine()
        engine.failure = True
        with serving(server.Service(engine)) as port:
            # When:
            with self.assertNoLogs(server.LOGGER, level="ERROR"):
                result = call(port, baseline_request())
        # Then:
        self.assertEqual(result, (
            500, server.error_body(500, baseline_request()["request_id"])))
