import copy
import io
import json
import re
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discord_notify as dn
import discord_transport as dt
import notification_report as nr

SHA = "a" * 40
MAPPING = {"owner": "111111111111111111", "just-simple0": "222222222222222222"}
WORKFLOW = {"id": 100, "path": ".github/workflows/ci.yml", "name": "CI"}


def run(n, *, conclusion="success", status="completed", attempt=1):
    return {"id": n, "run_number": n, "run_attempt": attempt, "conclusion": conclusion,
            "status": status, "head_sha": SHA, "head_branch": "main", "event": "push",
            "workflow_id": 100, "path": WORKFLOW["path"],
            "repository": {"id": dt.REPO_ID}, "head_repository": {"id": dt.REPO_ID}}


def report(n, holds=(), *, attempt=1, schema=1, observed_at=None, uncertainty=None):
    value = {"schema": schema, "repository_id": dt.REPO_ID, "run_id": n, "attempt": attempt,
            "workflow_sha": SHA, "checked_out_sha": SHA, "dry_run": False,
            "kind": "partial" if holds else "complete", "scan_complete": True,
            "holds": [{"issue_number": num, "reason_code": code} for num, code in holds]}
    if schema == 2:
        value.update(observed_at=observed_at,
                     observation_uncertainty_seconds=uncertainty if observed_at is not None else None)
    return value


def obj(n=18, *, pull=False):
    result = {"number": n, "id": n + 1000, "title": "title", "created_at": "2026-10-10T00:00:00Z",
              "state": "open", "user": {"login": "owner"}}
    if pull:
        result.update(head={"sha": SHA, "repo": {"id": 999}},
                      base={"ref": "main", "repo": {"id": dt.REPO_ID}},
                      merge_commit_sha="b" * 40, merged=False, merged_at=None,
                      requested_reviewers=[], requested_teams=[])
    return result


def event(n, action, at="2026-10-10T01:00:00Z", **kwargs):
    return {"id": n, "event": action, "created_at": at, **kwargs}


class Events(unittest.TestCase):
    def test_bootstrap_and_same_second_unseen(self):
        state = dn.new_state(); issue = obj()
        initial = dn.source_events([issue], {18: [event(1, "closed")]}, MAPPING)
        dn.reconcile_events(state, initial, bootstrap=True)
        self.assertFalse(state["outbox"])
        later = dn.source_events([issue], {18: [event(1, "closed"), event(2, "reopened")]}, MAPPING)
        dn.reconcile_events(state, later)
        self.assertEqual(list(state["outbox"]), ["issue:18:timeline:2"])
        dn.reconcile_events(state, later)
        self.assertEqual(len(state["outbox"]), 1)

    def test_merge_only_and_pair_deduplicate_keep_earlier_close(self):
        p = obj(pull=True); p.update(state="closed", merged=True, merged_at="2026-10-10T02:00:00Z")
        for paired in (False, True, "later"):
            events = [event(1, "closed"), event(2, "reopened"), event(3, "merged", p["merged_at"])]
            if paired: events.append(event(4, "closed", "2026-10-10T02:00:01Z" if paired == "later" else p["merged_at"]))
            messages = dn.source_events([p], {18: events}, MAPPING)
            visible = [m["content"] for m in messages.values() if m]
            self.assertEqual(sum("PR 병합" in m for m in visible), 1)
            self.assertEqual(sum("병합 없이 PR 종료" in m for m in visible), 1)

    def test_incomplete_merge_facts_fail_before_events_can_be_consumed(self):
        for change in ({"merged": None}, {"merged": 1}, {"merged": True, "merged_at": None},
                       {"merged": True, "merged_at": "invalid"},
                       {"merged": True, "state": "closed", "merged_at": "2026-02-30T00:00:00Z"},
                       {"merged": False, "merged_at": "2026-10-10T00:00:00Z"},
                       {"merged": True, "merged_at": "2026-10-10T00:00:00Z"}):
            p = obj(pull=True); p.update(change)
            with self.assertRaises(dt.Error):
                dn.source_events([p], {18: [event(1, "closed")]}, MAPPING)
        for field in ("merged", "merged_at", "state"):
            p = obj(pull=True); del p[field]
            with self.assertRaises(dt.Error): dn.source_events([p], {18: []}, MAPPING)

    def test_cancelled_request_and_rerequest(self):
        p = obj(pull=True); who = {"login": "owner"}
        timeline = [event(1, "review_requested", requested_reviewer=who),
                    event(2, "review_request_removed", requested_reviewer=who)]
        messages = dn.source_events([p], {18: timeline}, MAPPING)
        self.assertIsNone(messages["pr:18:timeline:1"])
        p["requested_reviewers"] = [who]
        timeline.append(event(3, "review_requested", requested_reviewer=who))
        messages = dn.source_events([p], {18: timeline}, MAPPING)
        self.assertEqual(messages["pr:18:timeline:3"]["allowed_mentions"]["users"], [MAPPING["owner"]])
        self.assertIsNone(messages["pr:18:timeline:1"])

    def test_missing_current_lists_or_invalid_request_never_consume(self):
        p = obj(pull=True); state = dn.new_state()
        timeline = [event(1, "review_requested", requested_reviewer={"login": "owner"})]
        for field in ("requested_reviewers", "requested_teams"):
            bad = copy.deepcopy(p); del bad[field]
            with self.assertRaises(dt.Error): dn.source_events([bad], {18: timeline}, MAPPING)
            self.assertFalse(state["seen"])
        for who in ({"requested_reviewer": {}}, {"requested_team": {"slug": None}}, {},
                    {"requested_reviewer": {"login": "owner"}, "requested_team": {"slug": "team"}}):
            with self.assertRaises(dt.Error): dn.source_events([p], {18: [event(1, "review_requested", **who)]}, MAPPING)

    def test_malicious_title_and_unknown_account(self):
        issue = obj(); issue["title"] = "@everyone <@111111111111111111> **bad**" * 200
        message = next(iter(dn.source_events([issue], {18: []}, MAPPING).values()))
        self.assertNotIn("@everyone", message["content"])
        self.assertNotIn("<@", message["content"])
        self.assertEqual(message["allowed_mentions"]["parse"], [])
        self.assertLess(len(message["content"]), 1900)
        message = dn.payload("title", "review", dn.object_url(18), {}, recipients=["unknown"])
        self.assertEqual(message["allowed_mentions"]["users"], [])

    def test_draft_creation_and_no_ready_notice(self):
        p = obj(pull=True); p["draft"] = True
        messages = dn.source_events([p], {18: [event(1, "ready_for_review")]}, MAPPING)
        self.assertEqual(len(messages), 1)
        self.assertIn("PR 생성", next(iter(messages.values()))["content"])

    def test_cancelled_persisted_unsent_request_retires_but_pending_fence_stays(self):
        p = obj(pull=True); who = {"login": "owner"}; p["requested_reviewers"] = [who]
        timeline = [event(1, "review_requested", requested_reviewer=who)]
        state = dn.new_state()
        dn.reconcile_events(state, dn.source_events([p], {18: timeline}, MAPPING))
        key = "pr:18:timeline:1"
        pending = copy.deepcopy(state); pending["outbox"][key]["status"] = "pending"
        p["requested_reviewers"] = []
        timeline.append(event(2, "review_request_removed", requested_reviewer=who))
        current = dn.source_events([p], {18: timeline}, MAPPING)
        dn.reconcile_events(state, current); dn.reconcile_events(pending, current)
        self.assertEqual(state["outbox"][key]["status"], "retired")
        self.assertEqual(pending["outbox"][key]["status"], "pending")

    def test_closed_pr_stale_review_request_is_cancelled_before_post_and_rerequest_after_reopen_works(self):
        p = obj(pull=True); who = {"login": "owner"}; p["requested_reviewers"] = [who]
        timeline = [event(1, "review_requested", requested_reviewer=who)]
        state = dn.new_state()
        dn.reconcile_events(state, dn.source_events([p], {18: timeline}, MAPPING))
        state["outbox"]["pr:18:created:2026-10-10T00:00:00Z"]["status"] = "delivered"
        old_key = "pr:18:timeline:1"
        self.assertEqual(state["outbox"][old_key]["status"], "ready")

        class GH:
            def repo(inner, path):
                self.assertEqual(path, "/pulls/18")
                return copy.deepcopy(p)
            def pages(inner, path):
                self.assertEqual(path, "/issues/18/timeline")
                return copy.deepcopy(timeline)

        class Ledger:
            def save(inner, value): pass

        class Webhook:
            calls = []
            def send(inner, message):
                inner.calls.append(message)
                return "333333333333333333"

        p["state"] = "closed"  # GitHub leaves the requested reviewer in the PR object.
        current_closed = dn.source_events([p], {18: timeline}, MAPPING)
        self.assertIsNone(current_closed[old_key])
        webhook = Webhook()
        self.assertEqual(dn.deliver(state, Ledger(), webhook,
                                    event_check=lambda key: dn.current_pr_event(GH(), key, MAPPING)), 0)
        self.assertEqual(webhook.calls, [])
        self.assertEqual(state["outbox"][old_key]["status"], "retired")

        pending = copy.deepcopy(state)
        pending["outbox"][old_key]["status"] = "pending"
        dn.reconcile_events(pending, current_closed)
        self.assertEqual(pending["outbox"][old_key]["status"], "pending")

        p["state"] = "open"
        timeline.append(event(2, "review_requested", "2026-10-10T02:00:00Z",
                              requested_reviewer=who))
        current_open = dn.source_events([p], {18: timeline}, MAPPING)
        new_key = "pr:18:timeline:2"
        self.assertIsNotNone(current_open[new_key])
        dn.reconcile_events(state, current_open)
        self.assertEqual(dn.deliver(state, Ledger(), webhook,
                                    event_check=lambda key: dn.current_pr_event(GH(), key, MAPPING)), 0)
        self.assertEqual(len(webhook.calls), 1)
        self.assertEqual(state["outbox"][new_key]["status"], "delivered")


class Incidents(unittest.TestCase):
    def _run_main_across_sync_rate_limit(self, state, initial_runs, retry_runs, reports,
                                        initial_details=None, retry_details=None):
        class GH:
            def __init__(self):
                self.runs = copy.deepcopy(initial_runs)
                self.detail_runs = copy.deepcopy(initial_details if initial_details is not None else initial_runs)
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                match = re.fullmatch(r"/actions/runs/([0-9]+)", path)
                if match:
                    run_id = int(match.group(1))
                    return copy.deepcopy(next(run for run in self.detail_runs if run["id"] == run_id))
                raise AssertionError(path)
            def pages(self, path, field):
                assert path == f"/actions/workflows/{dn.SYNC_ID}/runs"
                return copy.deepcopy(self.runs)

        class Ledger:
            def __init__(self): self.value = copy.deepcopy(state)
            def load(self): return copy.deepcopy(self.value)
            def save(self, value, **kwargs): self.value = copy.deepcopy(value)

        webhook_id, webhook_token = "111111111111111111", "a" * 40
        channel_id = dt.CHANNEL_ID
        gh, ledger, posts = GH(), Ledger(), []

        def discord_request(url, *, method="GET", payload=None, **kwargs):
            if method != "POST":
                return 200, {}, json.dumps({"guild_id": "1554806404320075786",
                                            "channel_id": channel_id}).encode()
            posts.append(copy.deepcopy(payload))
            if len(posts) == 1:
                gh.runs = copy.deepcopy(retry_runs)
                gh.detail_runs = copy.deepcopy(retry_details if retry_details is not None else retry_runs)
                raise dt.HTTPError(429, body=b'{"retry_after":0}')
            message = {"id": f"3333333333333333{len(posts)}", "channel_id": channel_id,
                       "webhook_id": webhook_id, "content": payload["content"]}
            return 200, {}, json.dumps(message).encode()

        config = json.dumps({"repository_id": dn.REPO_ID,
                             "sync_workflow_id": dn.SYNC_ID,
                             "ci_workflows": []}).encode()
        env = {"DISCORD_NOTIFICATIONS_ENABLED": "true", "GITHUB_TOKEN": "test-token",
               "DISCORD_WEBHOOK_URL": f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}",
               "DISCORD_CHANNEL_ID": channel_id}
        with (patch.object(Path, "read_bytes", return_value=config),
              patch.object(dn, "collect_objects", return_value=([], [], {})),
              patch.object(dn, "project_backlog", return_value=[]),
              patch.object(dn, "source_events", return_value={}),
              patch.object(dn.observation_clock, "verify_snapshot", return_value=(None, None)),
              patch.object(dn, "trusted_sync", return_value=True),
              patch.object(dt, "artifact_report", side_effect=lambda client, item:
                           report(item["id"], reports.get(item["id"], []))),
              patch.object(dt, "request", side_effect=discord_request),
              patch.object(dt.time, "sleep")):
            result = dn.main([], env=env, github_factory=lambda token: gh,
                             ledger_factory=lambda client: ledger)
        return result, ledger.value, posts

    def test_ci_episode_new_failures_and_current_recovery(self):
        state = dn.new_state()
        dn.reconcile_ci(state, [run(1, conclusion="failure")], [], [WORKFLOW], SHA, MAPPING)
        dn.reconcile_ci(state, [run(2, conclusion="failure")], [], [WORKFLOW], SHA, MAPPING)
        self.assertEqual(len(state["outbox"]), 1)
        dn.reconcile_ci(state, [run(3, status="in_progress"), run(2)], [], [WORKFLOW], SHA, MAPPING)
        self.assertEqual(len(state["outbox"]), 1)
        dn.reconcile_ci(state, [run(3)], [], [WORKFLOW], SHA, MAPPING)
        self.assertEqual(len(state["outbox"]), 2)
        self.assertEqual(list(state["outbox"].values())[1]["payload"]["allowed_mentions"]["users"], [])

    def test_pr_number_head_repo_event_and_current_sha(self):
        p = obj(pull=True); r = run(1)
        r.update(event="pull_request", head_sha="b" * 40,
                 pull_requests=[{"number": 18, "head": copy.deepcopy(p["head"]), "base": copy.deepcopy(p["base"])}])
        self.assertTrue(dn.ci_matches(r, p, WORKFLOW, SHA))
        for mutation in (lambda x: x.update(event="push"),
                         lambda x: x["pull_requests"][0].update(number=19),
                         lambda x: x["pull_requests"][0]["head"]["repo"].update(id=1000),
                         lambda x: x["pull_requests"][0]["head"].update(sha="c" * 40),
                         lambda x: x.update(pull_requests=[])):
            bad = copy.deepcopy(r); mutation(bad)
            self.assertFalse(dn.ci_matches(bad, p, WORKFLOW, SHA))

    def test_closed_pr_retires_without_recovery(self):
        state = dn.new_state(); p = obj(pull=True)
        state["incidents"]["ci:100:pr:18"] = {"alert": "old"}
        p["state"] = None
        dn.reconcile_ci(state, [], [p], [WORKFLOW], SHA, MAPPING)
        self.assertIn("ci:100:pr:18", state["incidents"])
        p["state"] = "closed"
        dn.reconcile_ci(state, [], [p], [WORKFLOW], SHA, MAPPING)
        self.assertFalse(state["incidents"]); self.assertFalse(state["outbox"])

    def test_missing_ci_sha_never_recovers_incident(self):
        p = obj(pull=True); p["merge_commit_sha"] = None
        r = run(1); r.update(event="pull_request", head_sha=SHA,
                            pull_requests=[{"number": 18, "head": copy.deepcopy(p["head"]), "base": copy.deepcopy(p["base"])}])
        state = dn.new_state()
        failed = copy.deepcopy(r); failed["conclusion"] = "failure"
        dn.reconcile_ci(state, [failed], [p], [WORKFLOW], SHA, MAPPING)
        for sha in (None, "", "short", True):
            bad = copy.deepcopy(r); bad["head_sha"] = sha
            self.assertFalse(dn.ci_matches(bad, p, WORKFLOW, SHA))
            dn.reconcile_ci(state, [bad], [p], [WORKFLOW], SHA, MAPPING)
        bad = copy.deepcopy(r); del bad["head_sha"]
        self.assertFalse(dn.ci_matches(bad, p, WORKFLOW, SHA))
        p["head"]["sha"] = None
        self.assertFalse(dn.ci_matches(r, p, WORKFLOW, SHA))
        self.assertIn("ci:100:pr:18", state["incidents"])
        self.assertFalse(any(k.startswith("recovery:") for k in state["outbox"]))

    def test_newer_incomplete_or_conflicting_run_cannot_clear_before_delivery(self):
        state = dn.new_state()
        failed = run(1, conclusion="failure")
        dn.reconcile_ci(state, [failed], [], [WORKFLOW], SHA, MAPPING)
        success = run(2); incomplete = run(3); del incomplete["head_sha"]
        dn.reconcile_ci(state, [failed, success, incomplete], [], [WORKFLOW], SHA, MAPPING)
        self.assertIn("ci:100:main", state["incidents"])
        candidate = run(3, attempt=2, status="in_progress")
        detail = run(3, attempt=1)
        class GH:
            def repo(inner, path): return copy.deepcopy(detail)
        observed = dn.refresh_ci_runs(GH(), [failed, success, candidate], [], [WORKFLOW], SHA)
        dn.reconcile_ci(state, observed, [], [WORKFLOW], SHA, MAPPING)
        self.assertIn("ci:100:main", state["incidents"])
        self.assertFalse(any(k.startswith("recovery:") for k in state["outbox"]))
        self.assertEqual(observed[-1]["status"], "in_progress")

    def test_unrelated_workflow_missing_sha_does_not_block_current_stream(self):
        state = dn.new_state(); unrelated = run(99)
        unrelated.update(workflow_id=200, path=".github/workflows/other.yml", head_sha=None)
        self.assertFalse(dn.ci_verdict(unrelated, None, WORKFLOW, SHA))
        dn.reconcile_ci(state, [run(1, conclusion="failure"), unrelated], [], [WORKFLOW], SHA, MAPPING)
        self.assertIn("ci:100:main", state["incidents"])

    def test_obsolete_unsent_head_rebases_failure_but_delivered_or_pending_deduplicate(self):
        for status in ("ready", "delivered", "pending"):
            state = dn.new_state(); first = run(1, conclusion="failure")
            dn.reconcile_ci(state, [first], [], [WORKFLOW], SHA, MAPPING)
            key = "failure:ci:100:main:1:1"; state["outbox"][key]["status"] = status
            if status == "delivered": state["outbox"][key]["message_id"] = "333333333333333333"
            latest = run(2, conclusion="failure"); latest["head_sha"] = "b"*40
            dn.reconcile_ci(state, [first, latest], [], [WORKFLOW], "b"*40, MAPPING)
            if status == "ready":
                self.assertEqual(state["outbox"][key]["status"], "retired")
                self.assertEqual(state["incidents"]["ci:100:main"]["alert"], "failure:ci:100:main:2:1")
            else:
                self.assertEqual(len(state["outbox"]), 1)
                self.assertEqual(state["outbox"][key]["status"], status)

    def test_closed_pr_retires_unsent_ci_alert(self):
        state = dn.new_state(); p = obj(pull=True)
        dn.queue(state, "old", dn.payload("CI", "failed", dn.object_url(18, True), MAPPING))
        state["incidents"]["ci:100:pr:18"] = {"alert": "old"}; p["state"] = "closed"
        dn.reconcile_ci(state, [], [p], [WORKFLOW], SHA, MAPPING)
        self.assertEqual(state["outbox"]["old"]["status"], "retired")

    def test_missing_report_success_is_unknown_and_failed_full_report_cannot_clear(self):
        state = dn.new_state()
        dn.reconcile_sync(state, run(1), None, MAPPING)
        self.assertFalse(state["outbox"])
        dn.reconcile_sync(state, run(2), report(2, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, run(3), report(3, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, run(4, conclusion="failure"), report(4), MAPPING)
        self.assertIn("18", state["holds"])
        self.assertFalse(any(k.startswith("hold-recovery") for k in state["outbox"]))

    def test_success_only_conclusions_clear_target(self):
        for conclusion in ("success", "failure", "cancelled", "neutral", "skipped"):
            state = dn.new_state()
            dn.reconcile_sync(state, run(1), report(1, [(18, "A")]), MAPPING)
            dn.reconcile_sync(state, run(2), report(2, [(18, "A")]), MAPPING)
            dn.reconcile_sync(state, run(3, conclusion=conclusion), report(3), MAPPING)
            self.assertEqual("18" not in state["holds"], conclusion == "success")

    def test_completed_failure_observed_while_latest_running_no_old_recovery(self):
        class GH:
            def repo(inner, path):
                match = re.fullmatch(r"/actions/runs/([0-9]+)", path)
                if match:
                    return copy.deepcopy({2: sync_run(2, conclusion="failure"),
                                          3: sync_run(3), 4: sync_run(4, status="in_progress")}
                                         [int(match.group(1))])
                raise AssertionError("No compare for same main SHA")
        def sync_run(n, **kwargs):
            return {**run(n, **kwargs), "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH, "event": "schedule"}
        state = dn.new_state()
        dn.reconcile_sync(state, sync_run(1), report(1, [(18, "A")]), MAPPING)
        with patch.object(dt, "artifact_report", side_effect=lambda gh, r: report(r["id"])):
            dn.reconcile_sync_runs(state, GH(), [sync_run(2, conclusion="failure"),
                                                sync_run(3), sync_run(4, status="in_progress")], SHA, MAPPING)
        self.assertIn("sync", state["incidents"])
        self.assertIn("18", state["holds"])
        self.assertFalse(any(k.startswith("recovery:") for k in state["outbox"]))
        self.assertEqual(state["sync_cursor"], [3, 3, 1])

    def test_untrusted_manual_attempt_is_observed_gap_not_report(self):
        class GH:
            def repo(inner, path): raise AssertionError("No compare for same main SHA")
        state = dn.new_state()
        def sync_run(n, **kwargs):
            return {**run(n, **kwargs), "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH, "event": "schedule"}
        dn.reconcile_sync(state, sync_run(1), report(1, [(18, "A")]), MAPPING)
        skipped = sync_run(1, attempt=2, conclusion="skipped")
        skipped.update(event="workflow_dispatch", actor={"id": int(dn.PM_ID)}, triggering_actor={"id": int(dn.PM_ID)})
        with patch.object(dt, "artifact_report", side_effect=lambda gh, r: report(r["id"], [(18, "A")])) as reader:
            dn.reconcile_sync_runs(state, GH(), [skipped, sync_run(2)], SHA, MAPPING)
            self.assertEqual(reader.call_count, 1)
        self.assertEqual(state["holds"]["18"]["reasons"]["A"]["streak"], 1)
        self.assertFalse(state["outbox"])

    def test_manual_dispatch_actor_list_detail_conflict_is_gap_but_approved_pm_run_still_counts(self):
        def sync_run(n):
            return {**run(n), "workflow_id": dn.SYNC_ID,
                    "path": dn.SYNC_PATH, "event": "schedule"}

        def dispatch_run(n, actor_id, triggering_id=None):
            value = {**run(n), "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                     "event": "workflow_dispatch", "actor": {"id": actor_id},
                     "triggering_actor": {"id": actor_id if triggering_id is None else triggering_id}}
            return value

        def artifact_bytes(run_id):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr(dt.nr.FILE_NAME, json.dumps(report(run_id, [(18, "A")])) )
            return stream.getvalue()

        class GH:
            token = "synthetic"
            def __init__(self, listed, detail): self.listed, self.detail = listed, detail
            def repo(self, path):
                if path == f"/actions/runs/{self.listed['id']}": return self.detail
                raise AssertionError(path)
            def pages(self, path, key):
                if path.endswith("/artifacts"):
                    rid = self.listed["id"]
                    return [{"id": 700 + rid, "name": f"notion-notification-{rid}-1",
                             "expired": False, "size_in_bytes": 120, "workflow_run": {
                                 "id": rid, "head_sha": SHA, "repository_id": dt.REPO_ID,
                                 "head_repository_id": dt.REPO_ID}}]
                raise AssertionError(path)

        state = dn.new_state()
        first = sync_run(1)
        dn.reconcile_sync(state, first, report(1, [(18, "A")]), MAPPING)
        listed = dispatch_run(2, int(dn.PM_ID))
        conflicting_detail = dispatch_run(2, 22)
        gh = GH(listed, conflicting_detail)
        with patch.object(dt, "request", side_effect=AssertionError("Mismatched actor must not download")):
            dn.reconcile_sync_runs(state, gh, [listed], SHA, MAPPING)
        self.assertEqual(state["sync_cursor"], [2, 2, 1])
        self.assertEqual(state["holds"]["18"]["reasons"]["A"]["streak"], 0)
        self.assertFalse(any(key.startswith("hold:18:A:") for key in state["outbox"]))

        approved_state = dn.new_state()
        dn.reconcile_sync(approved_state, first, report(1, [(18, "A")]), MAPPING)
        listed = dispatch_run(2, int(dn.PM_ID))
        gh = GH(listed, copy.deepcopy(listed))
        with patch.object(dt, "request", return_value=(200, {}, artifact_bytes(2))):
            dn.reconcile_sync_runs(approved_state, gh, [listed], SHA, MAPPING)
        self.assertEqual(approved_state["holds"]["18"]["reasons"]["A"]["streak"], 2)
        self.assertEqual(approved_state["holds"]["18"]["reasons"]["A"]["alert"], "hold:18:A:2")

    def test_closed_pr_manual_not_sent_does_not_revive_alert_or_recovery(self):
        state = dn.new_state(); p = obj(pull=True); p["state"] = "closed"
        key = "failure:ci:100:pr:18:1:1"
        dn.queue(state, key, dn.payload("CI", "failed", dn.object_url(18, True), MAPPING))
        state["outbox"][key]["status"] = "pending"
        state["incidents"]["ci:100:pr:18"] = {"alert": key}
        dn.reconcile_ci(state, [], [p], [WORKFLOW], SHA, MAPPING)
        self.assertEqual(state["outbox"][key]["status"], "pending")
        dn.resolve(state, key, "not_sent", None, None)
        dn.queue(state, "recovery:"+key, dn.payload("CI", "recovered", dn.object_url(18, True), MAPPING), dependencies=[key])
        dn.reconcile_ci(state, [], [p], [WORKFLOW], SHA, MAPPING)
        self.assertEqual(state["outbox"][key]["status"], "retired")
        self.assertEqual(state["outbox"]["recovery:"+key]["status"], "retired")

    def test_hold_two_distinct_runs_repeat_new_target_partial_recovery(self):
        state = dn.new_state(); a = [(18, "A")]
        dn.reconcile_sync(state, run(1), report(1, a), MAPPING)
        dn.reconcile_sync(state, run(1, attempt=2), report(1, a, attempt=2), MAPPING)
        self.assertFalse(state["outbox"])
        dn.reconcile_sync(state, run(2), report(2, a), MAPPING)
        self.assertEqual(len(state["outbox"]), 1)
        dn.reconcile_sync(state, run(3), report(3, a + [(19, "A")]), MAPPING)
        self.assertEqual(len(state["outbox"]), 1)
        dn.reconcile_sync(state, run(4), report(4, [(19, "A")]), MAPPING)
        self.assertEqual(len(state["outbox"]), 2)  # retired unsent #18 alert and #19 hold alert
        self.assertNotIn("hold-recovery:18:4:1", state["outbox"])
        dn.reconcile_sync(state, run(5), report(5, [(19, "A")]), MAPPING)
        self.assertEqual(len(state["outbox"]), 2)

    def test_reason_change_preserves_delivered_or_pending_recovery_anchor_until_current_clear(self):
        for status in ("delivered", "pending"):
            with self.subTest(status=status):
                state = dn.new_state()
                dn.reconcile_sync(state, run(1), report(1, [(18, "A")]), MAPPING)
                dn.reconcile_sync(state, run(2), report(2, [(18, "A")]), MAPPING)
                alert = "hold:18:A:2"
                state["outbox"][alert]["status"] = status
                if status == "delivered":
                    state["outbox"][alert]["message_id"] = "333333333333333333"

                dn.reconcile_sync(state, run(3), report(3, [(18, "B")]), MAPPING,
                                  current_snapshot=False)
                self.assertIn(alert, state["holds"]["18"]["recovery_alerts"])
                self.assertIsNone(state["holds"]["18"]["reasons"]["A"]["episode"])
                self.assertEqual(state["holds"]["18"]["reasons"]["A"]["streak"], 0)
                dn.reconcile_sync(state, run(4), report(4), MAPPING,
                                  current_snapshot=False)
                self.assertFalse(any(k.startswith("hold-recovery:18:") for k in state["outbox"]))
                dn.reconcile_sync(state, run(5), report(5), MAPPING,
                                  current_snapshot=True)

                recovery = state["outbox"]["hold-recovery:18:5:1"]
                self.assertEqual(recovery["dependencies"], [alert])
                self.assertEqual(state["outbox"][alert]["status"], status)
                self.assertIn(alert, state["invalidated"])

                class Ledger:
                    def save(self, value): pass
                class Webhook:
                    calls = 0
                    def send(self, message):
                        self.calls += 1
                        return "555555555555555555"
                webhook = Webhook()
                dn.deliver(state, Ledger(), webhook, hold_recovery_check=lambda key: True)
                if status == "pending":
                    self.assertEqual(webhook.calls, 0)
                    self.assertEqual(state["outbox"][alert]["status"], "pending")
                    self.assertEqual(recovery["status"], "ready")
                else:
                    self.assertEqual(webhook.calls, 1)
                    self.assertEqual(recovery["status"], "delivered")
                dt.validate_state(state)

    def test_ready_hold_alert_is_not_a_recovery_anchor_after_reason_change(self):
        state = dn.new_state()
        dn.reconcile_sync(state, run(1), report(1, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, run(2), report(2, [(18, "A")]), MAPPING)
        alert = "hold:18:A:2"
        dn.reconcile_sync(state, run(3), report(3, [(18, "B")]), MAPPING)
        self.assertEqual(state["outbox"][alert]["status"], "retired")
        self.assertNotIn(alert, state["holds"]["18"].get("recovery_alerts", []))
        dn.reconcile_sync(state, run(4), report(4), MAPPING)
        self.assertFalse(any(k.startswith("hold-recovery:18:") for k in state["outbox"]))

    def test_noncurrent_issue_clear_retains_alert_anchor_until_latest_clear(self):
        state = dn.new_state()
        dn.reconcile_sync(state, run(1), report(1, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, run(2), report(2, [(18, "A")]), MAPPING)
        alert_a = "hold:18:A:2"
        state["outbox"][alert_a].update(status="delivered", message_id="333333333333333333")
        dn.reconcile_sync(state, run(3), report(3, [(18, "B")]), MAPPING)
        dn.reconcile_sync(state, run(4), report(4, [(18, "B")]), MAPPING)
        alert_b = "hold:18:B:4"
        state["outbox"][alert_b].update(status="delivered", message_id="444444444444444444")

        dn.reconcile_sync(state, run(5), report(5), MAPPING, current_snapshot=False)
        self.assertEqual(state["holds"]["18"]["recovery_alerts"], [alert_a, alert_b])
        self.assertFalse(any(k.startswith("hold-recovery:18:") for k in state["outbox"]))
        dn.reconcile_sync(state, run(6), report(6), MAPPING, current_snapshot=True)
        self.assertEqual(state["outbox"]["hold-recovery:18:6:1"]["dependencies"],
                         [alert_a, alert_b])
        dt.validate_state(state)

    def test_recurrent_reason_has_new_episode_and_keeps_old_recovery_anchor(self):
        state = dn.new_state()
        dn.reconcile_sync(state, run(1), report(1, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, run(2), report(2, [(18, "A")]), MAPPING)
        old_alert = "hold:18:A:2"
        state["outbox"][old_alert].update(status="delivered", message_id="333333333333333333")
        old_episode = state["holds"]["18"]["reasons"]["A"]["episode"]["episode_id"]

        dn.reconcile_sync(state, run(3), report(3, [(18, "B")]), MAPPING)
        dn.reconcile_sync(state, run(4), report(4, [(18, "A")]), MAPPING)
        reason = state["holds"]["18"]["reasons"]["A"]
        self.assertEqual(reason["streak"], 1)
        self.assertNotEqual(reason["episode"]["episode_id"], old_episode)
        self.assertIn(old_alert, state["holds"]["18"]["recovery_alerts"])
        dn.reconcile_sync(state, run(5), report(5, [(18, "A")]), MAPPING)
        new_alert = "hold:18:A:5"
        state["outbox"][new_alert].update(status="delivered", message_id="444444444444444444")
        dn.reconcile_sync(state, run(6), report(6), MAPPING)
        self.assertEqual(state["outbox"]["hold-recovery:18:6:1"]["dependencies"],
                         [old_alert, new_alert])
        dt.validate_state(state)

    def test_delay_notice_requires_bounded_clocks_and_later_valid_snapshot_cancels(self):
        state = dn.new_state(); held = [(18, "A")]
        first = report(1, held, schema=2, observed_at="2026-10-10T12:00:00Z", uncertainty=1)
        dn.reconcile_sync(state, run(1), first, MAPPING)
        dn.reconcile_hold_delays(state, ("2026-10-10T12:16:59Z", 0), MAPPING)
        self.assertFalse(any(key.startswith("hold-delay:") for key in state["outbox"]))
        dn.reconcile_hold_delays(state, ("2026-10-10T12:17:00Z", 0), MAPPING)
        delay_key = "hold-delay:18:A:18:A:1:1"
        self.assertIn(delay_key, state["outbox"])
        self.assertEqual(state["holds"]["18"]["reasons"]["A"]["streak"], 1)

        followup = report(2, held, schema=2, observed_at=None, uncertainty=None)
        dn.reconcile_sync(state, run(2), followup, MAPPING)
        episode = state["holds"]["18"]["reasons"]["A"]["episode"]
        self.assertEqual(episode["delay_alert_key"], "cancelled")
        self.assertTrue(episode["delay_cancelled"])
        self.assertIn(delay_key, state["invalidated"])
        self.assertEqual(state["holds"]["18"]["reasons"]["A"]["streak"], 2)

    def test_delivered_or_pending_delay_notice_is_a_recovery_anchor_but_ready_is_not(self):
        for status in ("delivered", "pending", "ready"):
            with self.subTest(status=status):
                state = dn.new_state()
                dn.reconcile_sync(state, run(1), report(
                    1, [(18, "A")], schema=2,
                    observed_at="2026-10-10T12:00:00Z", uncertainty=1), MAPPING)
                dn.reconcile_hold_delays(state, ("2026-10-10T12:17:00Z", 0), MAPPING)
                delay_key = "hold-delay:18:A:18:A:1:1"
                if status != "ready":
                    state["outbox"][delay_key]["status"] = status
                if status == "delivered":
                    state["outbox"][delay_key]["message_id"] = "333333333333333333"

                dn.reconcile_sync(state, run(2), report(2), MAPPING)
                recovery_key = "hold-recovery:18:2:1"
                if status == "ready":
                    self.assertNotIn(recovery_key, state["outbox"])
                    self.assertEqual(state["outbox"][delay_key]["status"], "retired")
                else:
                    recovery = state["outbox"][recovery_key]
                    self.assertEqual(recovery["dependencies"], [delay_key])
                    self.assertIn(delay_key, state["invalidated"])

                    class Ledger:
                        def save(self, value): pass
                    class Webhook:
                        calls = 0
                        def send(self, message):
                            self.calls += 1
                            return "444444444444444444"
                    webhook = Webhook()
                    dn.deliver(state, Ledger(), webhook, hold_recovery_check=lambda key: True)
                    if status == "pending":
                        self.assertEqual(webhook.calls, 0)
                        self.assertEqual(state["outbox"][delay_key]["status"], "pending")
                        self.assertEqual(recovery["status"], "ready")
                    else:
                        self.assertEqual(webhook.calls, 1)
                        self.assertEqual(recovery["status"], "delivered")
                dt.validate_state(state)

    def test_followup_cancelling_delivered_delay_keeps_recovery_anchor(self):
        state = dn.new_state()
        dn.reconcile_sync(state, run(1), report(
            1, [(18, "A")], schema=2,
            observed_at="2026-10-10T12:00:00Z", uncertainty=1), MAPPING)
        dn.reconcile_hold_delays(state, ("2026-10-10T12:17:00Z", 0), MAPPING)
        delay_key = "hold-delay:18:A:18:A:1:1"
        state["outbox"][delay_key].update(status="delivered", message_id="333333333333333333")

        dn.reconcile_sync(state, run(2), report(2, [(18, "A")]), MAPPING)
        episode = state["holds"]["18"]["reasons"]["A"]["episode"]
        self.assertEqual(episode["delay_alert_key"], "cancelled")
        self.assertIn(delay_key, state["holds"]["18"]["recovery_alerts"])
        dn.reconcile_sync(state, run(3), report(3), MAPPING)
        self.assertEqual(state["outbox"]["hold-recovery:18:3:1"]["dependencies"],
                         [delay_key])
        dt.validate_state(state)

    def test_noncurrent_valid_clear_cancels_delay_and_starts_fresh_episode_on_recurrence(self):
        state = dn.new_state()
        held = report(1, [(18, "A")], schema=2,
                      observed_at="2026-10-10T12:00:00Z", uncertainty=1)
        dn.reconcile_sync(state, run(1), held, MAPPING, current_snapshot=False)
        dn.reconcile_hold_delays(state, ("2026-10-10T12:17:00Z", 1), MAPPING)
        delay_key = "hold-delay:18:A:18:A:1:1"
        self.assertIn(delay_key, state["outbox"])

        dn.reconcile_sync(state, run(2), report(2), MAPPING, current_snapshot=False)
        self.assertIn(delay_key, state["invalidated"])
        self.assertEqual(state["outbox"][delay_key]["status"], "retired")
        dt.validate_state(state)
        dn.reconcile_hold_delays(state, ("2026-10-10T13:00:00Z", 1), MAPPING)
        self.assertEqual(sum(k.startswith("hold-delay:18:A:") for k in state["outbox"]), 1)

        old_episode = state["holds"]["18"]["reasons"]["A"]["episode"]
        self.assertIsNone(old_episode)
        dn.reconcile_sync(state, run(3), report(3, [(18, "A")]), MAPPING,
                          current_snapshot=False)
        reason = state["holds"]["18"]["reasons"]["A"]
        self.assertEqual(reason["streak"], 1)
        self.assertNotEqual(reason["episode"]["episode_id"], "18:A:1:1")

    def test_noncurrent_clear_retires_unsent_hold_alert_but_keeps_pending_fence(self):
        for status, expected in (("ready", "retired"), ("pending", "pending")):
            with self.subTest(status=status):
                state = dn.new_state()
                dn.reconcile_sync(state, run(1), report(1, [(18, "A")]), MAPPING,
                                  current_snapshot=False)
                dn.reconcile_sync(state, run(2), report(2, [(18, "A")]), MAPPING,
                                  current_snapshot=False)
                key = "hold:18:A:2"
                state["outbox"][key]["status"] = status
                dn.reconcile_sync(state, run(3), report(3), MAPPING,
                                  current_snapshot=False)
                self.assertIn(key, state["invalidated"])
                self.assertEqual(state["outbox"][key]["status"], expected)
                self.assertFalse(dn.notice_active(state, key))
                dt.validate_state(state)

                class Ledger:
                    def save(self, value): pass
                class Webhook:
                    calls = 0
                    def send(self, message):
                        self.calls += 1
                        return "333333333333333333"
                webhook = Webhook()
                dn.deliver(state, Ledger(), webhook)
                self.assertEqual(webhook.calls, 0)
                self.assertEqual(state["outbox"][key]["status"], expected)

                dn.reconcile_sync(state, run(4), report(4, [(18, "A")]), MAPPING,
                                  current_snapshot=False)
                fresh_episode = state["holds"]["18"]["reasons"]["A"]["episode"]["episode_id"]
                self.assertNotEqual(fresh_episode, "18:A:1:1")
                dn.reconcile_sync(state, run(5), report(5, [(18, "A")]), MAPPING,
                                  current_snapshot=False)
                self.assertEqual(state["holds"]["18"]["reasons"]["A"]["alert"],
                                 "hold:18:A:5")
                self.assertEqual(state["outbox"][key]["status"], expected)
                dt.validate_state(state)

    def test_two_valid_runs_reserve_alert_while_newer_run_pending_then_failure(self):
        state = dn.new_state()
        def sync_run(n, **kwargs):
            return {**run(n, **kwargs), "workflow_id": dn.SYNC_ID,
                    "path": dn.SYNC_PATH, "event": "schedule"}
        dn.reconcile_sync(state, sync_run(1), report(1, [(18, "A")]), MAPPING)
        with patch.object(dt, "artifact_report",
                          side_effect=lambda gh, item: report(item["id"], [(18, "A")])):
            dn.reconcile_sync_runs(state, object(),
                                   [sync_run(2), sync_run(3, status="in_progress")],
                                   SHA, MAPPING)
        key = "hold:18:A:2"
        self.assertIn(key, state["outbox"])
        self.assertEqual(state["outbox"][key]["status"], "ready")

        with patch.object(dt, "artifact_report", side_effect=dt.Error("missing report")):
            dn.reconcile_sync_runs(state, object(),
                                   [sync_run(3, conclusion="failure")], SHA, MAPPING)
        self.assertTrue(dn.notice_active(state, key))
        class Ledger:
            def save(self, value): pass
        class Webhook:
            calls = 0
            def send(self, message):
                self.calls += 1
                return f"{333333333333333333 + self.calls}"
        webhook = Webhook()
        self.assertEqual(dn.deliver(state, Ledger(), webhook), 0)
        self.assertGreaterEqual(webhook.calls, 1)
        self.assertEqual(state["outbox"][key]["status"], "delivered")

    def test_noncurrent_recovery_waits_for_latest_completed_valid_snapshot(self):
        state = dn.new_state()
        dn.reconcile_sync(state, run(1), report(1, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, run(2), report(2, [(18, "A")]), MAPPING)
        key = "hold:18:A:2"
        state["outbox"][key].update(status="delivered", message_id="333333333333333333")

        dn.reconcile_sync(state, run(3), report(3), MAPPING,
                          current_snapshot=False)
        self.assertFalse(any(k.startswith("hold-recovery:") for k in state["outbox"]))
        self.assertIn("18", state["holds"])
        dn.reconcile_sync(state, run(4, conclusion="failure"), None, MAPPING)
        self.assertFalse(any(k.startswith("hold-recovery:") for k in state["outbox"]))
        dn.reconcile_sync(state, run(5), report(5), MAPPING,
                          current_snapshot=True)
        self.assertIn("hold-recovery:18:5:1", state["outbox"])
        dt.validate_state(state)

    def test_ready_hold_recovery_waits_for_newest_trusted_clear_before_post(self):
        def sync_run(n, *, conclusion="success", status="completed"):
            return {**run(n, conclusion=conclusion, status=status),
                    "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                    "event": "schedule"}

        state = dn.new_state()
        dn.reconcile_sync(state, run(1), report(1, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, run(2), report(2, [(18, "A")]), MAPPING)
        alert = "hold:18:A:2"
        state["outbox"][alert].update(status="delivered", message_id="333333333333333333")
        dn.reconcile_sync(state, run(3), report(3), MAPPING)
        recovery = "hold-recovery:18:3:1"
        original_dependencies = list(state["outbox"][recovery]["dependencies"])
        self.assertEqual(state["outbox"][recovery]["status"], "ready")

        class GH:
            def __init__(self, latest): self.latest = latest
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                match = re.fullmatch(r"/actions/runs/([0-9]+)", path)
                if match:
                    run_id = int(match.group(1))
                    return copy.deepcopy(next(run for run in self.runs if run["id"] == run_id))
                raise AssertionError(path)
            def pages(self, path, key):
                self.assert_request(path, key)
                return [sync_run(3), copy.deepcopy(self.latest)]
            @staticmethod
            def assert_request(path, key):
                assert path == f"/actions/workflows/{dn.SYNC_ID}/runs"
                assert key == "workflow_runs"

        class Ledger:
            def save(self, value): pass

        class Webhook:
            calls = 0
            def send(self, message):
                self.calls += 1
                return "333333333333333334"

        # A newer in-progress trusted run is observed by reconciliation. It
        # cannot authorize the older ready recovery, which stays retryable.
        gh = GH(sync_run(4, status="in_progress"))
        dn.reconcile_sync_runs(state, gh, [gh.latest], SHA, MAPPING)
        self.assertIsNone(dn.latest_sync_holds(gh, SHA))
        webhook = Webhook()
        self.assertEqual(dn.deliver(state, Ledger(), webhook), 0)
        self.assertEqual(webhook.calls, 0)
        self.assertEqual(state["outbox"][recovery]["status"], "ready")
        check = lambda key: dn.current_hold_recovery(gh, key, SHA)
        self.assertEqual(dn.deliver(state, Ledger(), webhook,
                                    hold_recovery_check=check), 0)
        self.assertEqual(webhook.calls, 0)
        self.assertEqual(state["outbox"][recovery]["status"], "ready")
        self.assertEqual(state["outbox"][recovery]["dependencies"], original_dependencies)

        # Failed or invalid latest evidence remains unknown and also preserves
        # the same recovery and dependency fence without POSTing.
        for latest, artifact_error in (
                (sync_run(4, conclusion="failure"), None),
                (sync_run(4), dt.Error("invalid latest artifact"))):
            with self.subTest(latest=latest["status"], conclusion=latest["conclusion"]):
                gh.latest = latest
                webhook = Webhook()
                with patch.object(dt, "artifact_report", side_effect=artifact_error) if artifact_error else patch.object(
                        dt, "artifact_report", side_effect=dt.Error("failed run has no valid artifact")):
                    self.assertIsNone(dn.latest_sync_holds(gh, SHA))
                    self.assertEqual(dn.deliver(state, Ledger(), webhook,
                                                hold_recovery_check=check), 0)
                self.assertEqual(webhook.calls, 0)
                self.assertEqual(state["outbox"][recovery]["status"], "ready")
                self.assertEqual(state["outbox"][recovery]["dependencies"], original_dependencies)

        # A later latest completed trusted clear confirms the same pending
        # recovery; it is delivered once with its original dependency intact.
        gh.latest = sync_run(4)
        with patch.object(dt, "artifact_report", return_value=report(4)):
            dn.reconcile_sync_runs(state, gh, [gh.latest], SHA, MAPPING)
            self.assertEqual(dn.latest_sync_holds(gh, SHA), set())
            webhook = Webhook()
            self.assertEqual(dn.deliver(state, Ledger(), webhook,
                                        hold_recovery_check=check), 0)
        self.assertEqual(webhook.calls, 1)
        self.assertEqual(state["outbox"][recovery]["status"], "delivered")
        self.assertEqual(state["outbox"][recovery]["dependencies"], original_dependencies)
        dt.validate_state(state)

    def test_newer_valid_snapshot_with_recurring_hold_retires_ready_recovery(self):
        state = dn.new_state()
        dn.reconcile_sync(state, run(1), report(1, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, run(2), report(2, [(18, "A")]), MAPPING)
        state["outbox"]["hold:18:A:2"].update(
            status="delivered", message_id="333333333333333333")
        dn.reconcile_sync(state, run(3), report(3), MAPPING)
        recovery = "hold-recovery:18:3:1"
        self.assertEqual(state["outbox"][recovery]["status"], "ready")

        dn.reconcile_sync(state, run(4), report(4, [(18, "A")]), MAPPING)
        self.assertIn(recovery, state["invalidated"])
        class Ledger:
            def save(self, value): pass
        class Webhook:
            calls = 0
            def send(self, message): self.calls += 1
        webhook = Webhook()
        dn.deliver(state, Ledger(), webhook,
                   hold_recovery_check=lambda key: self.fail("retired recovery must not check latest"))
        self.assertEqual(webhook.calls, 0)
        self.assertEqual(state["outbox"][recovery]["status"], "retired")

    def test_related_malformed_sha_consumes_gap_without_reading_its_report(self):
        def sync_run(n, *, conclusion="success", head_sha=SHA):
            value = {**run(n, conclusion=conclusion),
                     "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                     "event": "schedule", "head_sha": head_sha}
            return value

        class GH:
            def repo(self, path): raise AssertionError("Same-main runs need no compare")
            def pages(self, path, key): return self.runs

        state = dn.new_state()
        first = sync_run(1)
        dn.reconcile_sync(state, first, report(1, [(18, "A")]), MAPPING)
        gap = sync_run(2, conclusion="failure")
        del gap["head_sha"]
        latest = sync_run(3)
        gh = GH(); gh.runs = [first, gap, latest]
        with patch.object(dt, "artifact_report", side_effect=lambda client, item: report(
                item["id"], [(18, "A")])) as artifact:
            dn.reconcile_sync_runs(state, gh, gh.runs, SHA, MAPPING)
        # A failed list row without a matching complete detail binding is not
        # consumed; later valid runs wait for the next poll.
        self.assertEqual(artifact.call_args_list, [])
        self.assertEqual(state["sync_cursor"], [1, 1, 1])
        self.assertEqual(state["holds"]["18"]["reasons"]["A"]["streak"], 1)
        self.assertFalse(any(key.startswith("hold:18:A:") for key in state["outbox"]))

        # A malformed newest related run blocks an older valid clear; a
        # positively unrelated workflow/repository run does not.
        old_clear = sync_run(3)
        malformed_latest = sync_run(4, head_sha="short")
        gh.runs = [old_clear, malformed_latest]
        with patch.object(dt, "artifact_report", side_effect=AssertionError(
                "An older artifact cannot authorize recovery")):
            self.assertIsNone(dn.latest_sync_holds(gh, SHA))
        incomplete_scope = sync_run(4)
        del incomplete_scope["event"]
        gh.runs = [old_clear, incomplete_scope]
        with patch.object(dt, "artifact_report", side_effect=AssertionError(
                "Incomplete related metadata cannot authorize recovery")):
            self.assertIsNone(dn.latest_sync_holds(gh, SHA))
        unrelated = sync_run(5)
        unrelated["workflow_id"] = 999
        gh.runs = [old_clear, unrelated]
        with patch.object(dt, "artifact_report", return_value=report(3)):
            self.assertEqual(dn.latest_sync_holds(gh, SHA), set())

    def test_sync_scope_missing_or_malformed_identity_is_gap_only_when_related(self):
        complete = {**run(1), "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                    "event": "schedule"}
        cases = [
            ({"repository": None, "head_repository": None}, None),
            ({"repository": {"id": "bad"}, "head_repository": None}, None),
            ({"repository": {"id": dt.REPO_ID}, "head_repository": {"id": False}}, None),
            ({"workflow_id": "bad"}, None),
            ({"path": 7}, None),
            ({"workflow_id": 999}, False),
            ({"path": ".github/workflows/other.yml"}, False),
            ({"repository": {"id": dt.REPO_ID + 1}}, False),
            ({"head_repository": {"id": dt.REPO_ID + 1}}, False),
            ({}, True),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                candidate = copy.deepcopy(complete)
                candidate.update(changes)
                self.assertIs(dn.sync_scope(candidate), expected)
        identity_missing = copy.deepcopy(complete)
        for key in ("workflow_id", "path", "repository", "head_repository"):
            identity_missing.pop(key, None)
        self.assertIs(dn.sync_scope(identity_missing), False)
        self.assertIs(dn.sync_scope(identity_missing, workflow_endpoint=True), None)
        for changes in ({"workflow_id": 999}, {"repository": {"id": dt.REPO_ID + 1}}):
            candidate = {**identity_missing, **changes}
            self.assertIs(dn.sync_scope(candidate, workflow_endpoint=True), False)

    def test_related_missing_repo_identity_breaks_streak_and_blocks_old_clear_delivery(self):
        def sync_run(n):
            return {**run(n), "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                    "event": "schedule"}

        state = dn.new_state()
        first = sync_run(1)
        dn.reconcile_sync(state, first, report(1, [(18, "A")]), MAPPING)
        incomplete = sync_run(2)
        incomplete.pop("repository")
        incomplete.pop("head_repository")
        latest = sync_run(3)
        class GH:
            runs = []
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                match = re.fullmatch(r"/actions/runs/([0-9]+)", path)
                if match:
                    run_id = int(match.group(1))
                    return copy.deepcopy(next(run for run in self.runs if run["id"] == run_id))
                raise AssertionError(path)
            def pages(self, path, key): return self.runs
        gh = GH()
        gh.runs = [first, incomplete, latest]
        with patch.object(dt, "artifact_report", return_value=report(3, [(18, "A")])) as artifact:
            dn.reconcile_sync_runs(state, gh, gh.runs, SHA, MAPPING)
        self.assertEqual([call.args[1]["id"] for call in artifact.call_args_list], [3])
        self.assertEqual(state["holds"]["18"]["reasons"]["A"]["streak"], 1)
        self.assertFalse(any(key.startswith("hold:18:A:") for key in state["outbox"]))

        # A previously queued recovery remains ready, but unknown latest
        # identity cannot use the older clear to authorize a webhook POST.
        state = dn.new_state()
        dn.reconcile_sync(state, sync_run(1), report(1, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, sync_run(2), report(2, [(18, "A")]), MAPPING)
        state["outbox"]["hold:18:A:2"].update(status="delivered", message_id="333333333333333333")
        old_clear = sync_run(3)
        dn.reconcile_sync(state, old_clear, report(3), MAPPING)
        recovery = "hold-recovery:18:3:1"
        malformed_latest = sync_run(4)
        malformed_latest["repository"] = {"id": "not-an-id"}
        gh.runs = [old_clear, malformed_latest]
        self.assertIsNone(dn.current_hold_recovery(gh, recovery, SHA))
        class Ledger:
            def save(self, value): pass
        class Webhook:
            calls = 0
            def send(self, message): self.calls += 1; return "333333333333333334"
        webhook = Webhook()
        dependencies = list(state["outbox"][recovery]["dependencies"])
        self.assertEqual(dn.deliver(state, Ledger(), webhook,
                                    hold_recovery_check=lambda key: dn.current_hold_recovery(gh, key, SHA)), 0)
        self.assertEqual(webhook.calls, 0)
        self.assertEqual(state["outbox"][recovery]["status"], "ready")
        self.assertEqual(state["outbox"][recovery]["dependencies"], dependencies)

    def test_list_detail_artifact_conflict_cannot_clear_hold_or_post_recovery(self):
        def sync_run(n, *, conclusion="success"):
            return {**run(n, conclusion=conclusion), "workflow_id": dn.SYNC_ID,
                    "path": dn.SYNC_PATH, "event": "schedule"}

        cases = (
            ("list-success/detail-failure", {"conclusion": "failure"}, False),
            ("sha-conflict", {"head_sha": "b" * 40}, False),
            ("attempt-change", {"run_attempt": 2}, False),
            ("download-conclusion-change", {"conclusion": "failure"}, True),
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr(dt.nr.FILE_NAME, json.dumps(report(3)))
        class Ledger:
            def save(self, value): pass
        class Webhook:
            def __init__(self): self.calls = 0
            def send(self, message): self.calls += 1; return "333333333333333334"

        for label, changes, after_download in cases:
            with self.subTest(case=label):
                listed = sync_run(3)
                state = dn.new_state()
                dn.reconcile_sync(state, sync_run(1), report(1, [(18, "A")]), MAPPING)
                dn.reconcile_sync(state, sync_run(2), report(2, [(18, "A")]), MAPPING)
                state["outbox"]["hold:18:A:2"].update(
                    status="delivered", message_id="333333333333333333")

                class GH:
                    token = "synthetic"
                    run_reads = 0
                    def repo(inner, path):
                        if path == "/actions/runs/3":
                            inner.run_reads += 1
                            if not after_download or inner.run_reads == 2:
                                return {**listed, **changes}
                            return listed
                        raise AssertionError(path)
                    def pages(inner, path, key):
                        if path == "/actions/runs/3/artifacts":
                            return [{"id": 31, "name": "notion-notification-3-1", "expired": False,
                                     "size_in_bytes": 120, "workflow_run": {
                                         "id": 3, "head_sha": SHA, "repository_id": dt.REPO_ID,
                                         "head_repository_id": dt.REPO_ID}}]
                        raise AssertionError(path)
                gh = GH()
                with patch.object(dt, "request", return_value=(200, {}, buf.getvalue())):
                    dn.reconcile_sync_runs(state, gh, [listed], SHA, MAPPING)
                self.assertIn("18", state["holds"])
                self.assertFalse(any(key.startswith("hold-recovery:") for key in state["outbox"]))
                webhook = Webhook()
                self.assertEqual(dn.deliver(state, Ledger(), webhook,
                                            hold_recovery_check=lambda key: True), 0)
                self.assertEqual(webhook.calls, 0)

    def test_run_number_conflict_cannot_select_old_clear_or_post_recovery(self):
        def sync_run(n, *, run_number=None):
            value = {**run(n), "run_number": n if run_number is None else run_number,
                     "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH, "event": "schedule"}
            return value

        def zip_report(run_id, holds):
            value = io.BytesIO()
            with zipfile.ZipFile(value, "w") as archive:
                archive.writestr(dt.nr.FILE_NAME, json.dumps(report(run_id, holds)))
            return value.getvalue()

        state = dn.new_state()
        dn.reconcile_sync(state, sync_run(1), report(1, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, sync_run(2), report(2, [(18, "A")]), MAPPING)
        state["outbox"]["hold:18:A:2"].update(status="delivered", message_id="333333333333333333")
        listed_old_clear = sync_run(3, run_number=99)
        listed_current_hold = sync_run(4)
        detail_old_clear = sync_run(3, run_number=3)
        detail_current_hold = sync_run(4)

        class GH:
            token = "synthetic"
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                match = re.fullmatch(r"/actions/runs/([0-9]+)", path)
                if match: return {3: detail_old_clear, 4: detail_current_hold}[int(match.group(1))]
                raise AssertionError(path)
            def pages(self, path, key):
                if path == f"/actions/workflows/{dn.SYNC_ID}/runs":
                    return [listed_old_clear, listed_current_hold]
                match = re.fullmatch(r"/actions/runs/([0-9]+)/artifacts", path)
                if match:
                    run_id = int(match.group(1))
                    return [{"id": 300 + run_id, "name": f"notion-notification-{run_id}-1",
                             "expired": False, "size_in_bytes": 120, "workflow_run": {
                                 "id": run_id, "head_sha": SHA, "repository_id": dt.REPO_ID,
                                 "head_repository_id": dt.REPO_ID}}]
                raise AssertionError(path)
        gh = GH()
        downloaded = []
        def artifact_download(url, **kwargs):
            downloaded.append(url)
            return (200, {}, zip_report(3, []) if "/artifacts/303/" in url else
                    zip_report(4, [(18, "A")]))
        with patch.object(dt, "request", side_effect=artifact_download):
            dn.reconcile_sync_runs(state, gh, [listed_old_clear, listed_current_hold], SHA, MAPPING)
            recovery = dn.current_hold_recovery
            self.assertIsNone(recovery(gh, "hold-recovery:18:3:1", SHA))
            class Ledger:
                def save(self, value): pass
            class Webhook:
                calls = 0
                def send(self, message): self.calls += 1; return "333333333333333334"
            webhook = Webhook()
            dn.deliver(state, Ledger(), webhook,
                       hold_recovery_check=lambda key: recovery(gh, key, SHA))
        self.assertIn("18", state["holds"])
        self.assertFalse(any(key.startswith("hold-recovery:") and
                             state["outbox"][key]["status"] == "ready" for key in state["outbox"]))
        self.assertNotIn("/artifacts/303/zip", " ".join(downloaded))
        self.assertEqual(webhook.calls, 0)

    def test_workflow_endpoint_identity_gap_breaks_streak_and_blocks_recovery_post(self):
        def sync_run(n):
            return {**run(n), "workflow_id": dn.SYNC_ID,
                    "path": dn.SYNC_PATH, "event": "schedule"}

        state = dn.new_state()
        first = sync_run(1)
        dn.reconcile_sync(state, first, report(1, [(18, "A")]), MAPPING)
        missing = sync_run(2)
        for key in ("workflow_id", "path", "repository", "head_repository"):
            missing.pop(key, None)
        third = sync_run(3)
        class GH:
            runs = []
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                raise AssertionError(path)
            def pages(self, path, key): return self.runs
        gh = GH()
        gh.runs = [first, missing, third]
        with patch.object(dt, "artifact_report", return_value=report(3, [(18, "A")])) as artifact:
            dn.reconcile_sync_runs(state, gh, gh.runs, SHA, MAPPING)
        self.assertEqual([call.args[1]["id"] for call in artifact.call_args_list], [3])
        self.assertEqual(state["holds"]["18"]["reasons"]["A"]["streak"], 1)
        self.assertFalse(any(key.startswith("hold:18:A:") for key in state["outbox"]))

        # A later endpoint row with all per-run identity omitted blocks an old clear.
        state = dn.new_state()
        dn.reconcile_sync(state, sync_run(1), report(1, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, sync_run(2), report(2, [(18, "A")]), MAPPING)
        state["outbox"]["hold:18:A:2"].update(status="delivered", message_id="333333333333333333")
        clear = sync_run(3)
        dn.reconcile_sync(state, clear, report(3), MAPPING)
        latest = sync_run(4)
        for key in ("workflow_id", "path", "repository", "head_repository"):
            latest.pop(key, None)
        gh.runs = [clear, latest]
        self.assertIsNone(dn.current_hold_recovery(gh, "hold-recovery:18:3:1", SHA))
        class Ledger:
            def save(self, value): pass
        class Webhook:
            calls = 0
            def send(self, message): self.calls += 1; return "333333333333333334"
        webhook = Webhook()
        self.assertEqual(dn.deliver(state, Ledger(), webhook,
                                    hold_recovery_check=lambda key: dn.current_hold_recovery(gh, key, SHA)), 0)
        self.assertEqual(webhook.calls, 0)
        self.assertEqual(state["outbox"]["hold-recovery:18:3:1"]["status"], "ready")

    def test_main_rechecks_each_recovery_after_first_webhook_advances_latest_run(self):
        issue_numbers = (18, 19)
        state = dn.new_state()
        for n in (1, 2):
            dn.reconcile_sync(state, run(n), report(n, [(number, "A") for number in issue_numbers]), MAPPING)
        for number in issue_numbers:
            alert = f"hold:{number}:A:2"
            state["outbox"][alert].update(status="delivered", message_id=f"33333333333333333{number}")
        dn.reconcile_sync(state, run(3), report(3), MAPPING)
        recovery18 = "hold-recovery:18:3:1"
        recovery19 = "hold-recovery:19:3:1"
        original_dependencies = list(state["outbox"][recovery19]["dependencies"])

        def sync_run(n):
            return {**run(n), "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                    "event": "schedule"}

        class GH:
            def __init__(self): self.latest_runs = [sync_run(3)]
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                raise AssertionError(f"Unexpected repository lookup: {path}")
            def pages(self, path, key):
                self.assert_runs_request(path, key)
                return copy.deepcopy(self.latest_runs)
            @staticmethod
            def assert_runs_request(path, key):
                assert path == f"/actions/workflows/{dn.SYNC_ID}/runs"
                assert key == "workflow_runs"

        class Ledger:
            def __init__(self): self.value = copy.deepcopy(state)
            def load(self): return copy.deepcopy(self.value)
            def save(self, value, **kwargs): self.value = copy.deepcopy(value)

        class Webhook:
            def __init__(self, gh): self.gh, self.posts = gh, []
            def verify(self): pass
            def send(self, message):
                self.posts.append(copy.deepcopy(message))
                if len(self.posts) == 1:
                    self.gh.latest_runs = [sync_run(3), sync_run(4)]
                return f"33333333333333334{len(self.posts)}"

        gh = GH(); ledger = Ledger(); webhook = Webhook(gh)
        config = json.dumps({"repository_id": dn.REPO_ID,
                             "sync_workflow_id": dn.SYNC_ID,
                             "ci_workflows": []}).encode()
        env = {"DISCORD_NOTIFICATIONS_ENABLED": "true", "GITHUB_TOKEN": "test-token",
               "DISCORD_WEBHOOK_URL": "test-webhook", "DISCORD_CHANNEL_ID": "test-channel"}
        with (patch.object(Path, "read_bytes", return_value=config),
              patch.object(dn, "collect_objects", return_value=([], [], {})),
              patch.object(dn, "project_backlog", return_value=[]),
              patch.object(dn, "source_events", return_value={}),
              patch.object(dn.observation_clock, "verify_snapshot", return_value=(None, None)),
              patch.object(dt, "artifact_report", side_effect=lambda client, item: report(
                  item["id"], [(19, "A")] if item["id"] == 4 else []))):
            result = dn.main([], env=env, github_factory=lambda token: gh,
                             ledger_factory=lambda client: ledger,
                             discord_factory=lambda token, channel: webhook)

        self.assertEqual(result, 0)
        self.assertEqual(len(webhook.posts), 1)
        self.assertIn("Issue #18", webhook.posts[0]["content"])
        self.assertNotIn("Issue #19", webhook.posts[0]["content"])
        self.assertEqual(ledger.value["outbox"][recovery18]["status"], "delivered")
        self.assertEqual(ledger.value["outbox"][recovery19]["status"], "retired")
        self.assertIn(recovery19, ledger.value["invalidated"])
        self.assertEqual(ledger.value["outbox"][recovery19]["dependencies"], original_dependencies)
        dt.validate_state(ledger.value)

    def test_main_confirmed_hold_survives_pending_latest_then_429_clear_cancels_retry(self):
        def sync_run(n, *, status="completed", conclusion="success"):
            return {**run(n, status=status, conclusion=conclusion),
                    "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                    "event": "schedule"}

        state = dn.new_state()
        for n in (1, 2):
            dn.reconcile_sync(state, sync_run(n), report(
                n, [(18, "A"), (18, "B"), (19, "A")]), MAPPING)
        alert_keys = ("hold:18:A:2", "hold:18:B:2", "hold:19:A:2")

        class GH:
            def __init__(self):
                self.runs = [sync_run(1), sync_run(2),
                             sync_run(3, status="in_progress", conclusion=None)]
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                match = re.fullmatch(r"/actions/runs/([0-9]+)", path)
                if match:
                    run_id = int(match.group(1))
                    return copy.deepcopy(next(run for run in self.runs if run["id"] == run_id))
                raise AssertionError(path)
            def pages(self, path, field):
                assert path == f"/actions/workflows/{dn.SYNC_ID}/runs"
                return copy.deepcopy(self.runs)

        gh = GH()
        # A new active run is not evidence of a clear: the already confirmed
        # two-run alert remains eligible and is not erased by this observation.
        with patch.object(dn, "trusted_sync", return_value=True), \
                patch.object(dt, "artifact_report", side_effect=lambda client, item:
                             report(item["id"], [(18, "A"), (19, "A")] if item["id"] in (1, 2) else [])):
            self.assertTrue(dn.current_sync_notice(gh, alert_keys[0], SHA, state))

        class Ledger:
            def __init__(self): self.value = copy.deepcopy(state)
            def load(self): return copy.deepcopy(self.value)
            def save(self, value, **kwargs): self.value = copy.deepcopy(value)

        webhook_id, webhook_token = "111111111111111111", "a" * 40
        channel_id = dt.CHANNEL_ID
        post_calls = []

        def discord_request(url, *, method="GET", payload=None, **kwargs):
            if method != "POST":
                return 200, {}, json.dumps({"guild_id": "1554806404320075786",
                                            "channel_id": channel_id}).encode()
            post_calls.append(copy.deepcopy(payload))
            # A valid completed clear arrives after the first definitely
            # rejected request but before Discord's permitted 429 retry.
            gh.runs = [sync_run(1), sync_run(2), sync_run(3)]
            raise dt.HTTPError(429, body=b'{"retry_after":0}')

        config = json.dumps({"repository_id": dn.REPO_ID,
                             "sync_workflow_id": dn.SYNC_ID,
                             "ci_workflows": []}).encode()
        env = {"DISCORD_NOTIFICATIONS_ENABLED": "true", "GITHUB_TOKEN": "test-token",
               "DISCORD_WEBHOOK_URL": f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}",
               "DISCORD_CHANNEL_ID": channel_id}
        ledger = Ledger()
        with (patch.object(Path, "read_bytes", return_value=config),
              patch.object(dn, "collect_objects", return_value=([], [], {})),
              patch.object(dn, "project_backlog", return_value=[]),
              patch.object(dn, "source_events", return_value={}),
              patch.object(dn.observation_clock, "verify_snapshot", return_value=(None, None)),
              patch.object(dn, "trusted_sync", return_value=True),
              patch.object(dt, "artifact_report", side_effect=lambda client, item:
                           report(item["id"], [(18, "A"), (18, "B"), (19, "A")]
                                  if item["id"] in (1, 2) else [(18, "C")])),
              patch.object(dt, "request", side_effect=discord_request),
              patch.object(dt.time, "sleep")):
            result = dn.main([], env=env, github_factory=lambda token: gh,
                             ledger_factory=lambda client: ledger)

        self.assertEqual(result, 0)
        self.assertEqual(len(post_calls), 1)
        self.assertIn("Issue #18", post_calls[0]["content"])
        for key in alert_keys:
            self.assertEqual(ledger.value["outbox"][key]["status"], "retired")
            self.assertIn(key, ledger.value["invalidated"])
        dt.validate_state(ledger.value)

    def test_sync_notice_unknown_main_or_run_list_defers_without_losing_ready_alert(self):
        def sync_run(n):
            return {**run(n), "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                    "event": "schedule"}

        state = dn.new_state()
        for n in (1, 2):
            dn.reconcile_sync(state, sync_run(n), report(n, [(18, "A")]), MAPPING)
        key = "hold:18:A:2"
        dependencies = list(state["outbox"][key]["dependencies"])

        class GH:
            def __init__(self):
                self.main_sha = SHA
                self.runs = [sync_run(1), sync_run(2)]
                self.fail_list = False
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": self.main_sha}}
                raise AssertionError(path)
            def pages(self, path, field):
                if self.fail_list: raise dt.Error("Remote request outcome unknown")
                return copy.deepcopy(self.runs)

        class Ledger:
            def save(self, value): pass

        class NoPost:
            calls = 0
            def send(self, message): self.calls += 1

        gh, webhook = GH(), NoPost()
        with patch.object(dn, "trusted_sync", return_value=True), \
                patch.object(dt, "artifact_report", side_effect=lambda client, item:
                             report(item["id"], [(18, "A")])):
            for change in ("main", "list", "identity"):
                if change == "main": gh.main_sha = "b" * 40
                elif change == "list":
                    gh.main_sha = SHA
                    gh.fail_list = True
                else:
                    gh.fail_list = False
                    gh.runs = [sync_run(1), {**sync_run(2), "head_repository": None}]
                self.assertIsNone(dn.current_sync_notice(gh, key, SHA, state))
                self.assertEqual(dn.deliver(
                    state, Ledger(), webhook,
                    sync_check=lambda notice: dn.current_sync_notice(gh, notice, SHA, state)), 0)
                self.assertEqual(state["outbox"][key]["status"], "ready")
                self.assertEqual(state["outbox"][key]["dependencies"], dependencies)
        self.assertEqual(webhook.calls, 0)

    def test_hold_delay_allows_fresh_latest_in_progress_but_defers_if_list_unavailable(self):
        def sync_run(n, *, status="completed", conclusion="success"):
            return {**run(n, status=status, conclusion=conclusion),
                    "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                    "event": "schedule"}

        state = dn.new_state()
        origin = sync_run(1)
        dn.reconcile_sync(state, origin, report(
            1, [(18, "A")], schema=2, observed_at="2026-10-10T12:00:00Z", uncertainty=60), MAPPING)
        dn.reconcile_hold_delays(state, ("2026-10-10T12:17:00Z", 60), MAPPING)
        key = next(key for key in state["outbox"] if key.startswith("hold-delay:"))

        class GH:
            def __init__(self): self.fail = False
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                match = re.fullmatch(r"/actions/runs/([0-9]+)", path)
                if match:
                    return copy.deepcopy({1: origin, 2: sync_run(2, status="in_progress", conclusion=None)}[
                        int(match.group(1))])
                raise AssertionError(path)
            def pages(self, path, field):
                if self.fail: raise dt.Error("Remote request outcome unknown")
                return [origin, sync_run(2, status="in_progress", conclusion=None)]

        gh = GH()
        with patch.object(dn, "trusted_sync", return_value=True), \
                patch.object(dt, "artifact_report", return_value=report(
                    1, [(18, "A")], schema=2, observed_at="2026-10-10T12:00:00Z", uncertainty=60)):
            self.assertTrue(dn.current_sync_notice(gh, key, SHA, state))
            gh.fail = True
            self.assertIsNone(dn.current_sync_notice(gh, key, SHA, state))

    def test_actual_main_429_does_not_revive_hold_after_intermediate_clear_and_recurrence(self):
        def sync_run(n, *, status="completed", conclusion="success"):
            return {**run(n, status=status, conclusion=conclusion),
                    "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                    "event": "schedule"}

        state = dn.new_state()
        for n in (1, 2):
            dn.reconcile_sync(state, sync_run(n), report(n, [(18, "A")]), MAPPING)
        old_key = "hold:18:A:2"
        initial = [sync_run(1), sync_run(2), sync_run(3, status="in_progress", conclusion=None)]
        changed = [sync_run(1), sync_run(2), sync_run(3), sync_run(4)]
        result, final, posts = self._run_main_across_sync_rate_limit(
            state, initial, changed, {1: [(18, "A")], 2: [(18, "A")],
                                    3: [], 4: [(18, "A")]})
        self.assertEqual(result, 0)
        self.assertEqual(len(posts), 1)
        self.assertEqual(final["outbox"][old_key]["status"], "retired")
        self.assertIn(old_key, final["invalidated"])
        self.assertEqual(final["holds"]["18"]["reasons"]["A"]["streak"], 2)
        dt.validate_state(final)

        # Positive control: without an intervening clear, the same confirmed
        # hold remains eligible for the ordinary 429 retry and is delivered.
        state = dn.new_state()
        for n in (1, 2):
            dn.reconcile_sync(state, sync_run(n), report(n, [(18, "A")]), MAPPING)
        result, final, posts = self._run_main_across_sync_rate_limit(
            state, [sync_run(1), sync_run(2)], [sync_run(1), sync_run(2)],
            {1: [(18, "A")], 2: [(18, "A")]})
        self.assertEqual(result, 0)
        self.assertEqual(len(posts), 2)
        self.assertEqual(final["outbox"][old_key]["status"], "delivered")
        dt.validate_state(final)

    def test_actual_main_429_does_not_retry_notice_after_list_detail_conflict(self):
        def sync_run(n, *, status="completed", conclusion="success"):
            return {**run(n, status=status, conclusion=conclusion),
                    "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                    "event": "schedule"}

        state = dn.new_state()
        for n in (1, 2):
            dn.reconcile_sync(state, sync_run(n), report(n, [(18, "A")]), MAPPING)
        key = "hold:18:A:2"
        initial = [sync_run(1), sync_run(2),
                   sync_run(3, status="in_progress", conclusion=None)]
        conflicting_detail = [sync_run(1), sync_run(2), sync_run(3, conclusion="failure")]
        result, final, posts = self._run_main_across_sync_rate_limit(
            state, initial, initial, {1: [(18, "A")], 2: [(18, "A")]},
            retry_details=conflicting_detail)
        self.assertEqual(result, 0)
        self.assertEqual(len(posts), 1)  # The definite 429 is not blindly retried.
        self.assertEqual(final["outbox"][key]["status"], "ready")
        self.assertNotIn(key, final["invalidated"])
        dt.validate_state(final)

    def test_main_unverified_unrelated_counter_resets_hold_confirmation_then_valid_pair_posts(self):
        def sync_run(n, *, event_name="schedule", branch="main", conclusion="success"):
            return {**run(n, conclusion=conclusion), "workflow_id": dn.SYNC_ID,
                    "path": dn.SYNC_PATH, "event": event_name, "head_branch": branch}

        state = dn.new_state()
        first = sync_run(3)
        dn.reconcile_sync(state, first, report(3, [(18, "A")]), MAPPING)
        misleading = sync_run(4, event_name="push", branch="feature")
        conflicting_detail = sync_run(4, conclusion="failure")
        fifth = sync_run(5)
        result, after_gap, posts = self._run_main_across_sync_rate_limit(
            state, [first, misleading, fifth], [first, misleading, fifth],
            {3: [(18, "A")], 5: [(18, "A")]},
            initial_details=[first, conflicting_detail, fifth])
        self.assertEqual(result, 0)
        self.assertEqual(posts, [])
        self.assertEqual(after_gap["sync_cursor"], [5, 5, 1])
        self.assertEqual(after_gap["holds"]["18"]["reasons"]["A"]["streak"], 1)
        self.assertIsNone(after_gap["holds"]["18"]["reasons"]["A"]["alert"])
        self.assertEqual(after_gap["holds"]["18"]["reasons"]["A"]["episode"][
            "first_observed_run_id"], 5)

        sixth = sync_run(6)
        result, confirmed, posts = self._run_main_across_sync_rate_limit(
            after_gap, [fifth, sixth], [fifth, sixth],
            {5: [(18, "A")], 6: [(18, "A")]})
        self.assertEqual(result, 0)
        self.assertEqual(len(posts), 2)  # One definite 429, then the validated retry.
        self.assertEqual(confirmed["outbox"]["hold:18:A:6"]["status"], "delivered")

    def test_main_sync_failure_list_detail_conflict_preserves_cursor_then_next_poll_records_match(self):
        def sync_run(n, *, conclusion="success"):
            return {**run(n, conclusion=conclusion), "workflow_id": dn.SYNC_ID,
                    "path": dn.SYNC_PATH, "event": "schedule"}

        state = dn.new_state()
        third = sync_run(3)
        dn.reconcile_sync(state, third, report(3), MAPPING)
        failed = sync_run(4, conclusion="failure")
        conflicting_detail = sync_run(4, conclusion="success")
        result, unchanged, posts = self._run_main_across_sync_rate_limit(
            state, [failed], [failed], {}, initial_details=[conflicting_detail])
        self.assertEqual(result, 0)
        self.assertEqual(posts, [])
        self.assertEqual(unchanged["sync_cursor"], [3, 3, 1])
        self.assertNotIn("sync", unchanged["incidents"])

        result, retried, posts = self._run_main_across_sync_rate_limit(
            unchanged, [failed], [failed], {}, initial_details=[failed])
        self.assertEqual(result, 0)
        self.assertEqual(len(posts), 2)  # One definite 429, then the validated retry.
        self.assertEqual(retried["sync_cursor"], [4, 4, 1])
        self.assertIn("sync", retried["incidents"])
        self.assertEqual(retried["outbox"]["failure:sync:4:1"]["status"], "delivered")

    def test_actual_main_429_does_not_revive_sync_recovery_after_intermediate_failure(self):
        def sync_run(n, *, conclusion="success"):
            return {**run(n, conclusion=conclusion), "workflow_id": dn.SYNC_ID,
                    "path": dn.SYNC_PATH, "event": "schedule"}

        def recovered_state():
            state = dn.new_state()
            failure = sync_run(1, conclusion="failure")
            dn.reconcile_sync(state, failure, None, MAPPING)
            failure_key = "failure:sync:1:1"
            state["outbox"][failure_key].update(
                status="delivered", message_id="333333333333333333")
            dn.reconcile_sync(state, sync_run(2), report(2), MAPPING)
            recovery_key = "recovery:failure:sync:1:1:at:2:1"
            return state, failure_key, recovery_key

        state, failure_key, recovery_key = recovered_state()
        runs = [sync_run(1, conclusion="failure"), sync_run(2)]
        recurrence = runs + [sync_run(3, conclusion="failure"), sync_run(4)]
        result, final, posts = self._run_main_across_sync_rate_limit(
            state, runs, recurrence, {2: [], 4: []})
        self.assertEqual(result, 0)
        self.assertEqual(len(posts), 1)
        self.assertEqual(final["outbox"][recovery_key]["status"], "retired")
        self.assertIn(recovery_key, final["invalidated"])
        self.assertEqual(final["outbox"][recovery_key]["dependencies"], [failure_key])
        self.assertEqual(final["outbox"][failure_key]["status"], "delivered")
        dt.validate_state(final)

        # Positive control: with no later failure, a valid recovery still uses
        # the normal single 429 retry and is delivered.
        state, failure_key, recovery_key = recovered_state()
        result, final, posts = self._run_main_across_sync_rate_limit(
            state, runs, runs, {2: []})
        self.assertEqual(result, 0)
        self.assertEqual(len(posts), 2)
        self.assertEqual(final["outbox"][recovery_key]["status"], "delivered")
        self.assertEqual(final["outbox"][recovery_key]["dependencies"], [failure_key])
        dt.validate_state(final)

    def test_main_change_during_sync_observation_discards_only_sync_candidate_then_next_poll_consumes_run(self):
        def sync_run(n, head_sha):
            return {**run(n), "head_sha": head_sha, "workflow_id": dn.SYNC_ID,
                    "path": dn.SYNC_PATH, "event": "schedule"}

        state = dn.new_state()
        dn.reconcile_sync(state, sync_run(1, SHA), report(1, [(18, "A")]), MAPPING)
        saved_initial = copy.deepcopy(state)

        class GH:
            def __init__(self): self.main_sha = SHA; self.switch_on_sync_list = True
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": self.main_sha}}
                if path.startswith("/compare/"):
                    return {"status": "ahead", "base_commit": {"sha": SHA}}
                match = re.fullmatch(r"/actions/runs/([0-9]+)", path)
                if match:
                    rid = int(match.group(1))
                    return sync_run(rid, SHA if rid == 1 else self.main_sha)
                raise AssertionError(path)
            def pages(self, path, key):
                assert path == f"/actions/workflows/{dn.SYNC_ID}/runs"
                if self.switch_on_sync_list:
                    self.switch_on_sync_list = False
                    self.main_sha = "b" * 40
                return [sync_run(1, SHA), sync_run(2, self.main_sha)]

        class Ledger:
            def __init__(self): self.value = copy.deepcopy(saved_initial); self.saves = 0
            def load(self): return copy.deepcopy(self.value)
            def save(self, value, **kwargs): self.value = copy.deepcopy(value); self.saves += 1

        class Webhook:
            def __init__(self): self.posts = []
            def verify(self): pass
            def send(self, message):
                self.posts.append(copy.deepcopy(message))
                return f"33333333333333333{len(self.posts)}"

        config = json.dumps({"repository_id": dn.REPO_ID,
                             "sync_workflow_id": dn.SYNC_ID,
                             "ci_workflows": []}).encode()
        env = {"DISCORD_NOTIFICATIONS_ENABLED": "true", "GITHUB_TOKEN": "test-token",
               "DISCORD_WEBHOOK_URL": "test-webhook", "DISCORD_CHANNEL_ID": "test-channel"}
        gh, ledger, webhook = GH(), Ledger(), Webhook()
        patches = (patch.object(Path, "read_bytes", return_value=config),
                   patch.object(dn, "collect_objects", return_value=([], [], {})),
                   patch.object(dn, "project_backlog", return_value=[]),
                   patch.object(dn, "source_events", return_value={}),
                   patch.object(dn.observation_clock, "verify_snapshot", return_value=(None, None)),
                   patch.object(dt, "artifact_report", side_effect=lambda client, item: report(
                       item["id"], [(18, "A")])) )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            first_result = dn.main([], env=env, github_factory=lambda token: gh,
                                   ledger_factory=lambda client: ledger,
                                   discord_factory=lambda token, channel: webhook)
            self.assertEqual(first_result, 0)
            self.assertEqual(ledger.value["sync_cursor"], [1, 1, 1])
            self.assertEqual(ledger.value["holds"]["18"]["reasons"]["A"]["streak"], 1)
            self.assertEqual(webhook.posts, [])

            second_result = dn.main([], env=env, github_factory=lambda token: gh,
                                    ledger_factory=lambda client: ledger,
                                    discord_factory=lambda token, channel: webhook)
        self.assertEqual(second_result, 0)
        self.assertEqual(ledger.value["sync_cursor"], [2, 2, 1])
        self.assertEqual(ledger.value["holds"]["18"]["reasons"]["A"]["streak"], 2)
        self.assertEqual(ledger.value["holds"]["18"]["reasons"]["A"]["alert"], "hold:18:A:2")
        self.assertEqual(len(webhook.posts), 1)
        self.assertIn("Issue #18", webhook.posts[0]["content"])

    def test_main_sync_candidate_is_discarded_on_artifact_race_or_postcheck_error(self):
        def sync_run(n, head_sha):
            return {**run(n), "head_sha": head_sha, "workflow_id": dn.SYNC_ID,
                    "path": dn.SYNC_PATH, "event": "schedule"}

        config = json.dumps({"repository_id": dn.REPO_ID,
                             "sync_workflow_id": dn.SYNC_ID,
                             "ci_workflows": []}).encode()
        env = {"DISCORD_NOTIFICATIONS_ENABLED": "true", "GITHUB_TOKEN": "test-token",
               "DISCORD_WEBHOOK_URL": "test-webhook", "DISCORD_CHANNEL_ID": "test-channel"}

        for race_mode in ("artifact", "postcheck-error"):
            with self.subTest(race_mode=race_mode):
                state = dn.new_state()
                dn.reconcile_sync(state, sync_run(1, SHA), report(1, [(18, "A")]), MAPPING)
                class GH:
                    def __init__(self): self.main_sha = SHA; self.ref_reads = 0; self.fail_post = True
                    def repo(self, path):
                        if path.startswith("/compare/"):
                            base_sha = path.removeprefix("/compare/").split("...", 1)[0]
                            return {"status": "ahead", "base_commit": {"sha": base_sha}}
                        if path != "/git/ref/heads/main": raise AssertionError(path)
                        self.ref_reads += 1
                        if race_mode == "postcheck-error" and self.fail_post and self.ref_reads == 4:
                            raise dt.Error("main freshness probe failed")
                        return {"object": {"sha": self.main_sha}}
                    def pages(self, path, key):
                        return [sync_run(1, SHA), sync_run(2, self.main_sha)]
                class Ledger:
                    def __init__(self): self.value = copy.deepcopy(state)
                    def load(self): return copy.deepcopy(self.value)
                    def save(self, value, **kwargs): self.value = copy.deepcopy(value)
                class Webhook:
                    def __init__(self): self.posts = []
                    def verify(self): pass
                    def send(self, message):
                        self.posts.append(copy.deepcopy(message))
                        return f"33333333333333333{len(self.posts)}"
                gh, ledger, webhook = GH(), Ledger(), Webhook()
                artifact_switch = {"done": False}
                def artifact_report(client, item):
                    if race_mode == "artifact" and not artifact_switch["done"]:
                        artifact_switch["done"] = True
                        gh.main_sha = "b" * 40
                    return report(item["id"], [(18, "A")])
                def invoke():
                    with (patch.object(Path, "read_bytes", return_value=config),
                          patch.object(dn, "collect_objects", return_value=([], [], {})),
                          patch.object(dn, "project_backlog", return_value=[]),
                          patch.object(dn, "source_events", return_value={}),
                          patch.object(dn.observation_clock, "verify_snapshot", return_value=(None, None)),
                          patch.object(dt, "artifact_report", side_effect=artifact_report)):
                        return dn.main([], env=env, github_factory=lambda token: gh,
                                       ledger_factory=lambda client: ledger,
                                       discord_factory=lambda token, channel: webhook)

                self.assertEqual(invoke(), 0)
                self.assertEqual(ledger.value["sync_cursor"], [1, 1, 1])
                self.assertEqual(ledger.value["holds"]["18"]["reasons"]["A"]["streak"], 1)
                self.assertEqual(webhook.posts, [])
                gh.main_sha = "b" * 40
                gh.fail_post = False
                gh.ref_reads = 0
                self.assertEqual(invoke(), 0)
                self.assertEqual(ledger.value["sync_cursor"], [2, 2, 1])
                self.assertEqual(ledger.value["holds"]["18"]["reasons"]["A"]["streak"], 2)
                self.assertEqual(ledger.value["holds"]["18"]["reasons"]["A"]["alert"], "hold:18:A:2")
                self.assertEqual(len(webhook.posts), 1)

    def test_single_recovery_fails_closed_on_main_change_api_error_pending_or_recurrence(self):
        def ready_state():
            value = dn.new_state()
            dn.reconcile_sync(value, run(1), report(1, [(18, "A")]), MAPPING)
            dn.reconcile_sync(value, run(2), report(2, [(18, "A")]), MAPPING)
            value["outbox"]["hold:18:A:2"].update(
                status="delivered", message_id="333333333333333333")
            dn.reconcile_sync(value, run(3), report(3), MAPPING)
            return value, "hold-recovery:18:3:1"

        def sync_run(n, *, head_sha=SHA, status="completed", conclusion="success"):
            return {**run(n, status=status, conclusion=conclusion),
                    "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                    "event": "schedule", "head_sha": head_sha}

        class GH:
            def __init__(self, main_sha=SHA, latest=None, fail_ref=False, main_reads=None):
                self.main_sha, self.latest, self.fail_ref = main_sha, latest, fail_ref
                self.main_reads = list(main_reads or [])
            def repo(self, path):
                if path != "/git/ref/heads/main": raise AssertionError(path)
                if self.fail_ref: raise dt.Error("fresh main read failed")
                value = self.main_reads.pop(0) if self.main_reads else self.main_sha
                return {"object": {"sha": value}}
            def pages(self, path, key): return [sync_run(3), copy.deepcopy(self.latest)]

        class Ledger:
            def save(self, value): pass

        class Webhook:
            def __init__(self): self.calls = 0
            def send(self, message):
                self.calls += 1
                return "333333333333333334"

        scenarios = (
            ("main advanced", GH(main_sha="b" * 40, latest=sync_run(4, head_sha="b" * 40)), None),
            ("main advanced during latest read", GH(latest=sync_run(4),
                                                       main_reads=[SHA, "b" * 40]), None),
            ("fresh main API failure", GH(latest=sync_run(4), fail_ref=True), None),
            ("new run pending", GH(latest=sync_run(4, status="in_progress", conclusion=None)), None),
            ("latest completed recurrence", GH(latest=sync_run(4)), False),
        )
        for label, gh, expected in scenarios:
            with self.subTest(scenario=label):
                state, key = ready_state()
                dependencies = list(state["outbox"][key]["dependencies"])
                webhook = Webhook()
                artifact = (patch.object(dt, "artifact_report", return_value=report(4, [(18, "A")]))
                            if label == "latest completed recurrence" else
                            patch.object(dt, "artifact_report", side_effect=AssertionError(
                                "Unconfirmed sync should not read an artifact")))
                with artifact:
                    verdict = dn.current_hold_recovery(gh, key, SHA)
                    self.assertIs(verdict, expected)
                    dn.deliver(state, Ledger(), webhook,
                               hold_recovery_check=lambda notice: dn.current_hold_recovery(gh, notice, SHA))
                self.assertEqual(webhook.calls, 0)
                self.assertEqual(state["outbox"][key]["dependencies"], dependencies)
                if expected is False:
                    self.assertEqual(state["outbox"][key]["status"], "retired")
                    self.assertIn(key, state["invalidated"])
                else:
                    self.assertEqual(state["outbox"][key]["status"], "ready")

    def test_hold_recovery_checks_origin_to_latest_and_cancels_intermediate_recurrence(self):
        def ready_state():
            state = dn.new_state()
            for n in (1, 2):
                dn.reconcile_sync(state, sync_run(n), report(n, [(18, "A")]), MAPPING)
            state["outbox"]["hold:18:A:2"].update(status="delivered", message_id="333333333333333333")
            dn.reconcile_sync(state, sync_run(3), report(3), MAPPING)
            return state, "hold-recovery:18:3:1"

        def sync_run(n, *, conclusion="success", status="completed"):
            return {**run(n, conclusion=conclusion, status=status),
                    "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH, "event": "schedule"}

        origin, recurrence, final_clear = sync_run(3), sync_run(4), sync_run(5)
        runs = [origin, recurrence, final_clear]
        reports = {3: report(3), 4: report(4, [(18, "A")]), 5: report(5)}
        class GH:
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                raise AssertionError(path)
            def pages(self, path, key): return copy.deepcopy(runs)
        class Ledger:
            def save(self, value): pass
        class Webhook:
            def __init__(self): self.calls = 0
            def send(self, message): self.calls += 1; return "333333333333333334"

        gh = GH()
        state, key = ready_state()
        dependencies = list(state["outbox"][key]["dependencies"])
        reconciled = copy.deepcopy(state)
        with patch.object(dt, "artifact_report", side_effect=lambda client, item: reports[item["id"]]):
            self.assertIs(dn.current_hold_recovery(gh, key, SHA), False)
            webhook = Webhook()
            self.assertEqual(dn.deliver(state, Ledger(), webhook,
                                        hold_recovery_check=lambda notice: dn.current_hold_recovery(gh, notice, SHA)), 0)
            dn.reconcile_sync_runs(reconciled, gh, runs, SHA, MAPPING)
            reconciliation_webhook = Webhook()
            self.assertEqual(dn.deliver(reconciled, Ledger(), reconciliation_webhook), 0)
        self.assertEqual(webhook.calls, 0)
        self.assertEqual(reconciliation_webhook.calls, 0)
        self.assertEqual(state["outbox"][key]["status"], "retired")
        self.assertEqual(reconciled["outbox"][key]["status"], "retired")
        self.assertEqual(state["outbox"][key]["dependencies"], dependencies)
        self.assertEqual(reconciled["outbox"][key]["dependencies"], dependencies)

    def test_hold_recovery_defers_on_intermediate_gap_and_keeps_pending_fence(self):
        def ready_state():
            state = dn.new_state()
            dn.reconcile_sync(state, sync_run(1), report(1, [(18, "A")]), MAPPING)
            dn.reconcile_sync(state, sync_run(2), report(2, [(18, "A")]), MAPPING)
            state["outbox"]["hold:18:A:2"].update(status="delivered", message_id="333333333333333333")
            dn.reconcile_sync(state, sync_run(3), report(3), MAPPING)
            return state, "hold-recovery:18:3:1"

        def sync_run(n, *, conclusion="success", status="completed"):
            return {**run(n, conclusion=conclusion, status=status),
                    "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH, "event": "schedule"}

        class GH:
            def __init__(self, runs): self.runs = runs
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                match = re.fullmatch(r"/actions/runs/([0-9]+)", path)
                if match:
                    run_id = int(match.group(1))
                    return copy.deepcopy(next(run for run in self.runs if run["id"] == run_id))
                raise AssertionError(path)
            def pages(self, path, key): return copy.deepcopy(self.runs)
        class Ledger:
            def save(self, value): pass
        class Webhook:
            calls = 0
            def send(self, message): self.calls += 1; return "333333333333333334"

        origin, failed, final_clear = sync_run(3), sync_run(4, conclusion="failure"), sync_run(5)
        incomplete = sync_run(4)
        missing_status = sync_run(4); missing_status.pop("status")
        latest_missing_status = sync_run(5); latest_missing_status.pop("status")
        for field in ("workflow_id", "path", "repository", "head_repository"):
            incomplete.pop(field, None)
        scenarios = (
            ([origin, failed, final_clear], None),
            ([origin, sync_run(4, status="in_progress"), final_clear], None),
            ([origin, incomplete, final_clear], None),
            ([origin, missing_status, final_clear], None),
            ([origin, sync_run(4), latest_missing_status], None),
        )
        for runs, expected in scenarios:
            with self.subTest(middle=runs[1].get("conclusion"), status=runs[1].get("status")):
                state, key = ready_state()
                dependencies = list(state["outbox"][key]["dependencies"])
                gh = GH(runs)
                webhook = Webhook()
                with patch.object(dt, "artifact_report", return_value=report(5)):
                    self.assertIs(dn.current_hold_recovery(gh, key, SHA), expected)
                    self.assertEqual(dn.deliver(state, Ledger(), webhook,
                                                hold_recovery_check=lambda notice: dn.current_hold_recovery(gh, notice, SHA)), 0)
                self.assertEqual(webhook.calls, 0)
                self.assertEqual(state["outbox"][key]["status"], "ready")
                self.assertEqual(state["outbox"][key]["dependencies"], dependencies)

        pending, key = ready_state()
        pending["outbox"][key]["status"] = "pending"
        dependencies = list(pending["outbox"][key]["dependencies"])
        webhook = Webhook()
        self.assertEqual(dn.deliver(pending, Ledger(), webhook,
                                    hold_recovery_check=lambda notice: self.fail("pending fence must not be rechecked")), 1)
        self.assertEqual(webhook.calls, 0)
        self.assertEqual(pending["outbox"][key]["status"], "pending")
        self.assertEqual(pending["outbox"][key]["dependencies"], dependencies)

    def test_recovery_requires_counter_coverage_and_current_attempt_evidence(self):
        def ready_state():
            state = dn.new_state()
            for n in (1, 2):
                dn.reconcile_sync(state, sync_run(n), report(n, [(18, "A")]), MAPPING)
            state["outbox"]["hold:18:A:2"].update(
                status="delivered", message_id="333333333333333333")
            dn.reconcile_sync(state, sync_run(3), report(3), MAPPING)
            return state, "hold-recovery:18:3:1"

        def sync_run(n, *, attempt=1, event="schedule", branch="main", workflow_id=None,
                     repository_id=None, head_repository_id=None):
            value = {**run(n, attempt=attempt), "workflow_id": dn.SYNC_ID,
                     "path": dn.SYNC_PATH, "event": event, "head_branch": branch}
            if workflow_id is not None:
                value["workflow_id"] = workflow_id
            if repository_id is not None:
                value["repository"]["id"] = repository_id
            if head_repository_id is not None:
                value["head_repository"]["id"] = head_repository_id
            return value

        class GH:
            def __init__(self, runs): self.runs = runs
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                match = re.fullmatch(r"/actions/runs/([0-9]+)", path)
                if match:
                    run_id = int(match.group(1))
                    return copy.deepcopy(next(run for run in self.runs if run["id"] == run_id))
                raise AssertionError(path)
            def pages(self, path, key): return copy.deepcopy(self.runs)

        class Ledger:
            def save(self, value): pass

        class Webhook:
            def __init__(self): self.calls = 0
            def send(self, message):
                self.calls += 1
                return "333333333333333334"

        origin, middle, latest = sync_run(3), sync_run(4), sync_run(5)
        unrelated_branch = sync_run(4, event="push", branch="feature")
        attempt_two = sync_run(4, attempt=2)
        foreign_workflow = sync_run(4, workflow_id=dn.SYNC_ID + 1)
        foreign_repository = sync_run(4, repository_id=dt.REPO_ID + 1)
        foreign_head_repository = sync_run(4, head_repository_id=dt.REPO_ID + 1)
        conflicting_id = {**middle, "id": 404}
        conflicting_attempt = {**middle, "status": "in_progress", "conclusion": None}

        scenarios = (
            ([origin, latest], None, {3: report(3), 5: report(5)}),
            ([origin, attempt_two, latest], None, {3: report(3), 4: report(4, attempt=2), 5: report(5)}),
            ([origin, foreign_workflow, latest], None, {3: report(3), 5: report(5)}),
            ([origin, foreign_repository, latest], None, {3: report(3), 5: report(5)}),
            ([origin, foreign_head_repository, latest], None,
             {3: report(3), 5: report(5)}),
            ([origin, middle, conflicting_id, latest], None, {}),
            ([origin, middle, conflicting_attempt, latest], None, {}),
            ([origin, middle, latest], True, {3: report(3), 4: report(4), 5: report(5)}),
            ([origin, unrelated_branch, latest], True, {3: report(3), 5: report(5)}),
            # Exact duplicate list rows are benign and are consumed once.
            ([origin, middle, copy.deepcopy(middle), latest], True,
             {3: report(3), 4: report(4), 5: report(5)}),
        )
        for runs, expected, reports in scenarios:
            with self.subTest(run_numbers=[r["run_number"] for r in runs], expected=expected):
                state, key = ready_state()
                dependencies = list(state["outbox"][key]["dependencies"])
                self.assertTrue(dn.notice_active(state, key))
                gh, webhook = GH(runs), Webhook()
                consumed = []

                def artifact(client, item):
                    consumed.append((item["id"], item["run_attempt"]))
                    return reports[item["id"]]

                with patch.object(dt, "artifact_report", side_effect=artifact):
                    verdict = dn.current_hold_recovery(gh, key, SHA)
                    sent = dn.deliver(
                        state, Ledger(), webhook,
                        hold_recovery_check=lambda notice: dn.current_hold_recovery(gh, notice, SHA))

                self.assertIs(verdict, expected)
                self.assertEqual(webhook.calls, 1 if expected is True else 0)
                self.assertEqual(sent, 0)
                self.assertEqual(consumed, (([(3, 1), (4, 1), (5, 1)]
                                             if runs[1] is middle else [(3, 1), (5, 1)]) * 2
                                            if expected is True else []))
                if expected is not True:
                    self.assertEqual(state["outbox"][key]["status"], "ready")
                    self.assertEqual(state["outbox"][key]["dependencies"], dependencies)

    def test_main_related_metadata_gap_blocks_single_ready_recovery_post(self):
        state = dn.new_state()
        dn.reconcile_sync(state, run(1), report(1, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, run(2), report(2, [(18, "A")]), MAPPING)
        state["outbox"]["hold:18:A:2"].update(
            status="delivered", message_id="333333333333333333")
        dn.reconcile_sync(state, run(3), report(3), MAPPING)
        recovery = "hold-recovery:18:3:1"
        dependencies = list(state["outbox"][recovery]["dependencies"])
        malformed = {**run(4, conclusion="failure"),
                     "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                     "event": "schedule", "head_sha": "short"}

        class GH:
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                raise AssertionError(path)
            def pages(self, path, key): return [malformed]

        class Ledger:
            def __init__(self): self.value = copy.deepcopy(state)
            def load(self): return copy.deepcopy(self.value)
            def save(self, value, **kwargs): self.value = copy.deepcopy(value)

        class Webhook:
            calls = 0
            def verify(self): pass
            def send(self, message):
                self.calls += 1
                return "333333333333333334"

        gh = GH(); ledger = Ledger(); webhook = Webhook()
        config = json.dumps({"repository_id": dn.REPO_ID,
                             "sync_workflow_id": dn.SYNC_ID,
                             "ci_workflows": []}).encode()
        env = {"DISCORD_NOTIFICATIONS_ENABLED": "true", "GITHUB_TOKEN": "test-token",
               "DISCORD_WEBHOOK_URL": "test-webhook", "DISCORD_CHANNEL_ID": "test-channel"}
        with (patch.object(Path, "read_bytes", return_value=config),
              patch.object(dn, "collect_objects", return_value=([], [], {})),
              patch.object(dn, "project_backlog", return_value=[]),
              patch.object(dn, "source_events", return_value={}),
              patch.object(dn.observation_clock, "verify_snapshot", return_value=(None, None)),
              patch.object(dt, "artifact_report", side_effect=AssertionError(
                  "Malformed related run reports are never consumed"))):
            result = dn.main([], env=env, github_factory=lambda token: gh,
                             ledger_factory=lambda client: ledger,
                             discord_factory=lambda token, channel: webhook)

        self.assertEqual(result, 0)
        self.assertEqual(webhook.calls, 0)
        self.assertEqual(ledger.value["sync_cursor"], [3, 3, 1])
        self.assertEqual(ledger.value["outbox"][recovery]["status"], "ready")
        self.assertEqual(ledger.value["outbox"][recovery]["dependencies"], dependencies)
        dt.validate_state(ledger.value)

    def test_main_consumes_malformed_status_as_gap_and_later_polls_reach_hold_alert(self):
        def sync_run(n):
            return {**run(n), "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                    "event": "schedule"}

        state = dn.new_state()
        dn.reconcile_sync(state, sync_run(1), report(1, [(18, "A")]), MAPPING)

        class GH:
            def __init__(self): self.runs = []
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                raise AssertionError(path)
            def pages(self, path, key): return copy.deepcopy(self.runs)

        class Ledger:
            def __init__(self): self.value = copy.deepcopy(state)
            def load(self): return copy.deepcopy(self.value)
            def save(self, value, **kwargs): self.value = copy.deepcopy(value)

        class Webhook:
            def __init__(self): self.posts = []
            def verify(self): pass
            def send(self, message):
                self.posts.append(copy.deepcopy(message))
                return f"3333333333333333{len(self.posts):02d}"

        gh, ledger, webhook = GH(), Ledger(), Webhook()
        config = json.dumps({"repository_id": dn.REPO_ID,
                             "sync_workflow_id": dn.SYNC_ID,
                             "ci_workflows": []}).encode()
        env = {"DISCORD_NOTIFICATIONS_ENABLED": "true", "GITHUB_TOKEN": "test-token",
               "DISCORD_WEBHOOK_URL": "test-webhook", "DISCORD_CHANNEL_ID": "test-channel"}
        malformed = sync_run(2); malformed.pop("status")
        gh.runs = [malformed, sync_run(3)]
        with (patch.object(Path, "read_bytes", return_value=config),
              patch.object(dn, "collect_objects", return_value=([], [], {})),
              patch.object(dn, "project_backlog", return_value=set()),
              patch.object(dn, "source_events", return_value={}),
              patch.object(dn.observation_clock, "verify_snapshot", return_value=(None, None)),
              patch.object(dt, "artifact_report", side_effect=lambda client, item:
                           report(item["id"], [(18, "A")]))):
            self.assertEqual(dn.main([], env=env, github_factory=lambda token: gh,
                                     ledger_factory=lambda client: ledger,
                                     discord_factory=lambda token, channel: webhook), 0)
            self.assertEqual(ledger.value["sync_cursor"], [3, 3, 1])
            self.assertEqual(ledger.value["holds"]["18"]["reasons"]["A"]["streak"], 1)
            self.assertFalse(any(key.startswith("hold:18:A:") for key in ledger.value["outbox"]))

            gh.runs = [sync_run(3), sync_run(4)]
            self.assertEqual(dn.main([], env=env, github_factory=lambda token: gh,
                                     ledger_factory=lambda client: ledger,
                                     discord_factory=lambda token, channel: webhook), 0)

        self.assertEqual(ledger.value["sync_cursor"], [4, 4, 1])
        self.assertEqual(ledger.value["holds"]["18"]["reasons"]["A"]["streak"], 2)
        self.assertEqual(ledger.value["outbox"]["hold:18:A:4"]["status"], "delivered")
        self.assertEqual(len(webhook.posts), 1)

    def test_missing_or_uncertain_consumer_clock_never_queues_delay(self):
        state = dn.new_state()
        dn.reconcile_sync(state, run(1), report(
            1, [(18, "A")], schema=2, observed_at="2026-10-10T12:00:00Z", uncertainty=60), MAPPING)
        dn.reconcile_hold_delays(state, (None, None), MAPPING)
        dn.reconcile_hold_delays(state, ("2026-10-10T12:30:00Z", 61), MAPPING)
        self.assertFalse(any(key.startswith("hold-delay:") for key in state["outbox"]))

    def test_removed_reason_ends_episode_even_when_another_reason_remains(self):
        state = dn.new_state()
        for n in (1, 2):
            dn.reconcile_sync(state, run(n), report(n, [(18, "A")]), MAPPING)
        old_alert = state["holds"]["18"]["reasons"]["A"]["alert"]
        old_episode = state["holds"]["18"]["reasons"]["A"]["episode"]["episode_id"]
        dn.reconcile_sync(state, run(3), report(3, [(18, "B")]), MAPPING)
        old_reason = state["holds"]["18"]["reasons"]["A"]
        self.assertEqual(old_reason["streak"], 0)
        self.assertIsNone(old_reason["alert"])
        self.assertIsNone(old_reason["episode"])
        self.assertIn(old_alert, state["invalidated"])

        dn.reconcile_sync(state, run(4), report(4, [(18, "A")]), MAPPING)
        new_reason = state["holds"]["18"]["reasons"]["A"]
        self.assertEqual(new_reason["streak"], 1)
        self.assertNotEqual(new_reason["episode"]["episode_id"], old_episode)
        self.assertIsNone(new_reason["alert"])
        dn.reconcile_sync(state, run(5), report(5, [(18, "A")]), MAPPING)
        self.assertEqual(new_reason["alert"], "hold:18:A:5")

    def test_removed_reason_retires_old_delay_episode_while_other_reason_remains(self):
        state = dn.new_state()
        dn.reconcile_sync(state, run(1), report(
            1, [(18, "A")], schema=2, observed_at="2026-10-10T12:00:00Z", uncertainty=1), MAPPING)
        dn.reconcile_hold_delays(state, ("2026-10-10T12:17:00Z", 1), MAPPING)
        old_delay = "hold-delay:18:A:18:A:1:1"
        self.assertIn(old_delay, state["outbox"])

        dn.reconcile_sync(state, run(2), report(
            2, [(18, "B")], schema=2, observed_at="2026-10-10T12:18:00Z", uncertainty=1), MAPPING)
        self.assertIn(old_delay, state["invalidated"])
        reason_a = state["holds"]["18"]["reasons"]["A"]
        self.assertEqual(reason_a, {"streak": 0, "alert": None, "episode": None})
        dn.reconcile_hold_delays(state, ("2026-10-10T13:00:00Z", 1), MAPPING)
        self.assertEqual(sum(key.startswith("hold-delay:18:A:") for key in state["outbox"]), 1)

    def test_v1_ledger_upgrade_preserves_delivery_and_cursor(self):
        state = dn.new_state()
        dn.queue(state, "old-alert", dn.payload("Old", "Existing", dn.object_url(18), MAPPING))
        state["outbox"]["old-alert"]["status"] = "pending"
        state["holds"]["18"] = {"reasons": {"A": {"streak": 1, "alert": "old-alert"}}}
        state["schema"] = 1; state["sync_cursor"] = [20, 2, 1]
        dt.validate_state(state)
        old_outbox = copy.deepcopy(state["outbox"]); old_cursor = list(state["sync_cursor"])
        dn.upgrade_state(state)
        self.assertEqual(state["outbox"], old_outbox)
        self.assertEqual(state["sync_cursor"], old_cursor)
        self.assertIsNone(state["holds"]["18"]["reasons"]["A"]["episode"]["timer_origin_at"])
        dt.validate_state(state)

    def test_failure_gap_and_reason_change_not_clear(self):
        state = dn.new_state()
        dn.reconcile_sync(state, run(1), report(1, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, run(2, conclusion="failure"), None, MAPPING)
        dn.reconcile_sync(state, run(3), report(3, [(18, "A")]), MAPPING)
        self.assertIsNone(state["holds"]["18"]["reasons"]["A"]["alert"])
        dn.reconcile_sync(state, run(4), report(4, [(18, "A")]), MAPPING)
        alert = state["holds"]["18"]["reasons"]["A"]["alert"]
        dn.reconcile_sync(state, run(5), report(5, [(18, "B")]), MAPPING)
        dn.reconcile_sync(state, run(6), report(6, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, run(7), report(7, [(18, "A")]), MAPPING)
        self.assertNotEqual(state["holds"]["18"]["reasons"]["A"]["alert"], alert)
        self.assertEqual(state["holds"]["18"]["reasons"]["A"]["alert"], "hold:18:A:7")
        self.assertIn(alert, state["invalidated"])
        self.assertFalse(any(k.startswith("hold-recovery") for k in state["outbox"]))

    def test_old_run_high_attempt_cannot_recover_and_dry_cannot_clear(self):
        state = dn.new_state()
        dn.reconcile_sync(state, run(3), report(3, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, run(2, attempt=99), report(2, attempt=99), MAPPING)
        self.assertIn("18", state["holds"])
        dry = report(4); dry.update(dry_run=True, scan_complete=False, kind="skipped")
        dn.reconcile_sync(state, run(4), dry, MAPPING)
        self.assertIn("18", state["holds"])


class Delivery(unittest.TestCase):
    class Ledger:
        def __init__(self): self.states = []
        def save(self, state): self.states.append(copy.deepcopy(state))

    def message(self): return dn.payload("title", "body", dn.object_url(18), MAPPING)

    class NoPost:
        def send(self, msg): raise AssertionError("Obsolete notification must not POST")

    def test_cleared_hold_pending_then_not_sent_without_new_sync_retires(self):
        state = dn.new_state()
        dn.reconcile_sync(state, run(1), report(1, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, run(2), report(2, [(18, "A")]), MAPPING)
        key = next(iter(state["outbox"])); state["outbox"][key]["status"] = "pending"
        dn.reconcile_sync(state, run(3), report(3), MAPPING)
        self.assertEqual(state["outbox"][key]["status"], "pending")
        dt.validate_state(state)
        dn.resolve(state, key, "not_sent", None, None)
        dn.deliver(state, self.Ledger(), self.NoPost())
        self.assertEqual(state["outbox"][key]["status"], "retired")
        self.assertEqual(state["outbox"]["hold-recovery:18:3:1"]["status"], "ready")

    def test_recurrence_invalidates_pending_old_ci_recovery_durably(self):
        state = dn.new_state()
        dn.reconcile_ci(state, [run(1, conclusion="failure")], [], [WORKFLOW], SHA, MAPPING)
        first = next(iter(state["outbox"])); state["outbox"][first].update(status="delivered", message_id="333333333333333333")
        dn.reconcile_ci(state, [run(2)], [], [WORKFLOW], SHA, MAPPING)
        recovery = "recovery:" + first + ":at:2:1"; state["outbox"][recovery]["status"] = "pending"
        dn.reconcile_ci(state, [run(3, conclusion="failure")], [], [WORKFLOW], SHA, MAPPING)
        # Even after the new episode clears, the previous recovery stays obsolete.
        dn.reconcile_ci(state, [run(4)], [], [WORKFLOW], SHA, MAPPING)
        dn.resolve(state, recovery, "not_sent", None, None)
        dn.deliver(state, self.Ledger(), self.NoPost())
        self.assertEqual(state["outbox"][recovery]["status"], "retired")

    def test_closed_reopened_pr_pending_old_head_never_revives(self):
        state = dn.new_state(); p = obj(pull=True)
        key = "failure:ci:100:pr:18:1:1"
        dn.queue(state, key, self.message()); state["outbox"][key]["status"] = "pending"
        state["incidents"]["ci:100:pr:18"] = {"alert": key}
        p["state"] = "closed"; dn.reconcile_ci(state, [], [p], [WORKFLOW], SHA, MAPPING)
        p["state"] = "open"; p["head"]["sha"] = "c" * 40
        dn.resolve(state, key, "not_sent", None, None)
        dn.reconcile_ci(state, [], [p], [WORKFLOW], SHA, MAPPING)
        dn.deliver(state, self.Ledger(), self.NoPost())
        self.assertEqual(state["outbox"][key]["status"], "retired")

    def test_live_ci_check_can_retire_or_defer_before_pending_fence(self):
        for verdict, expected in ((False, "retired"), (None, "ready")):
            state = dn.new_state()
            dn.reconcile_ci(state, [run(1, conclusion="failure")], [], [WORKFLOW], SHA, MAPPING)
            key = next(iter(state["outbox"]))
            dn.deliver(state, self.Ledger(), self.NoPost(), ci_check=lambda key: verdict)
            self.assertEqual(state["outbox"][key]["status"], expected)

    def test_sync_notice_defers_failure_status_attempt_and_conclusion_list_detail_conflicts(self):
        def sync_run(n, *, conclusion="success", status="completed", attempt=1):
            return {**run(n, conclusion=conclusion, status=status, attempt=attempt),
                    "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                    "event": "schedule"}

        mutations = (
            {"conclusion": "success"},
            {"run_attempt": 2},
            {"status": "in_progress", "conclusion": None},
        )
        for changes in mutations:
            with self.subTest(changes=changes):
                state = dn.new_state()
                for n in (1, 2):
                    dn.reconcile_sync(state, sync_run(n), report(n, [(18, "A")]), MAPPING)
                failed = sync_run(3, conclusion="failure")
                dn.reconcile_sync(state, failed, None, MAPPING)
                hold_key, failure_key = "hold:18:A:2", "failure:sync:3:1"
                dependencies = list(state["outbox"][hold_key]["dependencies"])
                listed = [sync_run(1), sync_run(2), failed]
                detail = {1: listed[0], 2: listed[1],
                          3: {**failed, **changes}}

                class GH:
                    def repo(inner, path):
                        if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                        return copy.deepcopy(detail[int(path.rsplit("/", 1)[1])])
                    def pages(inner, path, field): return copy.deepcopy(listed)

                with patch.object(dn, "trusted_sync", return_value=True), \
                        patch.object(dt, "artifact_report", side_effect=lambda client, item:
                                     report(item["id"], [(18, "A")])):
                    self.assertIsNone(dn.current_sync_notice(GH(), hold_key, SHA, state))
                    self.assertIsNone(dn.current_sync_notice(GH(), failure_key, SHA, state))
                    class CountingPost:
                        calls = 0
                        def send(inner, message):
                            inner.calls += 1
                            return "333333333333333334"
                    webhook = CountingPost()
                    self.assertEqual(dn.deliver(
                        state, self.Ledger(), webhook,
                        sync_check=lambda key: dn.current_sync_notice(GH(), key, SHA, state)), 0)
                self.assertEqual(webhook.calls, 0)
                self.assertEqual(state["outbox"][hold_key]["status"], "ready")
                self.assertEqual(state["outbox"][failure_key]["status"], "ready")
                self.assertEqual(state["outbox"][hold_key]["dependencies"], dependencies)

    def test_sync_recovery_verifies_unrelated_counter_classification_before_excluding_it(self):
        def sync_run(n, *, event_name="schedule", branch="main", holds=()):
            value = {**run(n), "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                     "event": event_name, "head_branch": branch}
            return value

        state = dn.new_state()
        for n in (1, 2):
            dn.reconcile_sync(state, sync_run(n), report(n, [(18, "A")]), MAPPING)
        alert = "hold:18:A:2"
        state["outbox"][alert].update(status="delivered", message_id="333333333333333333")
        dn.reconcile_sync(state, sync_run(3), report(3), MAPPING)
        recovery = "hold-recovery:18:3:1"
        dependencies = list(state["outbox"][recovery]["dependencies"])
        origin, unrelated, latest = sync_run(3), sync_run(4, event_name="push", branch="feature"), sync_run(5)
        detail = {3: origin,
                  4: sync_run(4),  # The listed unrelated push is actually a related schedule.
                  5: latest}

        class GH:
            detail_reads = []
            def repo(inner, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                run_id = int(path.rsplit("/", 1)[1])
                inner.detail_reads.append(run_id)
                return copy.deepcopy(detail[run_id])
            def pages(inner, path, field): return [origin, unrelated, latest]

        gh = GH()
        with patch.object(dn, "sync_context", return_value=True), \
                patch.object(dn, "trusted_sync", return_value=True), \
                patch.object(dt, "artifact_report", side_effect=lambda client, item:
                             report(item["id"], [(18, "A")] if item["id"] == 4 else [])):
            self.assertIsNone(dn.current_hold_recovery(gh, recovery, SHA))
            class CountingPost:
                calls = 0
                def send(inner, message):
                    inner.calls += 1
                    return "333333333333333334"
            webhook = CountingPost()
            self.assertEqual(dn.deliver(
                state, self.Ledger(), webhook,
                hold_recovery_check=lambda key: dn.current_hold_recovery(gh, key, SHA)), 0)
        self.assertEqual(gh.detail_reads.count(4), 2)
        self.assertEqual(webhook.calls, 0)
        self.assertEqual(state["outbox"][recovery]["status"], "ready")
        self.assertEqual(state["outbox"][recovery]["dependencies"], dependencies)

    def test_ci_recovery_binds_repository_and_pr_associations_across_list_detail(self):
        pull = obj(pull=True)
        def pr_run(n, *, pr_number=18, conclusion="success"):
            value = run(n, conclusion=conclusion)
            association_pull = pull if pr_number == 18 else obj(pr_number, pull=True)
            value.update(event="pull_request", pull_requests=[{
                "number": pr_number, "head": copy.deepcopy(association_pull["head"]),
                "base": copy.deepcopy(association_pull["base"])}])
            return value

        cases = []
        listed = {1: pr_run(1, conclusion="failure"), 2: pr_run(2),
                  3: pr_run(3, pr_number=19), 4: pr_run(4)}
        detail_boundary = copy.deepcopy(listed)
        detail_boundary[2]["head_repository"] = {"id": dt.REPO_ID + 1}
        cases.append(("boundary head repository", [listed[1], listed[2]], detail_boundary))
        detail_intermediate = copy.deepcopy(listed)
        detail_intermediate[3] = pr_run(3, pr_number=18, conclusion="failure")
        cases.append(("unrelated list row becomes related failure", list(listed.values()), detail_intermediate))

        key = "recovery:failure:ci:100:pr:18:1:1:at:2:1"
        for label, observed, details in cases:
            with self.subTest(case=label):
                class GH:
                    def repo(inner, path):
                        if path == "/pulls/18": return copy.deepcopy(pull)
                        if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                        return copy.deepcopy(details[int(path.rsplit("/", 1)[1])])
                    def pages(inner, path, field): return copy.deepcopy(observed)
                self.assertIsNone(dn.current_ci_notice(GH(), key, [WORKFLOW]))

                state = dn.new_state()
                origin = "failure:ci:100:pr:18:1:1"
                dn.queue(state, origin, self.message())
                state["outbox"][origin].update(status="delivered", message_id="333333333333333333")
                dn.queue(state, key, self.message(), dependencies=[origin])
                class CountingPost:
                    calls = 0
                    def send(inner, message):
                        inner.calls += 1
                        return "333333333333333334"
                webhook = CountingPost()
                self.assertEqual(dn.deliver(
                    state, self.Ledger(), webhook,
                    ci_check=lambda notice: dn.current_ci_notice(GH(), notice, [WORKFLOW])), 0)
                self.assertEqual(webhook.calls, 0)
                self.assertEqual(state["outbox"][key]["status"], "ready")
                self.assertEqual(state["outbox"][key]["dependencies"], [origin])

    def test_reason_changed_retires_original_without_target_recovery(self):
        state = dn.new_state()
        dn.reconcile_sync(state, run(1), report(1, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, run(2), report(2, [(18, "A")]), MAPPING)
        key = "hold:18:A:2"; state["outbox"][key]["status"] = "pending"
        dn.reconcile_sync(state, run(3), report(3, [(18, "B")]), MAPPING)
        self.assertEqual(state["outbox"][key]["status"], "pending")
        self.assertIn(key, state["invalidated"])
        self.assertFalse(any(k.startswith("hold-recovery:") for k in state["outbox"]))
        dn.resolve(state, key, "not_sent", None, None)
        dn.deliver(state, self.Ledger(), self.NoPost())
        self.assertEqual(state["outbox"][key]["status"], "retired")

    def test_ci_detail_conflict_defers_and_missing_association_is_unknown(self):
        p = obj(pull=True)
        original = run(1, conclusion="failure")
        original.update(event="pull_request", pull_requests=[{"number": 18, "head": copy.deepcopy(p["head"]), "base": copy.deepcopy(p["base"])}])
        candidate = {**original, "id": 2, "run_number": 2, "conclusion": "success"}
        detail = copy.deepcopy(candidate)
        class GH:
            def repo(inner, path):
                if path == "/pulls/18": return copy.deepcopy(p)
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                return copy.deepcopy(original if path.endswith("/1") else detail)
            def pages(inner, path, key): return [copy.deepcopy(original), copy.deepcopy(candidate)]
        key = "recovery:failure:ci:100:pr:18:1:1:at:2:1"
        for field, value in (("run_attempt", 2), ("status", "in_progress"), ("conclusion", "failure"), ("head_sha", "b"*40), ("id", 3)):
            candidate.clear(); candidate.update(detail); candidate[field] = value
            self.assertIsNone(dn.current_ci_notice(GH(), key, [WORKFLOW]))
        candidate.clear(); candidate.update(detail)
        del original["pull_requests"]
        self.assertIsNone(dn.current_ci_notice(GH(), key, [WORKFLOW]))
        original["pull_requests"] = []
        self.assertFalse(dn.current_ci_notice(GH(), key, [WORKFLOW]))

    def test_intermediate_ci_failure_invalidates_old_recovery_after_new_success(self):
        records = {n: run(n, conclusion="failure" if n in (1, 3) else "success") for n in range(1, 5)}
        class GH:
            def repo(inner, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                return copy.deepcopy(records[int(path.rsplit("/", 1)[1])])
            def pages(inner, path, key): return list(copy.deepcopy(records).values())
        state = dn.new_state()
        dn.reconcile_ci(state, [records[1]], [], [WORKFLOW], SHA, MAPPING)
        alert = next(iter(state["outbox"])); state["outbox"][alert].update(status="delivered", message_id="333333333333333333")
        dn.reconcile_ci(state, [records[2]], [], [WORKFLOW], SHA, MAPPING)
        key = "recovery:"+alert+":at:2:1"; state["outbox"][key]["status"] = "pending"
        dn.reconcile_ci(state, [records[3], records[4]], [], [WORKFLOW], SHA, MAPPING)
        dn.resolve(state, key, "not_sent", None, None)
        dn.deliver(state, self.Ledger(), self.NoPost(), ci_check=lambda k: dn.current_ci_notice(GH(), k, [WORKFLOW]))
        self.assertEqual(state["outbox"][key]["status"], "retired")
        self.assertIn(key, state["invalidated"])

    def test_actual_ci_prepost_checks_latest_run_and_changed_head(self):
        p = obj(pull=True)
        original = run(1, conclusion="failure")
        original.update(event="pull_request", pull_requests=[{"number": 18, "head": copy.deepcopy(p["head"]), "base": copy.deepcopy(p["base"])}])
        latest = {**original, "id": 2, "run_number": 2, "conclusion": "success"}
        class GH:
            def repo(inner, path):
                if path == "/pulls/18": return copy.deepcopy(p)
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                return copy.deepcopy(original if path.endswith("/1") else latest)
            def pages(inner, path, key): return [copy.deepcopy(original), copy.deepcopy(latest)]
        key = "failure:ci:100:pr:18:1:1"
        self.assertFalse(dn.current_ci_notice(GH(), key, [WORKFLOW]))
        self.assertTrue(dn.current_ci_notice(GH(), "recovery:"+key+":at:2:1", [WORKFLOW]))
        latest["status"] = "in_progress"
        self.assertIsNone(dn.current_ci_notice(GH(), "recovery:"+key+":at:2:1", [WORKFLOW]))
        p["head"]["sha"] = "c" * 40
        self.assertFalse(dn.current_ci_notice(GH(), key, [WORKFLOW]))
        p["head"]["sha"] = None
        self.assertIsNone(dn.current_ci_notice(GH(), key, [WORKFLOW]))

    def test_intermediate_unknown_or_conflict_and_changed_recovery_attempt_defer(self):
        p = obj(pull=True)
        def pr_run(n, conclusion="success"):
            r = run(n, conclusion=conclusion)
            r.update(event="pull_request", pull_requests=[{"number": 18, "head": copy.deepcopy(p["head"]), "base": copy.deepcopy(p["base"])}])
            return r
        records = {n: pr_run(n, "failure" if n == 1 else "success") for n in range(1, 5)}
        listed = copy.deepcopy(records)
        class GH:
            def repo(inner, path):
                if path == "/pulls/18": return copy.deepcopy(p)
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                return copy.deepcopy(records[int(path.rsplit("/", 1)[1])])
            def pages(inner, path, key): return list(copy.deepcopy(listed).values())
        key = "recovery:failure:ci:100:pr:18:1:1:at:2:1"
        self.assertTrue(dn.current_ci_notice(GH(), key, [WORKFLOW]))
        del listed[3]["pull_requests"]
        self.assertIsNone(dn.current_ci_notice(GH(), key, [WORKFLOW]))

        listed[3] = copy.deepcopy(records[3]); records[3]["conclusion"] = "failure"
        self.assertIsNone(dn.current_ci_notice(GH(), key, [WORKFLOW]))
        listed[3]["conclusion"] = "failure"
        self.assertFalse(dn.current_ci_notice(GH(), key, [WORKFLOW]))
        records[3]["conclusion"] = "success"; listed[3]["conclusion"] = "success"
        records[2]["run_attempt"] = 3; listed[2]["run_attempt"] = 3
        self.assertIsNone(dn.current_ci_notice(GH(), key, [WORKFLOW]))

    def test_recovery_across_commit_change_validates_historical_original(self):
        for pr in (False, True):
            p = obj(pull=True) if pr else None
            original = run(1, conclusion="failure"); latest = run(2); latest["head_sha"] = "c"*40
            if pr:
                original.update(event="pull_request", pull_requests=[{"number": 18, "head": copy.deepcopy(p["head"]), "base": copy.deepcopy(p["base"])}])
                p["head"]["sha"] = "c"*40; p["merge_commit_sha"] = None
                latest.update(event="pull_request", pull_requests=[{"number": 18, "head": copy.deepcopy(p["head"]), "base": copy.deepcopy(p["base"])}])
            class GH:
                def repo(inner, path):
                    if path == "/pulls/18": return copy.deepcopy(p)
                    if path == "/git/ref/heads/main": return {"object": {"sha": "c"*40}}
                    return copy.deepcopy(original if path.endswith("/1") else latest)
                def pages(inner, path, key): return [copy.deepcopy(original), copy.deepcopy(latest)]
            key = "recovery:failure:ci:100:"+("pr:18" if pr else "main")+":1:1:at:2:1"
            self.assertTrue(dn.current_ci_notice(GH(), key, [WORKFLOW]))
            original["run_attempt"] = 2; original["status"] = "in_progress"
            self.assertIsNone(dn.current_ci_notice(GH(), key, [WORKFLOW]))

    def test_fence_before_post_and_unknown_never_resend_independent_continues(self):
        state = dn.new_state(); ledger = self.Ledger()
        dn.queue(state, "one", self.message()); dn.queue(state, "two", self.message())
        class Webhook:
            calls = 0
            def send(inner, message):
                inner.calls += 1
                self.assertEqual(ledger.states[-1]["outbox"]["one"]["status"], "pending")
                if inner.calls == 1: raise dt.Error("unknown")
                return "333333333333333333"
        webhook = Webhook()
        self.assertEqual(dn.deliver(state, ledger, webhook), 1)
        self.assertEqual(state["outbox"]["one"]["status"], "pending")
        self.assertEqual(state["outbox"]["two"]["status"], "delivered")
        dn.deliver(state, ledger, webhook)
        self.assertEqual(webhook.calls, 2)

    def test_recovery_origin_and_boundary_list_detail_conflicts_defer(self):
        records = {n: run(n, conclusion="failure" if n == 1 else "success") for n in range(1, 4)}
        listed = copy.deepcopy(records)
        class GH:
            def repo(inner, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                return copy.deepcopy(records[int(path.rsplit("/", 1)[1])])
            def pages(inner, path, key): return list(copy.deepcopy(listed).values())
        key = "recovery:failure:ci:100:main:1:1:at:2:1"
        self.assertTrue(dn.current_ci_notice(GH(), key, [WORKFLOW]))
        for rid in (1, 2):
            for field, value in (("run_attempt", 2), ("head_sha", "b"*40),
                                 ("status", "in_progress"), ("conclusion", "failure" if rid == 2 else "success")):
                listed[rid] = copy.deepcopy(records[rid]); listed[rid][field] = value
                self.assertIsNone(dn.current_ci_notice(GH(), key, [WORKFLOW]))
            listed[rid] = copy.deepcopy(records[rid])
        del listed[2]
        self.assertIsNone(dn.current_ci_notice(GH(), key, [WORKFLOW]))

    def test_ci_recovery_requires_counter_coverage_and_first_attempt_evidence(self):
        records = {
            1: run(1, conclusion="failure"),
            2: run(2),
            3: run(3),
            4: run(4),
        }
        key = "recovery:failure:ci:100:main:1:1:at:2:1"

        class GH:
            def __init__(self, observed, details=None):
                self.observed = observed
                self.details = records if details is None else details
                self.detail_reads = []
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                run_id = int(path.rsplit("/", 1)[1])
                self.detail_reads.append(run_id)
                return copy.deepcopy(self.details[run_id])
            def pages(self, path, field): return copy.deepcopy(self.observed)

        # A missing workflow counter slot cannot prove that no intervening
        # failure occurred, even when the latest completed run is clear.
        self.assertIsNone(dn.current_ci_notice(GH([records[1], records[2], records[4]]), key, [WORKFLOW]))

        # The workflow endpoint may expose only attempt 2; without attempt 1,
        # a hidden intervening failure is possible and recovery must defer.
        attempt_two = {**records[3], "run_attempt": 2}
        self.assertIsNone(dn.current_ci_notice(
            GH([records[1], records[2], attempt_two], {**records, 3: attempt_two}),
            key, [WORKFLOW]))

        # A proven unrelated PR run in the same workflow/base repository fills
        # its counter slot only after its unrelated classification is bound
        # to the exact workflow-run detail response.
        other_pr = obj(19, pull=True)
        unrelated = {**records[3], "event": "pull_request", "pull_requests": [{
            "number": 19, "head": copy.deepcopy(other_pr["head"]),
            "base": copy.deepcopy(other_pr["base"])}]}
        observed = [records[1], records[2], unrelated, records[4]]
        gh = GH(observed, {**records, 3: unrelated})
        self.assertTrue(dn.current_ci_notice(gh, key, [WORKFLOW]))
        self.assertIn(3, gh.detail_reads)

    def test_ci_recovery_checks_origin_to_latest_not_only_clear_boundary(self):
        records = {
            1: run(1, conclusion="failure"),
            2: run(2),
            3: run(3),
            4: run(4, conclusion="failure"),
            5: run(5),
        }
        failure_key = "failure:ci:100:main:1:1"
        key = "recovery:" + failure_key + ":at:3:1"

        class Ledger:
            def save(self, state): pass
        class Webhook:
            def __init__(self): self.calls = 0
            def send(self, message): self.calls += 1; return "333333333333333334"
        class GH:
            def __init__(self, observed, details=None):
                self.observed = observed
                self.details = records if details is None else details
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                return copy.deepcopy(self.details[int(path.rsplit("/", 1)[1])])
            def pages(self, path, field): return copy.deepcopy(self.observed)

        scenarios = (
            ("missing-before-boundary", [records[1], records[3]], None),
            ("pending-before-boundary", [records[1], {**records[2], "status": "in_progress",
                                                        "conclusion": None}, records[3]], None),
            ("hidden-attempt-before-boundary", [records[1], {**records[2], "run_attempt": 2},
                                                  records[3]], None),
            ("recurrence-after-boundary", [records[1], records[2], records[3], records[4], records[5]], False),
            # Consecutive failures before the first clear are one incident.
            ("same-incident-failure-before-clear", [records[1], run(2, conclusion="failure"),
                                                     records[3]], True),
        )
        for label, observed, expected in scenarios:
            with self.subTest(scenario=label):
                state = dn.new_state()
                dn.queue(state, failure_key, self.message())
                state["outbox"][failure_key].update(status="delivered", message_id="333333333333333333")
                state["incidents"]["ci:100:main"] = {"alert": failure_key}
                dn.reconcile_ci(state, [records[3]], [], [WORKFLOW], SHA, MAPPING)
                details = ({**records, 2: observed[1]}
                           if label == "same-incident-failure-before-clear" else None)
                gh = GH(observed, details)
                verdict = dn.current_ci_notice(gh, key, [WORKFLOW])
                self.assertIs(verdict, expected)
                webhook = Webhook()
                sent = dn.deliver(state, Ledger(), webhook,
                                  ci_check=lambda notice: dn.current_ci_notice(gh, notice, [WORKFLOW]))
                self.assertEqual(sent, 0)
                self.assertEqual(webhook.calls, 1 if expected is True else 0)
                if expected is None:
                    self.assertEqual(state["outbox"][key]["status"], "ready")
                    self.assertEqual(state["outbox"][key]["dependencies"], [failure_key])

    def test_ci_recovery_recurrence_before_boundary_blocks_initial_and_429_posts(self):
        failure_key = "failure:ci:100:main:1:1"
        key = "recovery:" + failure_key + ":at:4:1"
        for scenario in ("before-first-post", "before-429-retry", "consecutive-failures"):
            with self.subTest(scenario=scenario):
                records = {1: run(1, conclusion="failure"),
                           2: run(2, conclusion="failure"),
                           3: run(3, conclusion="failure"), 4: run(4)}
                recurrence = {**records, 2: run(2)}
                state = dn.new_state()
                dn.queue(state, failure_key, self.message())
                state["outbox"][failure_key].update(
                    status="delivered", message_id="333333333333333333")
                state["incidents"]["ci:100:main"] = {"alert": failure_key}
                dn.reconcile_ci(state, [records[4]], [], [WORKFLOW], SHA, MAPPING)

                class GH:
                    def __init__(self):
                        self.runs = recurrence if scenario == "before-first-post" else records
                    def repo(self, path):
                        if path == "/git/ref/heads/main":
                            return {"object": {"sha": SHA}}
                        return copy.deepcopy(self.runs[int(path.rsplit("/", 1)[1])])
                    def pages(self, path, field):
                        return copy.deepcopy(list(self.runs.values()))

                gh = GH()
                self.assertIs(dn.current_ci_notice(gh, key, [WORKFLOW]),
                              scenario != "before-first-post")
                webhook = dt.Discord(
                    "https://discord.com/api/webhooks/111111111111111111/" + "a" * 40,
                    dt.CHANNEL_ID)
                posts = []

                def request(url, *, method="GET", payload=None, **kwargs):
                    if method != "POST":
                        return 200, {}, json.dumps({"guild_id": "1554806404320075786",
                                                    "channel_id": dt.CHANNEL_ID}).encode()
                    posts.append(copy.deepcopy(payload))
                    if len(posts) == 1:
                        if scenario == "before-429-retry":
                            gh.runs = recurrence
                        raise dt.HTTPError(429, body=b'{"retry_after":0}')
                    return 200, {}, json.dumps({"id": "333333333333333334",
                        "channel_id": dt.CHANNEL_ID, "webhook_id": "111111111111111111",
                        "content": payload["content"]}).encode()

                with patch.object(dt, "request", side_effect=request), patch.object(dt.time, "sleep"):
                    sent = dn.deliver(state, self.Ledger(), webhook,
                                      ci_check=lambda notice: dn.current_ci_notice(gh, notice, [WORKFLOW]))
                expected_posts = {"before-first-post": 0, "before-429-retry": 1,
                                  "consecutive-failures": 2}[scenario]
                self.assertEqual(len(posts), expected_posts)
                self.assertEqual(sent, 0)
                self.assertEqual(state["outbox"][key]["status"],
                                 "delivered" if scenario == "consecutive-failures" else "retired")
                self.assertEqual(state["outbox"][key]["dependencies"], [failure_key])

    def test_ci_recovery_429_rechecks_origin_to_latest_recurrence(self):
        records = {1: run(1, conclusion="failure"), 2: run(2)}
        failure_key = "failure:ci:100:main:1:1"
        key = "recovery:" + failure_key + ":at:2:1"
        state = dn.new_state()
        dn.queue(state, failure_key, self.message())
        state["outbox"][failure_key].update(status="delivered", message_id="333333333333333333")
        state["incidents"]["ci:100:main"] = {"alert": failure_key}
        dn.reconcile_ci(state, [records[2]], [], [WORKFLOW], SHA, MAPPING)

        class GH:
            def __init__(self): self.runs = copy.deepcopy(records)
            def repo(self, path):
                if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
                return copy.deepcopy(self.runs[int(path.rsplit("/", 1)[1])])
            def pages(self, path, field): return copy.deepcopy(list(self.runs.values()))
        gh = GH()
        webhook = dt.Discord("https://discord.com/api/webhooks/111111111111111111/" + "a" * 40,
                             dt.CHANNEL_ID)
        posts = []
        def request(url, *, method="GET", payload=None, **kwargs):
            if method != "POST":
                return 200, {}, json.dumps({"guild_id": "1554806404320075786",
                                            "channel_id": dt.CHANNEL_ID}).encode()
            posts.append(copy.deepcopy(payload))
            gh.runs = {1: records[1], 2: records[2],
                       3: run(3, conclusion="failure"), 4: run(4)}
            raise dt.HTTPError(429, body=b'{"retry_after":0}')
        with patch.object(dt, "request", side_effect=request), patch.object(dt.time, "sleep"):
            sent = dn.deliver(state, self.Ledger(), webhook,
                              ci_check=lambda notice: dn.current_ci_notice(gh, notice, [WORKFLOW]))
        self.assertEqual(sent, 0)
        self.assertEqual(len(posts), 1)  # The recurrence blocks Discord's additional POST.
        self.assertEqual(state["outbox"][key]["status"], "retired")
        self.assertIn(key, state["invalidated"])

    def test_recovery_waits_confirmed_original(self):
        state = dn.new_state(); ledger = self.Ledger()
        dn.queue(state, "alert", self.message()); state["outbox"]["alert"]["status"] = "pending"
        dn.queue(state, "recovery", self.message(), dependencies=["alert"])
        class Webhook:
            def send(inner, msg): raise AssertionError("Must not send unconfirmed recovery")
        dn.deliver(state, ledger, Webhook())
        self.assertEqual(state["outbox"]["recovery"]["status"], "ready")

    def test_state_write_failure_prevents_post(self):
        state = dn.new_state(); dn.queue(state, "one", self.message())
        class Ledger:
            def save(inner, state): raise dt.Error("write failed")
        class Webhook:
            def send(inner, msg): raise AssertionError("Must not send")
        with self.assertRaises(dt.Error): dn.deliver(state, Ledger(), Webhook())

    def test_request_cancelled_during_poll_is_rechecked_before_post(self):
        state = dn.new_state(); ledger = self.Ledger()
        dn.queue(state, "pr:18:timeline:1", self.message())
        class Webhook:
            def send(inner, message): raise AssertionError("Cancelled request must not POST")
        self.assertEqual(dn.deliver(state, ledger, Webhook(), event_check=lambda key: None), 0)
        self.assertEqual(ledger.states[-1]["outbox"]["pr:18:timeline:1"]["status"], "retired")

    def test_confirmed_429_rechecks_dynamic_notices_before_retry(self):
        webhook = dt.Discord("https://discord.com/api/webhooks/111111111111111111/" + "a" * 40,
                             dt.CHANNEL_ID)
        message = self.message()
        receipt = {"id": "333333333333333333", "channel_id": webhook.channel_id,
                   "webhook_id": "111111111111111111", "content": message["content"]}
        cases = (
            ("pr:18:timeline:1", "event", None, "retired"),
            ("recovery:failure:ci:100:main:1:1:at:2:1", "ci", False, "retired"),
            ("hold-recovery:18:3:1", "hold", None, "ready"),
        )
        for key, kind, after_rejection, expected_status in cases:
            with self.subTest(kind=kind):
                state = dn.new_state(); dn.queue(state, key, message)
                if kind == "hold-recovery":
                    state["outbox"][key]["dependencies"] = ["hold-anchor"]
                    dn.queue(state, "hold-anchor", message)
                    state["outbox"]["hold-anchor"]["status"] = "delivered"
                calls = []

                def check(_):
                    calls.append(True)
                    return message if kind == "event" and len(calls) == 1 else (
                        True if kind in ("ci", "hold") and len(calls) == 1 else after_rejection)

                check_args = ({"event_check": check} if kind == "event" else
                              {"ci_check": check} if kind == "ci" else
                              {"hold_recovery_check": check})
                with patch.object(dt, "request", side_effect=[dt.HTTPError(429, body=b'{"retry_after":0}'),
                                                               (200, {}, json.dumps(receipt).encode())]) as request, \
                        patch.object(dt.time, "sleep"):
                    errors = dn.deliver(state, self.Ledger(), webhook, **check_args)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(errors, 0)
                self.assertEqual(state["outbox"][key]["status"], expected_status)

        # An unchanged dynamic notice still retries after the definite rejection.
        state = dn.new_state()
        retry_key = "failure:ci:100:main:1:1"
        dn.queue(state, retry_key, message)
        state["incidents"]["ci:100:main"] = {"alert": retry_key}
        retry_receipt = {**receipt, "content": state["outbox"][retry_key]["payload"]["content"]}
        retry_ledger = self.Ledger()
        with patch.object(dt, "request", side_effect=[dt.HTTPError(429, body=b'{"retry_after":0}'),
                                                       (200, {}, json.dumps(retry_receipt).encode())]) as request, \
                patch.object(dt.time, "sleep"):
            errors = dn.deliver(state, retry_ledger, webhook, ci_check=lambda key: True)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(errors, 0)
        self.assertEqual(state["outbox"][retry_key]["status"], "delivered")
        self.assertEqual(retry_ledger.states[0]["outbox"][retry_key]["status"], "pending")

    def test_sync_notice_untrusted_429_stays_ready_until_a_later_fresh_retry(self):
        state = dn.new_state()
        dn.reconcile_sync(state, run(1), report(1, [(18, "A")]), MAPPING)
        dn.reconcile_sync(state, run(2), report(2, [(18, "A")]), MAPPING)
        key = "hold:18:A:2"

        class Rejected:
            calls = 0
            def send(self, message):
                self.calls += 1
                raise dt.RateLimitRejected(429, {}, b"{}")

        rejected = Rejected()
        self.assertEqual(dn.deliver(state, self.Ledger(), rejected, sync_check=lambda _: True), 0)
        self.assertEqual(rejected.calls, 1)
        self.assertEqual(state["outbox"][key]["status"], "ready")

        class Accepted:
            calls = 0
            def send(self, message):
                self.calls += 1
                return "333333333333333333"

        accepted = Accepted()
        self.assertEqual(dn.deliver(state, self.Ledger(), accepted, sync_check=lambda _: True), 0)
        self.assertEqual(accepted.calls, 1)
        self.assertEqual(state["outbox"][key]["status"], "delivered")

    def test_unknown_sync_status_resets_streak_then_later_valid_runs_progress(self):
        def sync_run(n):
            return {**run(n), "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                    "event": "schedule"}

        state = dn.new_state()
        first = sync_run(1)
        dn.reconcile_sync(state, first, report(1, [(18, "A")]), MAPPING)
        missing_status = sync_run(2); missing_status.pop("status")
        third = sync_run(3)
        with patch.object(dt, "artifact_report", side_effect=lambda gh, item: report(item["id"], [(18, "A")])):
            dn.reconcile_sync_runs(state, object(), [missing_status, third], SHA, MAPPING)
        self.assertEqual(state["sync_cursor"], [3, 3, 1])
        self.assertEqual(state["holds"]["18"]["reasons"]["A"]["streak"], 1)
        self.assertFalse(any(key.startswith("hold:18:A:") for key in state["outbox"]))

        fourth = sync_run(4)
        with patch.object(dt, "artifact_report", return_value=report(4, [(18, "A")])):
            dn.reconcile_sync_runs(state, object(), [fourth], SHA, MAPPING)
        self.assertEqual(state["sync_cursor"], [4, 4, 1])
        self.assertEqual(state["holds"]["18"]["reasons"]["A"]["streak"], 2)
        self.assertIn("hold:18:A:4", state["outbox"])

    def test_intermediate_pending_sync_run_defers_later_run_and_replays_late_completion_in_order(self):
        def sync_run(n, *, status="completed", conclusion="success"):
            return {**run(n, status=status, conclusion=conclusion),
                    "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                    "event": "schedule"}

        state = dn.new_state()
        dn.reconcile_sync(state, sync_run(1), report(1, [(18, "A")]), MAPPING)
        pending, newer = sync_run(2, status="in_progress", conclusion=None), sync_run(3)
        with patch.object(dt, "artifact_report", side_effect=lambda gh, item:
                          report(item["id"], [(18, "A")])) as artifact:
            dn.reconcile_sync_runs(state, object(), [pending, newer], SHA, MAPPING)
        self.assertEqual(artifact.call_count, 0)
        self.assertEqual(state["sync_cursor"], [1, 1, 1])
        self.assertEqual(state["holds"]["18"]["reasons"]["A"]["streak"], 0)
        self.assertFalse(any(key.startswith("hold:18:A:") for key in state["outbox"]))

        # Once the delayed run completes, both ordered valid snapshots can be
        # consumed; the second one reserves the alert without skipping history.
        with patch.object(dt, "artifact_report", side_effect=lambda gh, item:
                          report(item["id"], [(18, "A")])):
            dn.reconcile_sync_runs(state, object(), [sync_run(2), newer], SHA, MAPPING)
        self.assertEqual(state["sync_cursor"], [3, 3, 1])
        self.assertEqual(state["holds"]["18"]["reasons"]["A"]["streak"], 2)
        self.assertIn("hold:18:A:3", state["outbox"])

    def test_pending_sync_gap_preserves_already_reserved_delivery_fences(self):
        def sync_run(n, *, status="completed", conclusion="success"):
            return {**run(n, status=status, conclusion=conclusion),
                    "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                    "event": "schedule"}

        for delivery_status in ("delivered", "pending"):
            with self.subTest(delivery_status=delivery_status):
                state = dn.new_state()
                for n in (1, 2):
                    dn.reconcile_sync(state, sync_run(n), report(n, [(18, "A")]), MAPPING)
                key = "hold:18:A:2"
                item = state["outbox"][key]
                item.update(status=delivery_status,
                            message_id="333333333333333333" if delivery_status == "delivered" else None)
                cursor = list(state["sync_cursor"])
                with patch.object(dt, "artifact_report", return_value=report(4, [(18, "A")])):
                    dn.reconcile_sync_runs(state, object(),
                                           [sync_run(3, status="in_progress", conclusion=None),
                                            sync_run(4)], SHA, MAPPING)
                self.assertEqual(state["sync_cursor"], cursor)
                self.assertEqual(state["holds"]["18"]["reasons"]["A"]["alert"], key)
                self.assertEqual(state["outbox"][key]["status"], delivery_status)
                self.assertEqual(state["outbox"][key]["message_id"], item["message_id"])
                self.assertEqual(state["holds"]["18"]["reasons"]["A"]["streak"], 2)

    def test_missing_sync_counter_run_resets_streak_but_unrelated_bound_run_fills_it(self):
        def sync_run(n, *, event_name="schedule", branch="main", workflow_id=None,
                     repo_id=None, head_repo_id=None):
            value = {**run(n), "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                     "event": event_name, "head_branch": branch}
            if workflow_id is not None: value["workflow_id"] = workflow_id
            if repo_id is not None: value["repository"]["id"] = repo_id
            if head_repo_id is not None: value["head_repository"]["id"] = head_repo_id
            return value

        for middle, expected_streak in (
            (None, 1),
            (sync_run(4, event_name="push", branch="feature"), 2),
            (sync_run(4, workflow_id=dn.SYNC_ID + 1), 1),
            (sync_run(4, repo_id=dt.REPO_ID + 1), 1),
            (sync_run(4, head_repo_id=dt.REPO_ID + 1), 1),
            (sync_run(4, head_repo_id=None) | {"head_repository": None}, 1),
        ):
            with self.subTest(middle=middle and (middle["workflow_id"], middle["repository"]["id"])):
                state = dn.new_state()
                third = sync_run(3)
                dn.reconcile_sync(state, third, report(3, [(18, "A")]), MAPPING)
                state["sync_cursor"] = [3, 3, 1]
                fifth = sync_run(5)
                runs = [x for x in (middle, fifth) if x is not None]
                artifacts = []

                def artifact(gh, item):
                    artifacts.append(item["id"])
                    return report(item["id"], [(18, "A")])

                class GH:
                    def repo(self, path):
                        match = re.fullmatch(r"/actions/runs/([0-9]+)", path)
                        if match:
                            run_id = int(match.group(1))
                            return copy.deepcopy(next(r for r in runs if r["id"] == run_id))
                        raise AssertionError(path)

                with patch.object(dt, "artifact_report", side_effect=artifact):
                    dn.reconcile_sync_runs(state, GH(), runs, SHA, MAPPING)
                reason = state["holds"]["18"]["reasons"]["A"]
                self.assertEqual(reason["streak"], expected_streak)
                self.assertEqual(5 in artifacts, True)
                self.assertEqual(4 in artifacts, False)
                self.assertEqual("hold:18:A:5" in state["outbox"], expected_streak == 2)

    def test_429_rejected_retry_and_5xx_unknown(self):
        webhook = dt.Discord("https://discord.com/api/webhooks/111111111111111111/" + "a" * 40,
                             dt.CHANNEL_ID)
        message = self.message()
        response = {"id": "333333333333333333", "channel_id": webhook.channel_id,
                    "webhook_id": "111111111111111111", "content": message["content"]}
        with patch.object(dt, "request", side_effect=[dt.HTTPError(429, body=b'{"retry_after":0}'),
                                                      (200, {}, json.dumps(response).encode())]) as request:
            self.assertEqual(webhook.send(message), response["id"])
            self.assertEqual(request.call_count, 2)
        with patch.object(dt, "request", side_effect=dt.HTTPError(500)) as request:
            with self.assertRaises(dt.Error): webhook.send(message)
            self.assertEqual(request.call_count, 1)

    def test_semantic_key_sealed_in_message_and_old_receipt_cannot_reuse(self):
        state = dn.new_state()
        dn.queue(state, "issue:18:closed:1", self.message())
        dn.queue(state, "issue:18:closed:2", self.message())
        first, second = state["outbox"].values()
        self.assertNotEqual(first["payload"]["content"], second["payload"]["content"])
        first.update(status="delivered", message_id="333333333333333333")
        second["status"] = "pending"
        with self.assertRaises(dt.Error):
            dn.resolve(state, "issue:18:closed:2", "found", "333333333333333333", None)
        second.update(status="delivered", message_id="333333333333333333")
        with self.assertRaises(dt.Error): dt.validate_state(state)


class Native19Closure(unittest.TestCase):
    class GH:
        def __init__(self, *, ci=(), sync=(), details=None, reports=None):
            self.ci, self.sync = copy.deepcopy(ci), copy.deepcopy(sync)
            self.details = copy.deepcopy(details if details is not None else
                                         {r["id"]: r for r in [*ci, *sync]})
            self.reports = copy.deepcopy(reports or {})
            self.detail_reads = []
            self.fail_details = set()
        def repo(self, path):
            if path == "/git/ref/heads/main": return {"object": {"sha": SHA}}
            if path == "/pulls/18": return obj(pull=True)
            if path == "/actions/workflows/100": return {**WORKFLOW, "state": "active"}
            rid = int(path.rsplit("/", 1)[1])
            self.detail_reads.append(rid)
            if rid in self.fail_details: raise dt.Error("synthetic detail unavailable")
            return copy.deepcopy(self.details[rid])
        def pages(self, path, field):
            if path == f"/actions/workflows/{dn.SYNC_ID}/runs": return copy.deepcopy(self.sync)
            if path == "/actions/workflows/100/runs": return copy.deepcopy(self.ci)
            raise AssertionError(path)

    class Ledger:
        def __init__(self, state): self.value = copy.deepcopy(state)
        def load(self): return copy.deepcopy(self.value)
        def save(self, state, **kwargs): self.value = copy.deepcopy(state)

    @staticmethod
    def sync_run(n, *, conclusion="success", unrelated=False):
        return {**run(n, conclusion=conclusion), "workflow_id": dn.SYNC_ID,
                "path": dn.SYNC_PATH, "event": "push" if unrelated else "schedule",
                "head_branch": "feature" if unrelated else "main"}

    @staticmethod
    def pr_run(n, *, number=18, conclusion="success"):
        pull = obj(number, pull=True)
        return {**run(n, conclusion=conclusion), "event": "pull_request",
                "pull_requests": [{"number": number, "head": copy.deepcopy(pull["head"]),
                                   "base": copy.deepcopy(pull["base"])}]}

    def artifact(self, gh, item):
        value = gh.reports.get(item["id"])
        if value is None: raise dt.Error("synthetic artifact unavailable")
        return copy.deepcopy(value)

    def posts(self, gh, operation, *, switch=None):
        posts = []
        def request(url, *, method="GET", payload=None, **kwargs):
            if method != "POST":
                return 200, {}, json.dumps({"guild_id": "1554806404320075786",
                                            "channel_id": dt.CHANNEL_ID}).encode()
            posts.append(copy.deepcopy(payload))
            if len(posts) == 1:
                if switch: switch()
                raise dt.HTTPError(429, body=b'{"retry_after":0}')
            return 200, {}, json.dumps({"id": "333333333333333334",
                "channel_id": dt.CHANNEL_ID, "webhook_id": "111111111111111111",
                "content": payload["content"]}).encode()
        webhook = dt.Discord("https://discord.com/api/webhooks/111111111111111111/" + "a" * 40,
                             dt.CHANNEL_ID)
        with (patch.object(dt, "request", side_effect=request), patch.object(dt.time, "sleep"),
              patch.object(dt, "artifact_report", side_effect=self.artifact)):
            result = operation(webhook)
        return result, posts

    def run_main(self, gh, state, *, ci=False):
        ledger = self.Ledger(state)
        config = json.dumps({"repository_id": dn.REPO_ID, "sync_workflow_id": dn.SYNC_ID,
                             "ci_workflows": [{"id": 100, "path": WORKFLOW["path"]}] if ci else []}).encode()
        env = {"DISCORD_NOTIFICATIONS_ENABLED": "true", "GITHUB_TOKEN": "test-token",
               "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/111111111111111111/" + "a" * 40,
               "DISCORD_CHANNEL_ID": dt.CHANNEL_ID}
        with (patch.object(Path, "read_bytes", return_value=config),
              patch.object(dn, "collect_objects", return_value=([], [obj(pull=True)] if ci else [], {})),
              patch.object(dn, "project_backlog", return_value=[]),
              patch.object(dn, "source_events", return_value={}),
              patch.object(dn.observation_clock, "verify_snapshot", return_value=(None, None))):
            result, posts = self.posts(gh, lambda webhook: dn.main(
                [], env=env, github_factory=lambda token: gh, ledger_factory=lambda client: ledger,
                discord_factory=lambda token, channel: webhook))
        return result, ledger.value, posts

    def ci_state(self):
        state = dn.new_state()
        failed, success = self.pr_run(1, conclusion="failure"), self.pr_run(2)
        dn.reconcile_ci(state, [failed], [obj(pull=True)], [WORKFLOW], SHA, MAPPING)
        failure_key = "failure:ci:100:pr:18:1:1"
        state["outbox"][failure_key].update(status="delivered", message_id="333333333333333333")
        return state, failed, success, failure_key

    def test_excluded_latest_ci_row_is_verified_before_main_ingestion(self):
        for mode in ("conflict", "unknown", "verified-unrelated"):
            with self.subTest(mode=mode):
                state, first, second, failure_key = self.ci_state()
                excluded = self.pr_run(3, number=19, conclusion="failure")
                details = {1: first, 2: second, 3: excluded if mode == "verified-unrelated" else
                           self.pr_run(3, conclusion="failure")}
                gh = self.GH(ci=[first, second, excluded], details=details)
                if mode == "unknown": gh.fail_details.add(3)
                result, after, posts = self.run_main(gh, state, ci=True)
                self.assertEqual(len(posts), 2 if mode == "verified-unrelated" else 0)
                self.assertIn(3, gh.detail_reads)
                recovery = "recovery:" + failure_key + ":at:2:1"
                if mode == "verified-unrelated":
                    self.assertEqual(result, 0)
                    self.assertEqual(after["outbox"][recovery]["status"], "delivered")
                else:
                    self.assertEqual(after["incidents"]["ci:100:pr:18"]["alert"], failure_key)
                    self.assertNotIn(recovery, after["outbox"])

    def test_excluded_latest_ci_row_blocks_first_post_and_429_retry(self):
        for mode in ("first-conflict", "retry-conflict", "verified-unrelated"):
            with self.subTest(mode=mode):
                state, first, second, failure_key = self.ci_state()
                dn.reconcile_ci(state, [second], [obj(pull=True)], [WORKFLOW], SHA, MAPPING)
                key = "recovery:" + failure_key + ":at:2:1"
                excluded = self.pr_run(3, number=19, conclusion="failure")
                gh = self.GH(ci=[first, second, excluded])
                conflict = self.pr_run(3, conclusion="failure")
                if mode == "first-conflict": gh.details[3] = conflict
                verdict = dn.current_ci_notice(gh, key, [WORKFLOW])
                _, posts = self.posts(gh, lambda webhook: dn.deliver(
                    state, self.Ledger(state), webhook,
                    ci_check=lambda notice: dn.current_ci_notice(gh, notice, [WORKFLOW])),
                    switch=(lambda: gh.details.update({3: conflict})) if mode == "retry-conflict" else None)
                self.assertEqual(len(posts), {"first-conflict": 0, "retry-conflict": 1,
                                              "verified-unrelated": 2}[mode])
                self.assertIs(verdict, None if mode == "first-conflict" else True)
                self.assertEqual(state["outbox"][key]["status"],
                                 "delivered" if mode == "verified-unrelated" else "ready")
                self.assertEqual(state["outbox"][key]["dependencies"], [failure_key])
                if mode != "verified-unrelated":
                    self.assertIsNone(dn.current_ci_notice(gh, failure_key, [WORKFLOW]))

    def sync_state(self, kind):
        state = dn.new_state()
        if kind in ("hold", "hold-recovery"):
            rows = [self.sync_run(1), self.sync_run(2)]
            reports = {n: report(n, [(18, "A")]) for n in (1, 2)}
            for row in rows: dn.reconcile_sync(state, row, reports[row["id"]], MAPPING)
            key = "hold:18:A:2"
            if kind == "hold-recovery":
                state["outbox"][key].update(status="delivered", message_id="333333333333333333")
                rows.append(self.sync_run(3)); reports[3] = report(3)
                dn.reconcile_sync(state, rows[-1], reports[3], MAPPING)
                key = "hold-recovery:18:3:1"
        else:
            rows = [self.sync_run(1, conclusion="failure")]; reports = {}
            dn.reconcile_sync(state, rows[0], None, MAPPING)
            key = "failure:sync:1:1"
            if kind == "failure-recovery":
                state["outbox"][key].update(status="delivered", message_id="333333333333333333")
                rows.append(self.sync_run(2)); reports[2] = report(2)
                dn.reconcile_sync(state, rows[-1], reports[2], MAPPING)
                key = "recovery:" + key + ":at:2:1"
        return state, rows, reports, key

    def test_excluded_latest_sync_row_blocks_adjacent_checks_and_429(self):
        for kind in ("hold", "hold-recovery", "failure", "failure-recovery"):
            for mode in ("first-conflict", "retry-conflict", "verified-unrelated"):
                with self.subTest(kind=kind, mode=mode):
                    state, rows, reports, key = self.sync_state(kind)
                    n = rows[-1]["id"] + 1
                    excluded = self.sync_run(n, unrelated=True)
                    conflict = self.sync_run(n, conclusion="failure" if kind == "failure-recovery" else "success")
                    reports[n] = report(n, [(18, "A")] if kind == "hold-recovery" else [])
                    gh = self.GH(sync=[*rows, excluded], reports=reports)
                    if mode == "first-conflict": gh.details[n] = conflict
                    check = (lambda notice: dn.current_hold_recovery(gh, notice, SHA)) if kind == "hold-recovery" else (
                        lambda notice: dn.current_sync_notice(gh, notice, SHA, state))
                    with patch.object(dt, "artifact_report", side_effect=self.artifact):
                        verdict = check(key)
                    _, posts = self.posts(gh, lambda webhook: dn.deliver(
                        state, self.Ledger(state), webhook,
                        hold_recovery_check=check if kind == "hold-recovery" else None,
                        sync_check=check if kind != "hold-recovery" else None),
                        switch=(lambda: gh.details.update({n: conflict})) if mode == "retry-conflict" else None)
                    self.assertEqual(len(posts), {"first-conflict": 0, "retry-conflict": 1,
                                                  "verified-unrelated": 2}[mode])
                    self.assertIs(verdict, None if mode == "first-conflict" else True)
                    self.assertEqual(state["outbox"][key]["status"],
                                     "delivered" if mode == "verified-unrelated" else "ready")

    def test_excluded_latest_sync_row_blocks_main_ingestion_and_preserves_verified_positive(self):
        for kind in ("hold", "hold-recovery"):
            for verified in (False, True):
                with self.subTest(kind=kind, verified=verified):
                    state, rows, reports, key = self.sync_state(kind)
                    n = rows[-1]["id"] + 1
                    excluded = self.sync_run(n, unrelated=True)
                    gh = self.GH(sync=[*rows, excluded], reports=reports)
                    if not verified: gh.details[n] = self.sync_run(n)
                    _, after, posts = self.run_main(gh, state)
                    self.assertEqual(len(posts), 2 if verified else 0)
                    self.assertEqual(after["sync_cursor"], state["sync_cursor"])
                    self.assertEqual(after["outbox"][key]["status"], "delivered" if verified else "ready")

    def test_sync_failure_recovery_rechecks_artifact_verified_success_before_boundary(self):
        rows = [self.sync_run(1, conclusion="failure"), self.sync_run(2),
                self.sync_run(3, conclusion="failure"), self.sync_run(4)]
        gh = self.GH(sync=rows, reports={4: report(4)})
        state = dn.new_state()
        with patch.object(dt, "artifact_report", side_effect=self.artifact):
            for end in range(1, 5):
                dn.reconcile_sync_runs(state, gh, rows[:end], SHA, MAPPING)
                if end == 1:
                    state["outbox"]["failure:sync:1:1"].update(
                        status="delivered", message_id="333333333333333333")
            key = "recovery:failure:sync:1:1:at:4:1"
            self.assertEqual(state["outbox"][key]["status"], "ready")
            self.assertIsNone(dn.current_sync_notice(gh, key, SHA, state))
            gh.reports[2] = report(2)
            verdict = dn.current_sync_notice(gh, key, SHA, state)
        _, posts = self.posts(gh, lambda webhook: dn.deliver(
            state, self.Ledger(state), webhook,
            sync_check=lambda notice: dn.current_sync_notice(gh, notice, SHA, state)))
        self.assertEqual(posts, [])
        self.assertIs(verdict, False)
        self.assertEqual(state["outbox"][key]["status"], "retired")

    def test_sync_failure_recovery_consecutive_failures_and_429_transition(self):
        for retry_recurrence in (False, True):
            with self.subTest(retry_recurrence=retry_recurrence):
                rows = [self.sync_run(n, conclusion="failure" if n < 4 else "success")
                        for n in range(1, 5)]
                gh = self.GH(sync=rows, reports={4: report(4)})
                state = dn.new_state()
                for row in rows: dn.reconcile_sync(state, row, gh.reports.get(row["id"]), MAPPING)
                key = "recovery:failure:sync:1:1:at:4:1"
                state["outbox"]["failure:sync:1:1"].update(
                    status="delivered", message_id="333333333333333333")
                def recurrence():
                    gh.sync[1] = self.sync_run(2)
                    gh.details[2] = self.sync_run(2)
                    gh.reports[2] = report(2)
                _, posts = self.posts(gh, lambda webhook: dn.deliver(
                    state, self.Ledger(state), webhook,
                    sync_check=lambda notice: dn.current_sync_notice(gh, notice, SHA, state)),
                    switch=recurrence if retry_recurrence else None)
                self.assertEqual(len(posts), 1 if retry_recurrence else 2)
                self.assertEqual(state["outbox"][key]["status"],
                                 "retired" if retry_recurrence else "delivered")


class Contract(unittest.TestCase):
    def test_report_schema_rejects_secret_and_mismatch(self):
        good = report(1, [(18, "HOLD")]); nr.validate(good, run_id=1, attempt=1, sha=SHA)
        for change in ({"token": "synthetic"}, {"run_id": True}, {"checked_out_sha": "b" * 40},
                       {"dry_run": True}, {"holds": good["holds"] * 2}):
            bad = {**good, **change}
            with self.assertRaises(ValueError): nr.validate(bad)

    def test_projection_resume_and_regular_reason(self):
        rows = [({"properties": {"번호": {"number": 18}}},
                 {"hold": {"code": "HOLD", "message": "private"}, "projection": {"private": "data"},
                  "resume": {"display_pending": True}})]
        self.assertEqual({x["reason_code"] for x in nr.collect_holds(rows)},
                         {"HOLD", "PROJECTION_PENDING", "RESUME_DISPLAY_PENDING"})
        self.assertNotIn("private", json.dumps(nr.collect_holds(rows)))

    def test_zip_single_member_and_path_traversal(self):
        for names, valid in (([nr.FILE_NAME], True), (["../" + nr.FILE_NAME], False),
                             ([nr.FILE_NAME, "extra"], False)):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as archive:
                for name in names: archive.writestr(name, json.dumps(report(1)))
            if valid: self.assertEqual(dt.report_zip(buf.getvalue()), report(1))
            else:
                with self.assertRaises(dt.Error): dt.report_zip(buf.getvalue())

    def test_disabled_missing_config_does_not_baseline(self):
        with patch.object(dt, "Ledger", side_effect=AssertionError("Must not access state")):
            self.assertEqual(dn.main([], env={}), 0)


class DiagnosticTests(unittest.TestCase):
    def test_only_fixed_classifications_are_exposed(self):
        secret = "https://discord.com/api/webhooks/123/not-a-real-secret"
        self.assertEqual(dn.diagnostic_reason(dt.Error(secret)), "unclassified")
        self.assertEqual(dn.diagnostic_reason(ValueError(secret)), "unclassified")
        self.assertEqual(dn.diagnostic_reason(dt.HTTPError(403, {"Location": secret}, secret.encode())), "http_403")
        self.assertEqual(dn.diagnostic_reason(dt.Error("Incomplete paginated response")), "pagination_count")

    def test_failure_log_has_stage_but_no_exception_payload(self):
        output = io.StringIO()
        with patch.object(dn.dt, "Discord", side_effect=ValueError("private credential")), patch("sys.stdout", output):
            self.assertEqual(dn.main([], env={"DISCORD_NOTIFICATIONS_ENABLED": "true"}), 1)
        self.assertIn("stage=configuration; reason=unclassified", output.getvalue())
        self.assertNotIn("private credential", output.getvalue())


if __name__ == "__main__": unittest.main()
