"""Inference HTTP framing and health checks; shared contracts and JSON responses."""
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TypeAlias

from baseline_contract import RAW_LIMIT, ERRORS, error_body, valid_id, reject_json_constant, parse_request
from http_response import JsonHandler

LOGGER = logging.getLogger("baseline.http")
JSON: TypeAlias = None | bool | int | float | str | list["JSON"] | dict[str, "JSON"]


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
    timing = getattr(handler, "timing", None)
    read_start = timing.now() if timing else None
    raw = handler.rfile.read(length)
    if timing:
        timing.emit("body_read", read_start)
    if len(raw) != length:
        raise FrameError(400)
    return raw



class InferenceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Base implementation prints raw exceptions and possibly request text.
        LOGGER.info("request_id=null status=500 duration_ms=0")


class InferenceHandler(JsonHandler):
    """Common loopback health and timed JSON responses; routes live in server."""
    def do_GET(self):
        self.started = time.monotonic()
        ready = self.server.service.ready.is_set()
        if self.path not in ("/healthz", "/readyz"):
            self.send_error(404)
            return
        self.respond(503 if self.path == "/readyz" and not ready else 200, {"ready": ready})
