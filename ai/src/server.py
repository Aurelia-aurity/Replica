"""Loopback-only inference API with explicitly enabled personalization."""
import json
import logging
from http.server import ThreadingHTTPServer
import re
import time

import baseline
from baseline_contract import RAW_LIMIT, error_body, valid_id, reject_json_constant, parse_request
from inference import ContextLimitExceeded, Inference
from inference_service import Service, allow_service
from http_response import JsonHandler
import persona_contract
from persona_adapter import log_failure
from request_timing import RequestTiming, bind_timing

LOGGER = logging.getLogger("baseline.http")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, service: Service):
        self.service = service
        super().__init__(("127.0.0.1", port), Handler)

    def handle_error(self, request, client_address):
        # Base implementation prints raw exceptions and possibly request text.
        LOGGER.info("request_id=null status=500 duration_ms=0")


class Handler(JsonHandler):
    def do_GET(self):
        self.started = time.monotonic()
        ready = self.server.service.ready.is_set()
        if self.path not in ("/healthz", "/readyz"):
            self.send_error(404)
            return
        self.respond(503 if self.path == "/readyz" and not ready else 200, {"ready": ready})

    def do_POST(self):
        self.started = time.monotonic()
        self.timing = RequestTiming()
        self.timing.emit("receipt", 0)
        if self.path not in ("/v1/baseline/generate", "/v1/persona/generate"):
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
        read_start = self.timing.now()
        raw = self.rfile.read(length)
        self.timing.emit("body_read", read_start)
        if len(raw) != length:
            self.respond(400, error_body(400))
            return
        request_id = None
        try:
            with self.timing.span("parse"):
                data = json.loads(raw.decode("utf-8"), parse_constant=reject_json_constant)
                if isinstance(data, dict) and valid_id(data.get("request_id")):
                    request_id = self.timing.request_id = data["request_id"]
                if self.headers.get_content_type() != "application/json":
                    raise ValueError
                request = (persona_contract.parse_request(data) if self.path == "/v1/persona/generate"
                           else parse_request(data))
        except persona_contract.PayloadTooLarge:
            self.respond(413, error_body(413, request_id))
            return
        except (ValueError, UnicodeError, RecursionError):
            self.respond(400, error_body(400, request_id))
            return
        if (len(request.message) > 16000 or len(request.history) > 20
                or any(len(text) > 16000 for pair in request.history for text in pair)):
            self.respond(413, error_body(413, request_id))
            return
        engine = service.engine_for(request)
        if not service.ready.is_set() or engine is None:
            self.respond(503, error_body(503, request_id))
            return
        try:
            with bind_timing(self.timing), self.timing.span("prepare_tokenize"):
                prepared = engine.prepare(request)
        except ContextLimitExceeded:
            self.respond(422, error_body(422, request_id))
            return
        except Exception as error:
            if isinstance(request, persona_contract.PersonaRequest):
                log_failure(request.request_id, error)
            self.respond(500, error_body(500, request_id))
            return
        service.request_timing.current = self.timing
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
    parser.add_argument("--mode", choices=("baseline", "persona"), default="baseline")
    try:
        args = parser.parse_args(argv)
        if not 0 < args.port <= 65535 or (
                args.context_limit is not None and args.context_limit < 1):
            raise ValueError
        settings = baseline.read_settings(args.config, "startup", args.model_path)
        if settings["model_id"] is None:
            raise ValueError
        service = Service(deadline_seconds=args.deadline_seconds)
        baseline.os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        baseline.os.environ["TQDM_DISABLE"] = "1"
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        for handler in logging.getLogger().handlers:
            handler.addFilter(lambda record: record.name == LOGGER.name)
        with Server(args.port, service) as server:
            service.start_loading(
                lambda: Inference(settings, args.context_limit),
                persona_model_name=settings["model_id"] if args.mode == "persona" else None)
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
