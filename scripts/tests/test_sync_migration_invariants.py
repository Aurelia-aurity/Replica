"""Reachable migration inputs: closure safety and real writer convergence."""
import itertools
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_project as gp
import sync_simple_plan as planner
from sync_rules import STATUSES
from sync_simple_apply import apply_issue
from sync_simple_state import CHECKPOINT, checkpoint_text, read_checkpoint
from sync_simple_fakes import CONFIG, NOW, OPTIONS, GitHub, Notion, Project, REST, conn, text
from test_sync_simple_state import pull


HISTORIES = ("initialization", "matching_fingerprint", "checkpoint",
             "reconstructed", "unknown_facts")
VALUES = (None, *STATUSES, "INVALID")
FACTS = tuple({"state": state, "reason": reason, "ready": ready}
              for state, reason in (("OPEN", None), ("CLOSED", "COMPLETED"),
                                    ("CLOSED", "NOT_PLANNED"), ("CLOSED", "DUPLICATE"),
                                    ("CLOSED", None))
              for ready in (False, True))
APPROXIMATE = {"reconstructed", "unknown_facts"}


def snapshot_for(facts):
    github = GitHub()
    github.issues[0].update(state=facts["state"], stateReason=facts["reason"],
                            closedAt=NOW if facts["state"] == "CLOSED" else None)
    if facts["ready"]:
        pr = pull()
        github.pulls = [pr]
        github.issues[0]["closedByPullRequestsReferences"] = conn([pr])
    snapshot = gp.fetch_repository_facts(github, REST(github))
    source = snapshot["issues"][800]
    if facts["reason"] == "DUPLICATE":
        source["duplicateOf"] = {"id": "I_82", "url": f"https://github.com/{gp.REPOSITORY}/issues/82"}
    return snapshot, source


class MigrationInvariantTests(unittest.TestCase):
    def test_unknown_project_option_is_rejected_before_planning(self):
        for name in ("INVALID", "진행 중"):
            with self.subTest(display_name=name):
                # Given: even a known display name cannot bless an unknown option ID.
                project = Project()
                item = project.items[0]
                item["fieldValues"]["nodes"] = [{
                    "__typename": "ProjectV2ItemFieldSingleSelectValue", "id": "bad",
                    "updatedAt": NOW, "field": {"id": CONFIG["status_field_id"]},
                    "optionId": "fixture-invalid", "name": name}]
                # When / Then: reject at the Project boundary without mutations.
                with self.assertRaises(gp.SyncError):
                    gp.fetch_project_item(project, CONFIG["project_id"], item["id"],
                                          CONFIG["status_field_id"], status_options=OPTIONS)
                self.assertFalse(any(mutation for _, _, mutation in project.calls))

    def test_exhaustive_plans_and_apply_reruns(self):
        snapshots = [snapshot_for(facts) for facts in FACTS]
        histories = [(kind, last) for kind in HISTORIES
                     for last in ((None,) if kind == "initialization" else STATUSES)]
        planned = applied = deferred_exact = 0
        for (kind, last), notion_value, project_value, facts_index in itertools.product(
                histories, VALUES, (None, *STATUSES), range(len(FACTS))):
            # Given: arbitrary prior events must not influence approximate history.
            facts = FACTS[facts_index]
            snapshot, source = snapshots[facts_index]
            notion = Notion(notion_value)
            if kind == "checkpoint" and last is not None:
                notion.pages[0]["properties"][CHECKPOINT] = text(checkpoint_text(last, facts))
            project = Project(project_value)
            item = {"id": project.items[0]["id"], "content_id": source["id"],
                    "status_option_id": CONFIG["status_options"].get(project_value)}
            previous = {"state": "CLOSED" if facts["state"] == "OPEN" else "OPEN",
                        "reason": None, "ready": not facts["ready"]}
            context = (kind, last, notion_value, project_value, facts)

            def history(page, *_):
                saved = read_checkpoint(page)
                return ((*saved, None, "checkpoint") if saved is not None else
                        (last, previous if kind == "reconstructed" else
                         facts if kind in {"checkpoint", "matching_fingerprint"} else None,
                         None, kind))

            with patch.object(planner, "history", side_effect=history):
                # When: run the actual planner with bounded status values.
                work = planner.plan_issue(source, notion.pages[0], item, snapshot, CONFIG)
                plan = work.plan
                planned += 1
                # Then: closure writes are justified by current GitHub facts, not old events.
                if facts["state"] == "CLOSED":
                    closed_target = {"COMPLETED": "완료", "NOT_PLANNED": "취소",
                                     "DUPLICATE": "취소"}.get(facts["reason"])
                    if plan["write_notion"] or plan["write_project"]:
                        self.assertIsNotNone(closed_target, context)
                        self.assertEqual(plan["target"], closed_target, context)
                if kind in APPROXIMATE and facts["state"] == "OPEN":
                    if plan["write_notion"] or plan["write_project"]:
                        self.assertIn(plan["target"], (last, notion_value, project_value), context)
                        self.assertFalse(plan["target"] == "검토 중" and not facts["ready"], context)
                    if last in {"완료", "취소"}:
                        self.assertFalse(plan["write_notion"] or plan["write_project"], context)
                # Actual ordered apply followed by an unchanged scan writes nothing.
                # Existing exact-history rule-3 handling deliberately preserves an independent
                # edit for scan 2 (test_review_rejection_preserves_independent_valid_edit_on_other_side).
                # This is not an approximate-history exception and must not change silently.
                target = work.plan["target"]
                after_notion = target if work.plan["write_notion"] else notion_value
                after_project = target if work.plan["write_project"] else project_value
                deferred = target is not None and after_notion != after_project
                self.assertFalse(kind in APPROXIMATE and deferred, context)
                apply_issue(notion, project, CONFIG, work, NOW)
                notion.calls.clear()
                project.calls.clear()
                item["status_option_id"] = (
                    project.items[0]["fieldValues"]["nodes"][0]["optionId"]
                    if project.items[0]["fieldValues"]["nodes"] else None)
                repeated = planner.plan_issue(source, notion.pages[0], item, snapshot, CONFIG)
                apply_issue(notion, project, CONFIG, repeated, NOW)
                if deferred:
                    self.assertEqual(read_checkpoint(notion.pages[0])[0], repeated.plan["target"], context)
                    notion.calls.clear()
                    project.calls.clear()
                    item["status_option_id"] = project.items[0]["fieldValues"]["nodes"][0]["optionId"]
                    settled = planner.plan_issue(source, notion.pages[0], item, snapshot, CONFIG)
                    apply_issue(notion, project, CONFIG, settled, NOW)
                    deferred_exact += 1
                self.assertFalse(any(mutation for _, _, mutation in project.calls), context)
                self.assertFalse(any(method == "PATCH" or path == "/pages"
                                     for method, path, _ in notion.calls), context)
                applied += 1
        print(f"MIGRATION_INVARIANTS plans={planned} apply_reruns={applied} "
              f"existing_exact_two_scan_cases={deferred_exact}")
