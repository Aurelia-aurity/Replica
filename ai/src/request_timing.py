"""Metadata-only local monotonic spans and CPU token observations; no queue."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import json
import logging
import time
import uuid

LOGGER = logging.getLogger("baseline.http")
CURRENT: ContextVar["RequestTiming | None"] = ContextVar("request_timing", default=None)


@contextmanager
def bind_timing(timing):
    """Bind explicitly in each handler/worker thread, then restore its context."""
    token = CURRENT.set(timing)
    try:
        yield
    finally:
        CURRENT.reset(token)


def timed(stage):
    timing = CURRENT.get()
    return timing.span(stage) if timing else nullcontext()


class TokenObserver:
    """Mutable timestamp/count accumulator; never retains or decodes token IDs."""
    def __init__(self, timing, prefix_tokens):
        self.timing = timing
        self.prefix_tokens = prefix_tokens
        self.prompt_pending = True
        self.generated_tokens = 0
        self.first_ns = None
        self.first_unforced_ns = None
        self.start_ns = None
        self.end_ns = None

    def put(self, value):
        # Transformers 4.57.1 sends the prompt once, then one token per greedy step.
        if self.prompt_pending:
            self.prompt_pending = False
            return
        if self.first_unforced_ns is not None:
            return
        self.generated_tokens += value.numel()
        if self.first_ns is None:
            self.first_ns = self.timing.now()
        if self.first_unforced_ns is None and self.generated_tokens > self.prefix_tokens:
            self.first_unforced_ns = self.first_ns if self.prefix_tokens == 0 else self.timing.now()

    def end(self):
        """The caller closes the model-call boundary, including exceptional exits."""


class RequestTiming:
    def __init__(self):
        self.origin = time.monotonic_ns()
        self.attempt_id = str(uuid.uuid4())
        self.request_id = None
        self.observer: TokenObserver | None = None

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
        observer = self.observer
        first = observer.first_ns if observer else None
        unforced = observer.first_unforced_ns if observer else None
        model_start = observer.start_ns if observer else None
        LOGGER.info(json.dumps({
            "schema_version": 1, "event": "request_timing",
            "attempt_id": self.attempt_id, "request_id": self.request_id,
            "stage": stage, "start_ns": start_ns, "end_ns": self.now(),
            "status": status, "write_ok": write_ok,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "queue_present": False, "queue_wait_ms": None,
            "first_token_ms": first / 1e6 if first is not None else None,
            "ttft_available": first is not None,
            "ttft_reason": ("observed_cpu_token" if first is not None else
                            "awaiting_first_token" if observer and observer.end_ns is None else
                            "no_generated_token" if observer else "observer_not_started"),
            "first_generated_token_ns": first,
            "first_unforced_token_ns": unforced,
            "first_unforced_token_reason": ("observed_cpu_token" if unforced is not None else
                                            "no_unforced_token" if observer else
                                            "observer_not_started"),
            "model_generate_start_ns": model_start,
            "model_generate_end_ns": observer.end_ns if observer else None,
            "model_first_token_ms": ((first - model_start) / 1e6
                                    if first is not None else None),
            "forced_prefix_tokens": observer.prefix_tokens if observer else None,
        }, separators=(",", ":")))


def startup_event(stage, origin, *, status):
    LOGGER.info(json.dumps({
        "schema_version": 1, "event": "startup_timing", "stage": stage,
        "elapsed_ns": time.monotonic_ns() - origin, "status": status,
    }, separators=(",", ":")))
