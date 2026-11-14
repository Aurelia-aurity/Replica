"""Timing boundaries, continuing worker events and content exclusion."""
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import request_timing
import server
from test_persona import FakePersona, PATH, assign, request as persona_request
from test_server import Engine, RecordingService, call, request, serving


def timing_records(logs):
    return [json.loads(line.split(":", 2)[-1]) for line in logs.output
            if '"event":"request_timing"' in line]


class RequestTimingTests(unittest.TestCase):
    def test_monotonic_offsets_use_one_local_clock(self):
        with patch.object(request_timing.time, "monotonic_ns", side_effect=[100, 120, 150]):
            timing = request_timing.RequestTiming()
            start = timing.now()
            with self.assertLogs("baseline.http", level="INFO") as logs:
                timing.emit("generate", start)
        row = timing_records(logs)[0]
        self.assertEqual((row["start_ns"], row["end_ns"]), (20, 50))
        self.assertIsNone(row["first_token_ms"])
        self.assertFalse(row["ttft_available"])
        self.assertFalse(row["queue_present"])
        self.assertIsNone(row["queue_wait_ms"])

    def test_request_spans_are_present_ordered_and_contain_only_metadata(self):
        secret = "SECRET_PROMPT_PROFILE_EVIDENCE_ANSWER"
        for persona in (False, True):
            for failure in (False, True):
                engine = Engine()
                fake = FakePersona()
                selected = fake if persona else engine
                selected.answer = secret
                selected.failure = failure
                data = persona_request() if persona else request(message=secret)
                if persona:
                    assign(data, ("persona", "display_name"), secret)
                    assign(data, ("evidence", 0, "content"), secret)
                    data["message"] = secret
                responded = threading.Event()
                original = server.Handler.respond

                def respond(handler, status, body):
                    original(handler, status, body)
                    responded.set()

                with self.subTest(persona=persona, failure=failure), serving(
                        server.Service(engine, persona_generator=fake)) as port, patch.object(
                            server.Handler, "respond", respond):
                    with self.assertLogs("baseline.http", level="INFO") as logs:
                        result = call(port, data, path=PATH if persona else "/v1/baseline/generate")
                        self.assertTrue(responded.wait(5))
                self.assertEqual(result[0], 500 if failure else 200)
                self.assertNotIn(secret, "\n".join(logs.output))
                rows = timing_records(logs)
                self.assertEqual({r["stage"] for r in rows}, {
                    "receipt", "body_read", "parse", "prepare_tokenize",
                    "admission_lock", "worker_handoff", "generate", "response_write"})
                self.assertEqual(len({r["attempt_id"] for r in rows}), 1)
                by_stage = {r["stage"]: r for r in rows}
                for row in rows:
                    self.assertLessEqual(row["start_ns"], row["end_ns"])
                    self.assertIsNone(row["first_token_ms"])
                ordered = [by_stage[s] for s in (
                    "receipt", "body_read", "parse", "prepare_tokenize", "admission_lock")]
                for before, after in zip(ordered, ordered[1:]):
                    self.assertLessEqual(before["end_ns"], after["start_ns"])
                self.assertTrue(by_stage["response_write"]["write_ok"])
                self.assertEqual(by_stage["generate"]["output_tokens"], 0 if failure else 1)

    def test_deadline_has_separate_response_and_later_worker_terminal(self):
        engine = Engine()
        engine.block = True
        service = RecordingService(engine, deadline_seconds=0.01)
        responded = threading.Event()
        original = server.Handler.respond

        def respond(handler, status, body):
            original(handler, status, body)
            responded.set()

        with serving(service) as port, patch.object(server.Handler, "respond", respond):
            with self.assertLogs("baseline.http", level="INFO") as logs:
                self.assertEqual(call(port, request())[0], 504)
                self.assertTrue(responded.wait(5))
                self.assertEqual(service.active_id, request()["request_id"])
                self.assertNotIn("generate", [r["stage"] for r in timing_records(logs)])
                engine.release.set()
                self.assertTrue(service.job.done.wait(5))
        rows = timing_records(logs)
        response = next(r for r in rows if r["stage"] == "response_write")
        worker = next(r for r in rows if r["stage"] == "generate")
        self.assertEqual((response["status"], worker["status"]), (504, 200))
        self.assertEqual(response["attempt_id"], worker["attempt_id"])
        self.assertLessEqual(response["end_ns"], worker["end_ns"])

    def test_failed_write_reports_not_delivered(self):
        handler = server.Handler.__new__(server.Handler)
        handler.timing = request_timing.RequestTiming()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock(spec=io.BytesIO)
        handler.wfile.write.side_effect = BrokenPipeError("SECRET_SOCKET")
        with self.assertLogs("baseline.http", level="INFO") as logs:
            handler.respond(200, {"message": "SECRET_ANSWER"})
        self.assertNotIn("SECRET", "\n".join(logs.output))
        self.assertFalse(timing_records(logs)[0]["write_ok"])

    def test_failed_parse_and_prepare_end_before_error_response_write(self):
        for stage, data, engine, status in (
                ("parse", request(message=""), Engine(), 400),
                ("prepare_tokenize", request(), Engine(context_limit=1), 422)):
            responded = threading.Event()
            original = server.Handler.respond

            def respond(handler, code, body):
                original(handler, code, body)
                responded.set()

            with self.subTest(stage=stage), serving(server.Service(engine)) as port, patch.object(
                    server.Handler, "respond", respond):
                with self.assertLogs("baseline.http", level="INFO") as logs:
                    self.assertEqual(call(port, data)[0], status)
                    self.assertTrue(responded.wait(5))
            rows = {r["stage"]: r for r in timing_records(logs)}
            self.assertLessEqual(rows[stage]["end_ns"], rows["response_write"]["start_ns"])
            self.assertEqual(rows["response_write"]["request_id"], data["request_id"])


if __name__ == "__main__":
    unittest.main()
