"""Deterministic workflow history, artifact and routing tests."""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
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
            "repository": {"id": nr.REPO_ID, "full_name": da.REPO},
            "head_repository": {"id": nr.REPO_ID, "full_name": da.REPO},
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

    def __init__(self, history=(), reports=None, *, pulls=(), author="author", attempts=None,
                 tag_main=False):
        self.history = list(history)
        self.reports = reports or {}
        self.pulls = pulls
        self.author = author
        self.attempts = attempts or {}
        self.tag_main = tag_main
        self.calls = []

    def exists(self, path):
        self.calls.append(("GET", path))
        if path != f"/repos/{da.REPO}/git/ref/tags/main":
            raise AssertionError(path)
        if isinstance(self.tag_main, da.Error):
            raise self.tag_main
        return self.tag_main

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
        if "/attempts/" in path:
            return self.attempts[int(path.rsplit("/", 1)[1])]
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

    def test_failed_first_attempt_then_success_sends_one_recovery(self):
        for make_run, pm in ((run, False), (sync, True)):
            with self.subTest(scope="sync" if pm else "main"):
                api = API([make_run(9)], attempts={1: make_run(conclusion="failure")})
                messages = self.send_run(make_run(run_attempt=2), api=api)
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0][0]["content"].splitlines()[1], "실행 오류 복구")
                self.assertEqual(messages[0][1], pm)
                self.assertEqual(messages[0][0]["allowed_mentions"]["users"], [])
                self.assertEqual(api.calls[0],
                                 ("GET", f"/repos/{da.REPO}/actions/runs/10/attempts/1"))

    def test_failed_first_attempt_then_failure_does_not_repeat_alert(self):
        api = API([run(9)], attempts={1: run(conclusion="failure")})
        messages = self.send_run(run(run_attempt=2, conclusion="failure"), api=api)
        self.assertEqual(messages, [])
        self.assertEqual(api.calls, [
            ("GET", f"/repos/{da.REPO}/actions/runs/10/attempts/1")])

    def test_cancelled_attempt_falls_back_to_previous_run(self):
        api = API([run(9, conclusion="failure")],
                  attempts={1: run(conclusion="cancelled")})
        messages = self.send_run(run(run_attempt=2), api=api)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0][0]["content"].splitlines()[1], "실행 오류 복구")
        self.assertIn("/workflows/", api.calls[1][1])

    def test_cancelled_and_skipped_attempts_walk_back_before_run_history(self):
        api = API([run(9)], attempts={
            3: run(run_attempt=3, conclusion="cancelled"),
            2: run(run_attempt=2, conclusion="skipped"),
            1: run(conclusion="failure")})
        messages = self.send_run(run(run_attempt=4), api=api)
        self.assertEqual(len(messages), 1)
        self.assertEqual(api.calls, [
            ("GET", f"/repos/{da.REPO}/actions/runs/10/attempts/{attempt}")
            for attempt in (3, 2, 1)])

    def test_successful_previous_attempt_overrides_older_run_failure(self):
        api = API([run(9, conclusion="failure")], attempts={1: run()})
        self.assertEqual(self.send_run(run(run_attempt=2), api=api), [])
        self.assertEqual(len(api.calls), 1)

    def test_pr_rerun_still_does_not_send_recovery(self):
        current = run(event="pull_request", branch="feature", run_attempt=2)
        api = API(attempts={1: run(event="pull_request", branch="feature",
                                   conclusion="failure")})
        self.assertEqual(self.send_run(current, api=api), [])

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

    def test_forged_current_pr_sync_producer_cannot_publish_its_report(self):
        current = sync()
        current.update(event="pull_request", head_branch="fork-branch",
                       head_repository={"id": 42, "full_name": "attacker/Replica"})
        messages = self.send_run(current, [sync(9)], {
            10: report(10, [(888, "FORGED_HOLD")]),
            9: report(9, [(79, "REAL_HOLD")])})
        self.assertEqual(messages, [])
        self.assertEqual(self.api.calls, [])

    def test_unknown_path_with_sync_name_is_not_a_trusted_producer(self):
        current = sync(conclusion="failure")
        current["path"] = ".github/workflows/attacker.yml"
        self.assertEqual(self.send_run(current), [])
        self.assertEqual(self.api.calls, [])

    def test_untrusted_failure_cannot_suppress_a_trusted_sync_failure(self):
        forged = sync(10, conclusion="failure")
        forged.update(event="pull_request", head_branch="fork-branch",
                      head_repository={"id": 42, "full_name": "attacker/Replica"})
        messages = self.send_run(sync(11, conclusion="failure"), [forged, sync(9)])
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0][1])
        self.assertEqual(messages[0][0]["allowed_mentions"]["users"], [MAP["just-simple0"]])

    def test_untrusted_report_is_ignored_between_trusted_hold_reports(self):
        forged = sync(10)
        forged.update(event="pull_request", head_branch="fork-branch",
                      head_repository={"id": 42, "full_name": "attacker/Replica"})
        messages = self.send_run(sync(11), [forged, sync(9)], {
            11: report(11, [(79, "REAL_HOLD")]), 10: report(10),
            9: report(9, [(79, "REAL_HOLD")])})
        self.assertEqual(messages, [])
        self.assertNotIn(("GET", f"/repos/{da.REPO}/actions/runs/10/artifacts"), self.api.calls)
        self.assertIn(("GET", f"/repos/{da.REPO}/actions/runs/9/artifacts"), self.api.calls)

    def test_sync_rejects_foreign_or_missing_repository_identity(self):
        for field in ("repository", "head_repository"):
            for identity in (None, {}, {"id": 42, "full_name": da.REPO},
                             {"id": nr.REPO_ID, "full_name": "attacker/Replica"}):
                with self.subTest(field=field, identity=identity):
                    current = sync(conclusion="failure")
                    current[field] = identity
                    self.assertEqual(self.send_run(current), [])
                    self.assertEqual(self.api.calls, [])

    def test_sync_producer_event_and_dispatch_ref_allowlist(self):
        for path in (".github/workflows/notion-sync.yml",
                     ".github/workflows/notion-sync-request.yml"):
            for event_name, branch in (("pull_request", "main"), ("push", "main"),
                                       ("workflow_run", "main"),
                                       ("workflow_dispatch", "feature")):
                with self.subTest(path=path, event=event_name, branch=branch):
                    current = sync(conclusion="failure")
                    current.update(path=path, event=event_name, head_branch=branch)
                    self.assertEqual(self.send_run(current), [])
                    self.assertEqual(self.api.calls, [])
        current = sync(conclusion="failure")
        current["path"] = ".github/workflows/notion-sync-request.yml"
        self.assertEqual(self.send_run(current), [])

    def test_trusted_sync_paths_accept_ref_suffix_and_allowed_main_events(self):
        for event_name in ("schedule", "issues", "workflow_dispatch"):
            with self.subTest(event=event_name):
                current = sync(conclusion="failure")
                current.update(event=event_name,
                               path=".github/workflows/notion-sync.yml@refs/heads/main")
                messages = self.send_run(current)
                self.assertEqual(len(messages), 1)
                self.assertTrue(messages[0][1])

    def test_tag_qualified_current_producer_cannot_alert_or_use_its_report(self):
        for path in (".github/workflows/notion-sync.yml",
                     ".github/workflows/notion-sync-request.yml"):
            for conclusion in ("failure", "success"):
                with self.subTest(path=path, conclusion=conclusion):
                    current = sync(conclusion=conclusion)
                    current.update(event="workflow_dispatch", path=path + "@refs/tags/main")
                    messages = self.send_run(current, [sync(9)], {
                        10: report(10, [(888, "FORGED_HOLD")]),
                        9: report(9, [(79, "REAL_HOLD")])})
                    self.assertEqual(messages, [])
                    self.assertEqual(self.api.calls, [])

    def test_nonmain_qualified_ref_is_rejected_for_main_only_sync_events(self):
        for event_name in ("schedule", "issues", "workflow_dispatch"):
            for ref in ("refs/tags/main", "refs/heads/feature", ""):
                with self.subTest(event=event_name, ref=ref):
                    current = sync(conclusion="failure")
                    current.update(event=event_name,
                                   path=".github/workflows/notion-sync.yml@" + ref)
                    self.assertEqual(self.send_run(current), [])
                    self.assertEqual(self.api.calls, [])

    def test_tag_qualified_failure_does_not_suppress_trusted_failure(self):
        tagged = sync(10, conclusion="failure")
        tagged.update(event="workflow_dispatch",
                      path=".github/workflows/notion-sync.yml@refs/tags/main")
        messages = self.send_run(sync(11, conclusion="failure"), [tagged, sync(9)])
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0][1])

    def test_tag_qualified_report_is_not_a_hold_baseline(self):
        tagged = sync(10)
        tagged.update(event="workflow_dispatch",
                      path=".github/workflows/notion-sync.yml@refs/tags/main")
        messages = self.send_run(sync(11), [tagged, sync(9)], {
            11: report(11, [(79, "REAL_HOLD")]), 10: report(10),
            9: report(9, [(79, "REAL_HOLD")])})
        self.assertEqual(messages, [])
        self.assertNotIn(("GET", f"/repos/{da.REPO}/actions/runs/10/artifacts"), self.api.calls)
        self.assertIn(("GET", f"/repos/{da.REPO}/actions/runs/9/artifacts"), self.api.calls)

    def test_tag_qualified_attempt_cannot_establish_failure_streak(self):
        tagged = sync(conclusion="failure")
        tagged.update(event="workflow_dispatch",
                      path=".github/workflows/notion-sync.yml@refs/tags/main")
        api = API([sync(9)], attempts={1: tagged})
        messages = self.send_run(sync(run_attempt=2, conclusion="failure"), api=api)
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0][1])

    def test_bare_dispatch_requires_absence_of_tag_named_main(self):
        for path in (".github/workflows/notion-sync.yml",
                     ".github/workflows/notion-sync-request.yml"):
            for tag_main in (True, False):
                with self.subTest(path=path, tag_main=tag_main):
                    current = sync(conclusion="failure")
                    current.update(event="workflow_dispatch", path=path)
                    api = API(tag_main=tag_main)
                    messages = self.send_run(current, api=api)
                    self.assertEqual(len(messages), 0 if tag_main else 1)
                    self.assertEqual(api.calls[0],
                                     ("GET", f"/repos/{da.REPO}/git/ref/tags/main"))

    def test_explicit_main_branch_dispatch_needs_no_tag_lookup(self):
        current = sync(conclusion="failure")
        current.update(event="workflow_dispatch",
                       path=".github/workflows/notion-sync.yml@refs/heads/main")
        api = API(tag_main=True)
        self.assertEqual(len(self.send_run(current, api=api)), 1)
        self.assertNotIn(("GET", f"/repos/{da.REPO}/git/ref/tags/main"), api.calls)

    def test_pull_request_target_retains_qualified_pr_head_exception(self):
        current = sync(conclusion="failure")
        current.update(event="pull_request_target", head_branch="feature", head_sha="b" * 40,
                       path=".github/workflows/notion-sync.yml@refs/heads/feature")
        api = API(tag_main=True)
        self.assertEqual(len(self.send_run(current, api=api)), 1)
        self.assertNotIn(("GET", f"/repos/{da.REPO}/git/ref/tags/main"), api.calls)

    def test_transient_tag_lookup_error_fails_run_without_a_notice(self):
        current = sync(conclusion="failure")
        current["event"] = "workflow_dispatch"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "event.json"
            source.write_text(json.dumps({"repository": {"full_name": da.REPO},
                                          "workflow_run": current}))
            env = {**ENV, "GITHUB_EVENT_PATH": str(source), "GITHUB_REPOSITORY": da.REPO}
            sent = []
            status = ra.main(env, client=API(tag_main=da.Error("GitHub API request failed")),
                             send=lambda message, env, pm=False: sent.append(message))
            self.assertEqual(status, 1)
            self.assertEqual(sent, [])

    def test_untrusted_attempt_cannot_suppress_failure_on_trusted_rerun(self):
        forged = sync(conclusion="failure")
        forged["event"] = "pull_request"
        api = API([sync(9)], attempts={1: forged})
        messages = self.send_run(sync(run_attempt=2, conclusion="failure"), api=api)
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0][1])

    def test_main_ci_rejects_foreign_head_repository_in_current_and_history(self):
        foreign = {"id": 42, "full_name": "attacker/Replica"}
        self.assertEqual(self.send_run(run(conclusion="failure", head_repository=foreign)), [])
        self.assertEqual(self.api.calls, [])
        messages = self.send_run(run(conclusion="failure"), [
            run(9, conclusion="failure", head_repository=foreign), run(8)])
        self.assertEqual(len(messages), 1)
        self.assertFalse(messages[0][1])

    def test_pr_ci_still_accepts_fork_head_repository(self):
        current = run(event="pull_request", branch="fork-branch", conclusion="failure",
                      head_repository={"id": 42, "full_name": "attacker/Replica"},
                      pull_requests=[{"number": 61, "base": {"ref": "main"}}])
        messages = self.send_run(current)
        self.assertEqual(len(messages), 1)
        self.assertFalse(messages[0][1])
        self.assertEqual(messages[0][0]["allowed_mentions"]["users"], [MAP["author"]])

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

    def test_hold_diff_compares_issue_reason_sets(self):
        cases = [
            ({(1, "NEW"), (3, "SAME"), (4, "B"), (4, "A")},
             {(1, "OLD"), (2, "GONE"), (3, "SAME")},
             ["동기화 보류 사유 변경 · #1 (OLD -> NEW)", "동기화 보류 해제 · #2",
              "동기화 보류 발생 · #4 (A, B)"]),
            ({(1, "A")}, {(1, "A"), (1, "B")},
             ["동기화 보류 사유 변경 · #1 (A, B -> A)"]),
            ({(1, "A"), (1, "B")}, {(1, "A")},
             ["동기화 보류 사유 변경 · #1 (A -> A, B)"]),
            ({(1, "A")}, {(1, "A")}, []),
            (set(), {(1, "A"), (1, "B")}, ["동기화 보류 해제 · #1"]),
        ]
        for current, previous, expected in cases:
            with self.subTest(current=current, previous=previous):
                self.assertEqual(ra.hold_changes(current, previous), expected)

    def test_new_and_changed_holds_mention_pm_but_clearance_does_not(self):
        reports = {10: report(10, [(1, "NEW"), (3, "B"), (3, "A")]),
                   9: report(9, [(1, "OLD"), (2, "GONE")])}
        messages = self.send_run(sync(), [sync(9)], reports)
        self.assertEqual(len(messages), 3)
        for (message, pm), description, mentions in zip(messages, [
                "동기화 보류 사유 변경 · #1 (OLD -> NEW)",
                "동기화 보류 해제 · #2", "동기화 보류 발생 · #3 (A, B)"],
                [[MAP["just-simple0"]], [], [MAP["just-simple0"]]]):
            with self.subTest(description=description):
                self.assertTrue(pm)
                self.assertIn(description, message["content"].splitlines())
                self.assertEqual(message["allowed_mentions"]["users"], mentions)

    def test_missing_previous_or_current_report_never_guesses(self):
        for reports in ({10: report(10, [(1, "NEW")])}, {9: report(9)}, {}):
            with self.subTest(reports=reports):
                self.assertEqual(self.send_run(sync(), [sync(9)], reports), [])

    def test_download_error_propagates_without_using_older_snapshot(self):
        reports = {10: report(10), 9: da.Error("download failed"),
                   8: report(8, [(1, "OLD")])}
        with self.assertRaises(da.Error):
            self.send_run(sync(), [sync(9), sync(8)], reports)
        self.assertNotIn(("GET", f"/repos/{da.REPO}/actions/runs/8/artifacts"), self.api.calls)

    def test_hold_api_listing_error_propagates(self):
        class BrokenArtifactsAPI(API):
            def pages(self, path, key=None):
                if "/artifacts" in path:
                    raise da.Error("GitHub API request failed")
                return super().pages(path, key)

        with self.assertRaises(da.Error):
            self.send_run(sync(), api=BrokenArtifactsAPI([sync(9)]))

    def test_transient_hold_download_failure_exits_nonzero_even_after_recovery(self):
        cases = [
            ([sync(9)], {10: da.Error("download failed")}, 0),
            ([sync(9)], {10: report(10), 9: da.Error("download failed")}, 0),
            ([sync(9, conclusion="failure"), sync(8)],
             {10: report(10), 8: da.Error("download failed")}, 1),
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "event.json"
            source.write_text(json.dumps({"repository": {"full_name": da.REPO},
                                          "workflow_run": sync()}))
            env = {**ENV, "GITHUB_EVENT_PATH": str(source), "GITHUB_REPOSITORY": da.REPO}
            for history, reports, notices in cases:
                with self.subTest(report_ids=list(reports), notices=notices):
                    sent = []
                    status = ra.main(env, client=API(history, reports),
                                     send=lambda message, env, pm=False: sent.append(message))
                    self.assertEqual(status, 1)
                    self.assertEqual(len(sent), notices)
                    if notices:
                        self.assertEqual(sent[0]["content"].splitlines()[1], "실행 오류 복구")

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

    def test_main_diagnostics_show_safe_errors_and_hide_raw_exception_details(self):
        webhook = "https://discord.com/api/webhooks/123/fake"
        token = "FAKE_RUN_TOKEN"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "event.json"
            source.write_text(json.dumps({"repository": {"full_name": da.REPO},
                                          "workflow_run": run(conclusion="failure")}))
            env = {**ENV, "GITHUB_EVENT_PATH": str(source), "GITHUB_REPOSITORY": da.REPO,
                   "DISCORD_WEBHOOK_URL": webhook, "GITHUB_TOKEN": token}
            for failure in (da.Error("Safe delivery failure"), ValueError(f"{webhook} {token}"),
                            KeyError(f"{webhook} {token}"), OSError(f"{webhook} {token}"),
                            TypeError(f"{webhook} {token}"), RuntimeError(f"{webhook} {token}"),
                            AttributeError(f"{webhook} {token}")):
                with self.subTest(exception=type(failure).__name__):
                    output = io.StringIO()

                    def send(message, environment, *, pm=False):
                        raise failure

                    with redirect_stdout(output):
                        status = ra.main(env, client=API(), send=send)
                    self.assertEqual(status, 1)
                    detail = str(failure) if isinstance(failure, da.Error) else type(failure).__name__
                    self.assertIn(f"Discord run alert failed: {detail}", output.getvalue())
                    self.assertNotIn(webhook, output.getvalue())
                    self.assertNotIn(token, output.getvalue())


class Reports(unittest.TestCase):
    def test_invalid_report_skip_log_includes_exception_class_not_raw_data(self):
        output = io.StringIO()
        with redirect_stdout(output):
            holds = ra.report_holds(API(reports={10: b"RAW_RESPONSE_SECRET"}), sync())
        self.assertIsNone(holds)
        self.assertIn("BadZipFile", output.getvalue())
        self.assertNotIn("RAW_RESPONSE_SECRET", output.getvalue())

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
