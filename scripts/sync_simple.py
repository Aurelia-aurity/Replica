"""Issue #80 shadow planner. Default mode reads only; apply is separately gated."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import github_project as gp
import notion_api as na
from sync_rules import decide
from sync_simple_state import index_rows, issue_facts, key_for, legacy_checkpoint


class ProjectClient(gp.GraphQLClient):
    """The shadow token can read Project, but this capability rejects mutations."""

    def __init__(self, token, *, allow_apply=False):
        super().__init__(token)
        self.allow_apply = allow_apply

    def request(self, query, variables=None, *, mutation=False):
        gp.require(not mutation or self.allow_apply, "Project writes disabled")
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


def sync(github, rest, project_client, notion, config, *, apply=False, env=None):
    """Scan everything before any apply; never mutate the legacy internal state."""
    env = os.environ if env is None else env
    gp.require(not apply or env.get("SYNC_SIMPLE_APPLY") == "true",
               "--apply requires SYNC_SIMPLE_APPLY=true")
    snapshot = gp.fetch_repository_facts(github, rest)
    project = gp.fetch_project(
        project_client, project_id=config["project_id"], owner_id=config["owner_id"],
        status_field_id=config["status_field_id"], status_options=config["status_options"],
        repository_node_id=snapshot["repository_node_id"])
    source_id = config["notion_source_id"]
    na.check_schema(notion, source_id)
    index = index_rows(notion, source_id, snapshot)
    gp.require(set(project["items"]).issubset(snapshot["issues"]), "Project source missing")
    by_option = {value: name for name, value in config["status_options"].items()}
    plans, bindings = [], []
    for issue_id, issue in sorted(snapshot["issues"].items(), key=lambda item: item[1]["number"]):
        page = index.get(key_for(issue_id))
        item = project["items"].get(issue_id)
        gp.require(item is None or item["content_id"] == issue["id"], "Project Issue ID mismatch")
        observed = issue_facts(issue, snapshot)
        last, previous, hold, history = legacy_checkpoint(page, issue, config, observed)
        notion_status = na.read_select(page, "작업 상태") if page else None
        project_status = by_option.get(item["status_option_id"]) if item else None
        decision = decide(last, notion_status, project_status, observed[0], previous)
        # A diagnostic remains until reviewed or a later status PATCH replaces it.
        # Clearing it on the next unchanged scan would manufacture a second write.
        note_change = bool(page and decision["note"] and
                           na.read_text(page, "확인 필요") != decision["note"])
        plan = {"issue": issue["number"], "notion": notion_status, "project": project_status,
                "last_synced": last, **decision, "old_hold": hold, "history": history,
                "missing_notion": page is None, "missing_project": item is None,
                "archived_project": bool(project["archived_items"].get(issue_id))}
        plan["write_notion"] = bool(page and (decision["write_notion"] or note_change))
        # A missing row cannot be created in this PATCH-only PR; report it explicitly.
        plans.append(plan)
        bindings.append((issue, page, item, decision, note_change))
    if apply:
        gp.require(all(not plan["missing_notion"] and not plan["archived_project"]
                       for plan in plans), "Apply requires active Notion/Project bindings")
        for plan, (issue, page, item, decision, _) in zip(plans, bindings):
            if plan["write_project"]:
                gp.require(plan["target"] in config["status_options"], "Project target invalid")
                if item is None:
                    item_id = gp.add_project_issue(project_client, config["project_id"], issue["id"])
                    added = gp.fetch_project_item(
                        project_client, config["project_id"], item_id,
                        config["status_field_id"], status_options=config["status_options"])
                    gp.require(added["content_id"] == issue["id"], "Project add readback mismatch")
                else:
                    item_id = item["id"]
                option = config["status_options"][plan["target"]]
                gp.set_project_status(project_client, config["project_id"], item_id,
                                      config["status_field_id"], option)
                readback = gp.fetch_project_item(
                    project_client, config["project_id"], item_id,
                    config["status_field_id"], status_options=config["status_options"])
                gp.require(readback["content_id"] == issue["id"]
                           and readback["status_option_id"] == option,
                           "Project status readback mismatch")
            if plan["write_notion"]:
                properties = {"확인 필요": na.text(plan["note"])}
                if decision["write_notion"]:
                    properties["작업 상태"] = {
                        "select": {"name": plan["target"]} if plan["target"] is not None else None}
                page_id = na.identifier(page["id"])
                notion.request("PATCH", f"/pages/{page_id}", {"properties": properties})
                readback = notion.request("GET", f"/pages/{page_id}")
                na.bound_page(readback, source_id)
                gp.require(na.identifier(readback["id"]) == page_id
                           and na.read_text(readback, "동기화 키") == key_for(issue["databaseId"])
                           and na.read_select(readback, "종류") == "Issue"
                           and readback["properties"]["번호"]["number"] == issue["number"],
                           "Notion write readback identity mismatch")
                gp.require(na.read_text(readback, "확인 필요") == plan["note"]
                           and (not decision["write_notion"] or
                                na.read_select(readback, "작업 상태") == plan["target"]),
                           "Notion write readback mismatch")
    counts = {"issues": len(plans),
              **{name: sum(bool(plan[name]) for plan in plans)
                 for name in ("write_project", "write_notion", "missing_notion",
                              "missing_project", "archived_project")},
              "initial_alignments": sum(plan["note"] == "초기 정렬" for plan in plans),
              "needs_confirmation": sum(plan["note"] == "확인 필요" for plan in plans),
              "old_holds": sum(plan["old_hold"] is not None for plan in plans)}
    return {"schema": "replica-sync-simple-plan-v1",
            "run": {"id": env.get("GITHUB_RUN_ID"), "attempt": env.get("GITHUB_RUN_ATTEMPT"),
                    "sha": env.get("GITHUB_SHA"), "mode": "apply" if apply else "dry-run"},
            "items": plans, "counts": counts}


def summary(report):
    """Only issue numbers, enumerated states and fixed notes enter Markdown."""
    lines = ["## Notion sync shadow", "",
             "| Issue | Notion | Project | Target | Project write | Notion write | Note |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    from sync_rules import STATUSES
    for item in report["items"]:
        states = [value if value in STATUSES else "unset/invalid"
                  for value in (item["notion"], item["project"], item["target"])]
        lines.append(f"| #{item['issue']} | {' | '.join(states)} | "
                     f"{item['write_project']} | {item['write_notion']} | {item['note']} |")
    lines.extend(["", "Missing rows, archived items and approximate history are listed in the JSON artifact.",
                  "Initial alignments are proposals, not migration approvals.", ""])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read only (default)")
    mode.add_argument("--apply", action="store_true", help="Requires SYNC_SIMPLE_APPLY=true")
    parser.add_argument("--report", default="sync-simple-plan.json")
    args = parser.parse_args(argv)
    env = os.environ
    try:
        gp.require(not args.apply or env.get("SYNC_SIMPLE_APPLY") == "true",
                   "--apply requires SYNC_SIMPLE_APPLY=true")
        config = load_config(env)
        report = sync(
            gp.GraphQLClient(env["GITHUB_TOKEN"]), gp.RESTClient(env["GITHUB_TOKEN"]),
            ProjectClient(env["PROJECT_TOKEN"], allow_apply=args.apply),
            na.NotionClient(env["NOTION_TOKEN"], allow_apply=args.apply),
            config, apply=args.apply, env=env)
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
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # Remote payloads and transport errors can contain credentials: fixed diagnostic only.
        print("Shadow sync failed; check configuration and remote schema.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
