"""Startup readiness and single-slot generation lifecycle."""
import logging
import math
import threading
import time
from typing import assert_never

from baseline_contract import error_body
from inference import Request
import persona_contract
from persona_adapter import PersonaGenerator, PersonaInference, log_failure
import profile_contract
from profile_adapter import ProfileConfig, ProfileInference
from profile_types import AnalyzeRequest, MergeRequest, ProfileRequest
from request_timing import bind_timing, startup_event, timed
from startup_warmup import warmup

LOGGER = logging.getLogger("baseline.http")


def allow_service(headers) -> int | None:
    """Undecided authentication hook: return None, 401, or 403. No body parsing."""
    return None


class Job:
    """Mutable result shared only through the completion event."""
    def __init__(self):
        self.done = threading.Event()
        self.status = 500
        self.body = None


class Service:
    """One mutable slot, reserved and released under the same lock."""
    def __init__(self, engine=None, deadline_seconds: float | None = None, auth=allow_service,
                 persona_generator: PersonaGenerator | None = None,
                 *, profile_config: ProfileConfig | None = None):
        if deadline_seconds is not None and (
                not math.isfinite(deadline_seconds) or deadline_seconds <= 0):
            raise ValueError
        self.engine = engine
        self.ready = threading.Event()
        self.profile_config = profile_config
        self.profiles: ProfileInference | None = None
        self.lock = threading.Lock()
        self.active_id = None
        self.deadline_seconds = deadline_seconds
        self.auth = auth
        self.loader = None
        self.persona_generator = persona_generator
        self.request_timing = threading.local()
        if engine is not None:
            self.initialize(engine)
            self.ready.set()

    def initialize(self, engine, *, persona_model_name: str | None = None) -> None:
        """Construct both optional adapters before startup can publish readiness."""
        self.profiles = (ProfileInference(engine, self.profile_config)
                         if self.profile_config is not None else None)
        if persona_model_name is not None:
            self.persona_generator = PersonaInference(engine, persona_model_name)
        self.engine = engine

    def prepare(self, request):
        return self.engine_for(request).prepare(request)

    def engine_for(self, request):
        match request:
            case Request():
                return self.engine
            case persona_contract.PersonaRequest():
                return self.persona_generator
            case AnalyzeRequest() | MergeRequest():
                return self.profiles
            case unreachable:
                assert_never(unreachable)

    def start_loading(self, factory, *, persona_model_name: str | None = None) -> None:
        """Publish readiness after load and three validated synthetic generations."""
        if self.loader is not None or self.ready.is_set():
            raise RuntimeError

        def load():
            origin = time.monotonic_ns()
            stage = "load"
            startup_event(stage, origin, status="begin")
            try:
                self.initialize(factory(), persona_model_name=persona_model_name)
                startup_event(stage, origin, status="complete")
                stage = "warmup"
                startup_event(stage, origin, status="begin")
                warmup(self.engine, self.persona_generator)
                startup_event(stage, origin, status="complete")
                self.ready.set()
                startup_event("ready", origin, status="complete")
            except Exception:
                # Runtime boundary: never expose model paths or raw exceptions.
                startup_event(stage, origin, status="failed")

        self.loader = threading.Thread(target=load, daemon=True)
        self.loader.start()

    def accept(self, request: Request | ProfileRequest | persona_contract.PersonaRequest, prepared):
        timing = getattr(self.request_timing, "current", None)
        start = timing.now() if timing else None
        with self.lock:
            if timing:
                timing.emit("admission_lock", start)
            if self.active_id == request.request_id:
                return 409, None
            if self.active_id is not None:
                return 429, None
            self.active_id = request.request_id
            job = Job()
            job.timing = timing
            job.admitted_ns = timing.now() if timing else None
            worker = threading.Thread(target=self._generate, args=(request, prepared, job), daemon=False)
            try:
                worker.start()
            except RuntimeError:
                self.active_id = None
                return 500, None
            return 200, job

    def _generate(self, request: Request | ProfileRequest | persona_contract.PersonaRequest,
                  prepared, job: Job) -> None:
        start = time.monotonic()
        timing = job.timing
        generation_start = timing.now() if timing else None
        if timing:
            timing.emit("worker_handoff", job.admitted_ns)
        output_tokens = 0
        try:
            with bind_timing(timing):
                result = self.engine_for(request).generate(request, prepared)
            output_tokens = result.output_tokens
            if isinstance(request, (Request, persona_contract.PersonaRequest)) and not result.message.strip():
                raise ValueError
            job.status = 200
            with bind_timing(timing), timed("success_body"):
                match request:
                    case persona_contract.PersonaRequest():
                        job.body = persona_contract.success_body(request, prepared, result)
                    case AnalyzeRequest() | MergeRequest():
                        job.body = profile_contract.success_body(request, result)
                    case Request():
                        job.body = {
                            "request_id": request.request_id, "message": result.message,
                            "finish_reason": result.finish_reason, "ai_generated": True,
                            "context": {"used_history_turns": prepared.used_history_turns,
                                        "dropped_history_turns": prepared.dropped_history_turns}}
                    case unreachable:
                        assert_never(unreachable)
        except Exception as error:
            # Compute boundary: all failures map to the safe fixed envelope.
            if isinstance(request, persona_contract.PersonaRequest):
                log_failure(request.request_id, error)
            job.status, job.body = 500, error_body(500, request.request_id)
        finally:
            with self.lock:
                self.active_id = None
            LOGGER.info("request_id=%s status=%d duration_ms=%.3f input_tokens=%d output_tokens=%d",
                        request.request_id, job.status, (time.monotonic() - start) * 1000,
                        len(prepared.token_ids), output_tokens)
            if timing:
                timing.emit("generate", generation_start, status=job.status,
                            input_tokens=len(prepared.token_ids), output_tokens=output_tokens)
            job.done.set()
