"""Loopback inference API with explicitly enabled persona and profile nodes."""
import json
import logging
import time
from typing import Final

import baseline
from inference import ContextLimitExceeded, Inference, Request
from inference_service import Service, allow_service
from http_contract import FrameError, InferenceHTTPServer, InferenceHandler, read_body
from baseline_contract import RAW_LIMIT, error_body, valid_id, parse_request, reject_json_constant
import persona_contract
from persona_adapter import log_failure
import profile_contract
from profile_adapter import ProfileConfig
from profile_types import Task
from request_timing import RequestTiming, bind_timing

LOGGER = logging.getLogger("baseline.http")
PROFILE_ROUTES: Final[dict[str, Task]] = {
    "/v1/persona-profiles/analyze": "analyze", "/v1/persona-profiles/merge": "merge"}


class Server(InferenceHTTPServer):
    def __init__(self, port: int, service: Service):
        self.service = service
        super().__init__(("127.0.0.1", port), Handler)


class Handler(InferenceHandler):
    def do_POST(self):
        self.started = time.monotonic()
        self.timing = RequestTiming()
        self.timing.emit("receipt", 0)
        service = self.server.service
        profile_task = PROFILE_ROUTES.get(self.path)
        if self.path not in ("/v1/baseline/generate", "/v1/persona/generate") and (
                profile_task is None or service.profile_config is None):
            self.send_error(404)
            return
        try:
            raw = read_body(self, service.auth)
        except FrameError as error:
            self.respond(error.status, error_body(error.status))
            return
        request_id = None
        parse_start = self.timing.now()
        try:
            try:
                data = (profile_contract.decode_json(raw.decode("utf-8")) if profile_task is not None
                        else json.loads(raw.decode("utf-8"), parse_constant=reject_json_constant))
                if isinstance(data, dict) and valid_id(data.get("request_id")):
                    request_id = self.timing.request_id = data["request_id"]
                if self.headers.get_content_type() != "application/json":
                    raise ValueError
                request = (profile_contract.parse_request(data, profile_task) if profile_task is not None
                           else persona_contract.parse_request(data) if self.path == "/v1/persona/generate"
                           else parse_request(data))
            finally:
                self.timing.emit("parse", parse_start)
        except persona_contract.PayloadTooLarge:
            self.respond(413, error_body(413, request_id))
            return
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
        engine = service.engine_for(request)
        if not service.ready.is_set() or engine is None:
            self.respond(503, error_body(503, request_id))
            return
        try:
            with bind_timing(self.timing), self.timing.span("prepare_tokenize"):
                prepared = service.prepare(request)
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
