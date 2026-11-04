"""Validate synthetic contract fixtures; no HTTP server, tokenizer or GPU code."""

import copy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


BASE = Path(__file__).resolve().parents[1] / "baseline"
SCHEMAS = {
    name: json.loads((BASE / f"{name}.schema.json").read_text())
    for name in ("request", "success", "error")
}
VALIDATORS = {name: Draft202012Validator(s) for name, s in SCHEMAS.items()}
ID_VALIDATOR = Draft202012Validator(SCHEMAS["request"]["properties"]["request_id"])
STATUS = {
    "INVALID_REQUEST": 400,
    "SERVICE_UNAUTHENTICATED": 401,
    "SERVICE_FORBIDDEN": 403,
    "PAYLOAD_TOO_LARGE": 413,
    "CONTEXT_LIMIT_EXCEEDED": 422,
    "MODEL_NOT_READY": 503,
    "REQUEST_IN_PROGRESS": 409,
    "AI_BUSY": 429,
    "INFERENCE_DEADLINE_EXCEEDED": 504,
    "INFERENCE_FAILED": 500,
}
FIXTURES = {
    p.stem: json.loads(p.read_text()) for p in sorted((BASE / "examples").glob("*.json"))
}


def documented_outcome(request, conditions):
    """Symbolic document check, NOT production admission/auth/context logic.

    Conditions stand for future observations. No compute or state transitions
    occur here; schema validation determines structural/cap errors only.
    """
    if conditions.get("raw_body_bytes", 0) > 262144:
        return "PAYLOAD_TOO_LARGE", False
    auth = conditions.get("service_auth")
    if auth == "unauthenticated":
        return "SERVICE_UNAUTHENTICATED", False
    if auth == "forbidden":
        return "SERVICE_FORBIDDEN", False
    if conditions.get("malformed_json"):
        return "INVALID_REQUEST", False
    safe_id = isinstance(request, dict) and ID_VALIDATOR.is_valid(request.get("request_id"))
    errors = list(VALIDATORS["request"].iter_errors(request))
    # Only upper text/array caps classify as 413; ID length is a format error.
    def cap(error):
        path = list(error.absolute_path)
        return error.validator in {"maxLength", "maxItems"} and path[0:1] in (
            ["message"], ["history"]
        )
    if any(not cap(e) for e in errors):
        return "INVALID_REQUEST", safe_id
    if errors:
        return "PAYLOAD_TOO_LARGE", safe_id
    if conditions.get("model_ready") is False:
        return "MODEL_NOT_READY", safe_id
    if conditions.get("context_fits_without_history") is False:
        return "CONTEXT_LIMIT_EXCEEDED", safe_id
    if conditions.get("active_duplicate"):
        return "REQUEST_IN_PROGRESS", safe_id
    if conditions.get("slot_occupied"):
        return "AI_BUSY", safe_id
    if conditions.get("deadline_exceeded"):
        return "INFERENCE_DEADLINE_EXCEEDED", safe_id
    if conditions.get("inference_failed") or conditions.get("blank_output"):
        return "INFERENCE_FAILED", safe_id
    return None, safe_id


def check_fixture(fixture):
    """Reject inconsistent examples, including structurally valid wrong IDs."""
    request, response = fixture["request"], fixture["response"]
    assert VALIDATORS["request"].is_valid(request) == fixture["request_valid"]
    if fixture["transport_outcome"] == "unknown":
        assert fixture["http_status"] is None and response is None
        return
    assert fixture["transport_outcome"] == "received"
    code, safe_id = documented_outcome(request, fixture["conditions"])
    if code is None:
        VALIDATORS["success"].validate(response)
        assert fixture["http_status"] == 200
        c = response["context"]
        assert c["used_history_turns"] + c["dropped_history_turns"] == len(request["history"])
        assert c["dropped_history_turns"] == fixture["conditions"].get("dropped_history_turns", 0)
    else:
        VALIDATORS["error"].validate(response)
        assert response["error"]["code"] == code
        assert fixture["http_status"] == STATUS[code]
    # Both successes and errors must correlate; early safe-ID-unavailable cases use null.
    assert response["request_id"] == (request["request_id"] if safe_id else None)


class BaselineContractTests(unittest.TestCase):
    def test_schema_definitions(self):
        for schema in SCHEMAS.values():
            Draft202012Validator.check_schema(schema)
        self.assertEqual(set(SCHEMAS["error"]["properties"]["error"]["properties"]["code"]["enum"]), set(STATUS))

    def test_all_examples_and_error_coverage(self):
        self.assertEqual(len(FIXTURES), 18)
        codes = set()
        for name, fixture in FIXTURES.items():
            with self.subTest(example=name):
                check_fixture(fixture)
                if fixture["response"] and "error" in fixture["response"]:
                    codes.add(fixture["response"]["error"]["code"])
        self.assertEqual(codes, set(STATUS))

    def request(self):
        return copy.deepcopy(FIXTURES["01-normal"]["request"])

    def test_required_unknown_and_role_fields(self):
        request = self.request()
        for field in request:
            r = self.request(); del r[field]
            with self.subTest(missing=field):
                self.assertFalse(VALIDATORS["request"].is_valid(r))
        for field in ("user_id", "conversation_id", "persona_id", "system_prompt", "metadata"):
            r = self.request(); r[field] = "synthetic"
            with self.subTest(unknown=field):
                self.assertFalse(VALIDATORS["request"].is_valid(r))
        for pair in ({"user": "질문"}, {"user": "질문", "assistant": "답", "role": "system"},
                     {"role": "tool", "content": "내용"}, {"user": "질문", "assistant": "\t"}):
            r = self.request(); r["history"] = [pair]
            with self.subTest(pair=pair):
                self.assertFalse(VALIDATORS["request"].is_valid(r))

    def test_types_blank_and_uuid(self):
        for field, value in (("message", None), ("message", 1), ("message", "\t\n\u3000"),
                             ("history", {}), ("max_new_tokens", True), ("max_new_tokens", "160"),
                             ("max_new_tokens", 1.5), ("request_id", None),
                             ("request_id", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
                             ("request_id", self.request()["request_id"] + "\n")):
            r = self.request(); r[field] = value
            with self.subTest(field=field, value=value):
                self.assertFalse(VALIDATORS["request"].is_valid(r))

    def test_caps_and_classification_overlap(self):
        for length, code in ((16000, None), (16001, "PAYLOAD_TOO_LARGE")):
            r = self.request(); r["message"] = "가" * length
            self.assertEqual(documented_outcome(r, {})[0], code)
        for count, code in ((20, None), (21, "PAYLOAD_TOO_LARGE")):
            r = self.request(); r["history"] = [{"user": "질문", "assistant": "답"}] * count
            self.assertEqual(documented_outcome(r, {})[0], code)
        for field in ("user", "assistant"):
            r = self.request(); r["history"] = [{"user": "질문", "assistant": "답"}]
            r["history"][0][field] = "가" * 16001
            self.assertEqual(documented_outcome(r, {})[0], "PAYLOAD_TOO_LARGE")
        r["unknown"] = True
        self.assertEqual(documented_outcome(r, {})[0], "INVALID_REQUEST")
        self.assertEqual(documented_outcome(r, {"raw_body_bytes": 262145})[0], "PAYLOAD_TOO_LARGE")
        self.assertIsNone(documented_outcome(self.request(), {"raw_body_bytes": 262144})[0])
        for budget in (1, 160, 1024):
            r = self.request(); r["max_new_tokens"] = budget
            self.assertTrue(VALIDATORS["request"].is_valid(r))
        for budget in (0, 1025):
            r = self.request(); r["max_new_tokens"] = budget
            self.assertEqual(documented_outcome(r, {})[0], "INVALID_REQUEST")

    def test_documented_precedence(self):
        r = self.request()
        cases = [({"raw_body_bytes": 262145, "service_auth": "forbidden"}, "PAYLOAD_TOO_LARGE"),
                 ({"service_auth": "forbidden", "model_ready": False}, "SERVICE_FORBIDDEN"),
                 ({"model_ready": False, "context_fits_without_history": False}, "MODEL_NOT_READY"),
                 ({"context_fits_without_history": False, "slot_occupied": True}, "CONTEXT_LIMIT_EXCEEDED"),
                 ({"active_duplicate": True, "slot_occupied": True}, "REQUEST_IN_PROGRESS")]
        for conditions, code in cases:
            with self.subTest(conditions=conditions):
                self.assertEqual(documented_outcome(r, conditions)[0], code)

    def test_success_and_error_id_mismatch_and_null(self):
        for name in ("01-normal", "07-busy", "10-ai-deadline", "05-invalid-blank"):
            for id_value in ("22222222-2222-4222-8222-222222222222", None):
                f = copy.deepcopy(FIXTURES[name]); f["response"]["request_id"] = id_value
                with self.subTest(example=name, id=id_value):
                    with self.assertRaises((AssertionError, ValidationError)):
                        check_fixture(f)

    def test_context_and_http_code_mismatch(self):
        f = copy.deepcopy(FIXTURES["03-history-trimmed"])
        f["response"]["context"]["used_history_turns"] = 2
        with self.assertRaises(AssertionError):
            check_fixture(f)
        f = copy.deepcopy(FIXTURES["07-busy"]); f["http_status"] = 409
        with self.assertRaises(AssertionError):
            check_fixture(f)
        f = copy.deepcopy(FIXTURES["16-transport-timeout"])
        f["response"] = FIXTURES["10-ai-deadline"]["response"]; f["http_status"] = 504
        with self.assertRaises(AssertionError):
            check_fixture(f)

    def test_response_shape_and_finish_reason(self):
        for field, value in (("message", "  "), ("ai_generated", False), ("finish_reason", "unknown")):
            response = copy.deepcopy(FIXTURES["01-normal"]["response"]); response[field] = value
            self.assertFalse(VALIDATORS["success"].is_valid(response))
        response = copy.deepcopy(FIXTURES["01-normal"]["response"])
        response["unknown"] = True
        self.assertFalse(VALIDATORS["success"].is_valid(response))
        for key, value in (("code", "PRIVATE_ERROR"), ("message", " ")):
            response = copy.deepcopy(FIXTURES["07-busy"]["response"]); response["error"][key] = value
            self.assertFalse(VALIDATORS["error"].is_valid(response))
        for schema_name, example in (("success", "01-normal"), ("error", "07-busy")):
            original = FIXTURES[example]["response"]
            for key in original:
                response = copy.deepcopy(original); del response[key]
                self.assertFalse(VALIDATORS[schema_name].is_valid(response))
            response = copy.deepcopy(original); response["unknown"] = "value"
            self.assertFalse(VALIDATORS[schema_name].is_valid(response))


if __name__ == "__main__":
    unittest.main()
