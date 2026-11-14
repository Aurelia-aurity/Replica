"""CPU token observations through real inference, adapter and HTTP boundaries."""
from contextlib import nullcontext
from dataclasses import replace
import itertools
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from inference import Inference, Prepared, Request
from persona_adapter import PersonaInference
from persona_contract import parse_request
import request_timing
from request_timing import RequestTiming, TokenObserver, bind_timing
import server
from test_persona import PATH, request as persona_request
from test_request_timing import timing_records
from test_server import RecordingService, Tokenizer, call, request, serving


def decoder(tokens, *, failure_at=None, gate=None):
    """Replace only unavailable tensor/model boundaries; keep production decoding."""
    engine = Inference.__new__(Inference)
    engine.context_limit, engine.system_prompt = 100000, "system"
    engine.torch = Mock()
    engine.torch.inference_mode = nullcontext
    engine.generation_config = lambda **kw: SimpleNamespace(**kw)
    engine.tokenizer = Tokenizer()
    engine.tokenizer.eos_token_id = 2
    engine.tokenizer.decode = lambda ids, **kw: "".join(chr(t) for t in ids if t != 2)
    engine.model = Mock()
    engine.model.generation_config.eos_token_id = 2

    def generate(**kw):
        observer = kw.get("streamer")
        if observer:
            observer.put(SimpleNamespace(numel=lambda: 7))  # Initial prompt callback.
        if gate:
            gate[0].set()
            if not gate[1].wait(5):
                raise AssertionError("generation gate was not released")
        for index, token in enumerate(tokens):
            if failure_at == index:
                raise RuntimeError
            if observer:
                observer.put(SimpleNamespace(numel=lambda: 1))
        if observer:
            observer.end()
        output = MagicMock()
        output.__getitem__.return_value = SimpleNamespace(tolist=lambda: list(tokens))
        return output

    engine.model.generate.side_effect = generate
    return engine


class TokenTimingTests(unittest.TestCase):
    def test_prompt_is_excluded_and_first_observations_are_written_once(self):
        timing = RequestTiming()
        timing.now = Mock(side_effect=[20, 40])
        observer = TokenObserver(timing, 2)
        token = SimpleNamespace(numel=lambda: 1)
        observer.put(token)
        self.assertIsNone(observer.first_ns)
        for _ in range(6):
            observer.put(token)
        observer.end()
        self.assertEqual((observer.first_ns, observer.first_unforced_ns), (20, 40))
        self.assertEqual(timing.now.call_count, 2)

    def test_eos_prefix_budget_and_no_tokens_preserve_null_meanings(self):
        for tokens, prefix, available, unforced in (
                ([], (), False, False), ([2], (), True, True),
                ([11], (11, 12), True, False), ([11, 12, 2], (11, 12), True, True)):
            with self.subTest(tokens=tokens, prefix=prefix):
                timing = RequestTiming()
                engine = decoder(tokens)
                with bind_timing(timing):
                    engine.generate(Request("id", "question", (), 3, prefix), Prepared((9,), 0, 0))
                with self.assertLogs("baseline.http", level="INFO") as logs:
                    timing.emit("generate", 0)
                row = timing_records(logs)[0]
                self.assertEqual(row["ttft_available"], available)
                self.assertEqual(row["first_unforced_token_ns"] is not None, unforced)
                self.assertEqual(row["forced_prefix_tokens"], len(prefix))
                self.assertEqual(row["ttft_reason"],
                                 "observed_cpu_token" if available else "no_generated_token")
                if available:
                    self.assertLessEqual(row["model_generate_start_ns"],
                                         row["first_generated_token_ns"])
                    self.assertLessEqual(row["first_generated_token_ns"],
                                         row["model_generate_end_ns"])
                else:
                    self.assertIsNone(row["first_token_ms"])
                    self.assertIsNone(row["model_first_token_ms"])

    def test_streamer_preserves_output_options_and_disjoint_host_intervals(self):
        engine = decoder([11, 12, 65, 2])
        req, prepared = Request("id", "private", (), 160, (11, 12)), Prepared((9, 8), 0, 0)
        control = engine.generate(req, prepared)
        options = engine.model.generate.call_args.kwargs
        ticks = itertools.count(100, 10)
        with patch.object(request_timing.time, "monotonic_ns", side_effect=lambda: next(ticks)):
            timing = RequestTiming()
            with self.assertLogs("baseline.http", level="INFO") as logs, bind_timing(timing):
                measured = engine.generate(req, prepared)
                timing.emit("generate", 0)
        self.assertEqual(measured, control)
        observed = engine.model.generate.call_args.kwargs
        self.assertEqual({k: v for k, v in observed.items() if k != "streamer"}, options)
        self.assertEqual(engine.model.generate.call_count, 2)
        self.assertEqual(engine.torch.cuda.synchronize.call_count, 2)
        rows = timing_records(logs)
        spans = rows[:-1]
        self.assertEqual([r["stage"] for r in spans],
                         ["input_setup", "model_generate_call", "output_decode", "cuda_drain"])
        for before, after in zip(spans, spans[1:]):
            self.assertLessEqual(before["end_ns"], after["start_ns"])
        for span in spans:
            self.assertGreaterEqual(span["start_ns"], 0)
            self.assertLessEqual(span["start_ns"], span["end_ns"])
            self.assertLessEqual(rows[-1]["start_ns"], span["start_ns"])
            self.assertLessEqual(span["end_ns"], rows[-1]["end_ns"])
        row = rows[-1]
        self.assertEqual(row["first_token_ms"], row["first_generated_token_ns"] / 1e6)
        self.assertEqual(row["model_first_token_ms"],
                         (row["first_generated_token_ns"] - row["model_generate_start_ns"]) / 1e6)

    def test_generation_failure_closes_call_and_drains_without_decode(self):
        for fail_at in (0, 1):
            timing = RequestTiming()
            engine = decoder([65, 2], failure_at=fail_at)
            with self.subTest(fail_at=fail_at), self.assertLogs("baseline.http", level="INFO") as logs:
                with bind_timing(timing), self.assertRaises(RuntimeError):
                    engine.generate(Request("id", "private", (), 3), Prepared((9,), 0, 0))
                timing.emit("generate", 0)
            rows = timing_records(logs)
            self.assertEqual([r["stage"] for r in rows],
                             ["input_setup", "model_generate_call", "cuda_drain", "generate"])
            self.assertEqual(rows[-1]["ttft_available"], fail_at == 1)
            self.assertIsNotNone(rows[-1]["model_generate_end_ns"])
            engine.torch.cuda.synchronize.assert_called_once_with(0)

    def test_history_retries_have_separate_template_spans_in_both_modes(self):
        for persona in (False, True):
            engine = decoder([65, 2])
            selected = PersonaInference(engine, "test") if persona else engine
            req = parse_request(persona_request()) if persona else server.parse_request(request())
            req = replace(req, history=(("old", "answer"), ("new", "reply")))
            keep = replace(req, history=req.history[1:])
            engine.context_limit = len(selected.prepare(keep).token_ids) + req.max_new_tokens
            timing = RequestTiming()
            with self.subTest(persona=persona), self.assertLogs("baseline.http", level="INFO") as logs:
                with bind_timing(timing), timing.span("prepare_tokenize"):
                    prepared = selected.prepare(req)
            rows = timing_records(logs)
            attempts = [r for r in rows if r["stage"] == "template_tokenize"]
            self.assertEqual(len(attempts), 2)
            self.assertEqual(prepared.dropped_history_turns, 1)
            self.assertLessEqual(attempts[-1]["end_ns"], rows[-1]["end_ns"])
            self.assertLessEqual(sum(r["end_ns"] - r["start_ns"] for r in attempts),
                                 rows[-1]["end_ns"] - rows[-1]["start_ns"])
            self.assertIsNone(request_timing.CURRENT.get())

    def test_http_adapter_metadata_excludes_content_and_is_request_local(self):
        secret = "PRIVATE_PROMPT_PROFILE_EVIDENCE_ANSWER"
        engine = decoder([ord(c) for c in json.dumps(
            {"message": secret, "evidence_used": []})] + [2])
        service = RecordingService(engine, persona_generator=PersonaInference(engine, "test"))
        data = persona_request()
        data["message"] = data["persona"]["display_name"] = data["evidence"][0]["content"] = secret
        with serving(service) as port, self.assertLogs("baseline.http", level="INFO") as logs:
            self.assertEqual(call(port, data, path=PATH)[0], 200)
            self.assertTrue(service.job.done.wait(5))
            first = service.job.timing
            service.persona_generator = PersonaInference(decoder([]), "test")
            self.assertEqual(call(port, data, path=PATH)[0], 500)
            self.assertTrue(service.job.done.wait(5))
            second = service.job.timing
        rows = timing_records(logs)
        required = {"persona_prompt_assembly", "prompt_assembly", "template_tokenize",
                    "prefix_encode", "input_setup", "model_generate_call",
                    "output_decode", "cuda_drain", "adapter_parse_guard", "success_body"}
        self.assertTrue(required <= {r["stage"] for r in rows})
        self.assertNotEqual(first.attempt_id, second.attempt_id)
        terminals = [r for r in rows if r["stage"] == "generate"]
        self.assertEqual([r["ttft_available"] for r in terminals], [True, False])
        self.assertEqual(terminals[0]["forced_prefix_tokens"], len('{"message":'))
        self.assertNotIn(secret, "\n".join(logs.output))

    def test_http_deadline_retains_later_first_token_and_slot_until_worker_done(self):
        entered, release = threading.Event(), threading.Event()
        response_logged = threading.Event()
        emit = RequestTiming.emit

        def observe(timing, stage, *args, **kwargs):
            emit(timing, stage, *args, **kwargs)
            if stage == "response_write":
                response_logged.set()

        engine = decoder([65, 2], gate=(entered, release))
        service = RecordingService(engine, deadline_seconds=0.01)
        with serving(service) as port, self.assertLogs(
                "baseline.http", level="INFO") as logs, patch.object(RequestTiming, "emit", observe):
            try:
                self.assertEqual(call(port, request())[0], 504)
                self.assertTrue(entered.wait(5))
                self.assertTrue(response_logged.wait(5), "response_write was not logged")
                self.assertEqual(service.active_id, request()["request_id"])
                response = next(r for r in timing_records(logs) if r["stage"] == "response_write")
                self.assertFalse(response["ttft_available"])
            finally:
                release.set()
                if service.job is not None:
                    self.assertTrue(service.job.done.wait(5))
        worker = next(r for r in timing_records(logs) if r["stage"] == "generate")
        self.assertEqual(worker["attempt_id"], response["attempt_id"])
        self.assertTrue(worker["ttft_available"])
        self.assertLessEqual(response["end_ns"], worker["first_generated_token_ns"])
