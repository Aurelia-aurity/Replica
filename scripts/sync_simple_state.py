"""Bind canonical rows and reconstruct read-only legacy status checkpoints."""
from __future__ import annotations

import hashlib
import json
import re

import github_project as gp
from notion_api import bound_page, query_all, read_select, read_text
from sync_rules import STATUSES

KEY_RE = re.compile(r"gh:([1-9][0-9]*):issue:([1-9][0-9]*)\Z")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def key_for(issue_id):
    return f"gh:{gp.REPOSITORY_ID}:issue:{issue_id}"


def index_rows(notion, source_id, snapshot):
    """Keys are REST /issues IDs, never PR database IDs; numbers only verify."""
    source = {key_for(row_id): (kind, row)
              for kind, rows in (("Issue", snapshot["issues"]), ("PR", snapshot["pulls"]))
              for row_id, row in rows.items()}
    gp.require(len(source) == len(snapshot["issues"]) + len(snapshot["pulls"]),
               "Canonical key collision")
    index, archived_keys = {}, set()
    for archived in (False, True):
        for page in query_all(notion, source_id, archived):
            bound_page(page, source_id)
            key = read_text(page, "동기화 키")
            if not key:
                continue
            match = KEY_RE.fullmatch(key)
            gp.require(key == "replica-sync-control" or
                       bool(match and int(match[1]) == gp.REPOSITORY_ID),
                       "Notion canonical key invalid")
            if archived or page.get("archived") or page.get("in_trash"):
                archived_keys.add(key)
                continue
            gp.require(key not in index, "Duplicate Notion canonical key")
            if key != "replica-sync-control":
                gp.require(key in source, "Notion key absent from GitHub snapshot")
                kind, row = source[key]
                number = page["properties"]["번호"]["number"]
                gp.require(read_select(page, "종류") == kind and type(number) is int
                           and number == row["number"], "Notion kind/number mismatch")
            else:
                gp.require(read_select(page, "종류") == "Sync", "Notion control kind mismatch")
            index[key] = page
    gp.require(not archived_keys.intersection(set(source) | set(index)),
               "Notion canonical row archived")
    return index


def issue_facts(issue, snapshot):
    """Mirror the existing engine's ready closing-linked PR and semantic hash."""
    refs, eligible, seen = [], [], set()
    pulls = {pull["id"]: pull for pull in snapshot["pulls"].values()}
    repo_id = snapshot["repository_node_id"]
    for ref in issue["linked_prs"]:
        gp.require(ref["id"] not in seen, "Duplicate closing PR reference")
        seen.add(ref["id"])
        base, repo = ref.get("baseRepository") or {}, ref.get("repository") or {}
        gp.require(isinstance(base.get("id"), str) and isinstance(repo.get("id"), str)
                   and ref.get("state") in {"OPEN", "CLOSED", "MERGED"}
                   and type(ref.get("isDraft")) is bool, "Closing PR reference invalid")
        entry = {"id": ref["id"], "number": ref["number"], "state": ref["state"],
                 "isDraft": ref["isDraft"], "baseRefName": ref.get("baseRefName"),
                 "baseRepository": base["id"], "repository": repo["id"]}
        refs.append(entry)
        if repo["id"] == repo_id:
            pull = pulls.get(ref["id"])
            gp.require(pull is not None and all(
                pull.get(field) == ref.get(field)
                for field in ("number", "state", "isDraft", "baseRefName")) and
                (pull.get("baseRepository") or {}).get("id") == base["id"],
                "Closing PR and repository snapshot mismatch")
        if (repo["id"] == repo_id and base["id"] == repo_id
                and ref.get("baseRefName") == "main"
                and ref["state"] == "OPEN" and not ref["isDraft"]):
            eligible.append(ref["id"])
    refs.sort(key=lambda value: value["id"])
    reopens = [{"id": event["id"], "createdAt": event["createdAt"]}
               for event in sorted(issue["reopens"],
                                   key=lambda event: (gp.parse_time(event["createdAt"]), event["id"]))]
    semantic = {
        "issue_id": issue["databaseId"], "issue_node_id": issue["id"],
        "state": issue["state"], "stateReason": issue.get("stateReason"),
        "closedAt": issue.get("closedAt"),
        "duplicate_id": (issue.get("duplicateOf") or {}).get("id"),
        "reopens": reopens, "linked_prs": refs,
    }
    facts = {"state": issue["state"], "reason": issue.get("stateReason"), "ready": bool(eligible)}
    return facts, digest(semantic), digest(sorted(eligible))


def legacy_checkpoint(page, issue, config, observed):
    """A matching semantic hash is exact; other history is explicitly approximate."""
    current, fingerprint, _ = observed
    try:
        state = json.loads(read_text(page, "동기화 내부 상태")) if page else None
    except (ValueError, TypeError):
        state = None
    if not isinstance(state, dict):
        return None, None, None, "initialization"
    binding = (state.get("repository_id") == gp.REPOSITORY_ID and
               state.get("issue_id") == issue["databaseId"] and
               state.get("project_id") == config["project_id"] and state.get("v") in (1, 2))
    if not binding:
        return None, None, None, "initialization"
    hold = state.get("hold")
    # Never copy untrusted free text into the public artifact or summary.
    old_hold = ({"code": hold["code"]} if isinstance(hold, dict)
                and isinstance(hold.get("code"), str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", hold["code"]) else None)
    baseline = state.get("baseline")
    if (not isinstance(baseline, dict) or baseline.get("notion_status") not in STATUSES
            or not isinstance(baseline.get("facts_fingerprint"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", baseline["facts_fingerprint"])):
        return None, None, old_hold, "initialization"
    last = baseline["notion_status"]
    if baseline.get("facts_fingerprint") == fingerprint:
        return last, current, old_hold, "matching_fingerprint"
    previous_hash = state.get("review_pr_hash")
    if previous_hash == digest([]):
        ready = False
    elif isinstance(previous_hash, str) and re.fullmatch(r"[0-9a-f]{64}", previous_hash):
        ready = True
    else:
        return last, None, old_hold, "unknown_facts"
    previous = {"state": "CLOSED" if last in {"완료", "취소"} else "OPEN",
                "reason": None, "ready": ready}
    return last, previous, old_hold, "reconstructed"
