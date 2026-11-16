import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notification_report as nr
import github_project as gp
import notion_api as na
import sync_simple as ss
from sync_simple_state import CHECKPOINT
from sync_simple_fakes import CONFIG, FIELD, OPTIONS, SOURCE, GitHub, Notion, Project, REST, checkpoint


class Producer(unittest.TestCase):
    def test_cli_failure_dry_run_and_success_preserve_notification_contract(self):
        sha = "a" * 40
        settings = {"GITHUB_TOKEN": "synthetic", "PROJECT_TOKEN": "synthetic",
            "NOTION_TOKEN": "synthetic", "PROJECT_ID": CONFIG["project_id"],
            "PROJECT_OWNER_ID": CONFIG["owner_id"], "PROJECT_STATUS_FIELD_ID": FIELD,
            "PROJECT_STATUS_OPTIONS": json.dumps(OPTIONS), "NOTION_DATA_SOURCE_ID": SOURCE,
            "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID),
            "NOTION_SYNC_ENABLED": "true", "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": sha}
        for mode in ("success", "failure", "dry-run"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                github, project = GitHub(), Project()
                notion = Notion("진행 중", checkpoint(github))
                if mode == "failure":
                    def fail(method, path, payload):
                        if CHECKPOINT in (payload or {}).get("properties", {}):
                            raise gp.SyncError("synthetic")
                    notion.before_request = fail
                report = str(Path(directory) / nr.FILE_NAME)
                args = ["--report", str(Path(directory) / "plan.json"), "--notification-report", report]
                if mode == "dry-run":
                    args.append("--dry-run")
                with patch.dict(os.environ, settings, clear=True), \
                     patch.object(gp, "GraphQLClient", return_value=github), \
                     patch.object(gp, "RESTClient", return_value=REST(github)), \
                     patch.object(ss, "ProjectClient", return_value=project), \
                     patch.object(na, "NotionClient", return_value=notion), \
                     patch.object(nr.subprocess, "run", return_value=types.SimpleNamespace(stdout=sha)), \
                     contextlib.redirect_stdout(io.StringIO()):
                    code = ss.main(args)
                value = nr.validate(json.loads(Path(report).read_text()), run_id=123, attempt=1, sha=sha)
                self.assertEqual(code, 1 if mode == "failure" else 0)
                self.assertEqual(value["kind"], {"success": "complete", "failure": "failed", "dry-run": "skipped"}[mode])
                self.assertEqual(value["scan_complete"], mode == "success")
                self.assertEqual(value["holds"], [])
                if mode == "failure":
                    self.assertEqual(na.read_text(notion.pages[0], CHECKPOINT), "")

    def test_report_write_actual_checkout_identity_and_safe_data(self):
        sha = "a" * 40
        env = {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": sha}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(nr.subprocess, "run", return_value=types.SimpleNamespace(stdout=sha+"\n")):
            path = Path(directory) / nr.FILE_NAME
            nr.write(path, env, kind="complete", scan_complete=True)
            value = json.loads(path.read_text()); self.assertEqual(value["attempt"], 2)
            self.assertEqual(value["schema"], 2)
            self.assertIsNone(value["observed_at"])
            self.assertIsNone(value["observation_uncertainty_seconds"])
            self.assertEqual(value["checked_out_sha"], sha)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(nr.subprocess, "run", return_value=types.SimpleNamespace(stdout="b"*40+"\n")):
            path = Path(directory) / nr.FILE_NAME
            with self.assertRaises(ValueError): nr.write(path, env, kind="complete", scan_complete=True)
            self.assertFalse(path.exists())


class ReportSchema(unittest.TestCase):
    def test_v1_compatibility_and_strict_v2_observation_pair(self):
        legacy = {"schema": 1, "repository_id": nr.REPO_ID, "run_id": 4, "attempt": 1,
                  "workflow_sha": "a" * 40, "checked_out_sha": "a" * 40,
                  "dry_run": False, "kind": "partial", "scan_complete": True,
                  "holds": [{"issue_number": 8, "reason_code": "HOLD"}]}
        self.assertIs(nr.validate(legacy), legacy)
        v2 = {**legacy, "schema": 2, "observed_at": "2026-10-10T12:00:00Z",
              "observation_uncertainty_seconds": 60}
        self.assertIs(nr.validate(v2), v2)
        for change in ({"observation_uncertainty_seconds": None},
                       {"observation_uncertainty_seconds": True},
                       {"observation_uncertainty_seconds": 61},
                       {"observation_uncertainty_seconds": -1},
                       {"observation_uncertainty_seconds": 1.5},
                       {"observed_at": "2026-10-10T12:00:00+09:00"},
                       {"observed_at": "not-a-time"}, {"extra": 1}):
            with self.assertRaises(ValueError): nr.validate({**v2, **change})
        for change in ({"observed_at": v2["observed_at"],
                       "observation_uncertainty_seconds": v2["observation_uncertainty_seconds"]},
                       {"unexpected": True}):
            with self.subTest(schema1_extra=change), self.assertRaises(ValueError):
                nr.validate({**legacy, **change})
        unknown = {**v2, "observed_at": None, "observation_uncertainty_seconds": None}
        self.assertIs(nr.validate(unknown), unknown)

    def test_v2_half_null_missing_clock_keys_and_untrusted_scan_states_are_rejected(self):
        valid = {"schema": 2, "repository_id": nr.REPO_ID, "run_id": 4, "attempt": 1,
                 "workflow_sha": "a" * 40, "checked_out_sha": "a" * 40,
                 "dry_run": False, "kind": "partial", "scan_complete": True,
                 "holds": [{"issue_number": 8, "reason_code": "HOLD"}],
                 "observed_at": "2026-10-10T12:00:00Z",
                 "observation_uncertainty_seconds": 60}
        cases = {
            "observed_at null with uncertainty": {**valid, "observed_at": None},
            "missing observed_at": {key: value for key, value in valid.items()
                                    if key != "observed_at"},
            "missing uncertainty": {key: value for key, value in valid.items()
                                    if key != "observation_uncertainty_seconds"},
            "incomplete scan with trusted clock": {**valid, "scan_complete": False,
                                                    "holds": []},
            "dry run with trusted clock": {**valid, "dry_run": True,
                                           "scan_complete": False, "holds": []},
            "failed report with trusted clock": {**valid, "kind": "failed",
                                                 "scan_complete": False, "holds": []},
        }
        for case, report in cases.items():
            with self.subTest(case=case), self.assertRaises(ValueError):
                nr.validate(report)


if __name__ == "__main__": unittest.main()
