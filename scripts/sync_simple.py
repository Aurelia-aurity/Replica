"""Issue #80 read-only shadow planner; every proposed change stays in the report."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import github_project as gp
import notion_api as na
from sync_rules import STATUSES, decide
from sync_simple_state import index_rows, issue_facts, key_for, legacy_checkpoint


class ProjectClient(gp.GraphQLClient):
    """The shadow token can read Project, but this capability rejects mutations."""

    def request(self, query, variables=None, *, mutation=False):
        gp.require(not mutation, "Project writes disabled")
        return super().request(query, variables, mutation=mutation)


def load_config(env):
    names = ("GITHUB_TOKEN", "PROJECT_TOKEN", "NOTION_TOKEN", "PROJECT_ID",
             "PROJECT_OWNER_ID", "PROJECT_STATUS_FIELD_ID", "PROJECT_STATUS_OPTIONS",
             "NOTION_DATA_SOURCE_ID")
    gp.require(all(env.get(name) for name in names), "Sync configuration missing")
    gp.require(env["PROJECT_ID"] == gp.EXPECTED_PROJECT_ID and
               env["PROJECT_OWNER_ID"] == gp.EXPECTED_PROJECT_OWNER_ID,
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


def sync(github, rest, project_client, notion, config, *, env=None):
    """Read complete snapshots and report proposals without any mutation path."""
    env = os.environ if env is None else env
    snapshot = gp.fetch_repository_facts(github, rest)
    project = gp.fetch_project(
        project_client, project_id=config["project_id"], owner_id=config["owner_id"],
        status_field_id=config["status_field_id"], status_options=config["status_options"],
        repository_node_id=snapshot["repository_node_id"])
    source_id = config["notion_source_id"]
    na.check_schema(notion, source_id, STATUSES)
    index = index_rows(notion, source_id, snapshot)
    gp.require(set(project["items"]).issubset(snapshot["issues"]), "Project source missing")
    by_option = {value: name for name, value in config["status_options"].items()}
    plans = []
    for issue_id, issue in sorted(snapshot["issues"].items(), key=lambda item: item[1]["number"]):
        page = index.get(key_for(issue_id))
        item = project["items"].get(issue_id)
        gp.require(item is None or item["content_id"] == issue["id"], "Project Issue ID mismatch")
        observed = issue_facts(issue, snapshot)
        last, previous, hold, history = legacy_checkpoint(page, issue, config, observed)
        notion_status = na.read_select(page, "작업 상태") if page else None
        project_status = by_option.get(item["status_option_id"]) if item else None
        decision = decide(last, notion_status, project_status, observed[0], previous)
        note_change = bool(page and decision["note"] and
                           na.read_text(page, "확인 필요") != decision["note"])
        plan = {"issue": issue["number"], **decision,
                **status_fields({"notion": notion_status, "project": project_status,
                                 "last_synced": last, "target": decision["target"]}),
                "old_hold": hold, "history": history,
                "missing_notion": page is None, "missing_project": item is None,
                "archived_project": bool(project["archived_items"].get(issue_id))}
        plan["write_notion"] = bool(page and decision["write_notion"])
        plan["write_note"] = note_change
        plans.append(plan)
    counts = {"issues": len(plans),
              **{name: sum(bool(plan[name]) for plan in plans)
                 for name in ("write_project", "write_notion", "write_note", "missing_notion",
                              "missing_project", "archived_project")},
              "initial_alignments": sum(plan["note"] == "초기 정렬" for plan in plans),
              "needs_confirmation": sum(plan["note"] == "확인 필요" for plan in plans),
              "old_holds": sum(plan["old_hold"] is not None for plan in plans)}
    return {"schema": "replica-sync-simple-plan-v1",
            "run": {"id": env.get("GITHUB_RUN_ID"), "attempt": env.get("GITHUB_RUN_ATTEMPT"),
                    "sha": env.get("GITHUB_SHA"), "mode": "dry-run"},
            "items": plans, "counts": counts}


def summary(report):
    """Only issue numbers, enumerated states and fixed notes enter Markdown."""
    lines = ["## Notion sync shadow", "",
             "| Issue | Notion | Project | Target | Project write | Notion status write | Note write | Note |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for item in report["items"]:
        states = [value if value in STATUSES else "unset/invalid"
                  for value in (item["notion"], item["project"], item["target"])]
        lines.append(f"| #{item['issue']} | {' | '.join(states)} | "
                     f"{item['write_project']} | {item['write_notion']} | "
                     f"{item['write_note']} | {item['note']} |")
    lines.extend(["", "Missing rows, archived items and approximate history are listed in the JSON artifact.",
                  "Initial alignments are proposals, not migration approvals.", ""])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Read only (always)")
    parser.add_argument("--report", default="sync-simple-plan.json")
    args = parser.parse_args(argv)
    env = os.environ
    try:
        config = load_config(env)
        report = sync(
            gp.GraphQLClient(env["GITHUB_TOKEN"]), gp.RESTClient(env["GITHUB_TOKEN"]),
            ProjectClient(env["PROJECT_TOKEN"]), na.NotionClient(env["NOTION_TOKEN"]),
            config, env=env)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
        table = summary(report)
        if env.get("GITHUB_STEP_SUMMARY"):
            with Path(env["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
                stream.write(table)
        print(json.dumps(report["counts"], sort_keys=True))
        return 0
    except gp.SyncError as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Remote payloads and transport errors can contain credentials: fixed diagnostic only.
        print(f"Shadow sync failed ({type(exc).__name__}); check configuration and remote schema.",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
