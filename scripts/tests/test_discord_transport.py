import base64
import copy
import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discord_transport as dt
import discord_notify as dn
from test_discord_notify import SHA, run, report


class Transport(unittest.TestCase):
    def test_pagination_total_truncation_fails(self):
        gh = dt.GitHub("synthetic")
        with patch.object(gh, "call", return_value=({"total_count": 2, "workflow_runs": [{}]}, {})):
            with self.assertRaises(dt.Error): gh.pages("/actions/runs", "workflow_runs")
        with patch.object(gh, "call", side_effect=[([{}] * 100, {"Link": '<x>; rel="next"'}),
                                                   ([{}], {})]) as calls:
            self.assertEqual(len(gh.pages("/issues")), 101)
            self.assertIn("page=2", calls.call_args[0][0])

    def test_ledger_cas_bot_both_roles_and_readback(self):
        state = dn.new_state(); captured = []
        class GH:
            def repo(inner, path, **kwargs):
                captured.append((path, kwargs))
                if not path: return {"id": dt.REPO_ID, "full_name": dt.REPO}
                if kwargs.get("method") == "PUT": return {}
                return {"type": "file", "path": dt.STATE_FILE, "encoding": "base64", "sha": SHA,
                        "content": base64.b64encode(dt.canonical(state)).decode()}
        ledger = dt.Ledger(GH()); self.assertEqual(ledger.load(), state)
        ledger.save(state)
        mutation = next(k["payload"] for _, k in captured if k.get("method") == "PUT")
        self.assertEqual(mutation["branch"], dt.BRANCH)
        self.assertEqual(mutation["sha"], SHA)
        self.assertEqual(mutation["author"], dt.BOT)
        self.assertEqual(mutation["committer"], dt.BOT)

    def test_orphan_bootstrap_and_response_loss_exact_readback(self):
        state = dn.new_state(); mutations = []
        class GH:
            ready = False
            def repo(inner, path, **kwargs):
                if not path: return {"id": dt.REPO_ID, "full_name": dt.REPO}
                if kwargs.get("method") == "POST":
                    mutations.append((path, kwargs["payload"]))
                    if path == "/git/refs":
                        inner.ready = True; raise dt.Error("lost response")
                    return {"sha": SHA}
                if not inner.ready: raise dt.HTTPError(404)
                return {"type": "file", "path": dt.STATE_FILE, "encoding": "base64", "sha": SHA,
                        "content": base64.b64encode(dt.canonical(state)).decode()}
        ledger = dt.Ledger(GH()); self.assertIsNone(ledger.load())
        ledger.save(state, bootstrap=True)
        commit = next(x for p, x in mutations if p == "/git/commits")
        self.assertEqual(commit["parents"], [])
        self.assertEqual(commit["author"], dt.BOT); self.assertEqual(commit["committer"], dt.BOT)
        tree = next(x for p, x in mutations if p == "/git/trees")
        self.assertEqual(len(tree["tree"]), 1)
        self.assertFalse(any("main" in p for p, _ in mutations))

    def test_missing_file_existing_branch_cannot_reinitialize(self):
        class GH:
            def repo(inner, path, **kwargs):
                if not path: return {"id": dt.REPO_ID, "full_name": dt.REPO}
                if "/contents/" in path: raise dt.HTTPError(404)
                return {"ref": "refs/heads/" + dt.BRANCH}
        with self.assertRaises(dt.Error): dt.Ledger(GH()).load()

    def test_bad_ledger_and_duplicate_json_are_errors(self):
        bad = dn.new_state(); bad["sync_cursor"] = [1, 2, True]
        with self.assertRaises(dt.Error): dt.validate_state(bad)
        with self.assertRaises(dt.Error): dt.json_data(b'{"schema":1,"schema":2}')

    def test_sync_branch_fork_and_dispatch_rejected(self):
        good = run(1); good.update(workflow_id=dn.SYNC_ID, path=dn.SYNC_PATH, event="schedule")
        class GH:
            def repo(inner, path): raise AssertionError("Unexpected lookup")
        self.assertTrue(dn.trusted_sync(GH(), good, SHA))
        for mutation in ({"head_branch": "fix/17-project-add-readback"},
                         {"head_repository": {"id": 99}}, {"workflow_id": 999},
                         {"event": "workflow_dispatch", "actor": {"id": 99}}):
            self.assertFalse(dn.trusted_sync(GH(), {**good, **mutation}, SHA))

    def test_artifact_attempt_missing_never_falls_back(self):
        r = run(1, attempt=2)
        class GH:
            def repo(inner, path): return r
            def pages(inner, path, field):
                return [{"name": "notion-notification-1-1"}]
        with self.assertRaises(dt.Error): dt.artifact_report(GH(), r)

    def test_artifact_race_after_download_rejected(self):
        r = run(1); buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr(dt.nr.FILE_NAME, json.dumps(report(1)))
        class GH:
            token = "synthetic"
            calls = 0
            def repo(inner, path):
                inner.calls += 1
                return r if inner.calls == 1 else {**r, "run_attempt": 2, "status": "in_progress"}
            def pages(inner, path, field):
                return [{"id": 99, "name": "notion-notification-1-1", "expired": False, "size_in_bytes": 200,
                         "workflow_run": {"id": 1, "head_sha": SHA, "repository_id": dt.REPO_ID,
                                          "head_repository_id": dt.REPO_ID}}]
        with patch.object(dt, "request", return_value=(200, {}, buf.getvalue())):
            with self.assertRaises(dt.Error): dt.artifact_report(GH(), r)

    def test_manual_guard_boundaries(self):
        env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
               "GITHUB_ACTOR_ID": dn.PM_ID, "GITHUB_ACTOR": dn.PM, "GITHUB_TRIGGERING_ACTOR": dn.PM,
               "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": SHA, "DISCORD_APPROVED_SHA": SHA}
        self.assertTrue(dn.manual_guard(env))
        for key, value in (("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_ACTOR_ID", "99"),
                           ("GITHUB_REF", "refs/heads/feature"), ("DISCORD_APPROVED_SHA", "b" * 40)):
            self.assertFalse(dn.manual_guard({**env, key: value}))

    def test_other_channel_in_same_guild_is_rejected_before_request(self):
        with patch.object(dt, "request", side_effect=AssertionError("Must not query unapproved channel")):
            with self.assertRaises(dt.Error):
                dt.Discord("https://discord.com/api/webhooks/111111111111111111/"+"a"*40,
                           "222222222222222222")

    def test_project_token_write_scope_skips_graphql(self):
        with patch.object(dt.GitHub, "call", return_value=({}, {"X-OAuth-Scopes": "read:project, project"})), \
                patch("github_project.GraphQLClient", side_effect=AssertionError("Must not query")):
            self.assertEqual(dn.project_backlog("synthetic", []), set())


class DraftSinglePost(unittest.TestCase):
    def client(self):
        return dt.Discord("https://discord.com/api/webhooks/111111111111111111/" + "a" * 40, dt.CHANNEL_ID)

    def test_no_retry_mode_limits_actual_post_on_errors_and_success(self):
        payload = {"content": "synthetic test"}
        receipt = {"id": "333333333333333333", "channel_id": dt.CHANNEL_ID,
                   "webhook_id": "111111111111111111", **payload}
        outcomes = [dt.HTTPError(429, body=b'{"retry_after":0}'), dt.HTTPError(403),
                    dt.HTTPError(500), dt.Error("ambiguous"), (200, {}, json.dumps(receipt).encode())]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__), patch.object(dt, "request") as request, \
                    patch.object(dt.time, "sleep") as sleep:
                request.side_effect = [outcome]
                if isinstance(outcome, Exception):
                    with self.assertRaises(dt.Error): self.client().send(payload, retry_rate_limit=False)
                else:
                    self.assertEqual(self.client().send(payload, retry_rate_limit=False), receipt["id"])
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args.kwargs["method"], "POST")
                sleep.assert_not_called()

    def test_operational_rate_limit_retry_remains_default(self):
        payload = {"content": "synthetic test"}
        receipt = {"id": "333333333333333333", "channel_id": dt.CHANNEL_ID,
                   "webhook_id": "111111111111111111", **payload}
        with patch.object(dt, "request", side_effect=[dt.HTTPError(429, body=b'{"retry_after":0}'),
                                                     (200, {}, json.dumps(receipt).encode())]) as request, \
                patch.object(dt.time, "sleep") as sleep:
            self.assertEqual(self.client().send(payload), receipt["id"])
            self.assertEqual(request.call_count, 2)
            sleep.assert_called_once_with(0.0)


class ComparePathRegression(unittest.TestCase):
    def test_actual_call_accepts_exact_compare_and_regular_route(self):
        gh = dt.GitHub("synthetic")
        path = f"/repos/{dt.REPO}/compare/{SHA}...{'b' * 40}"
        with patch.object(dt, "request", return_value=(200, {}, b'{}')) as request:
            gh.call(path)
            request.assert_called_once_with("https://api.github.com" + path, method="GET", token="synthetic", payload=None)
        with patch.object(dt, "request", return_value=(200, {}, b'[]')) as request:
            gh.repo("/issues")
            self.assertEqual(request.call_count, 1)
        with patch.object(dt, "request", return_value=(200, {}, b'{}')) as request:
            gh.repo("/contents/docs/file%20name.md?ref=automation%2Fdiscord-state")
            self.assertEqual(request.call_count, 1)

    def test_compare_malformed_encoded_and_mutating_rejected_before_http(self):
        prefix = f"/repos/{dt.REPO}/compare/"
        paths = [prefix + "main", prefix + "main%2E%2E%2Emain",
                 prefix + SHA + "%2e%2e%2e" + SHA,
                 prefix + SHA + "..." + SHA + "/extra",
                 prefix + SHA + "..." + SHA + "?x=1",
                 f"/repos/other/Replica/compare/{SHA}...{SHA}",
                 f"/repos/other/Replica/compare/main",
                 f"/repos/{dt.REPO}/compare%2Fmain",
                 f"/repos/{dt.REPO}/%63ompare/main",
                 f"/repos/{dt.REPO}/%2E/compare/main",
                 f"/repos/{dt.REPO}/%2e%2e/Replica/compare/main",
                 f"/repos/{dt.REPO}/issues/%2e%2e/pulls",
                 f"/repos/{dt.REPO}/issues%2F1",
                 f"/repos/{dt.REPO}/issues/%252e%252e/pulls",
                 f"/repos/{dt.REPO}/issues/%5C../pulls",
                 f"/repos/{dt.REPO}/issues/%0a/pulls",
                 "/repos/../issues", "relative", prefix + "A" * 40 + "..." + SHA]
        for path in paths:
            with self.subTest(path=path), patch.object(dt, "request") as request:
                with self.assertRaises(dt.Error): dt.GitHub("synthetic").call(path)
                request.assert_not_called()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method), patch.object(dt, "request") as request:
                with self.assertRaises(dt.Error):
                    dt.GitHub("synthetic").call(prefix + SHA + "..." + SHA, method=method)
                request.assert_not_called()

    def test_sync_context_uses_real_transport_ancestry_guard(self):
        source = {**run(9), "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH, "event": "schedule"}
        for status, base, expected in [("ahead", SHA, True), ("identical", SHA, True),
                                       ("behind", SHA, False), ("ahead", "c" * 40, False)]:
            with self.subTest(status=status, base=base), patch.object(dt, "request", return_value=(
                    200, {}, dt.canonical({"status": status, "base_commit": {"sha": base}}))) as request:
                self.assertEqual(dn.sync_context(dt.GitHub("synthetic"), source, "b" * 40), expected)
                self.assertEqual(request.call_count, 1)


if __name__ == "__main__": unittest.main()
