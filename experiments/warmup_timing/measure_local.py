"""Unfrozen protocol skeleton. Runs only an in-process doubled loopback server."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from double_runtime import DoubleEngine, DoublePersona
import server
from startup_warmup import persona_fixture


@contextmanager
def doubled_server():
    service = server.Service()
    with server.Server(0, service) as httpd:
        service.start_loading(DoubleEngine, persona_model_name="local-double")
        service.loader.join(5)
        if service.loader.is_alive() or not service.ready.is_set():
            raise AssertionError
        thread = threading.Thread(target=httpd.serve_forever,
                                  kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield httpd.server_port
        finally:
            httpd.shutdown()
            thread.join(5)
            if thread.is_alive():
                raise AssertionError


def attempt(port, count, concurrency, wave, participant, gate):
    request_id = str(uuid.uuid4())
    body = json.dumps(persona_fixture(count, request_id),
                      ensure_ascii=True, separators=(",", ":")).encode("ascii")
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    gate.wait(timeout=5)
    start = time.monotonic_ns()
    try:
        connection.request("POST", "/v1/persona/generate", body=body,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read())
        if data["request_id"] != request_id:
            raise AssertionError
        return {
            "event": "local_double_attempt", "protocol_frozen": False,
            "request_id": request_id, "evidence_count": count,
            "concurrency": concurrency, "wave": wave, "participant": participant,
            "fixture_sha256": hashlib.sha256(body).hexdigest(),
            "send_ns": start, "complete_ns": time.monotonic_ns(),
            "status": response.status, "ttft_available": False,
            "ttft_reason": "server_event_not_joined", "measurement_kind": "double",
        }
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=1)
    args = parser.parse_args(argv)
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    cells = [(count, concurrency) for count in (0, 3, 6, 10) for concurrency in (1, 2, 4)]
    with doubled_server() as port:
        for round_index in range(args.rounds):
            offset = round_index % len(cells)
            for count, concurrency in cells[offset:] + cells[:offset]:
                gate = threading.Barrier(concurrency)
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    futures = [
                        pool.submit(attempt, port, count, concurrency, round_index, i, gate)
                        for i in range(concurrency)
                    ]
                    for future in futures:
                        print(json.dumps(future.result(), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
