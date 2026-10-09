import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notification_report as nr
import sync_engine as se
import test_sync_engine_core as fixture


class Producer(unittest.TestCase):
    def fixture(self):
        test = fixture.SyncEngineCoreTests(); test.setUp(); return test

    def test_complete_report_only_after_final_readback(self):
        test = self.fixture(); result = {}
        test.run_sync(notification_result=result)
        self.assertEqual(result, {"scan_complete": True, "holds": []})
        self.assertTrue(test.notion.writes)

    def test_failed_final_readback_leaves_no_completion_evidence(self):
        test = self.fixture(); result = {}
        test.notion.fail_readback_after_patch.add(fixture.CONTROL_ID)
        with self.assertRaises(se.SyncError): test.run_sync(notification_result=result)
        self.assertEqual(result, {})

    def test_dry_run_never_claims_full_scan_for_recovery(self):
        test = self.fixture(); result = {}
        test.run_sync(dry_run=True, notification_result=result)
        self.assertEqual(result, {}); self.assertEqual(test.notion.writes, [])

    def test_report_write_actual_checkout_identity_and_safe_data(self):
        sha = "a" * 40
        env = {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": sha}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(nr.subprocess, "run", return_value=types.SimpleNamespace(stdout=sha+"\n")):
            path = Path(directory) / nr.FILE_NAME
            nr.write(path, env, kind="complete", scan_complete=True)
            value = json.loads(path.read_text()); self.assertEqual(value["attempt"], 2)
            self.assertEqual(value["checked_out_sha"], sha)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(nr.subprocess, "run", return_value=types.SimpleNamespace(stdout="b"*40+"\n")):
            path = Path(directory) / nr.FILE_NAME
            with self.assertRaises(ValueError): nr.write(path, env, kind="complete", scan_complete=True)
            self.assertFalse(path.exists())


if __name__ == "__main__": unittest.main()
