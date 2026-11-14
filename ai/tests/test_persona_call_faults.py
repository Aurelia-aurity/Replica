"""#44 UNKNOWN/AI-deadline observations with event-controlled real HTTP."""
from dataclasses import replace
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from persona_adapter import PersonaInference
import server
from test_persona import PATH
from test_persona_call_auth import (
    HEADERS, MARKERS, assert_private_logs, private_engine, private_request,
)
from test_server import OTHER_ID, RecordingService, call, request as baseline_request, serving

BASELINE = "/v1/baseline/generate"


class PersonaCallFaultTests(unittest.TestCase):
    def assert_error(self, result, status, request_id):
        self.assertEqual(result, (status, server.error_body(status, request_id)))

    def test_unknown_disconnect_and_ai_deadline_retain_slot_until_decode_finishes(self):
        for outcome in ("disconnect", "deadline"):
            engine = private_engine()
            engine.block = True
            service = RecordingService(
                engine, deadline_seconds=0.01 if outcome == "deadline" else None,
                persona_generator=PersonaInference(engine, "synthetic-model"))
            data = private_request()
            request_id = data["request_id"]
            connection = None
            with self.subTest(outcome=outcome), self.assertLogs(
                    server.LOGGER, level="INFO") as logs, serving(service) as port:
                try:
                    if outcome == "disconnect":
                        connection = HTTPConnection("127.0.0.1", port, timeout=5)
                        connection.request("POST", PATH, json.dumps(data), {
                            **HEADERS, "Content-Type": "application/json"})
                        self.assertTrue(service.accepted.wait(5))
                        self.assertTrue(engine.entered.wait(5))
                        connection.close()  # UNKNOWN: no response was read.
                    else:
                        self.assert_error(call(port, data, headers=HEADERS, path=PATH),
                                          504, request_id)
                        self.assertTrue(engine.entered.wait(5))
                    self.assertEqual(service.active_id, request_id)
                    self.assertFalse(service.job.done.is_set())
                    self.assert_error(call(port, data, headers=HEADERS, path=PATH), 409, request_id)
                    self.assert_error(call(port, dict(data, request_id=OTHER_ID),
                                           headers=HEADERS, path=PATH), 429, OTHER_ID)
                    self.assertEqual(engine.calls, 1)
                    engine.release.set()
                    self.assertTrue(service.job.done.wait(5))
                    self.assertIsNone(service.active_id)
                    # Explicit operator resubmission after completion, not automatic retry.
                    service.deadline_seconds = None
                    self.assertEqual(call(port, data, headers=HEADERS, path=PATH)[0], 200)
                    self.assertEqual(engine.calls, 2)
                finally:
                    engine.release.set()
                    if connection is not None:
                        connection.close()
                    if service.job is not None:
                        self.assertTrue(service.job.done.wait(5))
            for status in (200, 409, 429, 504) if outcome == "deadline" else (200, 409, 429):
                assert_private_logs(self, logs, status)

    def test_baseline_and_persona_reserve_the_same_slot_in_both_directions(self):
        for first_path in (PATH, BASELINE):
            engine = private_engine()
            engine.block = True
            service = RecordingService(
                engine, persona_generator=PersonaInference(engine, "synthetic-model"))
            data = private_request()
            request_id = data["request_id"]
            baseline = baseline_request(request_id=request_id, message=MARKERS[0])
            first = data if first_path == PATH else baseline
            second_path = BASELINE if first_path == PATH else PATH
            second = baseline if second_path == BASELINE else data
            results = []
            with self.subTest(first_path=first_path), self.assertLogs(
                    server.LOGGER, level="INFO") as logs, serving(service) as port:
                thread = threading.Thread(target=lambda: results.append(
                    call(port, first, headers=HEADERS, path=first_path)))
                thread.start()
                try:
                    self.assertTrue(service.accepted.wait(5))
                    self.assertTrue(engine.entered.wait(5))
                    self.assert_error(call(port, second, headers=HEADERS, path=second_path),
                                      409, request_id)
                    self.assert_error(call(port, dict(second, request_id=OTHER_ID),
                                           headers=HEADERS, path=second_path), 429, OTHER_ID)
                    self.assertEqual(engine.calls, 1)
                finally:
                    engine.release.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0][0], 200)
                self.assertTrue(service.job.done.is_set())
                self.assertIsNone(service.active_id)
                self.assertEqual(call(port, second, headers=HEADERS, path=second_path)[0], 200)
                self.assertEqual(engine.calls, 2)
            for status in (200, 409, 429):
                assert_private_logs(self, logs, status)

    def test_malformed_or_mismatched_model_citations_fail_once_with_fixed_500(self):
        outputs = (
            MARKERS[2],  # JSONDecodeError retains source in .doc; logs must not.
            json.dumps({"message": MARKERS[4], "evidence_used": [OTHER_ID]}),
            json.dumps({"message": MARKERS[4], "evidence_used": [2]}),
            json.dumps({"message": MARKERS[4], "evidence_used": [1], "request_id": OTHER_ID}),
        )
        for index, output in enumerate(outputs):
            engine = private_engine()
            engine.answer = output
            service = server.Service(engine, persona_generator=PersonaInference(engine, "test"))
            data = private_request()
            with self.subTest(output=index), self.assertLogs(
                    server.LOGGER, level="INFO") as logs, serving(service) as port:
                self.assert_error(call(port, data, headers=HEADERS, path=PATH),
                                  500, data["request_id"])
            self.assertEqual(engine.calls, 1)
            self.assertIsNone(service.active_id)
            assert_private_logs(self, logs, 500)

    def test_generator_evidence_id_mismatch_fails_at_response_boundary(self):
        engine = private_engine()
        adapter = PersonaInference(engine, "test")
        original = adapter.generate

        def mismatched(req, prepared):
            return replace(original(req, prepared), evidence_used=(OTHER_ID,))

        data = private_request()
        self.assertNotIn(OTHER_ID, [item["chunk_id"] for item in data["evidence"]])
        with patch.object(adapter, "generate", side_effect=mismatched), self.assertLogs(
                server.LOGGER, level="INFO") as logs, serving(
                    server.Service(engine, persona_generator=adapter)) as port:
            self.assert_error(call(port, data, headers=HEADERS, path=PATH),
                              500, data["request_id"])
        self.assertEqual(engine.calls, 1)
        assert_private_logs(self, logs, 500)
