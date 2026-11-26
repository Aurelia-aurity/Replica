# /// script
# requires-python = ">=3.12"
# dependencies = ["typer", "pydantic"]
# ///
# Run with existing RAG Python + PYTHONPATH; see CONFIRM_JUDGE_COMMAND.sh.
"""Generalize the DEV resume_format_retry.py checkpoint-resume policy.

The native route stays byte-identical. Calls are sequential; the newest started
checkpoint identifies the failing payload, including failures with no raw output.
Only that failed call is archived; valid cached calls are never reissued.
"""

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Final, TypedDict

import typer

STAGES: Final = ("quality", "semantic", "readjudication")
STRUCTURAL: Final = (
    "Witness utterance owner contradicts source", "Witness span is not exact response text",
    "Witness cites foreign source IDs", "Positive semantic error lacks a matching witness",
    "Semantic ID coverage mismatch", "Terra ID coverage mismatch",
    "Terra missing single usage receipt", "Missing semantic usage receipt",
    "Luna returned no agent message", "JSONDecodeError", "Invalid JSON",
    "validation error for Semantic", "validation errors for Semantic",
    "validation error for Ground", "validation errors for Ground",
    "validation error for Style", "validation errors for Style",
    "Duplicate JSON key",
)
TRANSPORT: Final = (
    "capacity", "overloaded", "429", "502", "503", "504", "rate limit",
    "connection", "transport", "timed out", "timeout", "stream disconnected",
    "temporarily unavailable",
)
FATAL: Final = (
    "budgetexhausted", "insufficient_quota", "insufficient quota", "usage limit",
    "budget exceeded", "unexpected model", "unexpected tool", "route code/rubric changed",
)


class RetryRecord(TypedDict):
    checkpoint: str
    payload_sha256: str
    reason: str
    attempt: int
    raw_sha256: str | None
    raw_present: bool


@dataclass(frozen=True, slots=True)
class Failure:
    started: Path
    raw: Path
    payload_sha256: str
    reason: str


def failure(output: Path, diagnostic: str) -> Failure | None:
    """A known structural error or no-label provider failure is retryable once."""
    if any(word in diagnostic.lower() for word in FATAL):
        return None
    checkpoints = [p for stage in STAGES
                   for p in (output / stage / "checkpoints").glob("*.raw.started.json")]
    if not checkpoints:
        return None
    started = max(checkpoints, key=lambda p: p.stat().st_mtime_ns)
    raw_path = started.with_name(started.name.replace(".raw.started.json", ".raw.json"))
    receipt = json.loads(raw_path.read_text()) if raw_path.exists() else {}
    text = str(receipt.get("stdout", ""))
    diagnostic = diagnostic + "\n" + str(receipt.get("stderr", "")) + "\n" + text
    if any(word in diagnostic.lower() for word in FATAL):
        return None
    # Only agent messages/answers are labels; event streams and reasoning are not.
    labels = bool(receipt.get("answers"))
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            item = event.get("item", {})
            labels |= bool(event.get("type") == "item.completed" and isinstance(item, dict)
                           and item.get("type") == "agent_message" and item.get("text"))
            message = event.get("message", {})
            labels |= bool(event.get("type") == "message_end" and isinstance(message, dict)
                           and message.get("role") == "assistant" and message.get("content"))
    structural = next((word for word in STRUCTURAL if word in diagnostic), None)
    no_output = not labels and any(word in diagnostic.lower() for word in TRANSPORT)
    if structural is None and not no_output:
        return None
    identity = json.loads(started.read_text())["payload_sha256"]
    return Failure(started, raw_path, identity, structural or "no-label capacity/transport failure")


def run(command: tuple[str, ...], cwd: Path, output: Path, retries: Path) -> int:
    """Resume exact cached inputs; each stage/payload can have at most one retry."""
    output.mkdir(parents=True, exist_ok=True)
    retries.mkdir(parents=True, exist_ok=True)
    ledger_path = retries / "retries.jsonl"
    ledger: list[RetryRecord] = [json.loads(line) for line in ledger_path.read_text().splitlines()] if ledger_path.exists() else []
    attempt = len(list(retries.glob("attempt-*.log"))) + 1
    while True:
        log_path = retries / f"attempt-{attempt:04d}.log"
        with log_path.open("x", encoding="utf-8") as log:
            rc = subprocess.run(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), check=False).returncode
        if rc == 0 and (output / "resolved.json").is_file() and (output / "resolved.json").stat().st_size:
            with (output / "run-complete.json").open("x", encoding="utf-8") as handle:
                json.dump({"complete": True, "items": 3960, "exit_code": 0,
                           "attempts": attempt, "retries": len(ledger)}, handle)
            return 0
        failed = failure(output, log_path.read_text())
        reason = "non-retryable execution failure"
        if failed is not None:
            key = str(failed.started.relative_to(output))
            if any(row["checkpoint"] == key for row in ledger):
                reason = "second failure for identical payload"
            else:
                archive = retries / f"failed-{len(ledger):04d}"
                archive.mkdir()
                record: RetryRecord = {
                    "checkpoint": key, "payload_sha256": failed.payload_sha256,
                    "reason": failed.reason, "attempt": attempt,
                    "raw_sha256": sha256(failed.raw.read_bytes()).hexdigest() if failed.raw.exists() else None,
                    "raw_present": failed.raw.exists(),
                }
                # Commit the retry receipt BEFORE removing its cache entry.
                # A partial move after interruption remains fail-closed.
                with ledger_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")
                ledger.append(record)
                shutil.move(str(failed.started), archive / failed.started.name)
                if failed.raw.exists():
                    shutil.move(str(failed.raw), archive / failed.raw.name)
                attempt += 1
                continue
        with (output / "run-inconclusive.json").open("x", encoding="utf-8") as handle:
            json.dump({"complete": False, "comparison_validity": "INCONCLUSIVE",
                       "reason": reason, "exit_code": rc, "attempts": attempt,
                       "log": str(log_path), "retries": len(ledger)}, handle)
        return 1


def main(prepared: Path, output: Path, identity: Path, authorization: str,
         route: Path = Path(__file__).resolve().parents[2] / ".local-work/judge-v2/stage100_route.py") -> None:
    """Explicit opt-in only. Never invoked by data preparation or GPU generation."""
    from prepare100 import ROOT, reject_forbidden
    reject_forbidden(prepared, output, identity)
    if not authorization.strip():
        raise ValueError(("Separate judge authorization required",))
    # Relative overrides are interpreted from the scorer's working directory.
    route = ROOT / route
    if not route.is_file() or not os.access(route, os.R_OK):
        raise typer.BadParameter("Expected a readable route file.", param_hint="--route")
    command = (
        sys.executable, "-B", "-s", "-u", str(route), "live",
        str(prepared), str(output), str(identity), "--authorization", authorization,
        "--max-jev", "3960", "--max-grounding", "199", "--max-style", "198",
        "--max-semantic-jev", "3960", "--max-semantic-terra", "199",
        "--max-readjudication", "528",
    )
    raise typer.Exit(run(command, ROOT, output, output.with_name(output.name + "-retries")))


if __name__ == "__main__":
    typer.run(main)
