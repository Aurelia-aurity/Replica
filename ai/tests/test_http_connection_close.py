"""Regression for sending another TCP segment before an early HTTP response."""
import json
import select
import socket
from threading import Event
from unittest.mock import patch

import pytest

from test_server import server, serving


@pytest.mark.parametrize(
    ("request_line", "status", "expected"),
    [
        ("GET /readyz", 503, {"ready": False}),
        ("POST /v1/persona/generate", 403, {
            "request_id": None,
            "error": {"code": "SERVICE_FORBIDDEN", "message": "서비스 호출 권한이 없습니다."},
        }),
    ],
)
def test_early_response_closes_cleanly_when_body_is_pending(
        request_line: str, status: int, expected: dict[str, bool | str | None | dict[str, str]],
) -> None:
    entered, pending = Event(), Event()
    respond = server.Handler.respond

    def wait_for_body(handler, response_status, body) -> None:
        # Headers have been parsed. Leave the next segment in the socket receive queue.
        entered.set()
        if select.select([handler.connection], [], [], 5)[0]:
            pending.set()
        respond(handler, response_status, body)

    with patch.object(server.Handler, "respond", wait_for_body), serving(
            server.Service(auth=lambda _: 403), join_handlers=True) as port:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(f"{request_line} HTTP/1.1\r\nHost: localhost\r\n"
                           "Content-Length: 4\r\n\r\n".encode("ascii"))
            assert entered.wait(5), "the server did not reach the early response"
            client.sendall(b"null")
            with client.makefile("rb") as stream:
                headers, payload = stream.read().split(b"\r\n\r\n", 1)

    assert pending.is_set(), "the server did not observe the pending body"
    assert int(headers.split(b" ", 2)[1]) == status
    assert json.loads(payload) == expected
