"""Full reconciliation of GitHub facts, Project status and Notion metadata."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import github_project as gp
import notion_api as na
import notification_report as nr
from sync_simple_apply import Changed, apply_issue, apply_pull, project_snapshot
from sync_simple_metadata import closure, differences, metadata
from sync_rules import STATUSES, decide
from sync_simple_state import history, index_rows, issue_facts, key_for


class ProjectClient(gp.GraphQLClient):
    """The same adapter is read-only unless this run explicitly enables writes."""

    def __init__(self, token, *, allow_apply=False, **kwargs):
        super().__init__(token, **kwargs)
        self.allow_apply = allow_apply

    def request(self, query, variables=None, *, mutation=False):
        gp.require(not mutation or self.allow_apply, "Project writes disabled")
        return super().request(query, variables, mutation=mutation)


def load_config(env):
    names = ("GITHUB_TOKEN", "PROJECT_TOKEN", "NOTION_TOKEN", "PROJECT_ID",
             "PROJECT_OWNER_ID", "PROJECT_STATUS_FIELD_ID", "PROJECT_STATUS_OPTIONS",
             "NOTION_DATA_SOURCE_ID", "PM_GITHUB_USER_ID")
    gp.require(all(env.get(name) for name in names), "Sync configuration missing")
    gp.require(env["PROJECT_ID"] == gp.EXPECTED_PROJECT_ID and
               env["PROJECT_OWNER_ID"] == gp.EXPECTED_PROJECT_OWNER_ID and
               env["PM_GITHUB_USER_ID"] == str(gp.EXPECTED_PM_USER_ID),
               "Project configuration mismatch")
    return {"project_id": env["PROJECT_ID"], "owner_id": env["PROJECT_OWNER_ID"],
            "status_field_id": env["PROJECT_STATUS_FIELD_ID"],
            "status_options": gp.parse_status_options(env["PROJECT_STATUS_OPTIONS"]),
            "notion_source_id": na.identifier(env["NOTION_DATA_SOURCE_ID"])}


def status_fields(values):
    """Bound every public status value; raw invalid remote text stays private."""
    result = {}
    for field, value in values.items():
        invalid = value is not None and value not in STATUSES
        result[field] = "invalid" if invalid else value
        result[field + "_invalid"] = invalid
    return result


def sync(github, rest, project_client, notion, config, *, apply=False, env=None,
         ordinary_claim=None, evidence=None):
    """Validate the entire scan before writes; independent items fail separately."""
    env = os.environ if env is None else env
    if env.get("SYNC_ORDINARY_REQUEST") == "true" or ordinary_claim is not None:
        from ordinary_claim import OrdinaryClaim
        gp.require(isinstance(ordinary_claim, OrdinaryClaim) and apply,
                   "Ordinary sync requires a verified claim")
        ordinary_claim.validate(env)
    snapshot = gp.fetch_repository_facts(github, rest)
    project = project_snapshot(project_client, config, snapshot, refresh=True)
    source_id = config["notion_source_id"]
    na.check_schema(notion, source_id, STATUSES)
    index = index_rows(notion, source_id, snapshot)
    by_option = {value: name for name, value in config["status_options"].items()}
    plans, work, pulls = [], [], []
    now = snapshot["server_time"].isoformat()
    # Preflight all metadata/history before the first external effect.
    for pull_id, pull in snapshot["pulls"].items():
        props = metadata("PR", pull, key_for(pull_id))
        page = index.get(key_for(pull_id))
        if page:
            differences(page, props)
        pulls.append((page, props))
    for issue_id, issue in sorted(snapshot["issues"].items(), key=lambda item: item[1]["number"]):
        page = index.get(key_for(issue_id))
        item = project["items"].get(issue_id)
        gp.require(item is None or item["content_id"] == issue["id"], "Project Issue ID mismatch")
        observed = issue_facts(issue, snapshot)
        last, previous, hold, history_kind = history(page, issue, config, observed)
        notion_status = na.read_select(page, "작업 상태") if page else None
        project_status = by_option.get(item["status_option_id"]) if item else None
        decision = decide(last, notion_status, project_status, observed[0], previous)
        if last is None and page is not None and item is not None:
            if notion_status != project_status or notion_status not in STATUSES:
                decision = {"target": None, "write_project": False,
                            "write_notion": False, "note": "초기 정렬"}
            else:
                # Equality is an initialization baseline, including prior cancellation.
                decision = decide(notion_status, notion_status, project_status, observed[0], previous)
        elif page is None or item is None and last is None:
            seed = notion_status or project_status or ("백로그" if page is None else None)
            if seed not in STATUSES or seed == "검토 중" and not observed[0]["ready"]:
                decision = {"target": None, "write_project": False,
                            "write_notion": False, "note": "초기 정렬"}
            else:
                decision = decide(seed, seed, seed, observed[0], previous)
                decision["write_project"] = project_status != decision["target"]
                decision["write_notion"] = notion_status != decision["target"]
        props = {**metadata("Issue", issue, key_for(issue_id)), **closure(issue)}
        if page:
            differences(page, props)
        if issue.get("stateReason") == "DUPLICATE" and not issue.get("duplicateOf"):
            decision["note"] = decision["note"] or "확인 필요"
        note_change = bool(page and na.read_text(page, "확인 필요") != (
            "확인 필요" if decision["note"] else ""))
        plan = {"issue": issue["number"], **decision,
                **status_fields({"notion": notion_status, "project": project_status,
                                 "last_synced": last, "target": decision["target"]}),
                "old_hold": hold, "history": history_kind,
                "missing_notion": page is None, "missing_project": item is None,
                "archived_project": bool(project["archived_items"].get(issue_id)),
                "outcome": "planned"}
        plan["write_notion"] = bool(page and decision["write_notion"])
        plan["write_note"] = note_change
        plans.append(plan)
        work.append((issue, page, item, plan, props, observed[0]))
    effects = {"created_prs": 0, "updated_prs": 0, "failed_prs": 0}
    if apply:
        for issue, page, item, plan, props, facts in work:
            if plan["archived_project"] or page is None and plan["target"] is None:
                plan["outcome"] = "needs_confirmation"
                continue
            try:
                _, _, entry = apply_issue(notion, project_client, config, issue, page, item,
                                          plan, props, facts, now)
                plan["outcome"] = "confirmed" if entry else "needs_confirmation"
                if entry and evidence is not None:
                    evidence.append(entry)
            except Changed:
                plan["outcome"] = "changed"
            except (gp.SyncError, OSError, ValueError, KeyError, TypeError, AttributeError):
                # Raw API values/errors never enter the public report.
                plan["outcome"] = "failed"
        for page, props in pulls:
            try:
                changed = page is not None and bool(differences(page, props))
                apply_pull(notion, config, page, props, now)
                effects["created_prs"] += page is None
                effects["updated_prs"] += changed
            except (gp.SyncError, OSError, ValueError, KeyError, TypeError, AttributeError):
                effects["failed_prs"] += 1
    counts = {"issues": len(plans),
              **{name: sum(bool(plan[name]) for plan in plans)
                 for name in ("write_project", "write_notion", "write_note", "missing_notion",
                              "missing_project", "archived_project")},
              "initial_alignments": sum(plan["note"] == "초기 정렬" for plan in plans),
              "needs_confirmation": sum(plan["note"] == "확인 필요" for plan in plans),
              "old_holds": sum(plan["old_hold"] is not None for plan in plans),
              "failed": sum(plan["outcome"] == "failed" for plan in plans) + effects["failed_prs"],
              "changed": sum(plan["outcome"] == "changed" for plan in plans),
              **effects}
    return {"schema": "replica-sync-simple-plan-v1",
            "run": {"id": env.get("GITHUB_RUN_ID"), "attempt": env.get("GITHUB_RUN_ATTEMPT"),
                    "sha": env.get("GITHUB_SHA"), "mode": "apply" if apply else "dry-run"},
            "items": plans, "counts": counts, "holds": []}


def summary(report):
    """Only issue numbers, enumerated states and fixed notes enter Markdown."""
    lines = ["## Notion sync", "",
             "| Issue | Notion | Project | Target | Project write | Notion status write | Note write | Note |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for item in report["items"]:
        states = [value if value in STATUSES else "unset/invalid"
                  for value in (item["notion"], item["project"], item["target"])]
        lines.append(f"| #{item['issue']} | {' | '.join(states)} | "
                     f"{item['write_project']} | {item['write_notion']} | "
                     f"{item['write_note']} | {item['note']} |")
    lines.extend(["", "Per-item outcomes and missing/archived rows are listed in the JSON artifact.",
                  "Initial alignment requires matching values selected by the PM.", ""])
    return "\n".join(lines)


def notification_kind(report):
    if report["counts"]["failed"]:
        return "failed"
    if report["run"]["mode"] == "dry-run":
        return "skipped"
    return ("partial" if any(item["outcome"] in {"changed", "needs_confirmation"}
                             for item in report["items"]) else "complete")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Read only; no API writes")
    parser.add_argument("--report", default="sync-simple-plan.json")
    parser.add_argument("--notification-report")
    args = parser.parse_args(argv)
    env = os.environ
    enabled = env.get("NOTION_SYNC_ENABLED") == "true"
    if not enabled and not args.dry_run:
        if args.notification_report:
            nr.write(args.notification_report, env, kind="skipped")
        print("Sync disabled")
        return 0
    try:
        config = load_config(env)
        report = sync(
            gp.GraphQLClient(env["GITHUB_TOKEN"]), gp.RESTClient(env["GITHUB_TOKEN"]),
            ProjectClient(env["PROJECT_TOKEN"], allow_apply=not args.dry_run),
            na.NotionClient(env["NOTION_TOKEN"], allow_apply=not args.dry_run),
            config, apply=not args.dry_run, env=env)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
        table = summary(report)
        if env.get("GITHUB_STEP_SUMMARY"):
            with Path(env["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
                stream.write(table)
        print(json.dumps(report["counts"], sort_keys=True))
        failed = report["counts"]["failed"] > 0
        if args.notification_report:
            nr.write(args.notification_report, env,
                     kind=notification_kind(report),
                     dry_run=args.dry_run, scan_complete=not (failed or args.dry_run), holds=[])
        return 1 if failed else 0
    except gp.SyncError as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Remote payloads and transport errors can contain credentials: fixed diagnostic only.
        print(f"Sync failed ({type(exc).__name__}); check configuration and remote schema.",
              file=sys.stderr)
    if args.notification_report:
        nr.write(args.notification_report, env, kind="failed", dry_run=args.dry_run)
    return 1


if __name__ == "__main__":
    sys.exit(main())
