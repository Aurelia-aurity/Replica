"""Metadata-only local monotonic spans; no token observer or request queue."""
from contextlib import contextmanager
import json
import logging
import time
import uuid

LOGGER = logging.getLogger("baseline.http")


class RequestTiming:
    def __init__(self):
        self.origin = time.monotonic_ns()
        self.attempt_id = str(uuid.uuid4())
        self.request_id = None

    def now(self):
        return time.monotonic_ns() - self.origin

    @contextmanager
    def span(self, stage):
        start = self.now()
        try:
            yield
        finally:
            self.emit(stage, start)

    def emit(self, stage, start_ns, *, status=None, write_ok=None,
             input_tokens=None, output_tokens=None):
        LOGGER.info(json.dumps({
            "schema_version": 1, "event": "request_timing",
            "attempt_id": self.attempt_id, "request_id": self.request_id,
            "stage": stage, "start_ns": start_ns, "end_ns": self.now(),
            "status": status, "write_ok": write_ok,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "queue_present": False, "queue_wait_ms": None,
            "first_token_ms": None, "ttft_available": False,
            "ttft_reason": "non_streaming_generate",
        }, separators=(",", ":")))


def startup_event(stage, origin, *, status):
    LOGGER.info(json.dumps({
        "schema_version": 1, "event": "startup_timing", "stage": stage,
        "elapsed_ns": time.monotonic_ns() - origin, "status": status,
    }, separators=(",", ":")))
