"""Loopback inference API with explicitly enabled profile-analysis nodes."""
import json
import logging
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from typing import Final, assert_never

import baseline
from inference import ContextLimitExceeded, Inference, Prepared, Request
from http_contract import RAW_LIMIT as RAW_LIMIT, ERRORS as ERRORS
from http_contract import FrameError, error_body, valid_id, parse_request, read_body
import profile_contract
from profile_adapter import ProfileConfig, ProfileInference
from profile_types import AnalyzeRequest, MergeRequest, ProfileRequest, Task

LOGGER = logging.getLogger("baseline.http")
PROFILE_ROUTES: Final[dict[str, Task]] = {
    "/v1/persona-profiles/analyze": "analyze", "/v1/persona-profiles/merge": "merge"}


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
    def __init__(self, engine: Inference | None = None, deadline_seconds: float | None = None, auth=allow_service,
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
        if engine is not None:
            self.initialize(engine)

    def initialize(self, engine: Inference) -> None:
        """Publish readiness only after the optional adapter is initialized."""
        self.profiles = (ProfileInference(engine, self.profile_config)
                         if self.profile_config is not None else None)
        self.engine = engine
        self.ready.set()

    def prepare(self, request: Request | ProfileRequest) -> Prepared:
        match request:
            case Request():
                assert self.engine is not None
                return self.engine.prepare(request)
            case AnalyzeRequest() | MergeRequest():
                assert self.profiles is not None
                return self.profiles.prepare(request)
            case unreachable:
                assert_never(unreachable)

    def start_loading(self, factory) -> None:
        """Publish readiness only after one successful background load."""
        if self.loader is not None:
            raise RuntimeError

        def load():
            try:
                self.initialize(factory())
            except Exception:
                # Runtime boundary: never expose model paths or raw exceptions.
                LOGGER.info("request_id=null status=503 duration_ms=0")

        self.loader = threading.Thread(target=load, daemon=True)
        self.loader.start()

    def accept(self, request: Request | ProfileRequest, prepared):
        with self.lock:
            if self.active_id == request.request_id:
                return 409, None
            if self.active_id is not None:
                return 429, None
            self.active_id = request.request_id
            job = Job()
            worker = threading.Thread(target=self._generate, args=(request, prepared, job), daemon=False)
            try:
                worker.start()
            except RuntimeError:
                self.active_id = None
                return 500, None
            return 200, job

    def _generate(self, request: Request | ProfileRequest, prepared, job: Job) -> None:
        start = time.monotonic()
        output_tokens = 0
        try:
            match request:
                case Request():
                    assert self.engine is not None
                    result = self.engine.generate(request, prepared)
                    output_tokens = result.output_tokens
                    if not result.message.strip():
                        raise ValueError
                    job.body = {
                        "request_id": request.request_id, "message": result.message,
                        "finish_reason": result.finish_reason, "ai_generated": True,
                        "context": {"used_history_turns": prepared.used_history_turns,
                                    "dropped_history_turns": prepared.dropped_history_turns}}
                case AnalyzeRequest() | MergeRequest():
                    assert self.profiles is not None
                    generated = self.profiles.generate(request, prepared)
                    output_tokens = generated.output_tokens
                    job.body = profile_contract.success_body(request, generated)
                case unreachable:
                    assert_never(unreachable)
            job.status = 200
        except Exception:
            # Compute boundary: all failures map to the safe fixed envelope.
            job.status, job.body = 500, error_body(500, request.request_id)
        finally:
            with self.lock:
                self.active_id = None
            LOGGER.info("request_id=%s status=%d duration_ms=%.3f input_tokens=%d output_tokens=%d",
                        request.request_id, job.status, (time.monotonic() - start) * 1000,
                        len(prepared.token_ids), output_tokens)
            job.done.set()


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, service: Service):
        self.service = service
        super().__init__(("127.0.0.1", port), Handler)

    def handle_error(self, request, client_address):
        # Base implementation prints raw exceptions and possibly request text.
        LOGGER.info("request_id=null status=500 duration_ms=0")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Base access logging includes untrusted URL/header text.
        pass

    def send_error(self, code, message=None, explain=None):
        self.respond(code, {"status": code})

    def respond(self, status: int, body) -> None:
        raw = json.dumps(body, ensure_ascii=True).encode("ascii")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            pass
        self.close_connection = True
        LOGGER.info("request_id=%s status=%d duration_ms=%.3f",
                    body.get("request_id") or "null", status,
                    (time.monotonic() - getattr(self, "started", time.monotonic())) * 1000)

    def do_GET(self):
        self.started = time.monotonic()
        ready = self.server.service.ready.is_set()
        if self.path not in ("/healthz", "/readyz"):
            self.send_error(404)
            return
        self.respond(503 if self.path == "/readyz" and not ready else 200, {"ready": ready})

    def do_POST(self):
        self.started = time.monotonic()
        service = self.server.service
        profile_task = PROFILE_ROUTES.get(self.path)
        if self.path != "/v1/baseline/generate" and (
                profile_task is None or service.profile_config is None):
            self.send_error(404)
            return
        try:
            raw = read_body(self, service.auth)
        except FrameError as error:
            self.respond(error.status, error_body(error.status))
            return
        request_id = None
        try:
            data = (profile_contract.decode_json(raw.decode("utf-8")) if profile_task is not None
                    else json.loads(raw.decode("utf-8"), parse_constant=lambda value: None))
            if isinstance(data, dict) and valid_id(data.get("request_id")):
                request_id = data["request_id"]
            if self.headers.get_content_type() != "application/json":
                raise ValueError
            request = (profile_contract.parse_request(data, profile_task) if profile_task is not None
                       else parse_request(data))
        except profile_contract.ProfileError as error:
            self.respond(413 if error.code == "size" else 400, error_body(
                413 if error.code == "size" else 400, request_id))
            return
        except (ValueError, UnicodeError, RecursionError):
            self.respond(400, error_body(400, request_id))
            return
        if isinstance(request, Request) and (len(request.message) > 16000 or len(request.history) > 20
                or any(len(text) > 16000 for pair in request.history for text in pair)):
            self.respond(413, error_body(413, request_id))
            return
        if not service.ready.is_set():
            self.respond(503, error_body(503, request_id))
            return
        try:
            prepared = service.prepare(request)
        except ContextLimitExceeded:
            self.respond(422, error_body(422, request_id))
            return
        except Exception:
            self.respond(500, error_body(500, request_id))
            return
        status, job = service.accept(request, prepared)
        if job is None:
            self.respond(status, error_body(status, request_id))
            return
        if not job.done.wait(service.deadline_seconds):
            self.respond(504, error_body(504, request_id))
            return
        self.respond(job.status, job.body)


def main(argv=None) -> int:
    parser = baseline.SafeArgumentParser(description=__doc__)
    parser.add_argument("--model-path", default=baseline.os.environ.get("REPLICA_MODEL_PATH"))
    parser.add_argument("--config", type=baseline.Path, default=baseline.DEFAULT_CONFIG)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--context-limit", type=int)
    parser.add_argument("--deadline-seconds", type=float)
    parser.add_argument("--enable-profile-analysis", action="store_true")
    parser.add_argument("--profile-output-tokens", type=int)
    try:
        args = parser.parse_args(argv)
        if not 0 < args.port <= 65535 or (
                args.context_limit is not None and args.context_limit < 1):
            raise ValueError
        if args.enable_profile_analysis != (args.profile_output_tokens is not None):
            raise ValueError
        settings = baseline.read_settings(args.config, "startup", args.model_path)
        if settings["model_id"] is None:
            raise ValueError
        config = (ProfileConfig(settings["model_id"], args.profile_output_tokens)
                  if args.enable_profile_analysis else None)
        service = Service(deadline_seconds=args.deadline_seconds, profile_config=config)
        baseline.os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        baseline.os.environ["TQDM_DISABLE"] = "1"
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        for handler in logging.getLogger().handlers:
            handler.addFilter(lambda record: record.name == LOGGER.name)
        with Server(args.port, service) as server:
            service.start_loading(lambda: Inference(settings, args.context_limit))
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        return 0
    except Exception:
        LOGGER.error("request_id=null status=503 duration_ms=0")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
