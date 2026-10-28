import json
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[1]))
import notification_report as nr
import observation_clock
import sync_engine as se
import test_sync_engine_core as fixture


class Producer(unittest.TestCase):
    def fixture(self):
        test = fixture.SyncEngineCoreTests(); test.setUp(); return test

    def test_complete_report_only_after_final_readback(self):
        test = self.fixture(); result = {}
        test.run_sync(notification_result=result)
        self.assertEqual(result, {"scan_complete": True, "holds": [],
                                  "observed_at": None,
                                  "observation_uncertainty_seconds": None})
        self.assertTrue(test.notion.writes)

    def test_failed_final_readback_leaves_no_completion_evidence(self):
        test = self.fixture(); result = {}
        test.notion.fail_readback_after_patch.add(fixture.CONTROL_ID)
        with self.assertRaises(se.SyncError): test.run_sync(notification_result=result)
        self.assertEqual(result, {})

    def test_verified_fresh_date_flows_through_sync_into_complete_and_partial_reports(self):
        sha = "a" * 40
        env = {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": sha}

        def fresh_date(_client):
            now = datetime.now(timezone.utc).replace(microsecond=0)
            return {"date": format_datetime(now, usegmt=True),
                    "cache_control": "no-cache, no-store", "age": "0", "x_cache": None}

        for report_kind in ("complete", "partial"):
            with self.subTest(kind=report_kind):
                test = self.fixture(); result = {}
                run_options = {}
                if report_kind == "partial":
                    test._configure_bidirectional_status(
                        notion_status="진행 중", project_status="준비 중",
                        baseline_project_status="백로그")
                    run_options["bidirectional_enabled"] = True
                with patch.object(observation_clock, "fetch_fresh_date",
                                  side_effect=fresh_date) as fetch:
                    test.run_sync(notification_result=result, **run_options)
                expected_holds = result["holds"]
                self.assertIs(result["scan_complete"], True)
                self.assertIsInstance(result.get("observed_at"), str)
                self.assertIsInstance(result.get("observation_uncertainty_seconds"), int)
                self.assertGreaterEqual(result["observation_uncertainty_seconds"], 0)
                self.assertLessEqual(result["observation_uncertainty_seconds"], 60)
                self.assertGreaterEqual(fetch.call_count, 2)
                self.assertEqual(report_kind == "partial", bool(expected_holds))
                if report_kind == "partial":
                    expected_holds = [{
                        "issue_number": fixture.ISSUE_NUMBER,
                        "reason_code": "BIDIRECTIONAL_CONFLICT"}]
                else:
                    expected_holds = []
                self.assertEqual(result["holds"], expected_holds)

                with tempfile.TemporaryDirectory() as directory, \
                        patch.object(nr.subprocess, "run",
                                     return_value=types.SimpleNamespace(stdout=sha + "\n")):
                    path = Path(directory) / nr.FILE_NAME
                    nr.write(path, env, kind=report_kind, scan_complete=True, holds=expected_holds,
                             observed_at=result["observed_at"],
                             observation_uncertainty_seconds=(
                                 result["observation_uncertainty_seconds"]))
                    serialized = json.loads(path.read_text())

                self.assertIs(nr.validate(serialized), serialized)
                self.assertEqual(serialized["schema"], 2)
                self.assertEqual(serialized["kind"], report_kind)
                self.assertEqual(serialized["observed_at"], result["observed_at"])
                self.assertEqual(serialized["observation_uncertainty_seconds"],
                                 result["observation_uncertainty_seconds"])
                self.assertEqual(serialized["holds"], expected_holds)

    def test_sync_to_schema2_write_uses_verified_server_date_and_fails_closed_on_bad_freshness(self):
        sha = "a" * 40
        env = {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": sha}
        expected_holds = [{"issue_number": fixture.ISSUE_NUMBER,
                           "reason_code": "BIDIRECTIONAL_CONFLICT"}]
        verify_snapshot = observation_clock.verify_snapshot

        def run_with_clock(evidence_factory):
            test = self.fixture()
            test._configure_bidirectional_status(
                notion_status="진행 중", project_status="준비 중",
                baseline_project_status="백로그")
            result = {}
            def checked_snapshot(mark, fetch_date):
                evidence = fetch_date()
                monotonic_samples = iter((mark.monotonic_ns, mark.monotonic_ns + 1_000_000_000))
                wall_samples = iter((mark.utc, mark.utc + timedelta(seconds=1)))
                return verify_snapshot(
                    mark, lambda: evidence,
                    monotonic_ns=lambda: next(monotonic_samples),
                    utc_now=lambda: next(wall_samples))

            with patch.object(observation_clock, "fetch_fresh_date",
                              side_effect=evidence_factory), \
                    patch.object(observation_clock, "verify_snapshot",
                                 side_effect=checked_snapshot):
                test.run_sync(bidirectional_enabled=True, notification_result=result)
            self.assertIs(result["scan_complete"], True)
            self.assertEqual(result["holds"], expected_holds)
            with tempfile.TemporaryDirectory() as directory, \
                    patch.object(nr.subprocess, "run",
                                 return_value=types.SimpleNamespace(stdout=sha + "\n")):
                path = Path(directory) / nr.FILE_NAME
                nr.write(path, env, kind="partial", scan_complete=True,
                         holds=result["holds"], observed_at=result["observed_at"],
                         observation_uncertainty_seconds=(
                             result["observation_uncertainty_seconds"]))
                saved = json.loads(path.read_text())
            self.assertEqual(saved["holds"], expected_holds)
            self.assertEqual(saved["scan_complete"], True)
            self.assertIs(nr.validate(saved), saved)
            return result, saved

        positive_local_samples = []

        def near_valid_server_date(_client):
            local_now = datetime.now(timezone.utc)
            positive_local_samples.append(local_now)
            server_now = local_now.replace(microsecond=0) + timedelta(seconds=20)
            return {"date": format_datetime(server_now, usegmt=True),
                    "cache_control": "no-cache, no-store", "age": None,
                    "x_cache": None}

        positive, serialized = run_with_clock(near_valid_server_date)
        self.assertIsInstance(positive["observed_at"], str)
        self.assertGreaterEqual(positive["observation_uncertainty_seconds"], 20)
        self.assertLessEqual(positive["observation_uncertainty_seconds"], 60)
        server_time = positive_local_samples[-1].replace(microsecond=0) + timedelta(seconds=20)
        observed = datetime.fromisoformat(positive["observed_at"].replace("Z", "+00:00"))
        self.assertLessEqual(abs((observed - server_time).total_seconds()), 2)
        self.assertGreater((observed - positive_local_samples[-1]).total_seconds(), 10)
        self.assertEqual(serialized["observed_at"], positive["observed_at"])
        self.assertEqual(serialized["observation_uncertainty_seconds"],
                         positive["observation_uncertainty_seconds"])

        invalid_evidence = {
            "15-minute server skew": lambda _client: {
                "date": format_datetime(datetime.now(timezone.utc) + timedelta(minutes=15),
                                        usegmt=True),
                "cache_control": "no-cache", "age": "0", "x_cache": None},
            "stale Date": lambda _client: {
                "date": format_datetime(datetime.now(timezone.utc) - timedelta(minutes=2),
                                        usegmt=True),
                "cache_control": "no-cache", "age": "0", "x_cache": None},
            "cached response": lambda _client: {
                "date": format_datetime(datetime.now(timezone.utc), usegmt=True),
                "cache_control": "no-cache", "age": "0", "x_cache": "HIT"},
        }
        for reason, evidence_factory in invalid_evidence.items():
            with self.subTest(reason=reason):
                result, saved = run_with_clock(evidence_factory)
                self.assertIsNone(result["observed_at"])
                self.assertIsNone(result["observation_uncertainty_seconds"])
                self.assertIsNone(saved["observed_at"])
                self.assertIsNone(saved["observation_uncertainty_seconds"])

        final_fetches = []

        def missing_final_snapshot_evidence(_client):
            final_fetches.append(True)
            date = format_datetime(datetime.now(timezone.utc), usegmt=True)
            if len(final_fetches) == 1:
                return {"date": date, "cache_control": "no-cache", "age": "0",
                        "x_cache": None}
            return {"date": date, "cache_control": None, "age": None, "x_cache": None}

        result, saved = run_with_clock(missing_final_snapshot_evidence)
        self.assertGreaterEqual(len(final_fetches), 2)
        self.assertIsNone(result["observed_at"])
        self.assertIsNone(result["observation_uncertainty_seconds"])
        self.assertIsNone(saved["observed_at"])
        self.assertIsNone(saved["observation_uncertainty_seconds"])

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
