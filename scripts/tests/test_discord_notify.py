import copy
import io
import json
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


def report(n, holds=(), *, attempt=1):
    return {"schema": 1, "repository_id": dt.REPO_ID, "run_id": n, "attempt": attempt,
            "workflow_sha": SHA, "checked_out_sha": SHA, "dry_run": False,
            "kind": "partial" if holds else "complete", "scan_complete": True,
            "holds": [{"issue_number": num, "reason_code": code} for num, code in holds]}


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


class Incidents(unittest.TestCase):
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
        observed = dn.refresh_ci_runs(GH(), [failed, success, candidate], {3: candidate})
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
            def repo(inner, path): raise AssertionError("No compare for same main SHA")
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
        self.assertEqual(len(state["outbox"]), 3)  # target18 recovery and target19 first alert
        recovery = state["outbox"]["hold-recovery:18:4:1"]
        self.assertIn("남은 보류 1건", recovery["payload"]["content"])
        dn.reconcile_sync(state, run(5), report(5, [(19, "A")]), MAPPING)
        self.assertEqual(len(state["outbox"]), 3)

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
        self.assertEqual(state["holds"]["18"]["reasons"]["A"]["alert"], alert)
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
