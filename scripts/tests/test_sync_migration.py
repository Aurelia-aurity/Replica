"""Readable migration scenarios from the issue 101 delta reviews."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notion_api as na
import sync_simple as ss
from sync_simple_state import CHECKPOINT, read_checkpoint
from sync_simple_fakes import CONFIG, OPTIONS, GitHub, Notion, Project, REST, checkpoint, conn
from test_sync_simple_state import pull


def scenario(history, baseline, notion_status, project_status, *, reason=None, draft=False, ready=False):
    github = GitHub()
    raw = json.loads(checkpoint(github, baseline)) if baseline is not None else None
    if raw is not None and history != "matching_fingerprint":
        raw["baseline"]["facts_fingerprint"] = "0" * 64
        if history == "unknown_facts":
            raw.pop("review_pr_hash")
    notion = Notion(notion_status, json.dumps(raw) if raw is not None else "")
    project = Project(project_status)
    if draft or ready:
        pr = pull()
        pr["isDraft"] = draft
        github.pulls = [pr]
        github.issues[0]["closedByPullRequestsReferences"] = conn([pr])
    if reason is not None:
        github.issues[0].update(state="CLOSED", stateReason=reason,
                                closedAt="2026-11-05T00:00:00Z")
    return github, notion, project


def scan(github, notion, project, *, apply=True):
    return ss.sync(github, REST(github), project, notion, CONFIG, apply=apply, env={})


class MigrationScenarioTests(unittest.TestCase):
    def test_named_review_scenarios_converge_or_require_initial_alignment(self):
        # Case, history, baseline, Notion, Project, closure reason, draft, target.
        cases = [
            ("B", "reconstructed", "백로그", "검토 중", "검토 중", None, False, "백로그"),
            ("B2", "unknown_facts", "백로그", "검토 중", "검토 중", None, False, "백로그"),
            ("B3", "matching_fingerprint", "백로그", "검토 중", "검토 중", None, True, "백로그"),
            ("D", "reconstructed", "완료", "완료", "진행 중", None, False, None),
            ("D2", "reconstructed", "완료", "진행 중", "완료", None, False, None),
            ("D3", "unknown_facts", "완료", "완료", "진행 중", None, False, None),
            ("D4", "unknown_facts", "완료", "진행 중", "완료", None, False, None),
            ("D5", "reconstructed", "완료", "검토 중", "진행 중", None, False, None),
            ("D6", "unknown_facts", "완료", "검토 중", "진행 중", None, False, None),
            ("C", "reconstructed", "백로그", "검토 중", "백로그", None, False, "백로그"),
            ("C2", "unknown_facts", "백로그", "검토 중", "백로그", None, False, "백로그"),
            ("C3", "matching_fingerprint", "백로그", "검토 중", "백로그", None, False, "백로그"),
            ("C4", "reconstructed", "백로그", "백로그", "검토 중", None, False, "백로그"),
            ("C5", "matching_fingerprint", "백로그", "검토 중", "백로그", None, True, "백로그"),
            ("N", "reconstructed", "백로그", "취소", "백로그", None, False, "백로그"),
            ("N2", "unknown_facts", "백로그", "취소", "백로그", None, False, "백로그"),
            ("F", "reconstructed", "진행 중", "진행 중", "완료", "COMPLETED", False, "완료"),
            ("F2", "unknown_facts", "진행 중", "완료", "진행 중", "COMPLETED", False, "완료"),
            ("F3", "reconstructed", "진행 중", "진행 중", "진행 중", "COMPLETED", False, "완료"),
            ("F4", "initialization", None, "진행 중", "완료", "COMPLETED", False, None),
            ("F5", "reconstructed", "진행 중", "진행 중", "취소", "NOT_PLANNED", False, "취소"),
            ("F6", "unknown_facts", "진행 중", "진행 중", "취소", "DUPLICATE", False, "취소"),
            ("J", "reconstructed", "완료", "검토 중", "검토 중", None, False, None),
            ("J2", "unknown_facts", "완료", "검토 중", "검토 중", None, False, None),
            ("J3", "reconstructed", "취소", "검토 중", "검토 중", None, False, None),
            ("M", "reconstructed", "완료", None, "진행 중", None, False, None),
            ("M2", "unknown_facts", "완료", None, "진행 중", None, False, None),
        ]
        for case, history, baseline, notion_status, project_status, reason, draft, target in cases:
            with self.subTest(case=case):
                # Given:
                github, notion, project = scenario(
                    history, baseline, notion_status, project_status, reason=reason, draft=draft)
                # When:
                result = scan(github, notion, project)
                # Then:
                plan = result["items"][0]
                self.assertEqual(result["counts"]["failed"], 0)
                self.assertEqual(plan["target"], target)
                expected_history = ("reconstructed" if history == "matching_fingerprint" and
                                    (draft or reason is not None) else history)
                self.assertEqual(plan["history"], expected_history)
                self.assertIn(f"#80 ({expected_history})", ss.summary(result))
                if target is None:
                    self.assertEqual(plan["note"], "초기 정렬")
                    self.assertFalse(plan["write_notion"] or plan["write_project"] or
                                     plan["write_checkpoint"])
                    self.assertEqual(na.read_select(notion.pages[0], "작업 상태"), notion_status)
                    self.assertEqual(na.read_text(notion.pages[0], CHECKPOINT), "")
                    self.assertFalse(any(mutation for _, _, mutation in project.calls))
                else:
                    self.assertEqual(na.read_select(notion.pages[0], "작업 상태"), target)
                    self.assertEqual(read_checkpoint(notion.pages[0])[0], target)
                    self.assertEqual(project.items[0]["fieldValues"]["nodes"][0]["optionId"],
                                     OPTIONS[target])
                    if reason is None:
                        self.assertEqual(plan["note"], "확인 필요")
                        self.assertEqual(na.read_text(notion.pages[0], "확인 필요"), "확인 필요")
                notion.calls.clear()
                project.calls.clear()
                scan(github, notion, project)
                self.assertFalse(any(mutation for _, _, mutation in project.calls))
                self.assertFalse(any(method == "PATCH" or path == "/pages"
                                     for method, path, _ in notion.calls))

    def test_equal_values_do_not_replay_approximate_ready_transitions(self):
        for baseline, current, ready, reason, target in (
                ("완료", "진행 중", False, None, "진행 중"),
                ("취소", "준비 중", False, None, "준비 중"),
                ("완료", "완료", False, None, "완료"),
                ("취소", "취소", False, None, "취소"),
                ("완료", "검토 중", True, None, "검토 중"),
                ("백로그", "준비 중", True, None, "준비 중"),
                ("검토 중", "준비 중", False, None, "준비 중"),
                ("완료", "진행 중", False, "COMPLETED", "완료")):
            with self.subTest(baseline=baseline, ready=ready, reason=reason):
                # Given:
                github, notion, project = scenario(
                    "reconstructed", baseline, current, current, ready=ready, reason=reason)
                # When:
                result = scan(github, notion, project)
                # Then:
                self.assertEqual(result["items"][0]["target"], target)
                if target is not None:
                    self.assertEqual(read_checkpoint(notion.pages[0])[0], target)
                    if reason is None:
                        self.assertFalse(result["items"][0]["write_notion"] or
                                         result["items"][0]["write_project"])
                else:
                    self.assertEqual(result["items"][0]["note"], "초기 정렬")
                    self.assertEqual(na.read_text(notion.pages[0], CHECKPOINT), "")

    def test_s1_s1b_s1c_settle_closed_legacy_baselines(self):
        for history in ("reconstructed", "unknown_facts"):
            for baseline in ("완료", "취소"):
                for case, status, reason in (
                        ("S1", "진행 중", None), ("S1b", "백로그", None),
                        ("S1c", "완료", "COMPLETED")):
                    with self.subTest(case=case, history=history, baseline=baseline):
                        # Given: S1b starts unresolved; the PM then aligns the boards.
                        github, notion, project = scenario(
                            history, baseline, "진행 중",
                            "백로그" if case == "S1b" else "진행 중", reason=reason)
                        legacy = na.read_text(notion.pages[0], "동기화 내부 상태")
                        if case == "S1b":
                            held = scan(github, notion, project)["items"][0]
                            self.assertEqual(held["note"], "초기 정렬")
                            self.assertFalse(held["write_notion"] or held["write_project"] or
                                             held["write_checkpoint"])
                            notion.pages[0]["properties"]["작업 상태"] = {"select": {"name": status}}
                        notion.calls.clear()
                        project.calls.clear()
                        # When: the next scan observes the agreed statuses or current closure.
                        result = scan(github, notion, project)
                        # Then: one scan records exact current facts; OPEN status is not rewritten.
                        plan = result["items"][0]
                        self.assertEqual(result["counts"]["failed"], 0)
                        self.assertEqual(plan["target"], status)
                        self.assertTrue(plan["write_checkpoint"])
                        if reason is None:
                            self.assertFalse(plan["write_notion"] or plan["write_project"])
                            self.assertFalse(any(mutation for _, _, mutation in project.calls))
                            self.assertFalse(any(
                                method == "PATCH" and "작업 상태" in body["properties"]
                                for method, _, body in notion.calls))
                        self.assertEqual(read_checkpoint(notion.pages[0]), (
                            status, {"state": "CLOSED" if reason else "OPEN",
                                     "reason": reason, "ready": False}))
                        self.assertEqual(na.read_select(notion.pages[0], "작업 상태"), status)
                        self.assertEqual(project.items[0]["fieldValues"]["nodes"][0]["optionId"],
                                         OPTIONS[status])
                        self.assertEqual(na.read_text(notion.pages[0], "동기화 내부 상태"), legacy)
                        notion.calls.clear()
                        project.calls.clear()
                        scan(github, notion, project)
                        self.assertFalse(any(mutation for _, _, mutation in project.calls))
                        self.assertFalse(any(method == "PATCH" or path == "/pages"
                                             for method, path, _ in notion.calls))
