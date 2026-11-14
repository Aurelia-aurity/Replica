"""Measurement-only process wrapper; new metrics require a new frozen protocol."""
import argparse
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai" / "src"))
import baseline
from inference import Inference
import inference_service
import server


class Events(logging.Handler):
    """Keep only the PR's structured, metadata-only timing records."""
    def __init__(self, output):
        super().__init__()
        self.output = output
        self.mutex = threading.Lock()

    def save(self, row):
        row["server_observed_ns"] = time.monotonic_ns()
        with self.mutex:
            self.output.write(json.dumps(row, separators=(",", ":")) + "\n")
            self.output.flush()

    def emit(self, record):
        if record.name != "baseline.http":
            return
        message = record.getMessage()
        if not message.startswith("{"):
            return
        row = json.loads(message)
        if row.get("event") in ("startup_timing", "request_timing"):
            self.save(row)


class ObservedService(server.Service):
    def __init__(self, events):
        super().__init__()
        self.events = events
        self.jobs = []
        self.jobs_lock = threading.Lock()

    def accept(self, request, prepared):
        status, job = super().accept(request, prepared)
        if job is not None:
            with self.jobs_lock:
                self.jobs.append(job)
        self.events.save({"event": "admission", "request_id": request.request_id,
                          "admitted": job is not None, "status": status})
        return status, job

    def _generate(self, request, prepared, job):
        self.events.save({"event": "worker_begin", "request_id": request.request_id})
        super()._generate(request, prepared, job)
        self.events.save({"event": "worker_terminal", "request_id": request.request_id,
                          "status": job.status, "done": job.done.is_set()})


class ObservedHandler(server.Handler):
    def do_GET(self):
        if self.path != "/measurement/idle":
            return super().do_GET()
        self.started = time.monotonic()
        service = self.server.service
        with service.jobs_lock:
            idle = all(job.done.is_set() for job in service.jobs)
        self.respond(200, {"idle": idle})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=("A", "B"), required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--double", action="store_true")
    args = parser.parse_args()
    with args.events.open("x") as output:
        events = Events(output)
        logger = logging.getLogger("baseline.http")
        logger.handlers[:] = [events]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        if args.arm == "A":
            # Benchmark control only; no production module/file is modified.
            inference_service.warmup = lambda engine, persona: None
        service = ObservedService(events)
        server.Handler = ObservedHandler
        if args.double:
            from double_runtime import DoubleEngine
            factory, model_name = DoubleEngine, "Qwen/Qwen3-8B"
        else:
            settings = baseline.read_settings(
                baseline.DEFAULT_CONFIG, "measurement", os.environ["REPLICA_MODEL_PATH"])
            factory, model_name = lambda: Inference(settings), settings["model_id"]
        with server.Server(0, service) as httpd:
            events.save({"event": "listener", "port": httpd.server_port, "arm": args.arm})
            service.start_loading(factory, persona_model_name=model_name)
            httpd.serve_forever(poll_interval=0.1)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Boundary: no message, traceback, request body or runtime stderr.
        print(json.dumps({"event": "process_failure",
                          "exception_type": type(error).__name__}), flush=True)
        raise SystemExit(1)
