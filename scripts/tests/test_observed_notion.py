import copy
import sys
import unittest
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_project as gp
import sync_engine as se
from observed_notion import ObservedNotion
from sync_observations import RunObservations
import test_sync_engine_core as f
from test_sync_observations import page, SOURCE, PAGE


class Client:
    def __init__(self, value):
        self.value = copy.deepcopy(value)
        self.calls = []
        self.fail = None

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(payload), kwargs))
        if self.fail == method:
            raise se.SyncError("synthetic uncertain response")
        if method in {"PATCH", "POST"}:
            self.value["properties"].update(copy.deepcopy(payload["properties"]))
        return copy.deepcopy(self.value)


class ObservedNotionTests(unittest.TestCase):
    def setUp(self):
        self.observer = RunObservations(source_id=SOURCE, project_id=gp.EXPECTED_PROJECT_ID)
        self.client = Client(page())
        self.adapter = ObservedNotion(self.client, self.observer, {"issue": page()})

    def patch(self, properties):
        return self.adapter.request("PATCH", f"/pages/{PAGE}",
                                    {"properties": properties}, write=True)

    def get(self):
        return self.adapter.request("GET", f"/pages/{PAGE}")

    def test_direct_status_patch_verified_then_last_metadata_state_digest(self):
        self.patch({"작업 상태": {"select": {"name": "진행 중"}}})
        self.assertEqual(self.observer.snapshot(), [])
        self.get()
        self.assertEqual(self.observer.snapshot()[0]["status"], "진행 중")
        state = se.new_issue_state(555, gp.EXPECTED_PROJECT_ID)
        state["migration_complete"] = True
        self.patch({"동기화 내부 상태": se.text_property(se.canonical_json(state))})
        self.assertEqual(self.observer.snapshot(), [])
        latest = self.get()
        self.assertEqual(self.observer.recheck(latest)["status"], "진행 중")

    def test_confirmed_metadata_cannot_learn_a_late_human_status(self):
        self.patch({"메모": se.text_property("synthetic")})
        self.client.value["properties"]["작업 상태"] = {"select": {"name": "진행 중"}}
        self.get()
        self.assertEqual(self.observer.snapshot(), [])
        self.get()
        self.assertEqual(self.observer.snapshot(), [])

    def test_readback_failure_cannot_be_rehabilitated_by_later_get_or_noop(self):
        self.patch({"작업 상태": {"select": {"name": "진행 중"}}})
        self.client.fail = "GET"
        with self.assertRaises(se.SyncError): self.get()
        self.client.fail = None
        self.get()
        self.adapter.record_untouched([(self.client.value, None)])
        self.assertEqual(self.observer.snapshot(), [])

    def test_unknown_patch_invalidates_previous_evidence_and_noop(self):
        self.patch({"작업 상태": {"select": {"name": "진행 중"}}})
        self.get()
        self.client.fail = "PATCH"
        with self.assertRaises(se.SyncError):
            self.patch({"작업 상태": {"select": {"name": "백로그"}}})
        self.client.fail = None
        self.get()
        self.adapter.record_untouched([(self.client.value, None)])
        self.assertEqual(self.observer.snapshot(), [])

    def test_failed_status_readback_then_metadata_cannot_restore_trust(self):
        for failure in ("mismatch", "get_error", "unknown_patch"):
            with self.subTest(failure=failure):
                self.setUp()
                self.patch({"작업 상태": {"select": {"name": "진행 중"}}})
                self.get()
                self.assertEqual(self.observer.snapshot()[0]["status"], "진행 중")
                if failure == "unknown_patch":
                    self.client.fail = "PATCH"
                    with self.assertRaises(se.SyncError):
                        self.patch({"작업 상태": {"select": {"name": "백로그"}}})
                else:
                    self.patch({"작업 상태": {"select": {"name": "진행 중"}}})
                    if failure == "get_error":
                        self.client.fail = "GET"
                        with self.assertRaises(se.SyncError): self.get()
                    else:
                        self.client.value["properties"]["작업 상태"] = {"select": {"name": "준비 중"}}
                        self.get()
                self.client.fail = None
                self.client.value["properties"]["작업 상태"] = {"select": {"name": "진행 중"}}
                self.patch({"확인 필요": se.text_property("synthetic recovery")})
                self.get()
                self.adapter.record_untouched([(self.client.value, None)])
                self.assertEqual(self.observer.snapshot(), [])
                # A new explicit status write and its full readback can restore trust.
                self.patch({"작업 상태": {"select": {"name": "진행 중"}}})
                self.get()
                self.assertEqual(self.observer.snapshot()[0]["status"], "진행 중")
                self.patch({"확인 필요": se.text_property("new verified metadata")})
                self.get()
                self.assertEqual(self.observer.snapshot()[0]["status"], "진행 중")

    def test_failed_status_readback_cannot_promote_later_null_through_metadata(self):
        self.patch({"작업 상태": {"select": {"name": "진행 중"}}})
        self.client.value["properties"]["작업 상태"] = {"select": {"name": "준비 중"}}
        self.get()
        self.client.value["properties"]["작업 상태"] = {"select": None}
        self.patch({"확인 필요": se.text_property("synthetic recovery")})
        self.get()
        self.assertEqual(self.observer.snapshot(), [])

    def test_all_payload_properties_must_match_not_only_status(self):
        self.patch({"작업 상태": {"select": {"name": "진행 중"}},
                    "메모": se.text_property("expected")})
        self.client.value["properties"]["메모"] = se.text_property("different")
        self.get()
        self.assertEqual(self.observer.snapshot(), [])

    def test_wrong_identity_and_archived_readback_fail_closed(self):
        for field, value in (("id", str(UUID(int=9))), ("archived", True)):
            with self.subTest(field=field):
                self.setUp()
                self.patch({"작업 상태": {"select": {"name": "진행 중"}}})
                self.client.value[field] = value
                self.get()
                self.assertEqual(self.observer.snapshot(), [])

    def test_untouched_noop_has_exact_engine_state_and_status_boundary(self):
        self.adapter.record_untouched([(page(), None)])
        self.assertIsNotNone(self.observer.recheck(page()))
        self.client.value["properties"]["작업 상태"] = {"select": {"name": "진행 중"}}
        self.adapter.record_untouched([(page(), None)])
        self.assertEqual(self.observer.snapshot(), [])

    def test_noop_digest_drift_does_not_mint_checkpoint(self):
        raw = se.read_text(self.client.value, "동기화 내부 상태")
        self.client.value["properties"]["동기화 내부 상태"] = se.text_property(" " + raw)
        self.adapter.record_untouched([(page(), None)])
        self.assertEqual(self.observer.snapshot(), [])

    def test_create_readback_uses_written_key_and_not_arbitrary_latest_key(self):
        adapter = ObservedNotion(self.client, self.observer, {})
        adapter.request("POST", "/pages", {"properties": page()["properties"]}, create=True)
        adapter.request("GET", f"/pages/{PAGE}")
        self.assertEqual(self.observer.snapshot()[0]["key"], se.key_for(gp.REPOSITORY_ID, 555))


class EngineObservationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f.SyncEngineCoreTests()
        self.fixture.setUp()
        self.observer = RunObservations(source_id=f.SOURCE_ID, project_id=gp.EXPECTED_PROJECT_ID)

    def test_full_engine_created_issue_and_later_run_include_all_handled_rows(self):
        issue = f.make_issue()
        self.fixture.repo_graph = f.RepoGraph(issues=[issue])
        self.fixture.rest = f.LegacyREST(issues=[{"id": f.ISSUE_ID,
            "node_id": f.ISSUE_NODE, "number": f.ISSUE_NUMBER, "state": "open"}])
        self.fixture.run_sync(run_observations=self.observer)
        observations = self.observer.snapshot()
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["key"], se.key_for(gp.REPOSITORY_ID, f.ISSUE_ID))
        self.assertIsNone(observations[0]["status"])
        self.assertIsNotNone(self.observer.recheck(self.fixture.notion.pages[observations[0]["page_id"]]))
        repeated = RunObservations(source_id=f.SOURCE_ID, project_id=gp.EXPECTED_PROJECT_ID)
        self.fixture.run_sync(run_observations=repeated)
        self.assertEqual(repeated.snapshot()[0]["key"], observations[0]["key"])

    def test_bidirectional_direct_terminal_completion_captures_final_state(self):
        self.fixture._configure_bidirectional_status(notion_status="진행 중")
        self.fixture.run_sync(bidirectional_enabled=True, run_observations=self.observer)
        observed = self.observer.recheck(self.fixture.notion.pages[f.ISSUE_PAGE_ID])
        self.assertIsNotNone(observed)
        self.assertEqual(observed["status"], "진행 중")
        self.assertEqual(self.fixture._read_bidirectional_state()["request"]["phase"], "completed")

    def test_terminal_mismatch_hold_metadata_cannot_mint_later_human_status(self):
        self.fixture._configure_bidirectional_status(notion_status="진행 중")
        injected = []

        def move_after_patch(path, properties, notion):
            if path != f"/pages/{f.ISSUE_PAGE_ID}":
                return
            state = se.decode_internal(se._notion_value(
                {"properties": properties}, "동기화 내부 상태", "rich_text"),
                kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=f.ISSUE_ID)
            checkpoint = ((state or {}).get("projection") or {}).get("display_checkpoint") or {}
            if (not injected and checkpoint.get("phase") == "completion_pending" and
                    "작업 상태" in properties):
                notion.pages[f.ISSUE_PAGE_ID]["properties"]["작업 상태"] = {"select": {"name": "준비 중"}}
                injected.append("terminal status mismatch")
            elif injected and "작업 상태" not in properties:
                notion.pages[f.ISSUE_PAGE_ID]["properties"]["작업 상태"] = {"select": {"name": "진행 중"}}
                injected.append("later matching human edit during metadata recovery")

        self.fixture.notion.after_patch = move_after_patch
        self.fixture.run_sync(bidirectional_enabled=True, run_observations=self.observer)
        self.assertIn("terminal status mismatch", injected)
        self.assertIn("later matching human edit during metadata recovery", injected)
        self.assertIsNotNone(self.fixture._read_bidirectional_state()["hold"])
        self.assertIsNone(self.observer.recheck(self.fixture.notion.pages[f.ISSUE_PAGE_ID]))
        self.assertNotIn(f.ISSUE_PAGE_ID, {value["page_id"] for value in self.observer.snapshot()})

    def test_full_engine_observes_23_unrelated_rows_and_late_edit_invalidates_only_one(self):
        issues = []
        for offset in range(23):
            issue = f.make_issue()
            issue.update(id=f"I_synthetic_{offset}", databaseId=f.ISSUE_ID + offset,
                         number=f.ISSUE_NUMBER + offset)
            issue["url"] = f"https://github.com/{gp.REPOSITORY}/issues/{issue['number']}"
            issues.append(issue)
        self.fixture.repo_graph = f.RepoGraph(issues=issues)
        self.fixture.rest = f.LegacyREST(issues=[{"id": issue["databaseId"],
            "node_id": issue["id"], "number": issue["number"], "state": "open"} for issue in issues])
        self.fixture.run_sync(run_observations=self.observer)
        observations = self.observer.snapshot()
        self.assertEqual(len(observations), 23)
        self.assertEqual({item["key"] for item in observations},
                         {se.key_for(gp.REPOSITORY_ID, issue["databaseId"]) for issue in issues})
        late = observations[12]
        self.fixture.notion.pages[late["page_id"]]["properties"]["작업 상태"] = {"select": {"name": "진행 중"}}
        self.assertIsNone(self.observer.recheck(self.fixture.notion.pages[late["page_id"]]))
        self.assertEqual(sum(self.observer.recheck(self.fixture.notion.pages[item["page_id"]]) is not None
                             for item in observations), 22)

    def test_dry_run_and_pm_resume_cannot_use_ordinary_observer(self):
        for kwargs in ({"dry_run": True}, {"resolve_issue_numbers": "18"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(se.SyncError):
                self.fixture.run_sync(run_observations=self.observer, **kwargs)
            self.assertEqual(self.fixture.notion.writes, [])


if __name__ == "__main__":
    unittest.main()
