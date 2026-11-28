"""Unit coverage for the time budget of HTTP connection cleanup."""
import sys
from pathlib import Path
from socket import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import http_contract


@pytest.mark.parametrize(
    ("read_durations", "expected_timeouts"),
    [
        pytest.param((None,), (1.0,), id="read-times-out"),
        pytest.param((0.25, 0.375, 0.375), (1.0, 0.75, 0.375), id="reads-use-up-budget"),
    ],
)
def test_shutdown_closes_within_one_shared_read_budget(
    monkeypatch: pytest.MonkeyPatch,
    read_durations: tuple[float | None, ...],
    expected_timeouts: tuple[float, ...],
) -> None:
    now = 10.0
    durations = iter(read_durations)
    timeouts: list[float] = []
    request = Mock(spec_set=socket)

    def recv(_size: int) -> bytes:
        nonlocal now
        timeout = request.settimeout.call_args.args[0]
        timeouts.append(timeout)
        duration = next(durations)
        if duration is None:
            now += timeout
            raise TimeoutError
        assert duration <= timeout
        now += duration
        return b"x"

    request.recv.side_effect = recv
    monkeypatch.setattr(http_contract, "time", SimpleNamespace(monotonic=lambda: now))
    # The shutdown hook needs no listener; keep even server construction off the wire.
    server = object.__new__(http_contract.InferenceHTTPServer)

    server.shutdown_request(request)

    request.close.assert_called_once_with()
    assert now == 11.0
    assert tuple(timeouts) == expected_timeouts
