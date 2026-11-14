"""Inference HTTP framing, health checks, safe responses and baseline parsing."""
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final, TypeAlias, TypedDict, TypeGuard

from inference import Request

JSON: TypeAlias = None | bool | int | float | str | list["JSON"] | dict[str, "JSON"]
LOGGER = logging.getLogger("baseline.http")
RAW_LIMIT: Final = 262144
ID_PATTERN: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ERRORS: Final = {
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


class ErrorDetail(TypedDict):
    code: str
    message: str


class ErrorBody(TypedDict):
    request_id: str | None
    error: ErrorDetail


@dataclass(frozen=True, slots=True)
class FrameError(ValueError):
    status: int

    def __str__(self) -> str:
        return f"HTTP frame: {self.status}"


def read_body(handler: BaseHTTPRequestHandler, auth: Callable[[HTTPMessage], int | None]) -> bytes:
    """Keep the raw byte cap before service authentication and JSON parsing."""
    lengths = handler.headers.get_all("Content-Length", [])
    length = None
    if len(lengths) == 1 and re.fullmatch(r"[0-9]+", lengths[0]):
        try:
            length = int(lengths[0])
        except ValueError:
            raise FrameError(413) from None
    if length is not None and length > RAW_LIMIT:
        raise FrameError(413)
    status = auth(handler.headers)
    if status is not None:
        raise FrameError(status)
    if (length is None or handler.headers.get("Transfer-Encoding") is not None
            or handler.headers.get("Content-Encoding") not in (None, "identity")):
        raise FrameError(400)
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise FrameError(400)
    return raw


def error_body(status: int, request_id: str | None = None) -> ErrorBody:
    code, message = ERRORS[status]
    return {"request_id": request_id, "error": {"code": code, "message": message}}


def valid_id(value: JSON) -> TypeGuard[str]:
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def reject_json_constant(_value: str) -> None:
    raise ValueError


def parse_request(data: JSON) -> Request:
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


class InferenceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Base implementation prints raw exceptions and possibly request text.
        LOGGER.info("request_id=null status=500 duration_ms=0")


class InferenceHandler(BaseHTTPRequestHandler):
    """Common loopback health and response behavior; routes live in server."""
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
