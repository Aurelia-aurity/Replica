"""Secret-free, bounded sync notification contract (data only)."""
import json
import re
import subprocess
from pathlib import Path
from datetime import datetime

REPO_ID = 1392442366
FILE_NAME = "notion-notification-report.json"
MAX_BYTES = 2_000_000


def validate(report, *, run_id=None, attempt=None, sha=None):
    common = {"repository_id", "run_id", "attempt", "workflow_sha",
              "checked_out_sha", "dry_run", "kind", "scan_complete", "holds"}
    if not isinstance(report, dict) or type(report.get("schema")) is not int:
        raise ValueError("Invalid notification report")
    schema = report["schema"]
    keys = common | {"schema"} if schema == 1 else common | {
        "schema", "observed_at", "observation_uncertainty_seconds"}
    if schema not in (1, 2) or set(report) != keys:
        raise ValueError("Invalid notification report")
    if type(report["repository_id"]) is not int or report["repository_id"] != REPO_ID:
        raise ValueError("Invalid notification report identity")
    for key in ("run_id", "attempt"):
        if type(report[key]) is not int or report[key] <= 0:
            raise ValueError("Invalid notification report counter")
    if any(not isinstance(report[k], str) or not re.fullmatch(r"[0-9a-f]{40}", report[k])
           for k in ("workflow_sha", "checked_out_sha")):
        raise ValueError("Invalid notification report SHA")
    if report["workflow_sha"] != report["checked_out_sha"]:
        raise ValueError("Notification report checkout mismatch")
    if type(report["dry_run"]) is not bool or type(report["scan_complete"]) is not bool:
        raise ValueError("Invalid notification report flags")
    if report["kind"] not in ("complete", "partial", "failed", "skipped"):
        raise ValueError("Invalid notification report kind")
    full = report["scan_complete"]
    if full and (report["dry_run"] or report["kind"] not in ("complete", "partial")):
        raise ValueError("Invalid notification report completion")
    if schema == 2:
        observed_at = report["observed_at"]
        uncertainty = report["observation_uncertainty_seconds"]
        if (observed_at is None) != (uncertainty is None):
            raise ValueError("Incomplete notification observation time")
        if observed_at is not None:
            if (not isinstance(observed_at, str) or not observed_at.endswith("Z") or
                    type(uncertainty) is not int or not 0 <= uncertainty <= 60):
                raise ValueError("Invalid notification observation time")
            try:
                parsed = datetime.fromisoformat(observed_at[:-1] + "+00:00")
            except ValueError:
                raise ValueError("Invalid notification observation time") from None
            if parsed.utcoffset().total_seconds() != 0:
                raise ValueError("Invalid notification observation time")
        if not full and (observed_at is not None or uncertainty is not None):
            raise ValueError("Incomplete report cannot carry observation time")
    holds = report["holds"]
    if not isinstance(holds, list) or len(holds) > 10000:
        raise ValueError("Invalid notification report holds")
    seen = set()
    for item in holds:
        if not isinstance(item, dict) or set(item) != {"issue_number", "reason_code"}:
            raise ValueError("Invalid notification target")
        n, code = item["issue_number"], item["reason_code"]
        if type(n) is not int or n <= 0 or not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
            raise ValueError("Invalid notification target")
        if (n, code) in seen:
            raise ValueError("Duplicate notification target")
        seen.add((n, code))
    if not full and holds or report["kind"] == "complete" and holds:
        raise ValueError("Inconsistent notification report")
    if run_id is not None and report["run_id"] != run_id or attempt is not None and report["attempt"] != attempt:
        raise ValueError("Notification report run mismatch")
    if sha is not None and report["workflow_sha"] != sha:
        raise ValueError("Notification report source mismatch")
    return report


def collect_holds(rows):
    result = set()
    for row, state in rows:
        if not state:
            continue
        number = row["properties"]["번호"]["number"]
        codes = []
        if state.get("hold"):
            codes.append(state["hold"]["code"])
        if state.get("projection"):
            codes.append("PROJECTION_PENDING")
        if (state.get("resume") or {}).get("display_pending"):
            codes.append("RESUME_DISPLAY_PENDING")
        result.update((number, code) for code in codes)
    return [{"issue_number": n, "reason_code": c} for n, c in sorted(result)]


def write(path, env, *, kind, dry_run=False, scan_complete=False, holds=(),
          observed_at=None, observation_uncertainty_seconds=None):
    # No source data, titles, raw errors or credentials enter this artifact.
    checkout = subprocess.run(["git", "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
    report = validate({"schema": 2, "repository_id": REPO_ID,
                       "run_id": int(env["GITHUB_RUN_ID"]),
                       "attempt": int(env["GITHUB_RUN_ATTEMPT"]),
                       "workflow_sha": env["GITHUB_SHA"], "checked_out_sha": checkout,
                       "dry_run": dry_run, "kind": kind,
                       "scan_complete": scan_complete, "holds": list(holds),
                       "observed_at": observed_at if scan_complete and not dry_run else None,
                       "observation_uncertainty_seconds": (
                           observation_uncertainty_seconds if scan_complete and not dry_run else None)})
    data = json.dumps(report, sort_keys=True).encode()
    if len(data) > MAX_BYTES:
        raise ValueError("Notification report too large")
    Path(path).write_bytes(data + b"\n")
