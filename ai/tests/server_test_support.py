"""Shared loopback server fixtures for AI tests."""

from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import inference
import server

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

    def encode(self, text, **kwargs):
        return [ord(c) for c in text]


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
def serving(service: server.Service, *, join_handlers: bool = False) -> Iterator[int]:
    httpd = server.Server(0, service)
    if join_handlers:
        httpd.daemon_threads = False
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
