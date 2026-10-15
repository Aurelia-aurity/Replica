"""Pure contracts for bidirectional Notion/GitHub Project status reconciliation."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

GENERAL_STATES = frozenset({"백로그", "준비 중", "진행 중"})
ALL_STATES = GENERAL_STATES | {"검토 중", "완료"}
REQUEST_PHASES = frozenset({
    "accepted", "waiting", "prepared", "sent", "uncertain", "confirmed",
    "completed", "rejected", "held",
})
REQUEST_UI_STATES = frozenset({
    "반영 대기", "반영 완료", "요청 거절", "자동 확인 중", "PM 확인 필요",
})
STAMP_KEYS = frozenset({"field_id", "option_id", "value_id", "updated_at"})


class StatusSyncError(ValueError):
    """Invalid persisted status-sync contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StatusSyncError(message)


def validate_stamp(stamp: object) -> dict:
    _require(isinstance(stamp, dict) and set(stamp) == STAMP_KEYS,
             "Project Status baseline metadata shape mismatch")
    _require(isinstance(stamp["field_id"], str) and bool(stamp["field_id"]),
             "Project Status field ID missing")
    for key in ("option_id", "value_id", "updated_at"):
        _require(stamp[key] is None or isinstance(stamp[key], str),
                 "Project Status value metadata type mismatch")
    if stamp["updated_at"] is not None:
        try:
            parsed = datetime.fromisoformat(stamp["updated_at"].replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise StatusSyncError("Project Status updatedAt is invalid") from None
        _require(parsed.tzinfo is not None, "Project Status updatedAt has no timezone")
    return stamp


def make_baseline(notion_status: str | None, project_stamp: dict,
                  project_item_id: str, facts_fingerprint: str,
                  observed_at: str | None) -> dict:
    _require(notion_status is None or notion_status in ALL_STATES,
             "Notion status baseline is invalid")
    validate_stamp(project_stamp)
    _require(isinstance(project_item_id, str) and bool(project_item_id),
             "Project item baseline ID is missing")
    _require(isinstance(facts_fingerprint, str) and len(facts_fingerprint) == 64,
             "Status baseline facts fingerprint is invalid")
    if observed_at is not None:
        try:
            parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise StatusSyncError("Status snapshot observed_at is invalid") from None
        _require(parsed.tzinfo is not None, "Status snapshot observed_at has no timezone")
    return {"notion_status": notion_status, "project": dict(project_stamp),
            "project_item_id": project_item_id,
            "facts_fingerprint": facts_fingerprint, "observed_at": observed_at}


def _project_change(baseline: dict, current: dict) -> bool | None:
    """Return changed/unchanged, or None when current metadata cannot prove it."""
    validate_stamp(baseline)
    validate_stamp(current)
    if baseline["field_id"] != current["field_id"]:
        return None
    if baseline["option_id"] != current["option_id"]:
        return True
    if (baseline["value_id"] is None or current["value_id"] is None or
            baseline["updated_at"] is None or current["updated_at"] is None):
        return None
    return (baseline["value_id"] != current["value_id"] or
            baseline["updated_at"] != current["updated_at"])


def decide(baseline: dict | None, notion_status: str | None, project_stamp: dict,
           project_status: str | None, *, facts_override: bool = False,
           facts_verified: bool = False) -> dict:
    """Classify a snapshot; differing two-sided changes never guess their order."""
    _require(notion_status is None or notion_status in ALL_STATES,
             "Notion task status is invalid")
    _require(project_status is None or project_status in ALL_STATES,
             "Project status is invalid")
    validate_stamp(project_stamp)
    if baseline is None:
        return {"action": "initialize", "target": None, "notion_changed": False,
                "project_changed": False}
    _require(isinstance(baseline, dict) and set(baseline) ==
             {"notion_status", "project", "project_item_id", "facts_fingerprint",
              "observed_at"}, "Status baseline shape mismatch")
    make_baseline(baseline["notion_status"], baseline["project"],
                  baseline["project_item_id"], baseline["facts_fingerprint"],
                  baseline["observed_at"])
    before_notion = baseline["notion_status"]
    notion_changed = notion_status != before_notion
    project_changed = _project_change(baseline["project"], project_stamp)

    if facts_override:
        return {"action": "facts_override", "target": None,
                "notion_changed": notion_changed, "project_changed": project_changed}
    # Preserve an invalid Notion request even when both visible values happen to
    # match (including null); it must not be mistaken for convergence. Verified
    # GitHub facts take precedence and are handled as a separate source transition.
    if notion_changed and notion_status not in GENERAL_STATES:
        return {"action": "hold_invalid_request", "target": None,
                "notion_changed": True, "project_changed": project_changed}
    same_project_field = baseline["project"]["field_id"] == project_stamp["field_id"]
    if notion_status == project_status and facts_verified and same_project_field:
        return {"action": "converged", "target": notion_status,
                "notion_changed": notion_changed,
                "project_changed": (False if project_changed is None else project_changed)}
    if project_changed is None:
        return {"action": "hold_unknown", "target": None,
                "notion_changed": notion_changed, "project_changed": None}
    if notion_changed and project_changed:
        if notion_status == project_status:
            if not facts_verified:
                return {"action": "hold_unknown", "target": None,
                        "notion_changed": True, "project_changed": True}
            return {"action": "converged", "target": notion_status,
                    "notion_changed": True, "project_changed": True}
        return {"action": "conflict", "target": None,
                "notion_changed": True, "project_changed": True}
    if notion_changed:
        if notion_status not in GENERAL_STATES:
            return {"action": "hold_invalid_request", "target": None,
                    "notion_changed": True, "project_changed": False}
        return {"action": "notion_request", "target": notion_status,
                "notion_changed": True, "project_changed": False}
    if project_changed:
        return {"action": "github_update", "target": project_status,
                "notion_changed": False, "project_changed": True}
    return {"action": "unchanged", "target": notion_status,
            "notion_changed": False, "project_changed": False}


def request_record(request_id: str, target: str | None, *, observed_from: str | None,
                   observed_to: str | None, prior_notion_status: str | None,
                   project_option_id: str | None, phase: str = "accepted",
                   reason: str = "") -> dict:
    _require(isinstance(request_id, str) and request_id and len(request_id) <= 128,
             "Status request ID invalid")
    _require(target is None or target in ALL_STATES, "Status request target invalid")
    _require(phase in REQUEST_PHASES, "Status request phase invalid")
    _require(prior_notion_status is None or prior_notion_status in ALL_STATES,
             "Status request prior value invalid")
    _require(project_option_id is None or isinstance(project_option_id, str),
             "Status request Project option invalid")
    for value in (observed_from, observed_to):
        if value is not None:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                raise StatusSyncError("Status request observation time invalid") from None
            _require(parsed.tzinfo is not None, "Status request observation time has no timezone")
    _require(isinstance(reason, str) and len(reason) <= 1000,
             "Status request reason invalid")
    return {"id": request_id, "target": target, "phase": phase,
            "observed_from": observed_from, "observed_to": observed_to,
            "prior_notion_status": prior_notion_status,
            "project_option_id": project_option_id, "requested_by": None,
            "reason": reason}


def fingerprint(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
