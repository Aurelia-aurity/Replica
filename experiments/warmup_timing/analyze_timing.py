"""Join metadata on request/attempt IDs and reduce host spans without content."""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path

TOKEN_FIELDS = (
    "first_token_ms", "ttft_available", "ttft_reason", "first_generated_token_ns",
    "first_unforced_token_ns", "first_unforced_token_reason", "model_first_token_ms",
    "model_generate_start_ns", "model_generate_end_ns", "forced_prefix_tokens",
)


def join_timing(result, events):
    rows = [r for r in events if r.get("event") == "request_timing"]
    attempt_ids = {r["attempt_id"] for r in rows if r["request_id"] == result["request_id"]}
    selected = [r for r in rows if r["attempt_id"] in attempt_ids]
    terminal = next((r for r in selected if r["stage"] == "generate"), None)
    result.update({key: terminal.get(key) if terminal else None for key in TOKEN_FIELDS})
    if terminal is None:
        result.update(ttft_available=False, ttft_reason="no_worker_terminal")
    spans = {}
    for row in selected:
        span = spans.setdefault(row["stage"], {"count": 0, "duration_ns": 0})
        span["count"] += 1
        span["duration_ns"] += row["end_ns"] - row["start_ns"]
    result["server_spans"] = spans
    result["ttft_source"] = "server_monotonic"
    return result


def distribution(values):
    values = sorted(values)
    return {"n": len(values), "p50": values[math.ceil(len(values) * .50) - 1] if values else None,
            "p95": values[math.ceil(len(values) * .95) - 1] if values else None,
            "maximum": values[-1] if values else None}


def analyze(rows):
    cells = defaultdict(list)
    for row in rows:
        if row["event"] == "client_attempt":
            key = (row["phase"], row["arm"], row["evidence_count"],
                   row["concurrency"], row.get("status"))
            cells[key].append(row)
    output = []
    for key, attempts in cells.items():
        spans = sorted({stage for row in attempts for stage in row.get("server_spans", {})})
        metrics = {}
        for field in ("first_token_ms", "model_first_token_ms", "first_unforced_token_ns"):
            metrics[field] = distribution([r[field] for r in attempts if r.get(field) is not None])
        output.append({
            "phase": key[0], "arm": key[1], "evidence_count": key[2],
            "concurrency": key[3], "status": key[4], "attempts": len(attempts),
            "measurement_kinds": sorted({r.get("measurement_kind", "unspecified") for r in attempts}),
            "token_metrics": metrics,
            "ttft_missing": sum(r.get("first_token_ms") is None for r in attempts),
            "spans": {stage: {
                "duration_ns": distribution([r["server_spans"][stage]["duration_ns"] for r in attempts
                                             if stage in r.get("server_spans", {})]),
                "occurrences": sum(r.get("server_spans", {}).get(stage, {}).get("count", 0)
                                   for r in attempts)} for stage in spans},
        })
    return {"event": "timing_analysis", "protocol_frozen": False, "cells": output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in (args.directory / "client.jsonl").read_text().splitlines()]
    print(json.dumps(analyze(rows), separators=(",", ":")))


if __name__ == "__main__":
    main()
