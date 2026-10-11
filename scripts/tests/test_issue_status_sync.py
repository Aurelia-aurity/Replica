import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import issue_status_sync as ss


BACKLOG = "f75ad846"
READY = "96beb7e0"
PROGRESS = "47fc9ee4"
REVIEW = "ff336f39"
DONE = "98236657"


def stamp(option=BACKLOG, value_id="PVTSV_1", updated_at="2026-10-01T00:00:00Z"):
    return {"field_id": "PVTSSF_1", "option_id": option,
            "value_id": value_id, "updated_at": updated_at}


def baseline(project=BACKLOG, notion="백로그"):
    return ss.make_baseline(notion, stamp(project), "PVTI_1", "a" * 64,
                            "2026-10-01T00:00:00Z")


class IssueStatusSyncTests(unittest.TestCase):
    def test_six_general_moves_are_classified_without_guessing(self):
        for source, target in (("백로그", "준비 중"), ("백로그", "진행 중"),
                               ("준비 중", "백로그"), ("준비 중", "진행 중"),
                               ("진행 중", "백로그"), ("진행 중", "준비 중")):
            with self.subTest(source=source, target=target):
                before = baseline(project={"백로그": BACKLOG, "준비 중": READY,
                                           "진행 중": PROGRESS}[source], notion=source)
                result = ss.decide(before, target, stamp(before["project"]["option_id"]), source)
                self.assertEqual(result["action"], "notion_request")
                self.assertEqual(result["target"], target)

    def test_invalid_review_done_and_null_requests_are_preserved_for_rejection(self):
        for target in ("검토 중", "완료", None):
            with self.subTest(target=target):
                result = ss.decide(baseline(), target, stamp(), "백로그",
                                   facts_verified=True)
                self.assertEqual(result["action"], "hold_invalid_request")
                self.assertTrue(result["notion_changed"])

    def test_same_visible_values_without_verified_facts_do_not_converge(self):
        result = ss.decide(baseline(), "백로그", stamp(), "백로그", facts_verified=False)
        self.assertNotEqual(result["action"], "converged")
        self.assertEqual(result["action"], "unchanged")

    def test_same_new_visible_values_require_verified_facts_to_converge(self):
        current = stamp(PROGRESS, "PVTSV_new", "2026-10-02T00:00:00Z")
        without_facts = ss.decide(baseline(), "진행 중", current, "진행 중",
                                  facts_verified=False)
        with_facts = ss.decide(baseline(), "진행 중", current, "진행 중",
                               facts_verified=True)
        self.assertNotEqual(without_facts["action"], "converged")
        self.assertEqual(without_facts["action"], "hold_unknown")
        self.assertEqual(with_facts["action"], "converged")

    def test_project_only_move_and_status_stamp_resets_are_detected(self):
        project_only = ss.decide(baseline(), "백로그",
            stamp(PROGRESS, "PVTSV_2", "2026-10-02T00:00:00Z"), "진행 중")
        self.assertEqual(project_only["action"], "github_update")
        self.assertEqual(project_only["target"], "진행 중")
        for current in (stamp(BACKLOG, "PVTSV_2", "2026-10-01T00:00:00Z"),
                        stamp(BACKLOG, "PVTSV_1", "2026-10-02T00:00:00Z")):
            with self.subTest(current=current):
                result = ss.decide(baseline(), "백로그", current, "백로그")
                self.assertEqual(result["action"], "github_update")
                self.assertEqual(result["target"], "백로그")

    def test_notion_only_general_state_change_is_a_request(self):
        result = ss.decide(baseline(), "진행 중", stamp(), "백로그")
        self.assertEqual(result["action"], "notion_request")
        self.assertEqual(result["target"], "진행 중")

    def test_two_different_changes_are_held_without_order_evidence(self):
        result = ss.decide(baseline(), "준비 중",
                           stamp(PROGRESS, "PVTSV_2", "2026-10-02T00:00:00Z"), "진행 중")
        self.assertEqual(result["action"], "conflict")

    def test_equal_current_values_converge_after_facts_validation_even_if_clock_unknown(self):
        current = stamp(PROGRESS, None, None)
        result = ss.decide(baseline(), "진행 중", current, "진행 중", facts_verified=True)
        self.assertEqual(result["action"], "converged")

    def test_unknown_project_value_identity_holds_even_when_status_names_match(self):
        current = {**stamp(PROGRESS, None, None), "field_id": "PVTSSF_replaced"}
        result = ss.decide(baseline(), "진행 중", current, "진행 중", facts_verified=True)
        self.assertEqual(result["action"], "hold_unknown")

    def test_invalid_null_request_is_preserved_before_same_value_convergence(self):
        result = ss.decide(baseline(), None, stamp(None, None, None), None,
                           facts_verified=True)
        self.assertEqual(result["action"], "hold_invalid_request")
        self.assertTrue(result["notion_changed"])

    def test_unknown_project_metadata_does_not_authorize_a_notion_only_request(self):
        current = stamp(BACKLOG, None, None)
        result = ss.decide(baseline(), "진행 중", current, "백로그")
        self.assertEqual(result["action"], "hold_unknown")

    def test_invalid_targets_and_null_remain_persistable_for_rejection_display(self):
        for target in (None, "검토 중", "완료"):
            with self.subTest(target=target):
                request = ss.request_record(
                    "request-1", target, observed_from=None, observed_to=None,
                    prior_notion_status="백로그", project_option_id=BACKLOG,
                    phase="rejected", reason="허용 범위 밖 요청")
                self.assertIsNone(request["requested_by"])
                self.assertEqual(request["target"], target)
                self.assertEqual(request["phase"], "rejected")

    def test_project_facts_baseline_is_bound_to_item_and_semantic_fingerprint(self):
        value = baseline()
        self.assertEqual(value["project_item_id"], "PVTI_1")
        self.assertEqual(value["facts_fingerprint"], "a" * 64)
        with self.assertRaises(ss.StatusSyncError):
            ss.make_baseline("백로그", stamp(), "", "a" * 64, None)

    def test_github_facts_override_wins_over_a_notionside_change(self):
        result = ss.decide(baseline(), "진행 중", stamp(), "백로그", facts_override=True)
        self.assertEqual(result["action"], "facts_override")
        self.assertIsNone(result["target"])

    def test_verified_github_facts_precede_invalid_notion_request(self):
        result = ss.decide(baseline(), "검토 중", stamp(PROGRESS), "진행 중",
                           facts_override=True)
        self.assertEqual(result["action"], "facts_override")
        self.assertTrue(result["notion_changed"])


if __name__ == "__main__":
    unittest.main()
