"""Loopback-only baseline HTTP API. Service authentication is deferred."""
import json
import logging
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import threading
import time

import baseline
from inference import ContextLimitExceeded, Inference, Request

RAW_LIMIT = 262144
ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ERRORS = {
    400: ("INVALID_REQUEST", "요청 형식이 올바르지 않습니다."),
    401: ("SERVICE_UNAUTHENTICATED", "서비스 인증이 필요합니다."),
    403: ("SERVICE_FORBIDDEN", "서비스 호출 권한이 없습니다."),
    413: ("PAYLOAD_TOO_LARGE", "요청 크기가 한도를 초과했습니다."),
    422: ("CONTEXT_LIMIT_EXCEEDED", "질문이 모델 문맥 한도를 초과했습니다."),
    503: ("MODEL_NOT_READY", "모델이 아직 준비되지 않았습니다."),
    409: ("REQUEST_IN_PROGRESS", "동일 요청이 처리 중입니다."),
    429: ("AI_BUSY", "AI가 다른 요청을 처리 중입니다."),
    504: ("INFERENCE_DEADLINE_EXCEEDED", "추론 마감 시간을 초과했습니다."),
    500: ("INFERENCE_FAILED", "답변 생성에 실패했습니다."),
}
LOGGER = logging.getLogger("baseline.http")


def error_body(status: int, request_id: str | None = None):
    code, message = ERRORS[status]
    return {"request_id": request_id, "error": {"code": code, "message": message}}


def valid_id(value) -> bool:
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def parse_request(data) -> Request:
    """Check all structural rules before checking any parsed length limit."""
    if not isinstance(data, dict) or set(data) != {
            "request_id", "message", "history", "max_new_tokens"}:
        raise ValueError
    if not valid_id(data["request_id"]):
        raise ValueError
    if not isinstance(data["message"], str) or not data["message"].strip():
        raise ValueError
    if not isinstance(data["history"], list):
        raise ValueError
    budget = data["max_new_tokens"]
    # JSON Schema integers include exactly integral JSON numbers, but not booleans.
    if type(budget) not in (int, float) or not 1 <= budget <= 1024 or int(budget) != budget:
        raise ValueError
    pairs = []
    for pair in data["history"]:
        if not isinstance(pair, dict) or set(pair) != {"user", "assistant"}:
            raise ValueError
        if any(not isinstance(pair[key], str) or not pair[key].strip()
               for key in ("user", "assistant")):
            raise ValueError
        pairs.append((pair["user"], pair["assistant"]))
    return Request(data["request_id"], data["message"], tuple(pairs), int(budget))


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
    def __init__(self, engine=None, deadline_seconds: float | None = None, auth=allow_service):
        if deadline_seconds is not None and (
                not math.isfinite(deadline_seconds) or deadline_seconds <= 0):
            raise ValueError
        self.engine = engine
        self.ready = threading.Event()
        if engine is not None:
            self.ready.set()
        self.lock = threading.Lock()
        self.active_id = None
        self.deadline_seconds = deadline_seconds
        self.auth = auth
        self.loader = None

    def start_loading(self, factory) -> None:
        """Publish readiness only after one successful background load."""
        if self.loader is not None:
            raise RuntimeError

        def load():
            try:
                self.engine = factory()
                self.ready.set()
            except Exception:
                # Runtime boundary: never expose model paths or raw exceptions.
                LOGGER.info("request_id=null status=503 duration_ms=0")

        self.loader = threading.Thread(target=load, daemon=True)
        self.loader.start()

    def accept(self, request: Request, prepared):
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

    def _generate(self, request: Request, prepared, job: Job) -> None:
        start = time.monotonic()
        output_tokens = 0
        try:
            result = self.engine.generate(request, prepared)
            output_tokens = result.output_tokens
            if not result.message.strip():
                raise ValueError
            job.status = 200
            job.body = {
                "request_id": request.request_id, "message": result.message,
                "finish_reason": result.finish_reason, "ai_generated": True,
                "context": {"used_history_turns": prepared.used_history_turns,
                            "dropped_history_turns": prepared.dropped_history_turns}}
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
        if self.path != "/v1/baseline/generate":
            self.send_error(404)
            return
        service = self.server.service
        lengths = self.headers.get_all("Content-Length", [])
        length = None
        if len(lengths) == 1 and re.fullmatch(r"[0-9]+", lengths[0]):
            try:
                length = int(lengths[0])
            except ValueError:
                self.respond(413, error_body(413))
                return
        if length is not None and length > RAW_LIMIT:
            self.respond(413, error_body(413))
            return
        status = service.auth(self.headers)
        if status is not None:
            self.respond(status, error_body(status))
            return
        # Only uncompressed, Content-Length framed JSON is supported.
        if (length is None or self.headers.get("Transfer-Encoding") is not None
                or self.headers.get("Content-Encoding") not in (None, "identity")):
            self.respond(400, error_body(400))
            return
        raw = self.rfile.read(length)
        if len(raw) != length:
            self.respond(400, error_body(400))
            return
        request_id = None
        try:
            data = json.loads(raw.decode("utf-8"), parse_constant=lambda value: None)
            if isinstance(data, dict) and valid_id(data.get("request_id")):
                request_id = data["request_id"]
            if self.headers.get_content_type() != "application/json":
                raise ValueError
            request = parse_request(data)
        except (ValueError, UnicodeError, RecursionError):
            self.respond(400, error_body(400, request_id))
            return
        if (len(request.message) > 16000 or len(request.history) > 20
                or any(len(text) > 16000 for pair in request.history for text in pair)):
            self.respond(413, error_body(413, request_id))
            return
        if not service.ready.is_set():
            self.respond(503, error_body(503, request_id))
            return
        try:
            prepared = service.engine.prepare(request)
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
    try:
        args = parser.parse_args(argv)
        if not 0 < args.port <= 65535 or (
                args.context_limit is not None and args.context_limit < 1):
            raise ValueError
        settings = baseline.read_settings(args.config, "startup", args.model_path)
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(config.get("model_id"), str) or not config["model_id"].strip():
            raise ValueError
        service = Service(deadline_seconds=args.deadline_seconds)
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
