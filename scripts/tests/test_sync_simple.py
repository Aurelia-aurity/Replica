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
import notion_api as na
import sync_simple as ss
import sync_simple_state as state
from sync_simple_fakes import CONFIG, FIELD, NOW, OPTIONS, PAGE, REPO, SOURCE
from sync_simple_fakes import GitHub, Notion, Project, REST, conn, page


def checkpoint(github, last="백로그"):
    snapshot = {"repository_node_id": REPO, "pulls": {}}
    _, fingerprint, ready_hash = state.issue_facts(github.issues[0], snapshot)
    return json.dumps({"v": 2, "repository_id": gp.REPOSITORY_ID, "issue_id": 800,
                       "project_id": CONFIG["project_id"], "review_pr_hash": ready_hash,
                       "baseline": {"notion_status": last, "facts_fingerprint": fingerprint},
                       "hold": {"code": "STATUS_CONFLICT", "message": "not for artifact"}})


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHub()
        self.rest = REST(self.github)
        self.project = Project()
        self.notion = Notion(internal=checkpoint(self.github))

    def run_sync(self, **kwargs):
        return ss.sync(self.github, self.rest, self.project, self.notion, CONFIG, **kwargs)

    def test_default_and_explicit_dry_run_have_no_writes(self):
        # Given: changes on both sides and an ambient apply authorization.
        self.notion.pages[0]["properties"]["작업 상태"] = {"select": {"name": "진행 중"}}
        # When:
        report = self.run_sync(env={"SYNC_SIMPLE_APPLY": "true"})
        # Then:
        self.assertEqual(report["items"][0]["target"], "진행 중")
        self.assertTrue(report["items"][0]["write_project"])
        self.assertEqual(report["run"]["mode"], "dry-run")
        self.assertEqual(self.project.mutations + self.notion.writes, [])
        self.assertEqual(report["items"][0]["old_hold"], {"code": "STATUS_CONFLICT"})

    def test_apply_is_rejected_before_any_reads_without_exact_gate(self):
        # Given:
        for value in (None, "TRUE", "false"):
            with self.subTest(value=value):
                # When / Then:
                with self.assertRaises(gp.SyncError):
                    self.run_sync(apply=True, env={"SYNC_SIMPLE_APPLY": value})
        self.assertEqual(self.github.calls + self.project.calls + self.notion.calls, [])

    def test_project_write_and_rerun_are_idempotent(self):
        # Given: one-sided Notion change.
        self.notion.pages[0]["properties"]["작업 상태"] = {"select": {"name": "진행 중"}}
        # When: apply followed by the same full scan.
        self.run_sync(apply=True, env={"SYNC_SIMPLE_APPLY": "true"})
        report = self.run_sync(apply=True, env={"SYNC_SIMPLE_APPLY": "true"})
        # Then:
        self.assertEqual(self.project.mutations, [gp.SET_STATUS_MUTATION])
        self.assertEqual(sum(query == gp.PROJECT_ITEM_QUERY for query, _, _ in self.project.calls), 1)
        self.assertFalse(report["items"][0]["write_project"])
        self.assertEqual(self.notion.writes, [])

    def test_notion_write_preserves_internal_state_and_rerun_is_idempotent(self):
        # Given: one-sided Project change, with a legacy hold.
        self.project = Project("진행 중")
        internal = na.read_text(self.notion.pages[0], "동기화 내부 상태")
        # When:
        self.run_sync(apply=True, env={"SYNC_SIMPLE_APPLY": "true"})
        report = self.run_sync(apply=True, env={"SYNC_SIMPLE_APPLY": "true"})
        # Then:
        self.assertEqual(len(self.notion.writes), 1)
        self.assertEqual(set(self.notion.writes[0]), {"작업 상태", "확인 필요"})
        self.assertEqual(na.read_text(self.notion.pages[0], "동기화 내부 상태"), internal)
        self.assertEqual(sum(method == "GET" and path.startswith("/pages/")
                             for method, path, _ in self.notion.calls), 1)
        self.assertFalse(report["items"][0]["write_notion"])

    def test_missing_project_is_added_and_each_mutation_read_back_once(self):
        # Given:
        self.project.items = []
        # When:
        self.run_sync(apply=True, env={"SYNC_SIMPLE_APPLY": "true"})
        # Then:
        self.assertEqual(self.project.mutations, [gp.ADD_ISSUE_MUTATION, gp.SET_STATUS_MUTATION])
        self.assertEqual(sum(query == gp.PROJECT_ITEM_QUERY for query, _, _ in self.project.calls), 2)

    def test_initial_alignment_and_invalid_reversion_do_not_write_on_rerun(self):
        # Given: diagnostics have no separate durable state property in this PR.
        for status, internal in (("준비 중", ""), ("unexpected", checkpoint(self.github))):
            with self.subTest(status=status):
                self.project, self.notion = Project(), Notion(status, internal)
                # When:
                self.run_sync(apply=True, env={"SYNC_SIMPLE_APPLY": "true"})
                first_counts = (len(self.project.mutations), len(self.notion.writes))
                report = self.run_sync(apply=True, env={"SYNC_SIMPLE_APPLY": "true"})
                # Then:
                self.assertEqual((len(self.project.mutations), len(self.notion.writes)), first_counts)
                self.assertFalse(report["items"][0]["write_project"])
                self.assertFalse(report["items"][0]["write_notion"])

    def test_readback_mismatch_stops_before_next_write(self):
        # Given: closure would change both systems, Project readback is wrong.
        self.github.issues[0].update(state="CLOSED", stateReason="COMPLETED", closedAt=NOW)
        self.project.corrupt_readback = True
        # When / Then:
        with self.assertRaises(gp.SyncError):
            self.run_sync(apply=True, env={"SYNC_SIMPLE_APPLY": "true"})
        self.assertEqual(self.notion.writes, [])

    def test_notion_readback_mismatch_fails(self):
        # Given:
        self.project = Project("진행 중")
        self.notion.corrupt_readback = True
        # When / Then:
        with self.assertRaises(gp.SyncError):
            self.run_sync(apply=True, env={"SYNC_SIMPLE_APPLY": "true"})

    def test_corrupt_or_missing_baseline_initializes_without_state_writes(self):
        # Given:
        for raw in ("", "{broken", "[]", '{"baseline": {}}'):
            with self.subTest(raw=raw):
                self.notion = Notion("준비 중", raw)
                # When:
                report = self.run_sync()
                # Then:
                self.assertIsNone(report["items"][0]["last_synced"])
                self.assertEqual(report["items"][0]["note"], "초기 정렬")
                self.assertEqual(self.notion.writes, [])

    def test_full_preflight_rejects_duplicate_wrong_number_and_archived_rows(self):
        # Given:
        for kind in ("duplicate", "number", "archived"):
            with self.subTest(kind=kind):
                self.notion = Notion("진행 중", checkpoint(self.github))
                if kind == "duplicate":
                    self.notion.pages.append(copy.deepcopy(self.notion.pages[0]))
                elif kind == "number":
                    self.notion.pages[0]["properties"]["번호"]["number"] = 81
                else:
                    self.notion.pages[0]["archived"] = True
                # When / Then:
                with self.assertRaises(gp.SyncError):
                    self.run_sync(apply=True, env={"SYNC_SIMPLE_APPLY": "true"})
                self.assertEqual(self.project.mutations + self.notion.writes, [])

    def test_missing_notion_is_reported_and_apply_refused(self):
        # Given:
        self.notion.pages = []
        # When:
        report = self.run_sync()
        # Then:
        self.assertTrue(report["items"][0]["missing_notion"])
        with self.assertRaises(gp.SyncError):
            self.run_sync(apply=True, env={"SYNC_SIMPLE_APPLY": "true"})
        self.assertEqual(self.project.mutations + self.notion.writes, [])

    def test_incomplete_query_fails_before_mutation(self):
        # Given:
        self.notion.query_transform = lambda result: {**result, "request_status": {"type": "incomplete"}}
        # When / Then:
        with self.assertRaises(gp.SyncError):
            self.run_sync(apply=True, env={"SYNC_SIMPLE_APPLY": "true"})
        self.assertEqual(self.project.mutations + self.notion.writes, [])

    def test_cli_defaults_to_read_only_even_with_apply_environment(self):
        # Given: real CLI entrypoint with only transport constructors replaced.
        env = {"GITHUB_TOKEN": "fixture", "PROJECT_TOKEN": "fixture", "NOTION_TOKEN": "fixture",
               "PROJECT_ID": CONFIG["project_id"], "PROJECT_OWNER_ID": CONFIG["owner_id"],
               "PROJECT_STATUS_FIELD_ID": FIELD, "PROJECT_STATUS_OPTIONS": json.dumps(OPTIONS),
               "NOTION_DATA_SOURCE_ID": SOURCE, "SYNC_SIMPLE_APPLY": "true"}
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "plan.json")
            summary_path = str(Path(directory) / "summary.md")
            with patch.dict(os.environ, {**env, "GITHUB_STEP_SUMMARY": summary_path}, clear=True), \
                 patch.object(gp, "GraphQLClient", return_value=self.github), \
                 patch.object(gp, "RESTClient", return_value=self.rest), \
                 patch.object(ss, "ProjectClient", return_value=self.project), \
                 patch.object(na, "NotionClient", return_value=self.notion), \
                 contextlib.redirect_stdout(io.StringIO()):
                # When:
                result = ss.main(["--report", path])
            # Then:
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(Path(path).read_text())["schema"], "replica-sync-simple-plan-v1")
            self.assertTrue(Path(summary_path).read_text().startswith("## "))
        self.assertEqual(self.project.mutations + self.notion.writes, [])
