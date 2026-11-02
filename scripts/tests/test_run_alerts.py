"""Deterministic workflow history, artifact and routing tests."""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discord_alerts as da
import notification_report as nr
import run_alerts as ra

SHA = "a" * 40
MAP = {"just-simple0": "111111111111111111", "author": "222222222222222222"}
ENV = {"DISCORD_EVENTS_MODE": "live", "DISCORD_USER_MAP": json.dumps(MAP)}


def run(number=10, *, conclusion="success", name="CI", event="push",
        branch="main", path=".github/workflows/ci.yml", **extra):
    return {"id": number, "run_number": number, "run_attempt": 1, "workflow_id": 100,
            "status": "completed", "conclusion": conclusion, "name": name,
            "event": event, "head_branch": branch, "head_sha": SHA, "path": path,
            "pull_requests": [], **extra}


def sync(number=10, **extra):
    return run(number, name="GitHub → Notion", path=".github/workflows/notion-sync.yml",
               event="schedule", **extra)


def report(number, holds=(), **extra):
    return {"schema": 1, "repository_id": nr.REPO_ID, "run_id": number, "attempt": 1,
            "workflow_sha": SHA, "checked_out_sha": SHA, "dry_run": False,
            "kind": "partial" if holds else "complete", "scan_complete": True,
            "holds": [{"issue_number": n, "reason_code": c} for n, c in holds], **extra}


def zipped(value, *, filename=nr.FILE_NAME):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(filename, json.dumps(value))
    return stream.getvalue()


class API:
    """In-memory read-only counterpart of the API endpoints used by run alerts."""

    def __init__(self, history=(), reports=None, *, pulls=(), author="author"):
        self.history = list(history)
        self.reports = reports or {}
        self.pulls = pulls
        self.author = author
        self.calls = []

    def pages(self, path, key=None):
        self.calls.append(("GET", path))
        if "/artifacts" in path:
            number = int(path.split("/runs/")[1].split("/")[0])
            if number not in self.reports:
                return iter([])
            return iter([{"id": number, "name": f"notion-notification-{number}-1",
                          "expired": False}])
        if "/commits/" in path:
            return iter(self.pulls)
        if "/workflows/" in path:
            return iter(self.history)
        raise AssertionError(path)

    def get(self, path):
        self.calls.append(("GET", path))
        if "/pulls/" not in path:
            raise AssertionError(path)
        return {"user": {"login": self.author}}

    def download(self, path):
        self.calls.append(("GET", path))
        number = int(path.split("/artifacts/")[1].split("/")[0])
        value = self.reports[number]
        if isinstance(value, da.Error):
            raise value
        return value if isinstance(value, bytes) else zipped(value)


class RunAlerts(unittest.TestCase):
    def send_run(self, current, history=(), reports=None, env=None, api=None):
        self.sent = []

        def send(message, environment, *, pm=False):
            self.sent.append((message, pm))

        self.api = api or API(history, reports)
        ra.notify_run(current, self.api, env or ENV, send=send)
        return self.sent

    def test_first_main_failure_mentions_pm_team_channel(self):
        messages = self.send_run(run(conclusion="failure"))
        self.assertEqual(len(messages), 1)
        self.assertFalse(messages[0][1])
        self.assertEqual(messages[0][0]["allowed_mentions"]["users"], [MAP["just-simple0"]])

    def test_each_failure_outcome_starts_streak(self):
        for conclusion in ("failure", "timed_out", "startup_failure"):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(len(self.send_run(run(conclusion=conclusion),
                                                   [run(9, conclusion="success")])), 1)

    def test_failure_streak_is_suppressed(self):
        self.assertEqual(self.send_run(run(conclusion="failure"), [
            run(9, conclusion="timed_out"), run(8)]), [])

    def test_recovery_only_after_failure_not_after_ignored_runs(self):
        messages = self.send_run(run(), [
            run(11, conclusion="failure"), run(10, conclusion="failure"),
            run(9, conclusion="cancelled"), run(8, conclusion="skipped"),
            run(7, conclusion="failure")])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0][0]["content"].splitlines()[1], "실행 오류 복구")
        self.assertEqual(messages[0][0]["allowed_mentions"]["users"], [])
        self.assertFalse(messages[0][1])

    def test_success_without_prior_failure_is_silent(self):
        for history in ([], [run(9)], [run(9, conclusion="neutral")],
                        [run(9, conclusion="action_required")]):
            with self.subTest(history=history):
                self.assertEqual(self.send_run(run(), history), [])

    def test_ignored_outcomes_never_query_or_deliver(self):
        for conclusion in ("cancelled", "skipped", "neutral", "action_required", "stale"):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(self.send_run(run(conclusion=conclusion)), [])
                self.assertEqual(self.api.calls, [])

    def test_main_scope_excludes_pr_and_other_branch_runs(self):
        history = [run(9, event="pull_request", conclusion="failure"),
                   run(8, branch="other", conclusion="failure"), run(7)]
        self.assertEqual(len(self.send_run(run(conclusion="failure"), history)), 1)

    def test_unconfigured_workflow_and_nonmain_push_are_ignored(self):
        for current in (run(name="Other", path="other.yml"), run(branch="feature"),
                        run(event="workflow_dispatch")):
            with self.subTest(current=current):
                self.assertEqual(self.send_run(current), [])
                self.assertEqual(self.api.calls, [])

    def test_ci_workflow_names_are_configurable(self):
        env = {**ENV, "DISCORD_CI_WORKFLOWS": '["Custom CI"]'}
        messages = self.send_run(run(name="Custom CI", path="custom.yml",
                                     conclusion="failure"), env=env)
        self.assertEqual(len(messages), 1)

    def test_backend_ci_is_supported_before_main_contains_its_workflow(self):
        messages = self.send_run(run(name="Backend tests",
            path=".github/workflows/backend-tests.yml", conclusion="failure"))
        self.assertEqual(len(messages), 1)

    def test_pr_failures_mention_authors_and_have_no_recovery(self):
        pr = {"number": 61, "base": {"ref": "main"}}
        current = run(event="pull_request", branch="feature", conclusion="failure",
                      pull_requests=[pr])
        messages = self.send_run(current)
        self.assertEqual(messages[0][0]["allowed_mentions"]["users"], [MAP["author"]])
        self.assertFalse(messages[0][1])
        current["conclusion"] = "success"
        self.assertEqual(self.send_run(current, [run(9, event="pull_request",
            branch="feature", conclusion="failure")]), [])

    def test_pr_dedupe_uses_same_branch_pr_events_only(self):
        current = run(event="pull_request", branch="feature", conclusion="failure",
                      pull_requests=[{"number": 61, "base": {"ref": "main"}}])
        history = [run(9, branch="other", event="pull_request", conclusion="failure"),
                   run(8, branch="feature", event="push", conclusion="failure"),
                   run(7, branch="feature", event="pull_request")]
        self.assertEqual(len(self.send_run(current, history)), 1)
        self.assertIn("branch=feature", self.api.calls[0][1])

    def test_empty_run_prs_resolve_from_commit_without_using_actor(self):
        pulls = [{"number": 61, "head": {"ref": "feature"}, "base": {"ref": "main"}},
                 {"number": 62, "head": {"ref": "other"}, "base": {"ref": "main"}}]
        api = API(pulls=pulls)
        messages = self.send_run(run(event="pull_request", branch="feature",
                                    conclusion="failure"), api=api)
        self.assertEqual(messages[0][0]["allowed_mentions"]["users"], [MAP["author"]])
        self.assertEqual(sum("/pulls/61" in path for _, path in api.calls), 1)
        self.assertEqual(sum("/pulls/62" in path for _, path in api.calls), 0)

    def test_missing_pr_author_fails_instead_of_guessing(self):
        with self.assertRaises(da.Error):
            self.send_run(run(event="pull_request", branch="feature", conclusion="failure"))

    def test_sync_includes_pr_target_head_branches_and_routes_pm(self):
        current = sync(conclusion="failure")
        current.update(event="pull_request_target", head_branch="feature")
        messages = self.send_run(current, [sync(9, conclusion="success")])
        self.assertTrue(messages[0][1])
        self.assertEqual(messages[0][0]["allowed_mentions"]["users"], [MAP["just-simple0"]])

    def test_dynamic_ordinary_request_run_name_uses_workflow_path(self):
        current = run(name="replica-sync-request:uuid:1",
                      path=".github/workflows/notion-sync-request.yml",
                      event="workflow_dispatch", conclusion="failure")
        messages = self.send_run(current)
        self.assertTrue(messages[0][1])
        self.assertEqual(messages[0][0]["content"].splitlines()[1],
                         "Replica · Notion ordinary request")

    def test_sync_recovery_pm_and_hold_changes_from_last_valid_success(self):
        history = [sync(9, conclusion="failure"), sync(8), sync(7), sync(6)]
        reports = {10: report(10, [(1, "NEW")]), 8: report(8, dry_run=True, scan_complete=False),
                   7: report(7, [(2, "OLD")])}
        messages = self.send_run(sync(), history, reports)
        self.assertEqual(len(messages), 3)
        self.assertTrue(all(pm for _, pm in messages))
        self.assertEqual(messages[0][0]["content"].splitlines()[1], "실행 오류 복구")
        self.assertEqual(messages[1][0]["allowed_mentions"]["users"], [MAP["just-simple0"]])
        self.assertEqual(messages[2][0]["allowed_mentions"]["users"], [])

    def test_hold_diff_tracks_issue_reason_pairs_and_stable_sets_are_silent(self):
        current = {(1, "NEW"), (3, "SAME")}
        previous = {(1, "OLD"), (2, "GONE"), (3, "SAME")}
        self.assertEqual(ra.hold_changes(current, previous), [
            "동기화 보류 발생 · #1 (NEW)", "동기화 보류 해제 · #1", "동기화 보류 해제 · #2"])
        self.assertEqual(ra.hold_changes(current, current), [])

    def test_missing_previous_or_current_report_never_guesses(self):
        for reports in ({10: report(10, [(1, "NEW")])}, {9: report(9)}, {}):
            with self.subTest(reports=reports):
                self.assertEqual(self.send_run(sync(), [sync(9)], reports), [])

    def test_missing_download_is_skipped_not_cleared(self):
        reports = {10: report(10), 9: da.Error("download failed"),
                   8: report(8, [(1, "OLD")])}
        self.assertEqual(self.send_run(sync(), [sync(9), sync(8)], reports), [])

    def test_previous_report_search_is_bounded(self):
        history = [sync(n) for n in range(29, 0, -1)]
        self.assertEqual(self.send_run(sync(30), history, {
            30: report(30), 9: report(9, [(1, "OLD")])}), [])
        artifact_calls = [path for _, path in self.api.calls if "/runs/" in path]
        self.assertEqual(len(artifact_calls), 21)

    def test_hold_report_binding_does_not_confuse_pr_head_with_checkout_sha(self):
        current = sync()
        current["head_sha"] = "b" * 40
        messages = self.send_run(current, [sync(9)],
                                 {10: report(10, [(1, "NEW")]), 9: report(9)})
        self.assertEqual(len(messages), 1)

    def test_main_exit_status_for_api_and_delivery_failure(self):
        class BrokenAPI(API):
            def pages(self, path, key=None):
                raise da.Error("API unavailable")

        def broken_send(message, env, *, pm=False):
            raise da.Error("delivery failed")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps({"repository": {"full_name": da.REPO},
                                        "workflow_run": run(conclusion="failure")}))
            env = {**ENV, "GITHUB_EVENT_PATH": str(path), "GITHUB_REPOSITORY": da.REPO}
            self.assertEqual(ra.main(env, client=BrokenAPI()), 1)
            self.assertEqual(ra.main(env, client=API(), send=broken_send), 1)
            self.assertEqual(ra.main({}), 0)
            self.assertEqual(ra.main({"DISCORD_EVENTS_MODE": "unknown"}), 1)


class Reports(unittest.TestCase):
    def test_report_rejects_wrong_run_attempt_and_repository(self):
        for change in ({"run_id": 9}, {"attempt": 2}, {"repository_id": 1},
                       {"scan_complete": False}, {"dry_run": True},
                       {"workflow_sha": "b" * 40}):
            with self.subTest(change=change):
                self.assertIsNone(ra.report_holds(API(reports={10: report(10, **change)}), sync()))

    def test_artifact_zip_is_data_only_and_exact_member_required(self):
        for data in (b"not a zip", zipped(report(10), filename="../" + nr.FILE_NAME)):
            with self.subTest(data=data[:10]):
                self.assertIsNone(ra.report_holds(API(reports={10: data}), sync()))

    def test_duplicate_report_member_is_rejected(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(nr.FILE_NAME, json.dumps(report(10)))
            # zipfile warns on duplicate names; suppress only the expected test fixture warning.
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(nr.FILE_NAME, json.dumps(report(10)))
        self.assertIsNone(ra.report_holds(API(reports={10: stream.getvalue()}), sync()))


if __name__ == "__main__":
    unittest.main()
