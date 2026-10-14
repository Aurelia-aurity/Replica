import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_project as gp
import ordinary_sync_request as ordinary
import sync_engine as se
from sync_observations import RunObservations
import test_sync_engine_core as f


ID = "11111111-1111-4111-8111-111111111111"
SHA = "a" * 40


def env():
    return {"SYNC_ORDINARY_REQUEST": "true", "NOTION_RELAY_ENABLED": "true",
        "NOTION_SYNC_ENABLED": "true", "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": gp.REPOSITORY,
        "GITHUB_REPOSITORY_ID": str(gp.REPOSITORY_ID), "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "123", "GITHUB_SHA": SHA,
        "GITHUB_ACTOR_ID": "7", "GITHUB_BOT_ACTOR_ID": "7", "GITHUB_ACTOR": "synthetic[bot]",
        "GITHUB_TRIGGERING_ACTOR": "synthetic[bot]", "RELAY_REQUEST_ID": ID, "RELAY_GENERATION": "1",
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
    def test_workflow_file_entrypoint_uses_canonical_claim_and_real_engine(self):
        setup = '''
import atexit, copy, json, os, subprocess, urllib.request
from pathlib import Path
from types import SimpleNamespace
import github_project as gp
import sync_engine as se
import ordinary_sync_request as ordinary
import test_sync_engine_core as f
import test_ordinary_sync_request as t
fixture = f.SyncEngineCoreTests(); fixture.setUp()
fixture._configure_bidirectional_status(notion_status="진행 중")
fixture.project.source_issues = {i["id"]: copy.deepcopy(i) for i in fixture.repo_graph.issues}
counts = {"claims": 0, "engine_calls": 0, "source_reads": 0, "canonical_claim": False}
config = f.config(); config["bidirectional_enabled"] = True
se._load_config = lambda env: config
graphs = iter([fixture.repo_graph, fixture.project])
gp.GraphQLClient = lambda token: next(graphs)
class Rest:
    def request(self, path):
        if "/actions/runs/" in path: return t.run()
        return fixture.rest.request(path)
gp.RESTClient = lambda token: Rest()
se.NotionClient = lambda token: fixture.notion
original_sync = se.sync
def sync(*args, **kwargs):
    counts["engine_calls"] += 1
    from ordinary_claim import OrdinaryClaim
    counts["canonical_claim"] = isinstance(kwargs["ordinary_claim"], OrdinaryClaim)
    kwargs["now"] = f.NOW
    return original_sync(*args, **kwargs)
se.sync = sync
original_facts = gp.fetch_repository_facts
def facts(*args, **kwargs):
    counts["source_reads"] += 1
    return original_facts(*args, **kwargs)
gp.fetch_repository_facts = facts
subprocess.run = lambda *args, **kwargs: SimpleNamespace(stdout=t.SHA)
class Response:
    status = 200
    def __init__(self, value): self.value = value
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, limit): return json.dumps(self.value).encode()
class Opener:
    def open(self, request, timeout):
        if request.full_url.endswith("/claim"):
            counts["claims"] += 1
            value = t.relay().post.return_value
            value["newly_authorized"] = os.environ["SYNTHETIC_SCENARIO"] != "duplicate"
            return Response(value)
        return Response({"sealed": True, "stored": True})
urllib.request.build_opener = lambda *args: Opener()
def save():
    counts["notion_writes"] = len(fixture.notion.writes)
    counts["project_writes"] = len(fixture.project.writes)
    Path("entrypoint-counts.json").write_text(json.dumps(counts))
atexit.register(save)
'''
        scripts = Path(__file__).resolve().parents[1]
        for scenario in ("valid", "invalid", "duplicate"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "sitecustomize.py").write_text(setup)
                child_env = {**env(), "SYNTHETIC_SCENARIO": scenario,
                    "GITHUB_TOKEN": "synthetic-github", "PROJECT_TOKEN": "synthetic-project",
                    "NOTION_TOKEN": "synthetic-notion", "NOTION_RELAY_URL": "https://replica.account.workers.dev",
                    "NOTION_RELAY_HOST": "replica.account.workers.dev",
                    "WORKFLOW_CALLBACK_KEY": "synthetic-workflow-only-key-00000000",
                    "PYTHONPATH": os.pathsep.join((directory, str(scripts), str(scripts / "tests")))}
                if scenario == "invalid": child_env["GITHUB_ACTOR_ID"] = "9"
                result = subprocess.run([sys.executable, "-B", str(scripts / "ordinary_sync_request.py")],
                    cwd=directory, env=child_env, capture_output=True, text=True, timeout=30)
                counts = json.loads((root / "entrypoint-counts.json").read_text())
                self.assertEqual(result.returncode, 0 if scenario == "valid" else 1, result.stderr)
                self.assertEqual(counts["claims"], 0 if scenario == "invalid" else 1)
                self.assertEqual(counts["engine_calls"], 1 if scenario == "valid" else 0)
                if scenario == "valid":
                    self.assertTrue(counts["canonical_claim"])
                    self.assertEqual(counts["source_reads"], 2)
                    self.assertGreater(counts["notion_writes"], 0)
                    self.assertEqual(counts["project_writes"], 1)
                    self.assertTrue((root / ordinary.EVIDENCE_FILE).exists())
                else:
                    self.assertEqual(counts["source_reads"], 0)
                    self.assertEqual(counts["notion_writes"], 0)
                    self.assertEqual(counts["project_writes"], 0)

    def test_one_time_claim_exact_context_and_independent_run(self):
        remote = Mock(); remote.request.return_value = run(); callback = relay()
        context = ordinary.claim_context(env(), remote, callback, SHA)
        self.assertEqual(context.body(), {"request_id": ID, "generation": 1,
            "run_id": 123, "sha": SHA, "attempt": 1})
        callback.post.assert_called_once_with("/claim", context.body())

    def test_forged_context_rerun_ref_and_pm_injection_rejected_before_claim(self):
        for key, value in {"GITHUB_RUN_ATTEMPT": "2", "GITHUB_REF": "refs/heads/feature",
                "GITHUB_ACTOR_ID": "9", "NOTION_SYNC_ENABLED": "false",
                "NOTION_RELAY_ENABLED": "false", "SYNC_RESOLVE_ISSUES": "18",
                "SYNC_APPROVED_SHA": SHA, "SYNC_DRY_RUN": "true"}.items():
            with self.subTest(key=key):
                current = env(); current[key] = value; callback = relay()
                with self.assertRaises(se.SyncError):
                    ordinary.claim_context(current, Mock(), callback, SHA)
                callback.post.assert_not_called()

    def test_independent_run_mismatch_and_duplicate_claim_never_authorize_engine(self):
        current = run(); current["triggering_actor"]["id"] = 9
        remote = Mock(); remote.request.return_value = current; callback = relay()
        with self.assertRaises(se.SyncError): ordinary.claim_context(env(), remote, callback, SHA)
        callback.post.assert_not_called()
        remote.request.return_value = run(); callback.post.return_value["newly_authorized"] = False
        with self.assertRaises(se.SyncError): ordinary.claim_context(env(), remote, callback, SHA)
        self.assertEqual(callback.post.call_count, 1)

    def test_engine_flag_alone_is_not_a_claim_or_pm_bypass(self):
        fixture = f.SyncEngineCoreTests(); fixture.setUp()
        with self.assertRaises(se.SyncError): fixture.run_sync(env=env())
        self.assertEqual(fixture.notion.writes, [])
        self.assertEqual(fixture.project.writes, [])

    def test_full_engine_claim_captures_terminal_metadata_and_strict_baseline(self):
        fixture = f.SyncEngineCoreTests(); fixture.setUp()
        fixture._configure_bidirectional_status(notion_status="진행 중")
        observations = RunObservations(source_id=f.SOURCE_ID, project_id=gp.EXPECTED_PROJECT_ID)
        context = ordinary.OrdinaryClaim(ID, 1, 123, SHA, 7)
        fixture.run_sync(bidirectional_enabled=True, env=env(), run_observations=observations,
                         ordinary_claim=context)
        facts = gp.fetch_repository_facts(fixture.repo_graph, fixture.rest)
        config = f.config(); config["bidirectional_enabled"] = True
        project = se._project_snapshot(fixture.project, config, facts)
        values = ordinary.collect(observations, fixture.notion, facts, project, config)
        self.assertEqual(len(values), 1)
        self.assertTrue(values[0]["reflection"])
        fixture.notion.pages[f.ISSUE_PAGE_ID]["properties"]["작업 상태"] = {"select": {"name": "백로그"}}
        self.assertEqual(ordinary.collect(observations, fixture.notion, facts, project, config), [])

    def test_strict_reflection_refuses_held_markers_and_project_fact_drift(self):
        fixture = f.SyncEngineCoreTests(); fixture.setUp()
        fixture._configure_bidirectional_status(notion_status="백로그")
        fixture.run_sync(bidirectional_enabled=True)
        observations = RunObservations(source_id=f.SOURCE_ID, project_id=gp.EXPECTED_PROJECT_ID)
        row = fixture.notion.pages[f.ISSUE_PAGE_ID]
        observations.record_handled(row, page_id=f.ISSUE_PAGE_ID,
            key_at_handling=se.key_for(gp.REPOSITORY_ID, f.ISSUE_ID), status_at_handling="백로그",
            outcome="handled", confirmed=True)
        facts = gp.fetch_repository_facts(fixture.repo_graph, fixture.rest)
        config = f.config(); config["bidirectional_enabled"] = True
        project = se._project_snapshot(fixture.project, config, facts)
        project["items"][f.ISSUE_ID]["status_updated_at"] = "2026-10-09T00:00:00Z"
        values = ordinary.collect(observations, fixture.notion, facts, project, config)
        self.assertEqual(len(values), 1)
        self.assertFalse(values[0]["reflection"])

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
        callback = Mock(); callback.post.side_effect = [se.SyncError("synthetic"), {"sealed": True}]
        with patch.object(se, "sync") as engine:
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
            with self.subTest(url=url), self.assertRaises(se.SyncError):
                ordinary.PinnedRelay({**config, "NOTION_RELAY_URL": url})
        client = ordinary.PinnedRelay(config, opener=Mock())
        with self.assertRaises(se.SyncError): client.post("/claim", {"text": "x" * 65536})
        client.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
