"""Exhaustive status tables, independent of transport and persisted state."""
import itertools
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sync_rules import STATUSES, decide

OPEN = {"state": "OPEN", "reason": None, "ready": False}
REVIEW = {**OPEN, "ready": True}


class RuleTests(unittest.TestCase):
    def test_named_general_change_cases(self):
        # Given: independently specified one-sided and two-sided examples.
        cases = [
            ("only_notion", "준비 중", "백로그", "준비 중", True, False),
            ("only_project", "백로그", "진행 중", "진행 중", False, True),
            ("both_different", "준비 중", "진행 중", "준비 중", True, False),
            ("both_equal", "진행 중", "진행 중", "진행 중", False, False),
        ]
        for name, notion, project, target, wp, wn in cases:
            with self.subTest(name=name):
                # When:
                result = decide("백로그", notion, project, OPEN, OPEN)
                # Then:
                self.assertEqual(result, {"target": target, "write_project": wp,
                                          "write_notion": wn, "note": ""})

    def test_every_status_triple_when_facts_unchanged(self):
        # Given: every baseline and pair, including the protected cancellation.
        for last, notion, project in itertools.product(STATUSES, repeat=3):
            with self.subTest(last=last, notion=notion, project=project):
                if last != "검토 중" and "검토 중" in (notion, project):
                    target, wp, wn, note = last, project == "검토 중", notion == "검토 중", "확인 필요"
                elif notion == "취소" and notion != last and project != notion:
                    target, wp, wn, note = last, project != last, True, "확인 필요"
                else:
                    target = notion if notion != last else project
                    wp, wn, note = project != target, notion != target, ""
                # When: classify one snapshot.
                result = decide(last, notion, project, OPEN, OPEN)
                # Then: compare to an independently constructed table row.
                self.assertEqual(result, {"target": target, "write_project": wp,
                                          "write_notion": wn, "note": note})

    def test_every_status_pair_when_initializing(self):
        # Given: no status/history baseline, all visible pairs.
        for notion, project in itertools.product(STATUSES, repeat=2):
            with self.subTest(notion=notion, project=project):
                expected = (
                    {"target": None, "write_project": False, "write_notion": False,
                     "note": "확인 필요"} if "검토 중" in (notion, project) else
                    {"target": project, "write_project": False, "write_notion": notion != project,
                     "note": "확인 필요"} if notion == "취소" and project != notion else
                    {"target": notion, "write_project": notion != project, "write_notion": False,
                     "note": "초기 정렬" if notion != project else ""})
                # When:
                result = decide(None, notion, project, OPEN, None)
                # Then:
                self.assertEqual(result, expected)

    def test_closure_wins_every_visible_pair_even_when_unchanged(self):
        # Given: each recognized closure and every visible pair.
        for reason, target in (("COMPLETED", "완료"), ("NOT_PLANNED", "취소"),
                               ("DUPLICATE", "취소")):
            facts = {"state": "CLOSED", "reason": reason, "ready": False}
            for notion, project in itertools.product((*STATUSES, None, "bad"), repeat=2):
                with self.subTest(reason=reason, notion=notion, project=project):
                    # When:
                    result = decide("진행 중", notion, project, facts, facts)
                    # Then:
                    self.assertEqual(result, {"target": target, "write_project": project != target,
                                              "write_notion": notion != target, "note": ""})

    def test_unknown_close_reason_keeps_both_values(self):
        # Given:
        for reason in (None, "UNKNOWN"):
            for notion, project in itertools.product(STATUSES, repeat=2):
                facts = {"state": "CLOSED", "reason": reason, "ready": True}
                # When:
                result = decide("백로그", notion, project, facts, OPEN)
                # Then:
                self.assertEqual(result, {"target": None, "write_project": False,
                                          "write_notion": False, "note": "확인 필요"})

    def test_fact_transition_table(self):
        # Given: transition and no-transition rows with deliberately manual values.
        closed = {"state": "CLOSED", "reason": "COMPLETED", "ready": False}
        cases = [
            (OPEN, closed, "백로그"), (REVIEW, closed, "검토 중"),
            (REVIEW, OPEN, "검토 중"), (OPEN, REVIEW, "진행 중"),
            (OPEN, OPEN, "준비 중"), (REVIEW, REVIEW, "준비 중"),
            (OPEN, None, "준비 중"), (REVIEW, None, "준비 중"),
        ]
        for current, previous, target in cases:
            with self.subTest(current=current, previous=previous):
                # When:
                result = decide("백로그", "준비 중", "백로그", current, previous)
                # Then:
                self.assertEqual(result["target"], target)
                self.assertEqual(result["write_project"], target != "백로그")
                self.assertEqual(result["write_notion"], target != "준비 중")

    def test_invalid_notion_values_revert_table(self):
        # Given:
        cases = [
            ("백로그", "bad", "진행 중", "백로그", True, True),
            ("준비 중", None, "진행 중", "준비 중", True, True),
            ("진행 중", "", "진행 중", "진행 중", False, True),
            ("검토 중", "취소", "완료", "검토 중", True, True),
            (None, None, "진행 중", "진행 중", False, True),
            (None, "bad", "준비 중", "준비 중", False, True),
            (None, None, None, None, False, False),
            (None, "취소", None, None, False, False),
            (None, "bad", None, None, False, False),
            ("진행 중", "", None, "진행 중", True, True),
        ]
        for last, notion, project, target, wp, wn in cases:
            with self.subTest(last=last, notion=notion, project=project):
                # When:
                result = decide(last, notion, project, OPEN, OPEN)
                # Then:
                self.assertEqual(result, {"target": target, "write_project": wp,
                                          "write_notion": wn, "note": "확인 필요"})

    def test_unset_project_receives_target(self):
        # Given: all valid Notion states with matching baseline.
        for status in STATUSES:
            # When:
            result = decide(status, status, None, OPEN, OPEN)
            # Then:
            self.assertEqual(result, {"target": status, "write_project": True,
                                      "write_notion": False, "note": ""})

    def test_fact_transition_is_not_repeated_after_history_advances(self):
        # Given: ready fact already consumed, then a human changes the card.
        # When:
        result = decide("검토 중", "진행 중", "검토 중", REVIEW, REVIEW)
        # Then:
        self.assertEqual(result, {"target": "진행 중", "write_project": True,
                                  "write_notion": False, "note": ""})
