"""GPU-free real HTTP checks, with event-controlled generation and tokenization."""
from contextlib import contextmanager
from http.client import HTTPConnection
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import inference
import server

FIXTURES = Path(__file__).parent / "fixtures" / "contract_baseline"
REQUEST_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"


def request(**changes):
    return {"request_id": REQUEST_ID, "message": "synthetic question",
            "history": [], "max_new_tokens": 160, **changes}


class Tokenizer:
    """Each code point and role marker is a token, plus a generation marker."""
    def __init__(self):
        self.calls = []

    def apply_chat_template(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return [ord(c) for m in messages for c in ("!" + m["content"])] + [9]


class Engine(inference.Inference):
    def __init__(self, context_limit=100000):
        self.context_limit = context_limit
        self.system_prompt = "system"
        self.tokenizer = Tokenizer()
        self.answer = "synthetic answer"
        self.reason = "stop"
        self.failure = False
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def generate(self, req, prepared):
        self.calls += 1
        self.entered.set()
        if self.block and not self.release.wait(5):
            raise RuntimeError("test gate was never released")
        if self.failure:
            raise RuntimeError("SECRET private prompt/path")
        return inference.Generated(self.answer, self.reason, 1)


class RecordingService(server.Service):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accepted = threading.Event()
        self.job = None

    def accept(self, req, prepared):
        status, job = super().accept(req, prepared)
        if job is not None:
            self.job = job
            self.accepted.set()
        return status, job


@contextmanager
def serving(service):
    httpd = server.Server(0, service)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield httpd.server_port
    finally:
        if service.engine is not None and hasattr(service.engine, "release"):
            service.engine.release.set()
        httpd.shutdown()
        thread.join(5)
        httpd.server_close()
        if thread.is_alive():
            raise AssertionError("HTTP server failed to stop")


def call(port, data=None, raw=None, headers=None, method="POST", path="/v1/baseline/generate"):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        body = raw if raw is not None else json.dumps(data).encode()
        connection.request(method, path, body=body,
                           headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


class ServerTests(unittest.TestCase):
    def assert_error(self, result, status, request_id=REQUEST_ID):
        self.assertEqual(result, (status, server.error_body(status, request_id)))

    def test_contract_examples_over_real_http(self):
        for file in sorted(FIXTURES.glob("*.json")):
            fixture = json.loads(file.read_text())
            if fixture["transport_outcome"] == "unknown":
                continue  # Exercised by the disconnect test, not a fabricated 504.
            with self.subTest(example=file.name):
                engine = Engine()
                conditions = fixture["conditions"]
                response = fixture["response"]
                if fixture["http_status"] == 200:
                    engine.answer = response["message"]
                    engine.reason = response["finish_reason"]
                engine.failure = conditions.get("inference_failed", False)
                if conditions.get("blank_output"):
                    engine.answer = " \n\t"
                if conditions.get("dropped_history_turns"):
                    req = server.parse_request(fixture["request"])
                    kept = inference.Request(req.request_id, req.message,
                                             req.history[1:], req.max_new_tokens)
                    engine.context_limit = len(engine.prepare(kept).token_ids) + req.max_new_tokens
                if conditions.get("context_fits_without_history") is False:
                    engine.context_limit = 1
                auth_status = {"unauthenticated": 401, "forbidden": 403}.get(conditions.get("service_auth"))
                auth = Mock(return_value=auth_status)
                service = RecordingService(engine, auth=auth)
                if conditions.get("model_ready") is False:
                    service.ready.clear()
                if conditions.get("slot_occupied"):
                    service.active_id = REQUEST_ID if conditions.get("active_duplicate") else OTHER_ID
                if conditions.get("deadline_exceeded"):
                    service.deadline_seconds = 0.01
                    engine.block = True
                with serving(service) as port:
                    raw = fixture["request"].encode() if isinstance(fixture["request"], str) else None
                    headers = {}
                    if conditions.get("raw_body_bytes"):
                        # No body read is needed to reject an oversized declared frame.
                        headers["Content-Length"] = str(conditions["raw_body_bytes"])
                        raw = b""
                    self.assertEqual(call(port, fixture["request"], raw, headers),
                                     (fixture["http_status"], response))
                if conditions.get("raw_body_bytes"):
                    auth.assert_not_called()

    def test_raw_limit_before_auth_and_parse(self):
        auth = Mock(return_value=401)
        with serving(server.Service(auth=auth)) as port:
            self.assert_error(call(port, raw=b"", headers={"Content-Length": "262145"}), 413, None)
        auth.assert_not_called()

    def test_auth_before_parse_without_id_extraction(self):
        for status in (401, 403):
            with self.subTest(status=status), serving(
                    server.Service(auth=lambda headers: status)) as port:
                with patch.object(server.json, "loads", wraps=json.loads) as loads:
                    # Don't wrap the client-side decoder in this process.
                    c = HTTPConnection("127.0.0.1", port, timeout=5)
                    try:
                        c.request("POST", "/v1/baseline/generate", body=b"{broken")
                        response = c.getresponse()
                        payload = response.read()
                        loads.assert_not_called()
                        self.assertEqual(response.status, status)
                    finally:
                        c.close()
                self.assertEqual(json.loads(payload), server.error_body(status))

    def test_structural_errors_before_parsed_limits_and_readiness(self):
        bad = [
            request(message="x" * 16001, extra=True),
            request(history=[{"user": "x" * 16001, "assistant": "a", "extra": 1}]),
            request(history=[{"user": "x", "assistant": "a"}] * 20 + [{"user": ""}]),
            request(max_new_tokens=True), request(max_new_tokens="160"),
            request(max_new_tokens=0), request(max_new_tokens=1025),
            request(max_new_tokens=1.5), request(message="\u2003\t\n"),
            request(history=[{"user": "x", "assistant": " "}]),
            request(history={}), request(history=[None]), request(message=12),
            {k: v for k, v in request().items() if k != "message"},
        ]
        with serving(server.Service()) as port:
            for data in bad:
                with self.subTest(data_type=type(data).__name__):
                    self.assert_error(call(port, data), 400)

    def test_parsed_limits_before_ready_and_context(self):
        with serving(server.Service()) as port:
            for data in (request(message="x" * 16001),
                         request(history=[{"user": "x", "assistant": "y"}] * 21),
                         request(history=[{"user": "x", "assistant": "y" * 16001}])):
                self.assert_error(call(port, data), 413)
            self.assert_error(call(port, request()), 503)

    def test_ready_before_context_before_duplicate_or_busy(self):
        engine = Engine(context_limit=1)
        service = server.Service(engine)
        service.active_id = REQUEST_ID
        with serving(service) as port:
            service.ready.clear()
            self.assert_error(call(port, request()), 503)
            service.ready.set()
            self.assert_error(call(port, request()), 422)
            self.assert_error(call(port, request(request_id=OTHER_ID)), 422, OTHER_ID)
        self.assertEqual(engine.calls, 0)

    def test_id_echo_and_null_boundaries(self):
        with serving(server.Service()) as port:
            for value in (None, "NOT-A-UUID", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
                          REQUEST_ID + "\n", 1):
                self.assert_error(call(port, request(request_id=value)), 400, None)
            self.assert_error(call(port, raw=b"{broken"), 400, None)
            self.assert_error(call(port, raw=b"\xff"), 400, None)
            self.assert_error(call(port, data=[]), 400, None)
            self.assert_error(call(port, request(message="")), 400)
            self.assert_error(call(port, request(message="x" * 16001)), 413)

    def test_raw_limit_is_bytes_and_boundary_is_inclusive(self):
        with serving(server.Service()) as port:
            raw = json.dumps(request()).encode()
            self.assert_error(call(port, raw=raw + b" " * (262144 - len(raw))), 503)
            # Declared byte count, not decoded character count.
            self.assert_error(call(port, raw=b"", headers={"Content-Length": "262145"}), 413, None)

    def test_length_boundaries_and_integral_json_numbers(self):
        with serving(server.Service(Engine())) as port:
            for budget in (1, 1024, 160.0):
                self.assertEqual(call(port, request(max_new_tokens=budget))[0], 200)
            self.assertEqual(call(port, request(message="\U0001f600" * 16000))[0], 200)
            self.assert_error(call(port, request(message="\U0001f600" * 16001)), 413)
            self.assertEqual(call(port, request(history=[{"user": "a", "assistant": "b"}] * 20))[0], 200)

    def test_disconnect_keeps_compute_slot_and_id_until_completion(self):
        fixture = json.loads((FIXTURES / "16-transport-timeout.json").read_text())
        engine = Engine()
        engine.block = True
        service = RecordingService(engine)
        with serving(service) as port:
            c = HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", "/v1/baseline/generate",
                      body=json.dumps(fixture["request"]), headers={"Content-Type": "application/json"})
            self.assertTrue(service.accepted.wait(5))
            c.close()  # UNKNOWN at caller; no envelope received or fabricated.
            self.assertIsNone(fixture["response"])
            self.assertIsNone(fixture["http_status"])
            self.assert_error(call(port, fixture["request"]), 409)
            self.assert_error(call(port, request(request_id=OTHER_ID)), 429, OTHER_ID)
            engine.release.set()
            self.assertTrue(service.job.done.wait(5))
            self.assertEqual(call(port, fixture["request"])[0], 200)
        self.assertEqual(engine.calls, 2)

    def test_deadline_keeps_compute_slot_and_id_until_completion(self):
        engine = Engine()
        engine.block = True
        service = RecordingService(engine, deadline_seconds=0.01)
        with serving(service) as port:
            self.assert_error(call(port, request()), 504)
            self.assertTrue(engine.entered.wait(5))
            self.assert_error(call(port, request()), 409)
            self.assert_error(call(port, request(request_id=OTHER_ID)), 429, OTHER_ID)
            engine.release.set()
            self.assertTrue(service.job.done.wait(5))
            service.deadline_seconds = None
            self.assertEqual(call(port, request())[0], 200)

    def test_simultaneous_accept_reserves_exactly_one_generation(self):
        for same_id in (True, False):
            with self.subTest(same_id=same_id):
                engine = Engine()
                engine.block = True
                service = RecordingService(engine)
                gate = threading.Barrier(3)
                results = []
                rejected = threading.Event()
                original_respond = server.Handler.respond
                def respond(handler, status, body):
                    original_respond(handler, status, body)
                    if status in (409, 429):
                        rejected.set()
                with serving(service) as port, patch.object(server.Handler, "respond", respond):
                    def send(req):
                        gate.wait(timeout=5)
                        results.append(call(port, req)[0])
                    threads = [threading.Thread(target=send, args=(request(),)),
                               threading.Thread(target=send, args=(
                                   request(request_id=REQUEST_ID if same_id else OTHER_ID),))]
                    for thread in threads:
                        thread.start()
                    gate.wait(timeout=5)
                    self.assertTrue(service.accepted.wait(5))
                    self.assertTrue(rejected.wait(5))
                    engine.release.set()
                    for thread in threads:
                        thread.join(5)
                        self.assertFalse(thread.is_alive())
                self.assertEqual(sorted(results), [200, 409 if same_id else 429])
                self.assertEqual(engine.calls, 1)

    def test_atomic_accept_checks_id_before_slot(self):
        engine = Engine()
        engine.block = True
        service = server.Service(engine)
        req = server.parse_request(request())
        prepared = engine.prepare(req)
        gate = threading.Barrier(3)
        results = []
        def accept():
            gate.wait(timeout=5)
            results.append(service.accept(req, prepared))
        threads = [threading.Thread(target=accept) for _ in range(2)]
        try:
            for thread in threads:
                thread.start()
            gate.wait(timeout=5)
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(sorted(status for status, _ in results), [200, 409])
            other = server.parse_request(request(request_id=OTHER_ID))
            self.assertEqual(service.accept(other, prepared), (429, None))
            self.assertEqual(engine.calls, 1)
        finally:
            engine.release.set()
            for _, job in results:
                if job is not None:
                    self.assertTrue(job.done.wait(5))

    def test_empty_and_failed_output_release_slot_without_leaking(self):
        for answer in ("", " \t\n", "failure"):
            engine = Engine()
            engine.answer = answer
            engine.failure = answer == "failure"
            service = server.Service(engine)
            with serving(service) as port, self.assertLogs("baseline.http", level="INFO") as logs:
                self.assert_error(call(port, request()), 500)
                self.assertIsNone(service.active_id)
            self.assertNotIn("SECRET", "".join(logs.output))
            self.assertNotIn("synthetic question", "".join(logs.output))

    def test_background_loading_and_readiness(self):
        service = server.Service()
        entered, release = threading.Event(), threading.Event()
        engine = Engine()
        def factory():
            entered.set()
            if not release.wait(5):
                raise RuntimeError
            return engine
        try:
            with serving(service) as port:
                service.start_loading(factory)
                self.assertTrue(entered.wait(5))
                self.assertEqual(call(port, method="GET", path="/healthz"), (200, {"ready": False}))
                self.assertEqual(call(port, method="GET", path="/readyz"), (503, {"ready": False}))
                self.assert_error(call(port, request()), 503)
                release.set()
                self.assertTrue(service.ready.wait(5))
                self.assertEqual(call(port, method="GET", path="/readyz"), (200, {"ready": True}))
                with self.assertRaises(RuntimeError):
                    service.start_loading(factory)
        finally:
            release.set()
            service.loader.join(5)

    def test_load_failure_stays_unready_without_raw_exception(self):
        service = server.Service()
        with self.assertLogs("baseline.http", level="INFO") as logs:
            service.start_loading(Mock(side_effect=RuntimeError("SECRET local model path")))
            service.loader.join(5)
        self.assertFalse(service.ready.is_set())
        self.assertNotIn("SECRET", "".join(logs.output))

    def test_no_default_deadline_and_invalid_configuration(self):
        self.assertIsNone(server.Service().deadline_seconds)
        for deadline in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                server.Service(deadline_seconds=deadline)

    def test_loopback_binding_and_unsupported_transport(self):
        with server.Server(0, server.Service()) as httpd:
            self.assertEqual(httpd.server_address[0], "127.0.0.1")
        with serving(server.Service()) as port:
            self.assert_error(call(port, request(), headers={"Content-Encoding": "gzip"}), 400, None)
            self.assert_error(call(port, request(), headers={"Transfer-Encoding": "chunked"}), 400, None)
            self.assert_error(call(port, request(), headers={"Content-Type": "text/plain"}), 400)

    def test_smoke_client_drives_all_cases_on_real_loopback_http(self):
        spec = importlib.util.spec_from_file_location(
            "smoke_baseline", Path(__file__).parents[1] / "scripts" / "smoke_baseline.py")
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
        engine = Engine(context_limit=4096)
        original_generate = engine.generate
        def generate(req, prepared):
            engine.reason = "length" if req.max_new_tokens == 8 else "stop"
            engine.block = req.max_new_tokens == 1024
            return original_generate(req, prepared)
        engine.generate = generate
        original_respond = server.Handler.respond
        rejected = set()
        lock = threading.Lock()
        def respond(handler, status, body):
            original_respond(handler, status, body)
            if status in (409, 429):
                with lock:
                    rejected.add(status)
                    if rejected == {409, 429}:
                        engine.release.set()
        # Wait for actual acceptance before the client's duplicate/busy submissions,
        # rather than relying on TCP send completion or GPU timing.
        service = RecordingService(engine)
        original_send = smoke.send
        acceptance_signals = []
        def send(*args, **kwargs):
            args = list(args)
            if len(args) > 3 and args[3] is not None:
                client_sent = args[3]
                args[3] = None
                def notify():
                    acceptance_signals.append(service.accepted.wait(5))
                    client_sent.set()
                service.accepted.clear()
                notifier = threading.Thread(target=notify)
                notifier.start()
                try:
                    return original_send(*args, **kwargs)
                finally:
                    notifier.join(5)
            return original_send(*args, **kwargs)
        from contextlib import redirect_stdout
        output = io.StringIO()
        with serving(service) as port, patch.object(server.Handler, "respond", respond), patch.object(
                smoke, "send", send), redirect_stdout(output):
            self.assertEqual(smoke.main(["--port", str(port)]), 0)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(records), 10)
        self.assertEqual(records[-1], {"failures": 0, "inconclusive": 0})
        self.assertEqual(rejected, {409, 429})
        self.assertEqual(acceptance_signals, [True])


class InferenceTests(unittest.TestCase):
    def test_trimming_uses_complete_pairs_and_generation_template(self):
        engine = Engine()
        req = server.parse_request(request(history=[
            {"user": "old", "assistant": "old answer"},
            {"user": "new", "assistant": "new answer"}]))
        kept = inference.Request(req.request_id, req.message, req.history[1:], req.max_new_tokens)
        engine.context_limit = len(engine.prepare(kept).token_ids) + req.max_new_tokens
        engine.tokenizer.calls.clear()
        prepared = engine.prepare(req)
        self.assertEqual((prepared.used_history_turns, prepared.dropped_history_turns), (1, 1))
        self.assertEqual(len(prepared.token_ids) + req.max_new_tokens, engine.context_limit)
        messages, kwargs = engine.tokenizer.calls[-1]
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
        self.assertEqual([m["content"] for m in messages[1:-1]], list(req.history[1]))
        self.assertEqual(messages[-1]["content"], req.message)
        self.assertEqual(kwargs, {"tokenize": True, "add_generation_prompt": True, "enable_thinking": False})
        engine.context_limit = len(engine.prepare(kept).token_ids) + req.max_new_tokens - 1
        prepared = engine.prepare(req)
        self.assertEqual((prepared.used_history_turns, prepared.dropped_history_turns), (0, 2))
        engine.context_limit = req.max_new_tokens
        with self.assertRaises(inference.ContextLimitExceeded):
            engine.prepare(req)

    def test_effective_context_uses_model_tokenizer_and_optional_cap(self):
        for tokenizer_limit, cap, expected in ((2048, None, 2048), (10**30, None, 4096),
                                                (2048, 512, 512), (2048, 9999, 2048)):
            model = Mock()
            model.config.max_position_embeddings = 4096
            tokenizer = Mock(model_max_length=tokenizer_limit)
            with patch.object(inference.baseline, "load_runtime",
                              return_value=(Mock(), Mock(), tokenizer, model)) as load:
                engine = inference.Inference({"system_prompt": "system"}, cap)
            self.assertEqual(engine.context_limit, expected)
            load.assert_called_once()

    def test_real_adapter_greedy_finish_reason_and_new_token_decode(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        for tokens, expected in (([8, 9], "length"), ([8, 2], "stop"), ([2], "stop")):
            engine = inference.Inference.__new__(inference.Inference)
            engine.torch = Mock()
            engine.torch.inference_mode = nullcontext
            engine.generation_config = lambda **kw: SimpleNamespace(**kw)
            engine.model = Mock()
            engine.model.generation_config.eos_token_id = [1, 2]
            output = Mock()
            output.__getitem__ = Mock(return_value=SimpleNamespace(tolist=lambda: tokens))
            engine.model.generate.return_value = output
            engine.tokenizer = Mock(eos_token_id=2)
            engine.tokenizer.decode.return_value = "answer"
            req = server.parse_request(request(max_new_tokens=2))
            result = engine.generate(req, inference.Prepared((10, 11, 12), 0, 0))
            self.assertEqual(result.finish_reason, expected)
            self.assertEqual(result.output_tokens, len(tokens))
            output.__getitem__.assert_called_once_with((0, slice(3, None)))
            engine.tokenizer.decode.assert_called_once_with(tokens, skip_special_tokens=True)
            config = engine.model.generate.call_args.kwargs["generation_config"]
            self.assertFalse(engine.model.generate.call_args.kwargs["use_model_defaults"])
            self.assertFalse(config.do_sample)
            self.assertEqual(config.max_new_tokens, 2)
            self.assertEqual(config.eos_token_id, [1, 2])
            engine.torch.cuda.synchronize.assert_called_once_with(0)

    def test_generation_failure_synchronizes_before_slot_can_be_returned(self):
        from contextlib import nullcontext
        engine = inference.Inference.__new__(inference.Inference)
        engine.torch = Mock()
        engine.torch.inference_mode = nullcontext
        engine.model = Mock()
        engine.model.generate.side_effect = RuntimeError("SECRET")
        engine.generation_config = Mock()
        engine.tokenizer = Mock()
        with self.assertRaises(RuntimeError):
            engine.generate(server.parse_request(request()), inference.Prepared((1, 2), 0, 0))
        engine.torch.cuda.synchronize.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
