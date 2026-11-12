import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_project as gp
import sync_simple as ss
from sync_simple_plan import status_fields
from sync_simple_fakes import CONFIG, FIELD, NOW, OPTIONS, SOURCE
from sync_simple_fakes import GitHub, Notion, Project, REST, checkpoint, item, page


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHub()
        self.rest = REST(self.github)
        self.project = Project()
        self.notion = Notion(internal=checkpoint(self.github))

    def run_sync(self, **kwargs):
        return ss.sync(self.github, self.rest, self.project, self.notion, CONFIG, **kwargs)

    def test_changed_notion_is_planned_without_mutation(self):
        # Given:
        before_project = copy.deepcopy(self.project.items)
        self.notion.pages[0]["properties"]["작업 상태"] = {"select": {"name": "진행 중"}}
        before_notion = copy.deepcopy(self.notion.pages)
        # When:
        report = self.run_sync()
        # Then:
        self.assertEqual(report["items"][0]["target"], "진행 중")
        self.assertTrue(report["items"][0]["write_project"])
        self.assertEqual(report["run"]["mode"], "dry-run")
        self.assertEqual(self.project.items, before_project)
        self.assertEqual(self.notion.pages, before_notion)
        self.assertEqual(report["items"][0]["old_hold"], {"code": "STATUS_CONFLICT"})

    def test_initial_alignment_remains_only_a_proposal(self):
        # Given:
        self.notion = Notion("준비 중")
        before = copy.deepcopy(self.project.items)
        # When:
        report = self.run_sync()
        # Then:
        self.assertEqual(report["items"][0]["note"], "초기 정렬")
        self.assertFalse(report["items"][0]["write_project"])
        self.assertEqual(self.project.items, before)

    def test_note_only_proposal_is_separate_from_status_write(self):
        # Given: unknown closure must leave both statuses alone.
        self.github.issues[0].update(state="CLOSED", stateReason="UNKNOWN", closedAt=NOW)
        # When:
        report = self.run_sync()
        # Then:
        self.assertFalse(report["items"][0]["write_notion"])
        self.assertTrue(report["items"][0]["write_note"])
        self.assertEqual(report["counts"]["write_notion"], 0)
        self.assertEqual(report["counts"]["write_note"], 1)

    def test_corrupt_or_missing_baseline_initializes_without_mutation(self):
        # Given:
        for raw in ("", "{broken", "[]", '{"baseline": {}}'):
            with self.subTest(raw=raw):
                self.notion = Notion("준비 중", raw)
                before = copy.deepcopy(self.notion.pages)
                # When:
                report = self.run_sync()
                # Then:
                self.assertIsNone(report["items"][0]["last_synced"])
                self.assertEqual(report["items"][0]["note"], "초기 정렬")
                self.assertEqual(self.notion.pages, before)

    def test_full_scan_rejects_duplicate_wrong_number_and_archived_rows(self):
        # Given:
        for kind in ("duplicate", "number", "archived", "is_archived"):
            with self.subTest(kind=kind):
                self.notion = Notion("진행 중", checkpoint(self.github))
                if kind == "duplicate":
                    self.notion.pages.append(copy.deepcopy(self.notion.pages[0]))
                elif kind == "number":
                    self.notion.pages[0]["properties"]["번호"]["number"] = 81
                else:
                    self.notion.pages[0][kind] = True
                # When / Then:
                with self.assertRaises(gp.SyncError):
                    self.run_sync()

    def test_missing_notion_and_project_are_reported(self):
        # Given:
        self.notion.pages = []
        self.project.items = []
        # When:
        report = self.run_sync()
        # Then:
        self.assertTrue(report["items"][0]["missing_notion"])
        self.assertTrue(report["items"][0]["missing_project"])
        self.assertFalse(report["items"][0]["write_notion"])
        self.assertEqual(self.project.items, [])
        self.assertEqual(self.notion.pages, [])

    def test_incomplete_query_fails(self):
        # Given:
        self.notion.query_transform = lambda result: {**result, "request_status": {"type": "incomplete"}}
        # When / Then:
        with self.assertRaises(gp.SyncError):
            self.run_sync()

    def test_two_issue_rows_keep_their_own_status_proposals(self):
        # Given:
        second = copy.deepcopy(self.github.issues[0])
        second.update(id="I_82", databaseId=802, number=82,
                      url=f"https://github.com/{gp.REPOSITORY}/issues/82")
        self.github.issues.append(second)
        second_page = page("완료")
        second_page["id"] = "00000000-0000-4000-8000-000000000082"
        second_page["properties"].update({"동기화 키": {"rich_text": [{"plain_text":
            f"gh:{gp.REPOSITORY_ID}:issue:802"}]}, "번호": {"number": 82}})
        second_item = item("진행 중")
        second_item.update(id="PVTI_82")
        second_item["content"].update(id="I_82", databaseId=802, number=82)
        self.project.items.append(second_item)
        self.notion.pages.insert(0, second_page)
        # When:
        report = self.run_sync()
        # Then:
        self.assertEqual([(row["issue"], row["target"]) for row in report["items"]],
                         [(80, "백로그"), (82, None)])

    def test_every_public_status_field_is_bounded_in_serialized_json(self):
        # Given:
        fields = ("notion", "project", "last_synced", "target")
        for value in (*OPTIONS, None, "PRIVATE_SYNTHETIC_STATUS"):
            # When:
            serialized = json.dumps(status_fields(dict.fromkeys(fields, value)), ensure_ascii=False)
            decoded = json.loads(serialized)
            # Then:
            self.assertNotIn("PRIVATE_SYNTHETIC_STATUS", serialized)
            for field in fields:
                self.assertEqual(decoded[field], "invalid" if value == "PRIVATE_SYNTHETIC_STATUS" else value)
                self.assertEqual(decoded[field + "_invalid"], value == "PRIVATE_SYNTHETIC_STATUS")

    def test_cli_serialized_report_redacts_invalid_notion_for_both_modes(self):
        # Given: real CLI entrypoint with only transport constructors replaced.
        env = {"GITHUB_TOKEN": "fixture", "PROJECT_TOKEN": "fixture", "NOTION_TOKEN": "fixture",
               "PROJECT_ID": CONFIG["project_id"], "PROJECT_OWNER_ID": CONFIG["owner_id"],
               "PROJECT_STATUS_FIELD_ID": FIELD, "PROJECT_STATUS_OPTIONS": json.dumps(OPTIONS),
               "NOTION_DATA_SOURCE_ID": SOURCE, "NOTION_SYNC_ENABLED": "true",
               "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID)}
        self.notion.pages[0]["properties"]["작업 상태"] = {"select": {"name": "PRIVATE_SYNTHETIC_STATUS"}}
        for mode in ([], ["--dry-run"]):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                self.notion = Notion("PRIVATE_SYNTHETIC_STATUS", checkpoint(self.github))
                path = str(Path(directory) / "plan.json")
                summary_path = str(Path(directory) / "summary.md")
                with patch.dict(os.environ, {**env, "GITHUB_STEP_SUMMARY": summary_path}, clear=True), \
                     patch.object(gp, "GraphQLClient", return_value=self.github), \
                     patch.object(gp, "RESTClient", return_value=self.rest), \
                     patch.object(ss, "ProjectClient", return_value=self.project), \
                     patch.object(ss.na, "NotionClient", return_value=self.notion), \
                     contextlib.redirect_stdout(io.StringIO()):
                    # When:
                    result = ss.main([*mode, "--report", path])
                # Then:
                self.assertEqual(result, 0)
                serialized = Path(path).read_text(encoding="utf-8")
                self.assertNotIn("PRIVATE_SYNTHETIC_STATUS", serialized)
                row = json.loads(serialized)["items"][0]
                self.assertEqual(row["notion"], "invalid")
                self.assertTrue(row["notion_invalid"])
                self.assertNotIn("PRIVATE_SYNTHETIC_STATUS", Path(summary_path).read_text(encoding="utf-8"))

    def test_configuration_cannot_rebind_manual_pm_authority(self):
        env = {"GITHUB_TOKEN": "fixture", "PROJECT_TOKEN": "fixture", "NOTION_TOKEN": "fixture",
               "PROJECT_ID": CONFIG["project_id"], "PROJECT_OWNER_ID": CONFIG["owner_id"],
               "PROJECT_STATUS_FIELD_ID": FIELD, "PROJECT_STATUS_OPTIONS": json.dumps(OPTIONS),
               "NOTION_DATA_SOURCE_ID": SOURCE, "PM_GITHUB_USER_ID": "123"}
        with self.assertRaises(gp.SyncError):
            ss.load_config(env)
        env["PM_GITHUB_USER_ID"] = str(gp.EXPECTED_PM_USER_ID)
        self.assertEqual(ss.load_config(env), CONFIG)
