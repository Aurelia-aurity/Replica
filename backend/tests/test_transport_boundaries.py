"""Synthetic failures around the real async urllib adapter. No sockets or real secrets."""

import asyncio
from urllib.error import URLError

import pytest

from app.db import DatabaseUnavailableError, Settings, ValidationError
from app.db.transport import UrllibTransport


@pytest.mark.parametrize("method, unknown", [("GET", False), ("POST", True), ("PATCH", True), ("DELETE", True)])
def test_transport_failure_is_not_retried_and_writes_remain_unknown(monkeypatch, method, unknown):
    calls = []

    class FailingOpener:
        def open(self, request, *, timeout):
            calls.append((request.full_url, timeout))
            raise URLError("synthetic-private-upstream-text")

    monkeypatch.setattr("app.db.transport.build_opener", lambda *_: FailingOpener())
    transport = UrllibTransport(Settings("https://example.supabase.co", "fake-public", "fake-server", 3))

    with pytest.raises(DatabaseUnavailableError) as caught:
        asyncio.run(transport.request(method, "/rest/v1/personas", headers={"apikey": "synthetic-key"}, params={}))

    assert caught.value.outcome_unknown is unknown
    assert calls == [("https://example.supabase.co/rest/v1/personas", 3)]
    assert "synthetic-private" not in str(caught.value)
    assert "synthetic-key" not in str(caught.value)


@pytest.mark.parametrize("path", ["https://other.test/data", "//other.test/data", "/data?redirect=other", "/data#other"])
def test_invalid_paths_are_rejected_before_a_request_can_forward_credentials(monkeypatch, path):
    def forbidden_opener(*_):
        pytest.fail("Invalid path must not open a connection")

    monkeypatch.setattr("app.db.transport.build_opener", forbidden_opener)
    transport = UrllibTransport(Settings("https://example.supabase.co", "fake-public", "fake-server"))
    with pytest.raises(ValidationError):
        asyncio.run(transport.request("GET", path, headers={"apikey": "synthetic-key"}, params={}))
