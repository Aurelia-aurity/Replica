"""GPU-free tests of the new measurement wrapper and failure accounting."""
import io
import json
import logging
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gpu_server
import measure_gpu
from analyze_timing import analyze, join_timing
from measure_local import DoubleEngine, DoublePersona
import server


class Tests(unittest.TestCase):
    def test_server_telemetry_failure_retains_completed_attempt_and_aborts(self):
        unreadable = OSError("PRIVATE_SERVER_LOG")
        corrupt = json.JSONDecodeError("PRIVATE_SERVER_LOG", "{", 0)
        malformed = [{"event": "request_timing"}]
        cases = [
            ([unreadable, unreadable], "log_read", "OSError", "OSError", 1),
            ([corrupt, corrupt], "log_read", "JSONDecodeError", "JSONDecodeError", 1),
            ([unreadable, corrupt], "log_read", "JSONDecodeError", "OSError", 1),
            ([[], unreadable], "log_read", "OSError", "OSError", 0),
            ([malformed, malformed], "join", "KeyError", "KeyError", 0),
        ]
        for reader_results, stage, error_type, abort_type, drain_failures in cases:
            with self.subTest(stage=stage, error=error_type), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                rows = measure_gpu.schedule()
                schedule = directory / "schedule.json"
                schedule.write_text(json.dumps(rows))
                outdir = directory / "results"
                completed = dict(rows[0], event="client_attempt", outcome="http", status=200,
                                 send_ns=10, complete_ns=20)
                process = Mock(returncode=-15)
                process.poll.return_value = None
                argv = ["measure_gpu", "--schedule", str(schedule), "--output-dir",
                        str(outdir), "--double"]
                with patch.object(sys, "argv", argv), patch.object(
                        measure_gpu.subprocess, "Popen", return_value=process), patch.object(
                            measure_gpu, "wait_ready", return_value=(1234, None)), patch.object(
                                measure_gpu, "attempt", return_value=completed), patch.object(
                                    measure_gpu, "get", return_value=(200, {"idle": True})), patch.object(
                                        measure_gpu, "read_events", side_effect=reader_results):
                    self.assertEqual(measure_gpu.main(), 1)
                raw = (outdir / "client.jsonl").read_text()
                events = [json.loads(line) for line in raw.splitlines()]
                attempts = [r for r in events if r["event"] == "client_attempt"]
                self.assertEqual(len(attempts), 1)
                self.assertEqual((attempts[0]["request_id"], attempts[0]["status"]),
                                 (rows[0]["request_id"], 200))
                self.assertEqual(attempts[0]["outcome"], "http")
                self.assertIsNone(attempts[0]["first_token_ms"])
                self.assertFalse(attempts[0]["ttft_available"])
                self.assertEqual(attempts[0]["ttft_reason"], "server_timing_unavailable")
                failures = [r for r in events if r["event"] == "server_timing_failure"]
                self.assertEqual(len(failures), 1)
                self.assertEqual((failures[0]["stage"], failures[0]["exception_type"]),
                                 (stage, error_type))
                self.assertEqual(sum(r["event"] == "drain_failure" for r in events), drain_failures)
                self.assertEqual((events[-1]["outcome"], events[-1]["exception_type"]),
                                 ("aborted", abort_type))
                self.assertNotIn("PRIVATE_SERVER_LOG", raw)

    def test_drain_failure_retains_completed_attempt_and_aborted_outcome(self):
        for error in (RuntimeError(), OSError("PRIVATE_DRAIN_FAILURE")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                schedule = directory / "schedule.json"
                rows = measure_gpu.schedule()
                schedule.write_text(json.dumps(rows))
                outdir = directory / "results"
                completed = dict(rows[0], event="client_attempt", outcome="transport_unknown",
                                 status=None, censored=True, send_ns=10, complete_ns=20)
                process = Mock(returncode=-15)
                process.poll.return_value = None
                argv = ["measure_gpu", "--schedule", str(schedule), "--output-dir",
                        str(outdir), "--double"]
                with patch.object(sys, "argv", argv), patch.object(
                        measure_gpu.subprocess, "Popen", return_value=process), patch.object(
                            measure_gpu, "wait_ready", return_value=(1234, None)), patch.object(
                                measure_gpu, "attempt", return_value=completed), patch.object(
                                    measure_gpu, "drain", side_effect=error):
                    self.assertEqual(measure_gpu.main(), 1)
                raw = (outdir / "client.jsonl").read_text()
                events = [json.loads(line) for line in raw.splitlines()]
                attempts = [r for r in events if r["event"] == "client_attempt"]
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0]["request_id"], rows[0]["request_id"])
                self.assertEqual(attempts[0]["outcome"], "transport_unknown")
                self.assertTrue(attempts[0]["censored"])
                self.assertIsNone(attempts[0]["first_token_ms"])
                self.assertEqual(attempts[0]["ttft_reason"], "no_worker_terminal")
                failures = [r for r in events if r["event"] == "drain_failure"]
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]["exception_type"], type(error).__name__)
                self.assertEqual(events[-1]["outcome"], "aborted")
                self.assertEqual(sum(r["event"] == "process_stop" for r in events), 1)
                self.assertNotIn("PRIVATE_DRAIN_FAILURE", raw)

    def test_join_aggregates_repeated_calls_and_preserves_missing_worker(self):
        base = dict(event="request_timing", attempt_id="attempt", request_id=None,
                    stage="template_tokenize", start_ns=10, end_ns=20)
        terminal = dict(base, request_id="request", stage="generate", start_ns=30, end_ns=90,
                        first_token_ms=.00005, ttft_available=True,
                        first_generated_token_ns=50, first_unforced_token_ns=60,
                        model_generate_start_ns=40, model_generate_end_ns=80,
                        model_first_token_ms=.00001, forced_prefix_tokens=2,
                        ttft_reason="observed_cpu_token")
        metadata = dict(event="client_attempt", phase="matrix", arm="B", evidence_count=3,
                        concurrency=1, status=200, measurement_kind="double")
        result = join_timing(dict(metadata, request_id="request"),
                             [base, dict(base, start_ns=21, end_ns=24), terminal])
        self.assertEqual(result["server_spans"]["template_tokenize"],
                         {"count": 2, "duration_ns": 13})
        self.assertEqual(result["first_generated_token_ns"], 50)
        rejected = join_timing(dict(metadata, status=429, request_id="other"), [terminal])
        self.assertIsNone(rejected["first_token_ms"])
        self.assertFalse(rejected["ttft_available"])
        self.assertEqual(rejected["ttft_reason"], "no_worker_terminal")
        cells = analyze([result, rejected])["cells"]
        self.assertEqual([cell["ttft_missing"] for cell in cells], [0, 1])
        self.assertEqual(cells[0]["spans"]["template_tokenize"]["occurrences"], 2)
        self.assertEqual(cells[1]["token_metrics"]["first_token_ms"],
                         {"n": 0, "p50": None, "p95": None, "maximum": None})

    def test_frozen_schedule(self):
        rows = measure_gpu.schedule()
        self.assertEqual(len(rows), 851)
        self.assertEqual(len({r["request_id"] for r in rows}), 851)
        self.assertEqual(rows, measure_gpu.schedule())
        self.assertEqual("".join(r["arm"] for r in rows[:10]), "ABBAABBAAB")
        matrix = [r for r in rows if r["phase"] == "matrix"]
        self.assertEqual(len(matrix), 840)
        for k in (0, 3, 6, 10):
            for c in (1, 2, 4):
                self.assertEqual(sum(r["evidence_count"] == k and r["concurrency"] == c
                                     for r in matrix), 30 * c)

    def test_failed_http_attempt_is_retained_without_payload(self):
        class Failure(DoublePersona):
            def generate(self, request, prepared):
                raise ValueError("PRIVATE_ANSWER_SENTINEL")
        output = io.StringIO()
        events = gpu_server.Events(output)
        logger = logging.getLogger("baseline.http")
        previous, propagate, level = logger.handlers[:], logger.propagate, logger.level
        logger.handlers, logger.propagate, logger.level = [events], False, logging.INFO
        service = gpu_server.ObservedService(events)
        service.engine, service.persona_generator = DoubleEngine(), Failure()
        service.ready.set()
        try:
            with server.Server(0, service) as httpd:
                thread = threading.Thread(target=httpd.serve_forever,
                                          kwargs={"poll_interval": 0.01})
                thread.start()
                try:
                    row = measure_gpu.schedule()[0]
                    result = measure_gpu.attempt(httpd.server_port, row, threading.Barrier(1))
                    self.assertEqual(result["status"], 500)
                    self.assertEqual(result["outcome"], "http")
                    self.assertNotIn("message", result)
                    self.assertNotIn("PRIVATE_ANSWER_SENTINEL", output.getvalue())
                    self.assertNotIn("Synthetic Rowan", output.getvalue())
                    self.assertEqual(len(service.jobs), 1)
                    self.assertTrue(service.jobs[0].done.wait(1))
                finally:
                    httpd.shutdown()
                    thread.join(1)
        finally:
            logger.handlers, logger.propagate, logger.level = previous, propagate, level


def validate(directory):
    rows = [json.loads(line) for line in (directory / "client.jsonl").read_text().splitlines()]
    attempts = [r for r in rows if r["event"] == "client_attempt"]
    assert len(attempts) == 851
    assert len({r["request_id"] for r in attempts}) == 851
    assert all(r["status"] in (200, 429) for r in attempts)
    assert all(r["contract_valid"] for r in attempts if r["status"] == 200)
    for row in attempts:
        assert row["measurement_kind"] == "double"
        assert row["ttft_available"] == (row["status"] == 200)
        if row["status"] == 200:
            assert (row["model_generate_start_ns"] <= row["first_generated_token_ns"]
                    <= row["first_unforced_token_ns"] <= row["model_generate_end_ns"])
            assert row["forced_prefix_tokens"] == len('{"message":')
            for stage in ("template_tokenize", "prefix_encode", "model_generate_call",
                          "output_decode", "cuda_drain", "adapter_parse_guard", "success_body"):
                assert row["server_spans"][stage]["count"] == 1
        else:
            assert row["first_token_ms"] is None
    assert sum(r["event"] == "wave_terminal" for r in rows) == 371
    assert sum(r["event"] == "process_stop" for r in rows) == 11
    assert rows[-1]["outcome"] == "schedule_exhausted"
    for path in directory.glob("*.jsonl"):
        raw = path.read_text()
        assert "Synthetic answer." not in raw and "Synthetic Rowan" not in raw
        assert "walking by the river" not in raw
    for path in directory.glob("*.server.jsonl"):
        events = [json.loads(line) for line in path.read_text().splitlines()]
        admitted = {r["request_id"] for r in events
                    if r["event"] == "admission" and r["admitted"]}
        terminal = {r["request_id"] for r in events if r["event"] == "worker_terminal"}
        assert admitted == terminal
    print(json.dumps({"event": "driver_validation", "attempts": 851,
                      "waves": 371, "processes": 11, "outcome": "PASS"}))


if __name__ == "__main__":
    if len(sys.argv) == 2:
        validate(Path(sys.argv[1]))
    else:
        unittest.main()
