"""Local #44 auth-seam and metadata-only logging checks; no deployment auth."""
from http.client import HTTPConnection
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from persona_adapter import PersonaInference
import server
from request_timing import RequestTiming
from test_persona import PATH, request
from server_test_support import Engine, call, serving

MARKERS = (
    "PRIVATE_PROMPT_44", "PRIVATE_PROFILE_44", "PRIVATE_EVIDENCE_44",
    "PRIVATE_HISTORY_44", "PRIVATE_REPLY_44", "PRIVATE_SYSTEM_44",
    "Bearer SYNTHETIC_SECRET_44", "PRIVATE_EXCEPTION_44",
)
HEADERS = {"Authorization": MARKERS[6]}


def private_request():
    data = request()
    data["message"] = MARKERS[0]
    data["style_profile"]["content"]["limitations"] = [MARKERS[1]]
    data["evidence"][0]["content"] = MARKERS[2]
    data["history"] = [{"user": MARKERS[3], "assistant": MARKERS[4]}]
    return data


def private_engine():
    engine = Engine()
    engine.system_prompt = MARKERS[5]
    engine.answer = json.dumps({"message": MARKERS[4], "evidence_used": [1]})
    return engine


def assert_private_timing(case, line):
    """The #116 JSON schema permits only fixed labels, IDs and numeric metadata."""
    row = json.loads(line)
    labels = {"schema_version", "event", "attempt_id", "request_id", "stage",
              "queue_present", "queue_wait_ms", "ttft_available", "ttft_reason",
              "first_unforced_token_reason", "write_ok"}
    numbers = {"start_ns", "end_ns", "status", "input_tokens", "output_tokens",
               "first_token_ms", "first_generated_token_ns", "first_unforced_token_ns",
               "model_generate_start_ns", "model_generate_end_ns", "model_first_token_ms",
               "forced_prefix_tokens"}
    case.assertEqual(set(row), labels | numbers)
    case.assertEqual(row["schema_version"], 1)
    case.assertEqual(row["event"], "request_timing")
    case.assertRegex(row["attempt_id"], r"^[0-9a-f-]{36}$")
    if row["request_id"] is not None:
        case.assertRegex(row["request_id"], r"^[0-9a-f-]{36}$")
    case.assertIn(row["stage"], {
        "receipt", "body_read", "parse", "prepare_tokenize", "prompt_assembly",
        "persona_prompt_assembly",
        "template_tokenize", "prefix_encode", "input_setup", "model_generate_call",
        "output_decode", "cuda_drain", "adapter_parse_guard", "admission_lock",
        "worker_handoff", "success_body", "generate", "response_write"})
    case.assertIn(row["ttft_reason"], {
        "observed_cpu_token", "awaiting_first_token", "no_generated_token",
        "observer_not_started"})
    case.assertIn(row["first_unforced_token_reason"], {
        "observed_cpu_token", "no_unforced_token", "observer_not_started"})
    case.assertIs(row["queue_present"], False)
    case.assertIsNone(row["queue_wait_ms"])
    case.assertIs(type(row["ttft_available"]), bool)
    case.assertIn(type(row["write_ok"]), (bool, type(None)))
    for key in numbers:
        if row[key] is not None:
            case.assertIn(type(row[key]), (int, float))
            case.assertGreaterEqual(row[key], 0)


def assert_private_logs(case, logs, status):
    """Allow counts, never token values, and static exception location metadata."""
    messages = [record.getMessage() for record in logs.records]
    case.assertTrue(messages)
    case.assertTrue(any(f"status={status} " in line for line in messages))
    for marker in MARKERS:
        case.assertNotIn(marker, "\n".join(messages))
    prefix = r"request_id=(?:null|[0-9a-f-]{36}) status=\d{3} "
    normal = prefix + r"duration_ms=\d+(?:\.\d+)?(?: input_tokens=\d+ output_tokens=\d+)?"
    failure = prefix + r"exception_type=\w+ stack=(.+)"
    for record, line in zip(logs.records, messages):
        case.assertIsNone(record.exc_info)
        if line.startswith("{"):
            assert_private_timing(case, line)
            continue
        if re.fullmatch(normal, line):
            continue
        match = re.fullmatch(failure, line)
        case.assertIsNotNone(match, line)
        frames = json.loads(match.group(1))
        case.assertTrue(frames)
        for frame in frames:
            case.assertEqual(set(frame), {"file", "function", "line"})
            case.assertNotIn("/", frame["file"])
            case.assertIsInstance(frame["line"], int)


class PersonaCallAuthTests(unittest.TestCase):
    def test_timing_privacy_rejects_payload_fields_and_string_metadata(self):
        timing = RequestTiming()
        with self.assertLogs(server.LOGGER, level="INFO") as logs:
            timing.emit("receipt", 0)
        row = json.loads(logs.records[0].getMessage())
        assert_private_timing(self, json.dumps(row))
        for field, value in (("message", MARKERS[0]), ("stage", MARKERS[1]),
                             ("input_tokens", MARKERS[2]), ("request_id", MARKERS[3])):
            with self.subTest(field=field), self.assertRaises(AssertionError):
                assert_private_timing(self, json.dumps({**row, field: value}))

    def test_fake_auth_decisions_use_fixed_envelopes_and_private_logs(self):
        for decision in (None, 401, 403):
            engine = private_engine()
            auth = Mock(return_value=decision)
            service = server.Service(
                engine, auth=auth, persona_generator=PersonaInference(engine, "synthetic-model"))
            with self.subTest(decision=decision), self.assertLogs(
                    server.LOGGER, level="INFO") as logs, serving(
                        service, join_handlers=True) as port:
                result = call(port, private_request(), headers=HEADERS, path=PATH)
            status = decision or 200
            self.assertEqual(result[0], status)
            if decision is None:
                self.assertEqual(result[1]["request_id"], private_request()["request_id"])
                self.assertEqual(engine.calls, 1)
            else:
                self.assertEqual(result[1], server.error_body(decision))
                self.assertEqual(engine.calls, 0)
            auth.assert_called_once()
            self.assertEqual(auth.call_args.args[0]["Authorization"], HEADERS["Authorization"])
            assert_private_logs(self, logs, status)

    def test_raw_size_rejection_precedes_auth_and_persona_parsing(self):
        auth = Mock(return_value=401)
        with patch.object(server.persona_contract, "parse_request") as parse:
            with self.assertLogs(server.LOGGER, level="INFO") as logs, serving(
                    server.Service(auth=auth), join_handlers=True) as port:
                result = call(port, raw=b"", headers={
                    **HEADERS, "Content-Length": str(server.RAW_LIMIT + 1)}, path=PATH)
            parse.assert_not_called()
        self.assertEqual(result, (413, server.error_body(413)))
        auth.assert_not_called()
        assert_private_logs(self, logs, 413)

    def test_denial_precedes_json_decode_even_for_valid_id_or_broken_body(self):
        for decision in (401, 403):
            for raw in (b"{broken", json.dumps(private_request()).encode()):
                auth = Mock(return_value=decision)
                with self.subTest(decision=decision, broken=raw == b"{broken"), self.assertLogs(
                        server.LOGGER, level="INFO") as logs, serving(
                            server.Service(auth=auth), join_handlers=True) as port:
                    connection = HTTPConnection("127.0.0.1", port, timeout=5)
                    try:
                        with patch.object(server.json, "loads") as decode:
                            connection.request("POST", PATH, raw, HEADERS)
                            response = connection.getresponse()
                            payload = response.read()
                            decode.assert_not_called()
                        self.assertEqual(response.status, decision)
                        self.assertEqual(json.loads(payload), server.error_body(decision))
                    finally:
                        connection.close()
                auth.assert_called_once()
                assert_private_logs(self, logs, decision)

    def test_null_request_id_has_safe_error_after_allowed_auth(self):
        auth = Mock(return_value=None)
        data = private_request()
        data["request_id"] = None
        with self.assertLogs(server.LOGGER, level="INFO") as logs, serving(
                server.Service(auth=auth), join_handlers=True) as port:
            self.assertEqual(call(port, data, headers=HEADERS, path=PATH),
                             (400, server.error_body(400)))
        auth.assert_called_once()
        assert_private_logs(self, logs, 400)

    def test_readyz_is_currently_ungated_even_when_post_is_denied(self):
        # Documents current GET behavior, not a proposed deployment policy.
        for ready in (False, True):
            auth = Mock(return_value=403)
            service = server.Service(private_engine() if ready else None, auth=auth)
            with self.subTest(ready=ready), self.assertLogs(
                    server.LOGGER, level="INFO") as logs, serving(
                        service, join_handlers=True) as port:
                status = 200 if ready else 503
                self.assertEqual(call(port, method="GET", path="/readyz", headers=HEADERS),
                                 (status, {"ready": ready}))
            auth.assert_not_called()
            assert_private_logs(self, logs, status)

    def test_failure_boundaries_log_only_metadata(self):
        for phase, status in (("parse", 400), ("size", 413), ("ready", 503),
                              ("context", 422), ("prepare", 500), ("decode", 500)):
            engine = private_engine()
            data = private_request()
            if phase == "parse":
                data["max_new_tokens"] = True
            if phase == "size":
                data["message"] += "x" * 16001
            if phase == "context":
                engine.context_limit = 1
            service = server.Service(engine, persona_generator=PersonaInference(engine, "test"))
            if phase == "ready":
                service.ready.clear()
            target = engine.tokenizer if phase == "prepare" else engine
            method = "apply_chat_template" if phase == "prepare" else "generate"
            failure = RuntimeError(MARKERS[7])
            with self.subTest(phase=phase), self.assertLogs(
                    server.LOGGER, level="INFO") as logs, serving(
                        service, join_handlers=True) as port:
                if phase in ("prepare", "decode"):
                    with patch.object(target, method, side_effect=failure):
                        result = call(port, data, headers=HEADERS, path=PATH)
                else:
                    result = call(port, data, headers=HEADERS, path=PATH)
            self.assertEqual(result, (status, server.error_body(status, data["request_id"])))
            assert_private_logs(self, logs, status)
