"""Full reconciliation of GitHub facts, Project status and Notion metadata."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import github_project as gp
import notion_api as na
import notification_report as nr
from sync_simple_apply import Changed, apply_issue, apply_metadata, project_snapshot
from sync_rules import STATUSES
from sync_simple_state import index_rows, key_for
from sync_simple_plan import plan_issue, plan_pull


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


def sync(github, rest, project_client, notion, config, *, apply=False, env=None,
         ordinary_claim=None, evidence=None, scan_context=None):
    """Validate the entire scan before writes; independent items fail separately."""
    env = os.environ if env is None else env
    if env.get("SYNC_ORDINARY_REQUEST") == "true" or ordinary_claim is not None:
        from ordinary_claim import OrdinaryClaim
        gp.require(isinstance(ordinary_claim, OrdinaryClaim) and apply,
                   "Ordinary sync requires a verified claim")
        ordinary_claim.validate(env)
    snapshot = gp.fetch_repository_facts(github, rest)
    project = project_snapshot(project_client, config, snapshot)
    if scan_context is not None:
        # Private caller context, deliberately excluded from the public report.
        scan_context.update(facts=snapshot, project=project)
    source_id = config["notion_source_id"]
    na.check_schema(notion, source_id, STATUSES)
    index = index_rows(notion, source_id, snapshot)
    now = snapshot["server_time"].isoformat()
    # Every preflight plan is also the input to apply; no metadata values are public.
    work = [plan_issue(issue, index.get(key_for(issue_id)), project["items"].get(issue_id),
                       snapshot, config, archived=bool(project["archived_items"].get(issue_id)))
            for issue_id, issue in sorted(snapshot["issues"].items(), key=lambda row: row[1]["number"])]
    pulls = [plan_pull(pull_id, pull, index.get(key_for(pull_id)))
             for pull_id, pull in sorted(snapshot["pulls"].items(), key=lambda row: row[1]["number"])]
    plans = [row.plan for row in work]
    effects = {"created_prs": 0, "updated_prs": 0, "failed_prs": 0}
    for row in work:
        plan = row.plan
        if plan["outcome"] == "failed":
            print(f"Issue #{plan['number']}: {plan['diagnostic']}", file=sys.stderr)
            continue
        if not apply:
            continue
        if row.page is None and plan["metadata_operation"] == "none":
            plan["outcome"] = "needs_confirmation"
            continue
        try:
            _, confirmed_item, entry = apply_issue(notion, project_client, config, row, now)
            if confirmed_item is not None:
                project["items"][row.source["databaseId"]] = confirmed_item
            plan["outcome"] = "confirmed" if entry else "needs_confirmation"
            if entry and evidence is not None:
                evidence.append(entry)
        except Changed:
            plan["outcome"] = "changed"
        except (gp.SyncError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            plan["outcome"] = "failed"
            print(f"Issue #{plan['number']}: {type(exc).__name__}", file=sys.stderr)
    if apply:
        for row in pulls:
            try:
                apply_metadata(notion, config, row, now)
                row.plan["outcome"] = "confirmed"
                effects["created_prs"] += row.plan["metadata_operation"] == "create"
                effects["updated_prs"] += row.plan["metadata_operation"] == "update"
            except Changed:
                row.plan["outcome"] = "changed"
            except (gp.SyncError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                row.plan["outcome"] = "failed"
                effects["failed_prs"] += 1
                print(f"PR #{row.plan['number']}: {type(exc).__name__}", file=sys.stderr)
    counts = {"issues": len(plans),
              **{name: sum(bool(plan[name]) for plan in plans)
                 for name in ("write_project", "write_notion", "write_note", "missing_notion",
                              "missing_project", "archived_project")},
              "initial_alignments": sum(plan["note"] == "초기 정렬" for plan in plans),
              "needs_confirmation": sum(plan["note"] == "확인 필요" for plan in plans),
              "old_holds": sum(plan["old_hold"] is not None for plan in plans),
              "failed": sum(plan["outcome"] == "failed" for plan in plans) + effects["failed_prs"],
              "changed": sum(row.plan["outcome"] == "changed" for row in work + pulls),
              "planned_issue_metadata_updates": sum(plan["metadata_operation"] == "update" for plan in plans),
              "planned_pr_creates": sum(row.plan["metadata_operation"] == "create" for row in pulls),
              "planned_pr_updates": sum(row.plan["metadata_operation"] == "update" for row in pulls),
              **effects}
    return {"schema": "replica-sync-simple-plan-v1",
            "run": {"id": env.get("GITHUB_RUN_ID"), "attempt": env.get("GITHUB_RUN_ATTEMPT"),
                    "sha": env.get("GITHUB_SHA"), "mode": "apply" if apply else "dry-run"},
            "items": plans, "pulls": [row.plan for row in pulls], "counts": counts, "holds": []}


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
    migrations = [f"#{item['issue']} ({item['history']})" for item in report["items"]
                  if item["history"] != "checkpoint"]
    if migrations:
        lines.extend(["", "Migration review: " + ", ".join(migrations)])
    lines.extend(["", "| Kind | Number | Metadata operation | Changed fields |",
                  "| --- | --- | --- | --- |"])
    for item in report["items"] + report["pulls"]:
        if item["metadata_operation"] != "none":
            lines.append(f"| {item['kind']} | #{item['number']} | {item['metadata_operation']} | "
                         f"{', '.join(item['metadata_fields'])} |")
    lines.extend(["", "Per-item outcomes and missing/archived rows are listed in the JSON artifact.",
                  "Initial alignment requires matching values selected by the PM.", ""])
    return "\n".join(lines)


def notification_kind(report):
    if report["counts"]["failed"]:
        return "failed"
    if report["run"]["mode"] == "dry-run":
        return "skipped"
    return ("partial" if any(item["outcome"] in {"changed", "needs_confirmation"}
                             for item in report["items"] + report["pulls"]) else "complete")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Read only; no API writes")
    parser.add_argument("--report", default="sync-simple-plan.json")
    parser.add_argument("--notification-report")
    args = parser.parse_args(argv)
    env = os.environ
    try:
        enabled = env.get("NOTION_SYNC_ENABLED") == "true"
        if not enabled and not args.dry_run:
            print("Sync disabled")
            if args.notification_report:
                try:
                    nr.write(args.notification_report, env, kind="skipped")
                except (OSError, ValueError, KeyError, TypeError, AttributeError,
                        subprocess.SubprocessError):
                    print("Notification report unavailable; run binding missing or invalid.",
                          file=sys.stderr)
                    return 1
            return 0
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
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        # Remote payloads and transport errors can contain credentials: fixed diagnostic only.
        print(f"Sync failed ({type(exc).__name__}); check configuration and remote schema.",
              file=sys.stderr)
    if args.notification_report:
        try:
            nr.write(args.notification_report, env, kind="failed", dry_run=args.dry_run)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
            print("Notification report unavailable; run binding missing or invalid.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
