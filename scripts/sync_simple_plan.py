"""One bounded plan for dry-run reporting and actual reconciliation."""
from __future__ import annotations

from typing import NamedTuple

import github_project as gp
import notion_api as na
from sync_rules import STATUSES, decide
from sync_simple_metadata import closure, differences, metadata
from sync_simple_state import CHECKPOINT, checkpoint_text, history, issue_facts, key_for


class Work(NamedTuple):
    source: dict
    page: dict | None
    item: dict | None
    plan: dict
    properties: dict
    facts: dict | None


def status_fields(values):
    """Raw invalid remote text never enters the public plan."""
    result = {}
    for field, value in values.items():
        invalid = value is not None and value not in STATUSES
        result[field] = "invalid" if invalid else value
        result[field + "_invalid"] = invalid
    return result


def metadata_plan(page, properties, *, enabled=True):
    fields = sorted((differences(page, properties) if page else properties) if enabled else ())
    return {"metadata_fields": fields,
            "metadata_operation": "create" if enabled and page is None else "update" if fields else "none"}


def migration_decision(last, notion_status, project_status, facts, *, missing=False):
    """Use exact inputs only; unsafe first-scan choices require PM alignment."""
    initial = {"target": None, "write_project": False, "write_notion": False, "note": "초기 정렬"}
    exact = decide(last, notion_status, project_status, facts, None)
    if last in {"완료", "취소"} and facts["state"] == "OPEN":
        return initial
    if not missing and last is None and (
            notion_status != project_status or notion_status not in STATUSES):
        return initial
    if facts["state"] == "CLOSED":
        return exact
    if (notion_status in STATUSES and project_status in STATUSES and
            notion_status != project_status):
        restoration = (exact["note"] == "확인 필요" and exact["target"] == last and
                       last in (notion_status, project_status))
        if not restoration:
            return initial
    target = exact["target"]
    if target == "검토 중" and not facts["ready"]:
        return initial
    if target is not None:
        after_notion = target if exact["write_notion"] else notion_status
        after_project = target if exact["write_project"] else project_status
        if target not in STATUSES or (not missing and after_notion != after_project):
            return initial
    elif notion_status == project_status == "검토 중":
        return initial
    return exact


def plan_issue(issue, page, item, snapshot, config, *, archived=False):
    observed = issue_facts(issue, snapshot)
    props = {**metadata("Issue", issue, key_for(issue["databaseId"])), **closure(issue)}
    notion_status = na.read_select(page, "작업 상태") if page else None
    by_option = {value: name for name, value in config["status_options"].items()}
    project_status = by_option.get(item["status_option_id"]) if item else None
    invalid_checkpoint = False
    try:
        last, previous, hold, history_kind = history(page, issue, config, observed)
    except (gp.SyncError, ValueError, KeyError, TypeError, AttributeError):
        # Do not fall back to a stale legacy baseline after new-state corruption.
        last, previous, hold, history_kind = None, None, None, "invalid_checkpoint"
        invalid_checkpoint = True

    initial = {"target": None, "write_project": False, "write_notion": False, "note": "초기 정렬"}
    approximate = history_kind in {"reconstructed", "unknown_facts"}
    if approximate:
        decision = migration_decision(last, notion_status, project_status, observed[0],
                                      missing=page is None or item is None)
    else:
        decision = decide(last, notion_status, project_status, observed[0], previous)
        if (history_kind not in {"checkpoint", "matching_fingerprint"} and
                page is not None and item is not None and
                notion_status == project_status and notion_status in STATUSES):
            decision = decide(notion_status, notion_status, project_status, observed[0], None)
        elif last is None and page is not None and item is not None:
            decision = initial
        elif page is None or (item is None and last is None):
            seed = notion_status or project_status or ("백로그" if page is None else None)
            if seed not in STATUSES:
                decision = initial
            else:
                decision = decide(seed, seed, seed, observed[0], previous)
                decision["write_project"] = decision["target"] is not None and project_status != decision["target"]
                decision["write_notion"] = decision["target"] is not None and notion_status != decision["target"]
    if (not approximate and history_kind != "checkpoint" and observed[0]["state"] == "OPEN" and
            decision["target"] == "검토 중" and not observed[0]["ready"]):
        # Neither equal-value nor missing-side initialization can bless unsupported review.
        decision = (decide(last, notion_status, project_status, observed[0], None)
                    if last in STATUSES and last != "검토 중" else initial)
    if (history_kind == "checkpoint" and last == notion_status == project_status and
            previous == observed[0] and na.read_text(page, "확인 필요") == "확인 필요"):
        # Keep a rejection diagnostic until inputs change; unchanged scans write nothing.
        decision["note"] = "확인 필요"
    if archived or invalid_checkpoint:
        decision = {"target": None, "write_project": False, "write_notion": False, "note": "확인 필요"}
    elif issue.get("stateReason") == "DUPLICATE" and not issue.get("duplicateOf"):
        decision["note"] = decision["note"] or "확인 필요"

    target = decision["target"]
    if item is None and target is not None:
        decision["write_project"] = True
    eligible = not invalid_checkpoint and (page is not None or (target is not None and not archived))
    expected_notion = target if page is None or decision["write_notion"] else notion_status
    expected_project = target if item is None or decision["write_project"] else project_status
    converges = (target is not None and expected_notion == expected_project == target and
                 not archived and not invalid_checkpoint and decision["note"] != "초기 정렬")
    plan = {"kind": "Issue", "number": issue["number"], "issue": issue["number"], **decision,
            **status_fields({"notion": notion_status, "project": project_status,
                             "last_synced": last, "target": target}),
            **metadata_plan(page, props, enabled=eligible),
            "write_notion": bool(page and decision["write_notion"]),
            "write_note": bool(page and not archived and not invalid_checkpoint and
                               na.read_text(page, "확인 필요") != ("확인 필요" if decision["note"] else "")),
            "write_checkpoint": bool(converges and (
                page is None or na.read_text(page, CHECKPOINT) != checkpoint_text(target, observed[0]))),
            "old_hold": hold, "history": history_kind,
            "missing_notion": page is None, "missing_project": item is None,
            "archived_project": archived,
            "diagnostic": "INVALID_CHECKPOINT" if invalid_checkpoint else "",
            "outcome": "failed" if invalid_checkpoint else "planned"}
    return Work(issue, page, item, plan, props, observed[0])


def plan_pull(pull_id, pull, page):
    props = metadata("PR", pull, key_for(pull_id))
    return Work(pull, page, None,
                {"kind": "PR", "number": pull["number"], **metadata_plan(page, props),
                 "outcome": "planned"}, props, None)
