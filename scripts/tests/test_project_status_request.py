import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_project as gp
import project_status_request as helper
import sync_engine as se


ID = "11111111-1111-4111-8111-111111111111"
PAGE = "22222222-2222-4222-8222-222222222222"


class Relay:
    base = "https://replica.synthetic.workers.dev"
    def __init__(self):
        self.state = None
        self.body = None
        self.calls = []
        self.lose = None

    def post(self, path, value):
        self.calls.append((path, copy.deepcopy(value)))
        if path == "/agent-prepare":
            self.body = value; self.state = "PREPARED"
        elif path == "/agent-attempt":
            if self.state != "PREPARED": raise se.SyncError("synthetic reservation already attempted")
            self.state = "ATTEMPTING"
        elif path == "/agent-abort":
            if self.state not in {"PREPARED", "ABORTED"}: raise se.SyncError("synthetic attempted fence")
            self.state = "ABORTED"
        elif path == "/agent-request":
            self.state = "CONFIRMED" if value["kind"] == "mutation" else "CONFIRMED_NOOP"
        elif path == "/agent-outcome":
            self.state = "REJECTED_FENCED" if value["outcome"] == "rejected" else "UNKNOWN_FENCED"
        if self.lose == path:
            self.lose = None; raise se.SyncError("synthetic response loss")
        response = {"operation_id": ID, "binding": helper.digest(self.body), "state": self.state}
        if path == "/agent-attempt": response["newly_authorized"] = True
        if path == "/agent-request": response["sync_queued"] = True
        return response


def snapshot(status="백로그"):
    return {"issue_id": 555, "item_id": "PVTI_synthetic",
        "stamp": {"field_id": helper.STATUS_FIELD_ID, "option_id": gp.EXPECTED_STATUS_OPTIONS[status],
                  "value_id": "PVTSV_synthetic", "updated_at": "2026-10-01T00:00:00Z"},
        "source_fingerprint": "a" * 64}


class ProjectHelperTests(unittest.TestCase):
    def setUp(self):
        # Preserve synthetic local journals for audit; no automatic deletion.
        self.directory = tempfile.mkdtemp(prefix="replica-helper-test-")
        self.journal = helper.LocalJournal(self.directory, ID)
        self.relay = Relay()
        self.current = snapshot()
        self.mutations = 0

    def reader(self, number):
        self.assertEqual(number, 18)
        return copy.deepcopy(self.current)

    def mutate(self, body):
        with self.journal.path.open() as stream: saved = __import__("json").load(stream)
        self.assertTrue(saved["may_have_sent"])
        self.assertEqual(self.relay.state, "ATTEMPTING")
        self.mutations += 1
        self.current["stamp"]["option_id"] = gp.EXPECTED_STATUS_OPTIONS[body["target"]]

    def invoke(self, **kwargs):
        return helper.run_helper(self.journal, self.relay, kwargs.pop("reader", self.reader),
            kwargs.pop("mutate", self.mutate), issue_number=18, page_id=PAGE,
            target="진행 중", options=gp.EXPECTED_STATUS_OPTIONS, **kwargs)

    def test_confirmed_mutation_then_restart_is_evidence_only(self):
        result = self.invoke()
        self.assertEqual(result["outcome"], "CONFIRMED")
        self.assertTrue(result["sync_queued"])
        self.assertFalse(result["notion_reflected"])
        self.assertEqual(self.mutations, 1)
        self.invoke(); self.assertEqual(self.mutations, 1)
        self.assertEqual([path for path, _ in self.relay.calls].count("/agent-attempt"), 1)

    def test_attempt_ack_loss_never_calls_mutation_even_after_restart(self):
        self.relay.lose = "/agent-attempt"
        self.assertEqual(self.invoke()["outcome"], "UNKNOWN_FENCED")
        self.invoke(); self.assertEqual(self.mutations, 0)
        self.assertEqual([path for path, _ in self.relay.calls].count("/agent-attempt"), 1)

    def test_result_ack_loss_retries_saved_receipt_only_without_mutation(self):
        self.relay.lose = "/agent-request"
        result = self.invoke()
        self.assertEqual(result["outcome"], "DELIVERY_UNKNOWN")
        self.assertTrue(result["project_confirmed"])
        self.assertFalse(result["sync_queued"])
        self.assertEqual(self.invoke()["outcome"], "CONFIRMED")
        self.assertEqual(self.mutations, 1)
        payloads = [value for path, value in self.relay.calls if path == "/agent-request"]
        self.assertEqual(payloads[0], payloads[1])

    def test_ambiguous_api_and_mismatching_later_readback_are_never_noop_success(self):
        def unknown(body):
            self.mutations += 1
            self.current = snapshot("준비 중")
            raise se.SyncError("synthetic timeout; may have written")
        self.assertEqual(self.invoke(mutate=unknown)["outcome"], "UNKNOWN_FENCED")
        self.current = snapshot("진행 중")
        self.invoke(); self.assertEqual(self.mutations, 1)
        self.assertNotIn("/agent-request", [path for path, _ in self.relay.calls])

    def test_known_rejection_retains_fence_with_no_receipt(self):
        def reject(body): raise helper.MutationRejected("synthetic known rejection")
        result = self.invoke(mutate=reject)
        self.assertEqual(result["outcome"], "REJECTED_FENCED")
        self.assertFalse(result["sync_queued"])
        self.invoke(); self.assertNotIn("/agent-request", [path for path, _ in self.relay.calls])

    def test_stale_before_facts_safely_aborts_without_attempt(self):
        reads = 0
        def changed(number):
            nonlocal reads; reads += 1
            return snapshot("백로그" if reads == 1 else "준비 중")
        self.assertEqual(self.invoke(reader=changed)["outcome"], "ABORTED")
        self.assertEqual(self.mutations, 0)
        self.assertNotIn("/agent-attempt", [path for path, _ in self.relay.calls])

    def test_fresh_noop_uses_shared_prepare_and_creates_no_mutation(self):
        self.current = snapshot("진행 중")
        self.assertEqual(self.invoke()["outcome"], "CONFIRMED_NOOP")
        self.assertEqual(self.mutations, 0)
        self.assertEqual([path for path, _ in self.relay.calls], ["/agent-prepare", "/agent-request"])

    def test_same_operation_local_lock_and_changed_relay_binding_fail_before_mutation(self):
        with self.journal.locked():
            with self.assertRaises(se.SyncError): self.invoke()
        self.assertEqual(self.mutations, 0)
        self.invoke(); self.relay.base = "https://other.synthetic.workers.dev"
        with self.assertRaises(se.SyncError): self.invoke()
        self.assertEqual(self.mutations, 1)

    def test_source_changed_after_mutation_is_fenced_not_success(self):
        def changed(body):
            self.mutate(body); self.current["source_fingerprint"] = "b" * 64
        self.assertEqual(self.invoke(mutate=changed)["outcome"], "UNKNOWN_FENCED")
        self.assertEqual(self.mutations, 1)

    def test_invalid_terminal_target_and_missing_issue_abort_flag_do_not_write(self):
        with self.assertRaises(se.SyncError):
            helper.run_helper(self.journal, self.relay, self.reader, self.mutate,
                issue_number=18, page_id=PAGE, target="완료", options=gp.EXPECTED_STATUS_OPTIONS)
        with self.assertRaises(se.SyncError): self.invoke(abort=True)
        self.assertEqual(self.relay.calls, [])


if __name__ == "__main__":
    unittest.main()
