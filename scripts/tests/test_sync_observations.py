import copy
import hashlib
import sys
import unittest
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_project as gp
import sync_engine as se
from sync_observations import RunObservations
import test_sync_engine_core as fixtures


SOURCE = str(UUID(int=1))
PAGE = str(UUID(int=2))


def page(*, page_id=PAGE, issue_id=555, status="백로그", state=None):
    state = state if state is not None else se.new_issue_state(issue_id, gp.EXPECTED_PROJECT_ID)
    return {
        "id": page_id, "parent": {"data_source_id": SOURCE}, "archived": False,
        "properties": {
            "동기화 키": se.text_property(se.key_for(gp.REPOSITORY_ID, issue_id)),
            "종류": {"select": {"name": "Issue"}},
            "작업 상태": {"select": {"name": status} if status else None},
            "동기화 내부 상태": se.text_property(se.canonical_json(state)),
            "메모": se.text_property("private-note-sentinel"),
            "일정": {"date": {"start": "2026-10-01"}},
        },
    }


class RunObservationTests(unittest.TestCase):
    def setUp(self):
        self.observer = RunObservations(source_id=SOURCE, project_id=gp.EXPECTED_PROJECT_ID)

    def record(self, value, **kwargs):
        return self.observer.record_handled(value, page_id=value["id"],
            key_at_handling=kwargs.pop("key_at_handling", se.read_text(value, "동기화 키")),
            status_at_handling=kwargs.pop("status_at_handling", "백로그"),
            outcome=kwargs.pop("outcome", "handled"),
            confirmed=kwargs.pop("confirmed", True), **kwargs)

    def test_late_human_status_does_not_mint_or_refresh_checkpoint(self):
        value = page()
        self.assertTrue(self.record(value))
        changed = page(status="진행 중")
        self.assertIsNone(self.observer.recheck(changed))
        self.assertEqual(self.observer.snapshot()[0]["status"], "백로그")
        self.assertFalse(self.record(changed, status_at_handling="백로그"))
        self.assertEqual(self.observer.snapshot(), [])

    def test_unconfirmed_write_invalidates_old_evidence(self):
        value = page()
        self.assertTrue(self.record(value))
        self.assertFalse(self.record(value, confirmed=False))
        self.assertIsNone(self.observer.recheck(value))

    def test_fresh_recheck_cannot_create_handling_evidence(self):
        self.assertIsNone(self.observer.recheck(page()))
        self.assertEqual(self.observer.snapshot(), [])

    def test_exact_text_digest_and_no_manual_content(self):
        value = page()
        raw = se.read_text(value, "동기화 내부 상태")
        self.assertTrue(self.record(value))
        observation = self.observer.recheck(value)
        self.assertEqual(observation["state_sha256"], hashlib.sha256(raw.encode()).hexdigest())
        self.assertNotIn("private-note-sentinel", str(observation))
        self.assertNotIn("메모", observation)
        changed = copy.deepcopy(value)
        changed["properties"]["동기화 내부 상태"] = se.text_property(" " + raw)
        self.assertIsNone(self.observer.recheck(changed))

    def test_project_hold_is_echo_only_and_does_not_mutate_state(self):
        state = se.new_issue_state(555, gp.EXPECTED_PROJECT_ID)
        state["hold"] = se._new_hold("OPEN_PROJECT_COMPLETED", "synthetic", "a" * 64)
        value = page(state=state)
        before = copy.deepcopy(value)
        self.assertTrue(self.record(value))
        observed = self.observer.recheck(value)
        self.assertEqual(observed["kind"], "echo_observation")
        self.assertEqual(observed["outcome"], "blocked")
        self.assertNotIn("reflected", observed)
        self.assertEqual(value, before)

    def test_valid_unresolved_request_stays_blocked_not_reflected(self):
        fixture = fixtures.SyncEngineCoreTests()
        fixture.setUp()
        fixture._seed_prepared_request_without_pending()
        value = copy.deepcopy(fixture.notion.pages[fixtures.ISSUE_PAGE_ID])
        value["parent"]["data_source_id"] = SOURCE
        state = fixture._read_bidirectional_state()
        state["request"]["phase"] = "uncertain"
        state["notion_write"]["phase"] = "uncertain"
        raw = se.canonical_json(state)
        value["properties"]["동기화 내부 상태"] = se.text_property(raw)
        self.assertIsNotNone(se.decode_internal(raw, kind="issue",
            project_id=gp.EXPECTED_PROJECT_ID, object_id=fixtures.ISSUE_ID))
        before = copy.deepcopy(value)
        self.assertTrue(self.record(value, status_at_handling="진행 중", outcome="blocked"))
        self.assertEqual(self.observer.recheck(value)["outcome"], "blocked")
        self.assertEqual(se.read_text(value, "동기화 내부 상태"), raw)
        self.assertEqual(value, before)

    def test_valid_v1_state_keeps_exact_original_digest(self):
        current = se.new_issue_state(555, gp.EXPECTED_PROJECT_ID)
        legacy = {key: current[key] for key in se.ISSUE_STATE_V1_KEYS}
        legacy["v"] = 1
        value = page(state=legacy)
        self.assertTrue(self.record(value, outcome="blocked"))
        self.assertEqual(self.observer.recheck(value)["state_sha256"],
            hashlib.sha256(se.read_text(value, "동기화 내부 상태").encode()).hexdigest())
        self.assertEqual(se.read_text(value, "동기화 내부 상태"), se.canonical_json(legacy))

    def test_last_confirmed_handling_replaces_previous_checkpoint(self):
        self.assertTrue(self.record(page()))
        self.assertTrue(self.record(page(status="진행 중"), status_at_handling="진행 중"))
        result = self.observer.snapshot()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "진행 중")
        self.assertIsNone(self.observer.recheck(page()))

    def test_manual_field_edit_does_not_change_status_observation(self):
        value = page()
        self.assertTrue(self.record(value))
        value["properties"]["메모"] = se.text_property("changed-private-note")
        self.assertIsNotNone(self.observer.recheck(value))

    def test_foreign_project_state_is_not_echo_evidence(self):
        state = se.new_issue_state(555, "PVT_foreign_project")
        self.assertFalse(self.record(page(state=state)))

    def test_identity_and_state_fail_closed(self):
        for mutation in ("parent", "key", "kind", "state", "archived"):
            with self.subTest(mutation=mutation):
                value = page()
                if mutation == "parent": value["parent"]["data_source_id"] = str(UUID(int=9))
                if mutation == "key": value["properties"]["동기화 키"] = se.text_property("gh:1:issue:555")
                if mutation == "kind": value["properties"]["종류"]["select"]["name"] = "PR"
                if mutation == "state": value["properties"]["동기화 내부 상태"] = se.text_property("{bad")
                if mutation == "archived": value["archived"] = True
                self.assertFalse(self.record(value))

    def test_response_page_identity_cannot_replace_target(self):
        value = page()
        self.assertTrue(self.record(value))
        self.assertFalse(self.observer.record_handled(page(page_id=str(UUID(int=9))),
            page_id=PAGE, key_at_handling=se.key_for(gp.REPOSITORY_ID, 555),
            status_at_handling="백로그", outcome="handled", confirmed=True))
        self.assertEqual(self.observer.snapshot(), [])

    def test_consistent_but_different_issue_cannot_rebind_handled_page(self):
        self.assertTrue(self.record(page()))
        self.assertFalse(self.record(page(issue_id=777),
            key_at_handling=se.key_for(gp.REPOSITORY_ID, 555)))
        self.assertEqual(self.observer.snapshot(), [])

    def test_confirmed_null_status_is_not_a_general_state_assumption(self):
        value = page(status=None)
        self.assertTrue(self.record(value, status_at_handling=None))
        self.assertIsNone(self.observer.recheck(value)["status"])

    def test_duplicate_canonical_key_invalidates_both_pages(self):
        self.assertTrue(self.record(page()))
        self.assertFalse(self.record(page(page_id=str(UUID(int=9)))))
        self.assertEqual(self.observer.snapshot(), [])
        self.assertFalse(self.record(page()))

    def test_full_scope_has_no_ten_receipt_truncation(self):
        for n in range(23):
            self.assertTrue(self.record(page(page_id=str(UUID(int=100 + n)), issue_id=1000 + n)))
        result = self.observer.snapshot()
        self.assertEqual(len(result), 23)
        result[0]["status"] = "완료"
        self.assertEqual(self.observer.snapshot()[0]["status"], "백로그")


if __name__ == "__main__":
    unittest.main()
