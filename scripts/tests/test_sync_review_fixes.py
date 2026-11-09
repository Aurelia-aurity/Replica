"""Review regressions for availability, migration, metadata and scan budgets."""
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_project as gp
import notion_api as na
import sync_simple as ss
from sync_simple_metadata import metadata
from sync_simple_state import CHECKPOINT, checkpoint_text, read_checkpoint
from sync_simple_fakes import CONFIG, OPTIONS, PAGE, GitHub, Notion, Project, REST, checkpoint, conn, item, page, text
from test_sync_simple_state import pull


OPEN = {"state": "OPEN", "reason": None, "ready": False}


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.github, self.project = GitHub(), Project()
        self.notion = Notion(internal=checkpoint(self.github))

    def sync(self, *, apply=True):
        return ss.sync(self.github, REST(self.github), self.project, self.notion, CONFIG, apply=apply)

    def test_notification_report_without_actions_context_never_escapes_cli_boundary(self):
        for flags, extra_env in (([], {}), (["--dry-run"], {}), ([], {"NOTION_SYNC_ENABLED": "true"})):
            with self.subTest(flags=flags, extra_env=extra_env), tempfile.TemporaryDirectory() as directory:
                report = Path(directory) / "notification.json"
                result = subprocess.run(
                    [sys.executable, "-B", "scripts/notion_sync.py", *flags,
                     "--notification-report", str(report)],
                    cwd=Path(__file__).resolve().parents[2],
                    env={"PATH": os.defpath, **extra_env}, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn("Notification report unavailable; run binding missing or invalid.", result.stderr)
                self.assertFalse(report.exists())

    def test_invalid_cell_is_named_without_leaking_and_other_issue_still_converges(self):
        self.notion.pages[0]["properties"][CHECKPOINT] = text("PRIVATE_BROKEN_JSON")
        second = copy.deepcopy(self.github.issues[0])
        second.update(id="I_82", databaseId=802, number=82,
                      url=f"https://github.com/{gp.REPOSITORY}/issues/82")
        self.github.issues.append(second)
        other_page = page("진행 중")
        other_page["id"] = "00000000-0000-4000-8000-000000000082"
        other_page["properties"].update(metadata("Issue", second, f"gh:{gp.REPOSITORY_ID}:issue:802"))
        other_page["properties"][CHECKPOINT] = text(checkpoint_text("백로그", OPEN))
        other_item = item()
        other_item["id"] = "PVTI_82"
        other_item["content"].update(id="I_82", databaseId=802, number=82)
        self.notion.pages.append(other_page)
        self.project.items.append(other_item)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            report = self.sync()
        self.assertIn("#80: INVALID_CHECKPOINT", stderr.getvalue())
        self.assertNotIn("PRIVATE_BROKEN_JSON", stderr.getvalue() + json.dumps(report))
        self.assertEqual(report["counts"]["failed"], 1)
        self.assertEqual(other_item["fieldValues"]["nodes"][0]["optionId"], OPTIONS["진행 중"])
        self.assertEqual(read_checkpoint(other_page)[0], "진행 중")
        self.assertFalse(any(method == "PATCH" and path == f"/pages/{PAGE}"
                             for method, path, _ in self.notion.calls))

    def test_noop_rechecks_project_once_and_never_queries_it_in_dry_run(self):
        self.sync()
        self.project.calls.clear()
        self.notion.calls.clear()
        self.sync()
        self.assertEqual(sum(query == gp.PROJECT_ITEM_QUERY for query, _, _ in self.project.calls), 1)
        self.assertEqual(sum(method == "GET" and path == f"/pages/{PAGE}"
                             for method, path, _ in self.notion.calls), 1)
        self.assertFalse(any(mutation for _, _, mutation in self.project.calls))
        self.assertFalse(any(method == "PATCH" for method, _, _ in self.notion.calls))
        self.project.calls.clear()
        self.sync(apply=False)
        self.assertEqual(sum(query == gp.PROJECT_ITEM_QUERY for query, _, _ in self.project.calls), 0)

    def test_equal_migration_values_do_not_replay_approximate_transitions(self):
        cases = [("완료", "진행 중", False, False, "진행 중"),
                 ("백로그", "준비 중", True, False, "준비 중"),
                 ("검토 중", "준비 중", False, False, "준비 중"),
                 ("완료", "진행 중", False, True, "완료")]
        for legacy_status, current, ready, closed, expected in cases:
            with self.subTest(case=(legacy_status, current, ready, closed)):
                self.setUp()
                raw = json.loads(checkpoint(self.github, legacy_status))
                raw["baseline"]["facts_fingerprint"] = "0" * 64
                if legacy_status == "검토 중":
                    raw["review_pr_hash"] = "a" * 64
                self.notion = Notion(current, json.dumps(raw))
                self.project = Project(current)
                if ready:
                    pr = pull()
                    self.github.pulls = [pr]
                    self.github.issues[0]["closedByPullRequestsReferences"] = conn([pr])
                if closed:
                    self.github.issues[0].update(state="CLOSED", stateReason="COMPLETED",
                                                 closedAt="2026-11-05T00:00:00Z")
                report = self.sync()
                self.assertEqual(read_checkpoint(self.notion.pages[0])[0], expected)
                self.assertEqual(na.read_select(self.notion.pages[0], "작업 상태"), expected)
                self.assertIn("#80 (reconstructed)", ss.summary(report))

    def test_readyless_review_is_not_seeded_for_equal_or_missing_sides(self):
        for missing in (None, "notion", "project"):
            with self.subTest(missing=missing):
                self.notion, self.project = Notion("검토 중"), Project("검토 중")
                if missing == "notion":
                    self.notion.pages = []
                elif missing == "project":
                    self.project.items = []
                report = self.sync()
                self.assertEqual(report["items"][0]["note"], "초기 정렬")
                self.assertFalse(any(mutation for _, _, mutation in self.project.calls))
                if self.notion.pages:
                    self.assertEqual(na.read_select(self.notion.pages[0], "작업 상태"), "검토 중")
                    self.assertEqual(na.read_text(self.notion.pages[0], CHECKPOINT), "")
                else:
                    self.assertEqual(self.notion.pages, [])

    def test_review_rejection_initializes_missing_project_to_valid_legacy_baseline(self):
        self.notion.pages[0]["properties"]["작업 상태"] = {"select": {"name": "검토 중"}}
        self.project.items = []
        report = self.sync()
        self.assertEqual(report["counts"]["failed"], 0)
        self.assertEqual(read_checkpoint(self.notion.pages[0]), ("백로그", OPEN))
        self.assertEqual(self.project.items[0]["fieldValues"]["nodes"][0]["optionId"], OPTIONS["백로그"])

    def test_dry_run_metadata_plans_equal_apply_and_reveal_no_values(self):
        self.github.issues[0]["title"] = "PRIVATE_UPDATED_TITLE"
        self.github.pulls = [pull()]
        before = copy.deepcopy((self.notion.pages, self.project.items))
        dry = self.sync(apply=False)
        self.assertEqual((self.notion.pages, self.project.items), before)
        self.assertEqual(dry["items"][0]["metadata_operation"], "update")
        self.assertEqual(dry["items"][0]["metadata_fields"], ["제목"])
        self.assertFalse(dry["items"][0]["write_notion"])
        self.assertEqual(dry["pulls"][0]["metadata_operation"], "create")
        self.assertEqual(dry["counts"]["planned_pr_creates"], 1)
        actual = self.sync()
        for key in ("items", "pulls"):
            expected = [{k: v for k, v in row.items() if k != "outcome"} for row in dry[key]]
            observed = [{k: v for k, v in row.items() if k != "outcome"} for row in actual[key]]
            self.assertEqual(observed, expected)
        self.assertNotIn("PRIVATE_UPDATED_TITLE", json.dumps(dry) + ss.summary(dry))
        status_fields = {"동기화 시각", CHECKPOINT, "작업 상태", "확인 필요"}
        issue_fields = set().union(*(set(payload["properties"]) - status_fields
            for method, path, payload in self.notion.calls if method == "PATCH" and path == f"/pages/{PAGE}"))
        self.assertEqual(issue_fields, set(dry["items"][0]["metadata_fields"]))
        pr_create = next(payload for method, path, payload in self.notion.calls
                         if method == "POST" and path == "/pages")
        self.assertEqual(set(pr_create["properties"]) - {"동기화 시각"},
                         set(dry["pulls"][0]["metadata_fields"]))
        self.github.pulls[0]["title"] = "PRIVATE_PR_UPDATE"
        update = self.sync(apply=False)
        self.assertEqual(update["pulls"][0]["metadata_operation"], "update")
        self.assertEqual(update["pulls"][0]["metadata_fields"], ["제목"])

    def test_archived_project_keeps_active_notion_metadata_current_without_status_effects(self):
        self.sync()
        before = copy.deepcopy(self.notion.pages[0]["properties"])
        self.project.items[0]["isArchived"] = True
        self.github.issues[0].update(title="Changed while archived", state="CLOSED",
                                     stateReason="COMPLETED", closedAt="2026-11-05T00:00:00Z")
        self.project.calls.clear()
        self.notion.calls.clear()
        report = self.sync()
        self.assertEqual(na.read_select(self.notion.pages[0], "GitHub 상태"), "Closed")
        self.assertEqual(na.read_select(self.notion.pages[0], "종료 사유"), "완료")
        self.assertEqual(self.notion.pages[0]["properties"]["제목"]["title"][0]["text"]["content"],
                         "#80 Changed while archived")
        for name in ("작업 상태", CHECKPOINT, "메모", "일정", "동기화 내부 상태"):
            self.assertEqual(self.notion.pages[0]["properties"][name], before[name])
        self.assertFalse(any(mutation for _, _, mutation in self.project.calls))
        self.assertFalse(report["items"][0]["write_notion"])
        self.assertFalse(report["items"][0]["write_checkpoint"])
        writes = sum(method == "PATCH" for method, _, _ in self.notion.calls)
        self.sync()
        self.assertEqual(sum(method == "PATCH" for method, _, _ in self.notion.calls), writes)
