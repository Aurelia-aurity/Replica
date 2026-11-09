"""Writer regressions from PR #92, through wire-shaped, ID-bound adapters."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_project as gp
import notion_api as na
import sync_simple as ss
from sync_simple_metadata import closure, metadata
from sync_simple_state import CHECKPOINT, checkpoint_text, read_checkpoint
from sync_simple_fakes import (CONFIG, LEGACY, OPTIONS, PAGE, GitHub, Notion,
                               Project, REST, checkpoint, conn, item, page, text)
from test_sync_simple_state import pull


OPEN = {"state": "OPEN", "reason": None, "ready": False}


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHub()
        self.project = Project()
        self.notion = Notion(internal=checkpoint(self.github))

    def run_sync(self):
        return ss.sync(self.github, REST(self.github), self.project, self.notion, CONFIG, apply=True)

    def notion_status(self, status):
        self.notion.pages[0]["properties"]["작업 상태"] = {"select": {"name": status}}

    def project_status(self, status):
        self.project.items[0]["fieldValues"] = item(status)["fieldValues"]

    def assert_converged(self, target):
        self.assertEqual(na.read_select(self.notion.pages[0], "작업 상태"), target)
        self.assertEqual(self.project.items[0]["fieldValues"]["nodes"][0]["optionId"], OPTIONS[target])
        self.assertEqual(read_checkpoint(self.notion.pages[0])[0], target)

    def writes(self):
        return (sum(mutation for _, _, mutation in self.project.calls),
                sum(method == "PATCH" or path == "/pages" for method, path, _ in self.notion.calls))

    def test_astra_h1_edit_back_to_old_baseline_survives(self):
        self.notion_status("진행 중")
        self.run_sync()
        self.notion_status("백로그")
        self.run_sync()
        self.assert_converged("백로그")

    def test_astra_h1_project_cancellation_converges_once_across_four_runs(self):
        self.project_status("취소")
        self.run_sync()
        first = self.writes()
        for _ in range(3):
            self.run_sync()
            self.assert_converged("취소")
        self.assertEqual(self.writes(), first)

    def test_astra_h1_invalid_notion_and_project_edit_converge_in_one_run(self):
        self.notion_status("PRIVATE_INVALID")
        self.project_status("진행 중")
        self.run_sync()
        first = self.writes()
        self.run_sync()
        self.assert_converged("백로그")
        # The diagnostic may clear on the next clean scan; no status write may recur.
        patches = [payload["properties"].get("작업 상태")
                   for method, _, payload in self.notion.calls if method == "PATCH"]
        self.assertEqual(sum(value is not None for value in patches), 1)
        self.assertEqual(self.writes()[0], first[0])

    def test_fable_high1_a_one_sided_project_review_after_notion_propagation(self):
        # The new review rule additionally requires a ready closing-linked PR.
        pr = pull()
        self.github.pulls = [pr]
        self.github.issues[0]["closedByPullRequestsReferences"] = conn([pr])
        self.notion.pages[0]["properties"][CHECKPOINT] = text(
            checkpoint_text("백로그", {**OPEN, "ready": True}))
        self.notion_status("진행 중")
        self.run_sync()
        self.project_status("검토 중")
        self.run_sync()
        self.assert_converged("검토 중")

    def test_fable_high1_b_reopen_runs_once_then_keeps_new_project_edit(self):
        raw = json.loads(checkpoint(self.github, "완료"))
        raw["baseline"]["facts_fingerprint"] = "0" * 64
        self.notion = Notion("완료", json.dumps(raw))
        self.project = Project("완료")
        self.run_sync()
        self.assert_converged("백로그")
        self.project_status("진행 중")
        self.run_sync()
        self.assert_converged("진행 중")

    def test_fable_high2_initial_alignment_never_writes_status_or_checkpoint(self):
        self.notion = Notion("준비 중")
        for _ in range(2):
            report = self.run_sync()
        self.assertEqual(report["items"][0]["note"], "초기 정렬")
        self.assertEqual(self.writes()[0], 0)
        self.assertEqual(na.read_select(self.notion.pages[0], "작업 상태"), "준비 중")
        self.assertEqual(na.read_text(self.notion.pages[0], CHECKPOINT), "")
        self.assertEqual(na.read_text(self.notion.pages[0], "확인 필요"), "확인 필요")
        self.project_status("준비 중")
        self.run_sync()
        self.assert_converged("준비 중")

    def test_review_without_ready_pr_is_reverted_on_either_side(self):
        for side in ("notion", "project", "both"):
            with self.subTest(side=side):
                self.setUp()
                if side != "project":
                    self.notion_status("검토 중")
                if side != "notion":
                    self.project_status("검토 중")
                report = self.run_sync()
                self.assert_converged("백로그")
                self.assertEqual(report["items"][0]["note"], "확인 필요")

    def test_review_rejection_preserves_independent_valid_edit_on_other_side(self):
        for notion, project in (("진행 중", "검토 중"), ("검토 중", "진행 중")):
            with self.subTest(notion=notion, project=project):
                self.setUp()
                self.notion_status(notion)
                self.project_status(project)
                self.run_sync()
                self.assertEqual(na.read_select(self.notion.pages[0], "작업 상태"),
                                 "백로그" if notion == "검토 중" else notion)
                self.assertEqual(self.project.items[0]["fieldValues"]["nodes"][0]["optionId"],
                                 OPTIONS["백로그" if project == "검토 중" else project])
                self.assertEqual(na.read_text(self.notion.pages[0], CHECKPOINT), "")
                self.run_sync()
                self.assert_converged("진행 중")

    def test_migration_preserves_legacy_state_and_user_properties(self):
        before = copy.deepcopy(self.notion.pages[0]["properties"])
        self.run_sync()
        self.assert_converged("백로그")
        for name in ("동기화 내부 상태", "일정", "메모"):
            self.assertEqual(self.notion.pages[0]["properties"][name], before[name])
        first = self.writes()
        self.run_sync()
        self.assertEqual(self.writes(), first)

    def test_empty_legacy_equal_sides_seed_without_status_changes(self):
        self.notion = Notion("취소")
        self.project = Project("취소")
        self.run_sync()
        self.assert_converged("취소")
        self.assertEqual(self.writes()[0], 0)
        self.assertFalse(any("작업 상태" in p["properties"] for m, _, p in self.notion.calls if m == "PATCH"))

    def test_new_issue_created_in_both_services_then_rerun_is_noop(self):
        self.notion.pages = []
        self.project.items = []
        report = self.run_sync()
        self.assertEqual(report["counts"]["failed"], 0)
        self.assert_converged("백로그")
        first = self.writes()
        self.run_sync()
        self.assertEqual(self.writes(), first)
        self.assertEqual(len(self.notion.pages), 1)
        self.assertEqual(len(self.project.items), 1)

    def test_missing_project_preserves_new_notion_edit_relative_to_valid_baseline(self):
        self.project.items = []
        self.notion_status("진행 중")
        self.run_sync()
        self.assert_converged("진행 중")

    def test_partial_new_issue_creation_is_recovered_without_duplicate_project_item(self):
        self.notion.pages = []
        self.project.items = []
        def fail_create(method, path, payload):
            if path == "/pages":
                self.notion.before_request = lambda *args: None
                raise gp.SyncError("synthetic create failure")
        self.notion.before_request = fail_create
        self.assertEqual(self.run_sync()["counts"]["failed"], 1)
        self.assertEqual(len(self.project.items), 1)
        self.assertEqual(self.notion.pages, [])
        self.run_sync()
        self.assert_converged("백로그")
        self.assertEqual(len(self.project.items), 1)
        self.assertEqual(len(self.notion.pages), 1)

    def test_new_duplicate_issue_preserves_missing_representative_diagnostic(self):
        self.github.issues[0].update(state="CLOSED", stateReason="DUPLICATE",
                                     closedAt="2026-11-05T00:00:00Z")
        self.notion.pages = []
        self.project.items = []
        self.run_sync()
        self.assert_converged("취소")
        self.assertEqual(na.read_text(self.notion.pages[0], "확인 필요"), "확인 필요")

    def test_id_bound_fakes_modify_correct_issue_and_preserve_pr_status(self):
        pr = pull()
        self.github.pulls = [pr]
        pr_page = page("준비 중")
        pr_page["id"] = "00000000-0000-4000-8000-000000000081"
        pr_page["properties"].update(metadata("PR", pr, f"gh:{gp.REPOSITORY_ID}:issue:901"))
        self.notion.pages.insert(0, pr_page)
        self.project_status("진행 중")
        report = self.run_sync()
        self.assertEqual(report["counts"]["failed"], 0)
        issue_page = next(row for row in self.notion.pages if row["id"] == PAGE)
        self.assertEqual(na.read_select(issue_page, "작업 상태"), "진행 중")
        self.assertEqual(na.read_select(pr_page, "작업 상태"), "준비 중")
        self.assertEqual(na.read_text(pr_page, CHECKPOINT), "")
        for method, path, payload in self.notion.calls:
            if method == "PATCH" and ("작업 상태" in payload["properties"] or CHECKPOINT in payload["properties"]):
                self.assertEqual(path, f"/pages/{PAGE}")

    def test_metadata_matches_frozen_legacy_producer(self):
        for case in LEGACY["metadata"]:
            with self.subTest(kind=case["kind"], state=case["row"]["state"]):
                self.assertEqual(metadata(case["kind"], case["row"], case["key"]), case["expected"])
        for case in LEGACY["closure"]:
            with self.subTest(reason=case["issue"]["stateReason"]):
                self.assertEqual(closure(case["issue"]), case["expected"])

    def test_metadata_only_change_is_written_once_and_dates_compare_by_instant(self):
        self.run_sync()
        self.github.issues[0]["title"] = "Renamed synthetic issue"
        self.run_sync()
        self.assertEqual(self.notion.pages[0]["properties"]["제목"]["title"][0]["text"]["content"],
                         "#80 Renamed synthetic issue")
        first = self.writes()
        self.notion.pages[0]["properties"]["GitHub 수정"]["date"]["start"] = "2026-11-05T09:00:00+09:00"
        self.run_sync()
        self.assertEqual(self.writes(), first)

    def test_notion_status_or_checkpoint_change_before_patch_skips_item(self):
        for changed_property in ("작업 상태", CHECKPOINT):
            with self.subTest(changed_property=changed_property):
                self.setUp()
                self.project_status("진행 중")
                def race(method, path, payload):
                    if method == "GET" and path == f"/pages/{PAGE}":
                        self.notion.before_request = lambda *args: None
                        self.notion.pages[0]["properties"][changed_property] = (
                            {"select": {"name": "준비 중"}} if changed_property == "작업 상태" else
                            text(checkpoint_text("준비 중", OPEN)))
                self.notion.before_request = race
                report = self.run_sync()
                self.assertEqual(report["items"][0]["outcome"], "changed")
                self.assertEqual(self.writes(), (0, 0))

    def test_partial_failure_at_every_effect_never_advances_checkpoint_and_retries(self):
        for failure in ("project_write", "project_readback", "notion_write", "notion_readback", "checkpoint"):
            with self.subTest(failure=failure):
                self.setUp()
                self.notion_status("진행 중")
                # Force a display update after Project propagation.
                self.github.issues[0]["title"] = "Updated title"
                original_project = self.project.request
                original_notion = self.notion.request
                armed = {"value": True, "project_written": False, "notion_written": False}
                def project_request(query, variables=None, *, mutation=False):
                    if (armed["value"] and (failure == "project_write" and mutation or
                            failure == "project_readback" and query == gp.PROJECT_ITEM_QUERY and armed["project_written"])):
                        armed["value"] = False
                        raise gp.SyncError("synthetic")
                    result = original_project(query, variables, mutation=mutation)
                    armed["project_written"] |= mutation
                    return result
                def notion_request(method, path, payload=None):
                    fields = (payload or {}).get("properties", {})
                    if (armed["value"] and (
                            failure == "notion_write" and method == "PATCH" and CHECKPOINT not in fields or
                            failure == "notion_readback" and method == "GET" and armed["notion_written"] or
                            failure == "checkpoint" and CHECKPOINT in fields)):
                        armed["value"] = False
                        raise gp.SyncError("synthetic")
                    result = original_notion(method, path, payload)
                    armed["notion_written"] |= method == "PATCH"
                    return result
                self.project.request = project_request
                self.notion.request = notion_request
                report = self.run_sync()
                self.assertEqual(report["counts"]["failed"], 1)
                self.assertEqual(na.read_text(self.notion.pages[0], CHECKPOINT), "")
                self.run_sync()
                self.assert_converged("진행 중")

    def test_cancellation_checkpoint_failure_does_not_revert_successful_propagation(self):
        self.project_status("취소")
        def fail_checkpoint(method, path, payload):
            if CHECKPOINT in (payload or {}).get("properties", {}):
                self.notion.before_request = lambda *args: None
                raise gp.SyncError("synthetic")
        self.notion.before_request = fail_checkpoint
        self.assertEqual(self.run_sync()["counts"]["failed"], 1)
        self.assertEqual(na.read_text(self.notion.pages[0], CHECKPOINT), "")
        self.run_sync()
        self.assert_converged("취소")

    def test_ready_appearance_and_disappearance_are_consumed_once(self):
        self.run_sync()
        pr = pull()
        self.github.pulls = [pr]
        self.github.issues[0]["closedByPullRequestsReferences"] = conn([pr])
        self.run_sync()
        self.assert_converged("검토 중")
        self.project_status("준비 중")
        self.run_sync()
        self.assert_converged("준비 중")
        self.github.issues[0]["closedByPullRequestsReferences"] = conn([])
        self.run_sync()
        self.assert_converged("진행 중")
        self.project_status("준비 중")
        self.run_sync()
        self.assert_converged("준비 중")

    def test_two_issue_writes_bind_to_distinct_actual_page_and_item_ids(self):
        second = copy.deepcopy(self.github.issues[0])
        second.update(id="I_82", databaseId=802, number=82,
                      url=f"https://github.com/{gp.REPOSITORY}/issues/82")
        self.github.issues.append(second)
        second_page = page("준비 중")
        second_page["id"] = "00000000-0000-4000-8000-000000000082"
        second_page["properties"].update(metadata("Issue", second, f"gh:{gp.REPOSITORY_ID}:issue:802"))
        second_page["properties"][CHECKPOINT] = text(checkpoint_text("백로그", OPEN))
        second_item = item("백로그")
        second_item["id"] = "PVTI_82"
        second_item["content"].update(id="I_82", databaseId=802, number=82)
        self.project.items.insert(0, second_item)
        self.notion.pages.insert(0, second_page)
        # Change the first issue from Project, the second from Notion.
        self.project.items[1]["fieldValues"] = item("진행 중")["fieldValues"]
        report = self.run_sync()
        self.assertEqual(report["counts"]["failed"], 0)
        self.assertEqual([na.read_select(row, "작업 상태") for row in self.notion.pages],
                         ["준비 중", "진행 중"])
        self.assertEqual([row["fieldValues"]["nodes"][0]["optionId"] for row in self.project.items],
                         [OPTIONS["준비 중"], OPTIONS["진행 중"]])

    def test_pr_creation_and_updates_preserve_identity_and_do_not_create_project_items(self):
        pr = pull()
        self.github.pulls = [pr]
        self.run_sync()
        pr_page = next(row for row in self.notion.pages if na.read_select(row, "종류") == "PR")
        self.assertEqual(na.read_text(pr_page, "동기화 키"), f"gh:{gp.REPOSITORY_ID}:issue:901")
        self.assertEqual(len(self.project.items), 1)
        self.github.pulls[0].update(isDraft=True, title="Changed PR")
        self.run_sync()
        self.assertEqual(na.read_select(pr_page, "GitHub 상태"), "Draft")
        self.assertEqual(pr_page["properties"]["제목"]["title"][0]["text"]["content"], "Changed PR")
        self.assertEqual(na.read_text(pr_page, CHECKPOINT), "")

    def test_invalid_new_checkpoint_fails_scan_instead_of_reusing_legacy(self):
        for raw in ("{broken", '{"v":true,"status":"백로그","facts":{}}',
                    '{"v":1,"status":"백로그","facts":{"closed":0,"reason":null,"ready":false}}'):
            self.notion.pages[0]["properties"][CHECKPOINT] = text(raw)
            with self.assertRaises(gp.SyncError):
                self.run_sync()
            self.assertEqual(self.writes(), (0, 0))

    def test_wrong_page_or_project_readback_never_records_success(self):
        for side in ("notion", "project"):
            with self.subTest(side=side):
                self.setUp()
                self.notion_status("진행 중")
                if side == "project":
                    original = self.project.request
                    def wrong(query, variables=None, *, mutation=False):
                        result = original(query, variables, mutation=mutation)
                        if query == gp.PROJECT_ITEM_QUERY and any(m for _, _, m in self.project.calls):
                            result[0]["node"]["content"]["id"] = "I_other"
                        return result
                    self.project.request = wrong
                else:
                    original = self.notion.request
                    def wrong(method, path, payload=None):
                        result = original(method, path, payload)
                        if method == "GET" and path.startswith("/pages/"):
                            result["id"] = "00000000-0000-4000-8000-000000000099"
                        return result
                    self.notion.request = wrong
                report = self.run_sync()
                self.assertEqual(report["counts"]["failed"], 1)
                self.assertEqual(na.read_text(self.notion.pages[0], CHECKPOINT), "")
