"""Synthetic protocol controller; new timing metrics are not yet frozen."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "ai" / "src"))
from startup_warmup import persona_fixture
import persona_contract
from baseline_contract import error_body
from inference import Prepared
from analyze_timing import join_timing

NAMESPACE = uuid.UUID("964b5652-344e-5cbb-8644-42e845abb153")


def encoded(data):
    return json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def schedule(rounds=30):
    rows = []
    def add(arm, cycle, phase, count, concurrency, wave, participant):
        row = dict(arm=arm, cycle=cycle, phase=phase, evidence_count=count,
                   concurrency=concurrency, wave=wave, participant=participant)
        row["request_id"] = str(uuid.uuid5(NAMESPACE, encoded(row).decode()))
        body = encoded(persona_fixture(count, row["request_id"]))
        persona_contract.parse_request(json.loads(body))
        row["fixture_sha256"] = hashlib.sha256(body).hexdigest()
        rows.append(row)
    for cycle, order in enumerate(("AB", "BA", "AB", "BA", "AB")):
        for arm in order:
            add(arm, cycle, "paired_first", 3, 1, 0, 0)
    add("B", 5, "matrix_first", 3, 1, 0, 0)
    cells = [(k, c) for k in (0, 3, 6, 10) for c in (1, 2, 4)]
    for r in range(rounds):
        offset = r % len(cells)
        for k, c in cells[offset:] + cells[:offset]:
            for participant in range(c):
                add("B", 5, "matrix", k, c, r, participant)
    return rows


def save(output, row):
    output.write(json.dumps(row, separators=(",", ":")) + "\n")
    output.flush()


def get(port, path):
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def attempt(port, row, gate):
    body = encoded(persona_fixture(row["evidence_count"], row["request_id"]))
    if hashlib.sha256(body).hexdigest() != row["fixture_sha256"]:
        raise ValueError("fixture hash mismatch")
    connection = HTTPConnection("127.0.0.1", port, timeout=120)
    gate.wait(timeout=10)
    start = time.monotonic_ns()
    result = dict(row, event="client_attempt", send_ns=start, status=None,
                  finish_reason=None, contract_valid=False, response_bytes=None,
                  first_token_ms=None, ttft_available=False,
                  ttft_reason="server_event_not_joined")
    try:
        connection.request("POST", "/v1/persona/generate", body=body,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read()
        result.update(status=response.status, response_bytes=len(raw), outcome="http")
        try:
            data = json.loads(raw)
            if response.status == 200:
                request = persona_contract.parse_request(json.loads(body))
                prepared = Prepared((), data["context"]["used_history_turns"],
                                    data["context"]["dropped_history_turns"])
                generated = persona_contract.PersonaGenerated(
                    data["message"], data["finish_reason"], 0,
                    tuple(data["evidence_used"]), data["model_name"])
                valid = (persona_contract.success_body(request, prepared, generated) == data
                         and data["model_name"] == "Qwen/Qwen3-8B")
                result["finish_reason"] = data.get("finish_reason")
            else:
                valid = data == error_body(response.status, row["request_id"])
            result["contract_valid"] = bool(valid)
        except (ValueError, TypeError, AttributeError, KeyError):
            result["outcome"] = "invalid_response"
    except (OSError, HTTPException) as error:
        result.update(outcome="transport_unknown", exception_type=type(error).__name__,
                      censored=isinstance(error, TimeoutError))
    finally:
        result["complete_ns"] = time.monotonic_ns()
        connection.close()
    return result


def read_events(path):
    if not path.exists():
        return []
    # A trailing partial write is not a complete JSONL event yet.
    return [json.loads(line) for line in path.read_text().splitlines(keepends=True)
            if line.endswith("\n")]


def wait_ready(process, events, output, alias):
    limit = time.monotonic() + 600
    while time.monotonic() < limit and process.poll() is None:
        rows = read_events(events)
        if any(r.get("status") == "failed" for r in rows):
            return None, "startup_failed"
        listener = next((r for r in rows if r["event"] == "listener"), None)
        if listener:
            try:
                status, data = get(listener["port"], "/readyz")
                save(output, dict(event="ready_probe", process=alias, status=status,
                                  ready=data["ready"], client_ns=time.monotonic_ns()))
                if status == 200 and data["ready"]:
                    return listener["port"], None
            except (OSError, HTTPException):
                pass
        time.sleep(0.1)
    return None, "readiness_timeout_or_process_exit"


def drain(port, process, events):
    limit = time.monotonic() + 600
    while process.poll() is None and time.monotonic() < limit:
        status, data = get(port, "/measurement/idle")
        records = read_events(events)
        admitted = {r["request_id"] for r in records
                    if r["event"] == "admission" and r["admitted"]}
        terminal = {r["request_id"] for r in records if r["event"] == "worker_terminal"}
        if status == 200 and data["idle"] and admitted <= terminal:
            return
        time.sleep(0.1)
    raise RuntimeError("worker_drain_unresolved")


def run_process(rows, output, outdir, alias, double):
    events = outdir / f"{alias}.server.jsonl"
    command = [sys.executable, "-B", str(HERE / "gpu_server.py"),
               "--arm", rows[0]["arm"], "--events", str(events)]
    if double:
        command.append("--double")
    with (outdir / f"{alias}.process.jsonl").open("x") as process_output:
        process = subprocess.Popen(command, stdout=process_output, stderr=subprocess.DEVNULL)
        save(output, dict(event="process_start", process=alias, arm=rows[0]["arm"],
                          cycle=rows[0]["cycle"], client_ns=time.monotonic_ns()))
        try:
            port, failure = wait_ready(process, events, output, alias)
            if failure:
                save(output, dict(event="startup_failure", process=alias, reason=failure))
                for row in rows:
                    save(output, dict(row, event="client_attempt", outcome="not_launched",
                                      reason=failure))
                return
            index = 0
            while index < len(rows):
                size = rows[index]["concurrency"]
                wave = rows[index:index + size]
                gate = threading.Barrier(size)
                with ThreadPoolExecutor(max_workers=size) as pool:
                    futures = [pool.submit(attempt, port, row, gate) for row in wave]
                    results = [future.result() for future in futures]
                try:
                    drain(port, process, events)
                except (OSError, HTTPException, ValueError, RuntimeError) as error:
                    save(output, dict(event="drain_failure", process=alias,
                                      exception_type=type(error).__name__))
                    raise
                finally:
                    telemetry_error = None
                    telemetry_stage = "log_read"
                    try:
                        server_events = read_events(events)
                        telemetry_stage = "join"
                        observed = [join_timing(dict(result), server_events) for result in results]
                    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                        telemetry_error = error
                        save(output, dict(event="server_timing_failure", process=alias,
                                          stage=telemetry_stage, exception_type=type(error).__name__))
                        observed = [join_timing(dict(result), []) for result in results]
                        for result in observed:
                            result["ttft_reason"] = "server_timing_unavailable"
                    for result in observed:
                        result["measurement_kind"] = "double" if double else "model"
                        save(output, dict(result, process=alias))
                    if telemetry_error is not None and sys.exception() is None:
                        raise telemetry_error
                save(output, dict(event="wave_terminal", process=alias,
                                  phase=wave[0]["phase"], wave=wave[0]["wave"],
                                  evidence_count=wave[0]["evidence_count"], concurrency=size,
                                  launch_skew_ns=max(r["send_ns"] for r in results)
                                  - min(r["send_ns"] for r in results),
                                  makespan_ns=max(r["complete_ns"] for r in results)
                                  - min(r["send_ns"] for r in results)))
                index += size
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=30)
            save(output, dict(event="process_stop", process=alias,
                              returncode=process.returncode, client_ns=time.monotonic_ns()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--schedule", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--double", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        args.schedule.write_bytes(encoded(schedule()) + b"\n")
        return
    rows = json.loads(args.schedule.read_bytes())
    if rows != schedule() or len(rows) != 851:
        raise ValueError("schedule mismatch")
    args.output_dir.mkdir(exist_ok=False)
    with (args.output_dir / "client.jsonl").open("x") as output:
        try:
            for i in range(10):
                run_process(rows[i:i + 1], output, args.output_dir,
                            f"pair-{rows[i]['cycle']}-{rows[i]['arm']}", args.double)
            run_process(rows[10:], output, args.output_dir, "matrix-B", args.double)
            save(output, dict(event="run_terminal", outcome="schedule_exhausted"))
        except Exception as error:
            save(output, dict(event="run_terminal", outcome="aborted",
                              exception_type=type(error).__name__))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
