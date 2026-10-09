import copy
import io
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discord_probe as dp
import discord_notify as dn
import discord_transport as dt

SHA = "a" * 40
MID = "123456789012345678"


def environment(send="false"):
    return {"GITHUB_REPOSITORY": dt.REPO, "GITHUB_REPOSITORY_ID": str(dt.REPO_ID),
            "GITHUB_EVENT_NAME": "workflow_dispatch", "DISCORD_MODE": "probe",
            "GITHUB_REF": "refs/heads/fix/test", "GITHUB_SHA": SHA, "DISCORD_APPROVED_SHA": SHA,
            "GITHUB_ACTOR_ID": dn.PM_ID, "GITHUB_ACTOR": dn.PM, "GITHUB_TRIGGERING_ACTOR": dn.PM,
            "GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1", "DISCORD_DRAFT_PR": "31",
            "DISCORD_SEND_TEST": send, "GITHUB_TOKEN": "synthetic",
            "DISCORD_USER_MAP": '{"Just-Simple0":"123456789012345678"}',
            "DISCORD_PROJECT_READ_TOKEN": "exclude", "PROJECT_TOKEN": "exclude", "NOTION_TOKEN": "exclude"}


def pull():
    return {"number": 31, "state": "open", "draft": True,
            "head": {"sha": SHA, "ref": "fix/test", "repo": {"id": dt.REPO_ID}},
            "base": {"ref": "main", "repo": {"id": dt.REPO_ID}}}


class Probe(unittest.TestCase):
    def test_dispatch_guard_matrix(self):
        self.assertEqual(dp.context(environment())["pr"], 31)
        for key, value in [("GITHUB_REPOSITORY_ID", "1"), ("GITHUB_EVENT_NAME", "pull_request_target"),
                           ("GITHUB_REF", "refs/heads/main"), ("GITHUB_REF", "refs/tags/v1"),
                           ("DISCORD_APPROVED_SHA", "b" * 40), ("GITHUB_ACTOR_ID", "1"),
                           ("GITHUB_TRIGGERING_ACTOR", "other"), ("GITHUB_RUN_ATTEMPT", "2"),
                           ("DISCORD_SEND_TEST", "yes"), ("DISCORD_DRAFT_PR", "31;command"),
                           ("DISCORD_RESOLVE_KEY", "existing-delivery")]:
            with self.subTest(key=key, value=value), self.assertRaises(dt.Error):
                dp.context({**environment(), key: value})

    def test_pr_identity_and_draft_guard(self):
        ctx = dp.context(environment())
        dp.verify_pr(Mock(repo=Mock(return_value=pull())), ctx)
        changed = []
        for key, value in [("draft", False), ("state", "closed"), ("number", 32)]:
            changed.append({**pull(), key: value})
        for parent, key, value in [("head", "sha", "b" * 40), ("head", "ref", "other"),
                                   ("head", "repo", {"id": 1}), ("base", "repo", {"id": 1}),
                                   ("base", "ref", "other")]:
            item = pull(); item[parent][key] = value; changed.append(item)
        for item in changed:
            with self.subTest(item=item), self.assertRaises(dt.Error):
                dp.verify_pr(Mock(repo=Mock(return_value=item)), ctx)

    def test_read_only_api_rejects_writes_before_transport(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method), patch.object(dt, "request") as request:
                with self.assertRaises(dt.Error): dp.ReadOnlyGitHub("synthetic").repo("/issues", method=method)
                request.assert_not_called()
        with patch.object(dt, "request", return_value=(200, {}, b'[]')) as request:
            dp.ReadOnlyGitHub("synthetic").repo("/issues")
            self.assertEqual(request.call_count, 1)

    def test_real_observer_injected_services_never_mutate_or_query_project(self):
        class Reader:
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                raise AssertionError("Unexpected API route")
            def pages(self, path, field=None): return []
        state = dn.new_state(); memory = dp.MemoryLedger(state); real = Mock()
        preview = dp.PreviewDiscord(real, state)
        env = {**environment(), "DISCORD_NOTIFICATIONS_ENABLED": "true",
               "DISCORD_PROJECT_READ_TOKEN": "", "DISCORD_RESOLVE_KEY": ""}
        with patch.object(dt, "request", side_effect=AssertionError("No transport write")), \
                patch("github_project.GraphQLClient", side_effect=AssertionError("No Project query")):
            self.assertEqual(dn.main([], env=env, github_factory=lambda token: Reader(),
                                     ledger_factory=lambda github: memory,
                                     discord_factory=lambda url, channel: preview), 0)
        real.verify.assert_called_once(); real.send.assert_not_called()
        self.assertEqual(memory.state, state)

    def test_memory_and_synthetic_receipts_preserve_pending_and_dependencies(self):
        state = dn.new_state(); msg = dn.payload("label", "body", dn.object_url(31), {})
        dn.queue(state, "old", msg); state["outbox"]["old"].update(status="delivered", message_id=str(10**19))
        dn.queue(state, "one", msg); dn.queue(state, "two", msg, dependencies=["one"])
        dn.queue(state, "pending", msg); state["outbox"]["pending"]["status"] = "pending"
        dn.queue(state, "blocked", msg, dependencies=["pending"])
        original = copy.deepcopy(state); memory = dp.MemoryLedger(state); preview = dp.PreviewDiscord(Mock(), state)
        with patch.object(dt, "request", side_effect=AssertionError("No remote writes")):
            self.assertEqual(dn.deliver(state, memory, preview), 1)  # Existing pending requires inspection.
        self.assertEqual(preview.count, 2)
        self.assertEqual(memory.state["outbox"]["one"]["status"], "delivered")
        self.assertEqual(memory.state["outbox"]["two"]["status"], "delivered")
        self.assertEqual(memory.state["outbox"]["pending"]["status"], "pending")
        self.assertEqual(memory.state["outbox"]["blocked"]["status"], "ready")
        self.assertNotEqual(state["outbox"]["one"]["message_id"], str(10**19))
        dt.validate_state(memory.state)
        copied = memory.load(); copied["outbox"].clear(); self.assertTrue(memory.state["outbox"])
        bad = memory.load(); bad["outbox"]["two"]["message_id"] = bad["outbox"]["one"]["message_id"]
        with self.assertRaises(dt.Error): memory.save(bad)
        self.assertEqual(original["outbox"]["one"]["status"], "ready")
        with self.assertRaises(dt.Error): dp.MemoryLedger(None)
        with self.assertRaises(dt.Error): memory.save(state, bootstrap=True)

    def execute(self, send="false", *, send_error=None, second_pr=None, observer_code=0, mapping=True):
        env = environment(send); receipts = []; observed = []; reads = []
        if not mapping: env["DISCORD_USER_MAP"] = "{}"
        real = Mock(); real.send.return_value = MID
        if send_error: real.send.side_effect = send_error
        pulls = iter([pull(), second_pr or pull()])
        def read(path):
            reads.append(path)
            return next(pulls) if path.startswith("/pulls/") else {"object": {"sha": SHA}}
        gh = Mock(repo=Mock(side_effect=read))
        def observer(args, **kw):
            observed.append(kw["env"])
            for key in ("DISCORD_PROJECT_READ_TOKEN", "PROJECT_TOKEN", "NOTION_TOKEN"):
                self.assertEqual(kw["env"][key], "")
            self.assertEqual(dn.project_backlog(kw["env"]["DISCORD_PROJECT_READ_TOKEN"], []), set())
            return observer_code
        with ExitStack() as stack:
            stack.enter_context(patch.object(dp, "ReadOnlyGitHub", return_value=gh))
            stack.enter_context(patch.object(dt, "Ledger", return_value=Mock(load=Mock(return_value=dn.new_state()))))
            stack.enter_context(patch.object(dt, "Discord", return_value=real))
            stack.enter_context(patch.object(dp.subprocess, "check_output", return_value=SHA))
            stack.enter_context(patch.object(dn, "main", side_effect=observer))
            stack.enter_context(patch("github_project.GraphQLClient", side_effect=AssertionError("No Project path")))
            stack.enter_context(patch.object(dp, "save_result", side_effect=lambda r: receipts.append(copy.deepcopy(r))))
            stack.enter_context(patch("sys.stdout", new=io.StringIO()))
            code = dp.main(env)
        self.assertEqual(env["DISCORD_PROJECT_READ_TOKEN"], "exclude")
        return code, real, receipts, reads

    def test_no_post_probe_and_confirmed_pm_test_receipt(self):
        code, real, receipts, _ = self.execute()
        self.assertEqual(code, 0); real.send.assert_not_called()
        self.assertEqual(receipts[-1]["test"]["status"], "not_requested")
        code, real, receipts, reads = self.execute("true")
        self.assertEqual(code, 0); real.send.assert_called_once()
        self.assertEqual(receipts[0]["test"]["status"], "pending")
        self.assertEqual(receipts[-1]["test"]["message_id"], MID)
        self.assertTrue(receipts[-1]["ledger_unchanged"])
        message = real.send.call_args[0][0]
        self.assertEqual(real.send.call_args.kwargs, {"retry_rate_limit": False})
        self.assertIn("테스트 알림", message["content"])
        self.assertEqual(message["allowed_mentions"], {"parse": [], "users": [MID], "replied_user": False})
        self.assertEqual(reads.count("/pulls/31"), 2)

    def test_ambiguous_or_rejected_test_has_one_post_only(self):
        for error, status in [(dt.Error("private URL"), "unknown"), (dt.HTTPError(403), "rejected")]:
            with self.subTest(status=status):
                code, real, receipts, _ = self.execute("true", send_error=error)
                self.assertEqual(code, 1); real.send.assert_called_once()
                self.assertEqual(receipts[-1]["test"]["status"], status)
                self.assertNotIn("private URL", str(receipts))

    def test_changed_draft_failed_observation_or_missing_mapping_never_posts(self):
        cases = [{"second_pr": {**pull(), "draft": False}}, {"observer_code": 1}, {"mapping": False}]
        for kw in cases:
            with self.subTest(case=kw):
                code, real, _, _ = self.execute("true", **kw)
                self.assertEqual(code, 1); real.send.assert_not_called()


if __name__ == "__main__": unittest.main()
