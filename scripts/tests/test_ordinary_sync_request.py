import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_project as gp
import ordinary_sync_request as ordinary
import sync_simple as ss
import notion_api as na
from sync_simple_apply import project_snapshot
from sync_simple_fakes import CONFIG, PAGE, GitHub, Notion, Project, REST, checkpoint, text
from sync_simple_state import CHECKPOINT


ID = "11111111-1111-4111-8111-111111111111"
SHA = "a" * 40


def env():
    return {"SYNC_ORDINARY_REQUEST": "true", "NOTION_RELAY_ENABLED": "true",
        "NOTION_SYNC_ENABLED": "true", "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": gp.REPOSITORY,
        "GITHUB_REPOSITORY_ID": str(gp.REPOSITORY_ID), "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "123", "GITHUB_SHA": SHA,
        "GITHUB_ACTOR_ID": "7", "GITHUB_DISPATCH_ACTOR_ID": "7", "GITHUB_ACTOR": "synthetic-user",
        "GITHUB_TRIGGERING_ACTOR": "synthetic-user", "RELAY_REQUEST_ID": ID, "RELAY_GENERATION": "1",
        "GITHUB_WORKFLOW_REF": f"{gp.REPOSITORY}/{ordinary.WORKFLOW_PATH}@refs/heads/main",
        "GITHUB_WORKFLOW_ID": "8"}


def run():
    return {"id": 123, "repository": {"id": gp.REPOSITORY_ID},
        "head_repository": {"id": gp.REPOSITORY_ID}, "head_branch": "main", "head_sha": SHA,
        "event": "workflow_dispatch", "run_attempt": 1, "path": ordinary.WORKFLOW_PATH,
        "workflow_id": 8, "display_title": f"replica-sync-request:{ID}:1",
        "actor": {"id": 7}, "triggering_actor": {"id": 7}}


def relay():
    value = Mock()
    value.post.return_value = {"request_id": ID, "newly_authorized": True,
        "receipts": ["notion:synthetic"], "project_id": gp.EXPECTED_PROJECT_ID,
        "repository_id": gp.REPOSITORY_ID}
    return value


class OrdinaryRequestTests(unittest.TestCase):
    def test_cli_claim_drives_real_engine_and_reports_then_refuses_duplicate(self):
        from types import SimpleNamespace
        github, project = GitHub(), Project()
        notion = Notion("진행 중", checkpoint(github))
        remote = REST(github)
        original = remote.request
        remote.request = lambda path: run() if "/actions/runs/" in path else original(path)
        settings = {**env(), "GITHUB_TOKEN": "synthetic", "PROJECT_TOKEN": "synthetic",
                    "NOTION_TOKEN": "synthetic"}
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate), tempfile.TemporaryDirectory() as directory:
                old = os.getcwd()
                try:
                    os.chdir(directory)
                    callback = relay()
                    def respond(path, value):
                        if path == "/claim":
                            return {**relay().post.return_value, "newly_authorized": not duplicate}
                        return {"stored": True, "sealed": True}
                    callback.post.side_effect = respond
                    before = (copy.deepcopy(notion.pages), copy.deepcopy(project.items))
                    with patch.dict(os.environ, settings, clear=True), \
                         patch.object(ss, "load_config", return_value=CONFIG), \
                         patch.object(gp, "GraphQLClient", side_effect=[github, project]), \
                         patch.object(gp, "RESTClient", return_value=remote), \
                         patch.object(na, "NotionClient", return_value=notion), \
                         patch.object(ordinary, "PinnedRelay", return_value=callback), \
                         patch.object(subprocess, "run", return_value=SimpleNamespace(stdout=SHA)):
                        result = ordinary.main([])
                    self.assertEqual(result, 1 if duplicate else 0)
                    if duplicate:
                        self.assertEqual((notion.pages, project.items), before)
                    else:
                        saved = json.loads(Path(ordinary.EVIDENCE_FILE).read_text())
                        self.assertEqual(saved["report"]["holds"], [])
                        self.assertEqual(saved["entries"][0]["status"], "진행 중")
                        self.assertTrue(saved["entries"][0]["reflection"])
                        self.assertEqual([c.args[0] for c in callback.post.call_args_list],
                                         ["/claim", "/result/chunk", "/result"])
                finally:
                    os.chdir(old)

    def test_one_time_claim_exact_context_and_independent_run(self):
        remote = Mock(); remote.request.return_value = run(); callback = relay()
        context = ordinary.claim_context(env(), remote, callback, SHA)
        self.assertEqual(context.body(), {"request_id": ID, "generation": 1,
            "run_id": 123, "sha": SHA, "attempt": 1})
        callback.post.assert_called_once_with("/claim", context.body())

    def test_forged_context_rerun_ref_and_pm_injection_rejected_before_claim(self):
        for key, value in {"GITHUB_RUN_ATTEMPT": "2", "GITHUB_REF": "refs/heads/feature",
                "GITHUB_ACTOR_ID": "9", "GITHUB_DISPATCH_ACTOR_ID": "9",
                "GITHUB_TRIGGERING_ACTOR": "different-user", "NOTION_SYNC_ENABLED": "false",
                "NOTION_RELAY_ENABLED": "false", "SYNC_RESOLVE_ISSUES": "18",
                "SYNC_APPROVED_SHA": SHA, "SYNC_DRY_RUN": "true"}.items():
            with self.subTest(key=key):
                current = env(); current[key] = value; callback = relay()
                with self.assertRaises(gp.SyncError):
                    ordinary.claim_context(current, Mock(), callback, SHA)
                callback.post.assert_not_called()

    def test_independent_run_mismatch_and_duplicate_claim_never_authorize_engine(self):
        current = run(); current["triggering_actor"]["id"] = 9
        remote = Mock(); remote.request.return_value = current; callback = relay()
        with self.assertRaises(gp.SyncError): ordinary.claim_context(env(), remote, callback, SHA)
        callback.post.assert_not_called()
        remote.request.return_value = run(); callback.post.return_value["newly_authorized"] = False
        with self.assertRaises(gp.SyncError): ordinary.claim_context(env(), remote, callback, SHA)
        self.assertEqual(callback.post.call_count, 1)

    def test_engine_flag_alone_is_not_a_claim_or_pm_bypass(self):
        github, project, notion = GitHub(), Project(), Notion()
        with self.assertRaises(gp.SyncError):
            ss.sync(github, REST(github), project, notion, CONFIG, apply=True, env=env())
        self.assertEqual(notion.calls, [])
        self.assertEqual(project.calls, [])

    def test_confirmed_evidence_is_rechecked_and_never_promotes_later_edits(self):
        github, project = GitHub(), Project()
        notion = Notion("진행 중", checkpoint(github))
        entries = []
        ss.sync(github, REST(github), project, notion, CONFIG, apply=True, evidence=entries)
        facts = gp.fetch_repository_facts(github, REST(github))
        snapshot = project_snapshot(project, CONFIG, facts, refresh=True)
        self.assertTrue(ordinary.collect(entries, notion, facts, snapshot, CONFIG)[0]["reflection"])
        snapshot["items"][800]["status_option_id"] = CONFIG["status_options"]["준비 중"]
        self.assertFalse(ordinary.collect(entries, notion, facts, snapshot, CONFIG)[0]["reflection"])
        notion.pages[0]["properties"][CHECKPOINT] = text("")
        self.assertEqual(ordinary.collect(entries, notion, facts, snapshot, CONFIG), [])
        self.assertEqual(ordinary.collect([], notion, facts, snapshot, CONFIG), [])

    def test_periodic_request_with_no_receipts_still_requires_one_time_claim(self):
        remote = Mock(); remote.request.return_value = run(); callback = relay()
        callback.post.return_value["receipts"] = []
        context = ordinary.claim_context(env(), remote, callback, SHA)
        self.assertEqual(context.request_id, ID)
        callback.post.return_value["newly_authorized"] = False
        with self.assertRaises(gp.SyncError):
            ordinary.claim_context(env(), remote, callback, SHA)

    def test_split_23_entries_full_chain_and_empty_result_are_not_truncated(self):
        context = ordinary.OrdinaryClaim(ID, 1, 123, SHA, 7)
        report = {"schema": 1, "repository_id": gp.REPOSITORY_ID, "run_id": 123,
            "attempt": 1, "workflow_sha": SHA, "checked_out_sha": SHA, "dry_run": False,
            "kind": "complete", "scan_complete": True, "holds": []}
        entries = [{"synthetic": i} for i in range(23)]
        chunks, seal = ordinary.evidence_payloads(context, entries, report)
        self.assertEqual([len(value["entries"]) for value in chunks], [10, 10, 3])
        self.assertEqual(seal["entry_count"], 23)
        previous = ordinary.GENESIS
        for value in chunks:
            unsigned = {key: item for key, item in value.items() if key != "chunk_digest"}
            self.assertEqual(unsigned["previous_digest"], previous)
            previous = hashlib.sha256(ordinary.canonical(unsigned).encode()).hexdigest()
            self.assertEqual(value["chunk_digest"], previous)
        self.assertEqual(seal["final_digest"], previous)
        self.assertEqual(ordinary.evidence_payloads(context, [], report)[1]["final_digest"], ordinary.GENESIS)

    def test_result_retry_never_calls_claim_or_reexecutes_sync(self):
        context = ordinary.OrdinaryClaim(ID, 1, 123, SHA, 7)
        report = {"schema": 1, "repository_id": gp.REPOSITORY_ID, "run_id": 123,
            "attempt": 1, "workflow_sha": SHA, "checked_out_sha": SHA, "dry_run": False,
            "kind": "failed", "scan_complete": False, "holds": []}
        callback = Mock(); callback.post.side_effect = [gp.SyncError("synthetic"), {"sealed": True}]
        with patch.object(ss, "sync") as engine:
            ordinary.deliver_evidence(callback, context, [], report, sleep=lambda _: None)
            engine.assert_not_called()
        self.assertEqual([call.args[0] for call in callback.post.call_args_list], ["/result", "/result"])
        self.assertEqual(callback.post.call_args_list[0].args[1], callback.post.call_args_list[1].args[1])

    def test_pinned_https_url_and_callback_body_limit(self):
        config = {"NOTION_RELAY_URL": "https://replica.account.workers.dev", "NOTION_RELAY_HOST": "replica.account.workers.dev",
                  "WORKFLOW_CALLBACK_KEY": "synthetic-workflow-only-key-00000000"}
        ordinary.PinnedRelay(config)
        for url in ["http://replica.account.workers.dev", "https://evil.invalid", "https://replica.account.workers.dev?key=private",
                    "https://replica.account.workers.dev:443", "https://replica.account.workers.dev/other"]:
            with self.subTest(url=url), self.assertRaises(gp.SyncError):
                ordinary.PinnedRelay({**config, "NOTION_RELAY_URL": url})
        client = ordinary.PinnedRelay(config, opener=Mock())
        with self.assertRaises(gp.SyncError): client.post("/claim", {"text": "x" * 65536})
        client.opener.open.assert_not_called()

    def test_callback_sends_explicit_user_agent(self):
        # Cloudflare rejects the default Python-urllib agent before the Worker runs (error 1010).
        config = {"NOTION_RELAY_URL": "https://replica.account.workers.dev", "NOTION_RELAY_HOST": "replica.account.workers.dev",
                  "WORKFLOW_CALLBACK_KEY": "synthetic-workflow-only-key-00000000"}
        response = MagicMock(); response.status = 200; response.read.return_value = b"{}"
        opener = Mock(); opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        ordinary.PinnedRelay(config, opener=opener).post("/claim", {})
        agent = opener.open.call_args.args[0].get_header("User-agent")
        self.assertTrue(agent and not agent.startswith("Python-urllib"))


if __name__ == "__main__":
    unittest.main()
