"""JSON response writing and safe delivery metadata."""
from http.server import BaseHTTPRequestHandler
import json
import logging
import time

LOGGER = logging.getLogger("baseline.http")


class JsonHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Base access logging includes untrusted URL/header text.
        pass

    def send_error(self, code, message=None, explain=None):
        self.respond(code, {"status": code})

    def respond(self, status: int, body) -> None:
        timing = getattr(self, "timing", None)
        write_start = timing.now() if timing else None
        raw = json.dumps(body, ensure_ascii=True).encode("ascii")
        write_ok = False
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            write_ok = True
        except OSError:
            write_ok = False
        self.close_connection = True
        if timing:
            timing.emit("response_write", write_start, status=status, write_ok=write_ok)
        LOGGER.info("request_id=%s status=%d duration_ms=%.3f",
                    body.get("request_id") or "null", status,
                    (time.monotonic() - getattr(self, "started", time.monotonic())) * 1000)
