import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_project as gp
import notion_api as na
import sync_simple as ss
import sync_simple_state as state
from sync_simple_fakes import CONFIG, LEGACY, NOW, REPO, GitHub, Notion, Project, REST, checkpoint, conn, page, text


def pull():
    return {"id": "PR_81", "databaseId": 900, "legacy_issue_id": 901, "number": 81,
            "title": "Fixture PR", "url": f"https://github.com/{gp.REPOSITORY}/pull/81",
            "createdAt": NOW, "updatedAt": NOW, "mergedAt": None, "state": "OPEN",
            "isDraft": False, "baseRefName": "main",
            "baseRepository": {"id": REPO, "databaseId": gp.REPOSITORY_ID},
            "repository": {"id": REPO}, "assignees": conn([]), "labels": conn([]),
            "closingIssuesReferences": conn([{"id": "I_80", "databaseId": 800,
                                             "number": 80, "repository": {"id": REPO}}])}


class StateTests(unittest.TestCase):
    def test_ready_eligibility_table_matches_existing_contract(self):
        # Given: only an open, non-draft, main, same-repository closing ref qualifies.
        cases = [({}, True), ({"isDraft": True}, False), ({"state": "MERGED"}, False),
                 ({"baseRefName": "develop"}, False),
                 ({"baseRepository": {"id": "R_other"}}, False),
                 ({"repository": {"id": "R_other"}}, False)]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                github = GitHub()
                pr = {**pull(), **changes}
                github.issues[0]["linked_prs"] = [pr]
                snapshot = {"repository_node_id": REPO, "pulls": {901: pr}}
                # When:
                facts, _, _ = state.issue_facts(github.issues[0], snapshot)
                # Then:
                self.assertEqual(facts["ready"], expected)

    def test_same_repository_snapshot_drift_fails(self):
        # Given:
        github = GitHub()
        pr = pull()
        github.issues[0]["linked_prs"] = [pr]
        # When / Then:
        with self.assertRaises(gp.SyncError):
            state.issue_facts(github.issues[0], {"repository_node_id": REPO, "pulls": {}})

    def test_pr_identity_uses_legacy_issue_id_not_pr_database_id(self):
        # Given:
        github, notion = GitHub(), Notion()
        github.pulls = [pull()]
        pr_page = page()
        pr_page["id"] = "00000000-0000-4000-8000-000000000003"
        pr_page["properties"].update({"동기화 키": text(state.key_for(901)),
                                     "번호": {"number": 81}, "종류": {"select": {"name": "PR"}}})
        notion.pages.append(pr_page)
        # When:
        report = ss.sync(github, REST(github), Project(), notion, CONFIG)
        # Then:
        self.assertEqual([item["issue"] for item in report["items"]], [80])
        self.assertTrue(all(method in {"GET", "POST"} for method, _, _ in notion.calls))

    def test_legacy_fingerprint_matches_independent_producer_with_reordered_events(self):
        # Given: nontrivial history and links, hashed by the old engine before any new producer runs.
        github = GitHub()
        ready = pull()
        draft = {**pull(), "id": "PR_82", "number": 82, "databaseId": 902,
                 "legacy_issue_id": 903, "isDraft": True}
        github.pulls = [draft, ready]
        github.issues[0]["linked_prs"] = [draft, ready]
        github.issues[0]["reopens"] = [
            {"id": "RE_new", "createdAt": "2026-11-04T00:00:00Z"},
            {"id": "RE_old", "createdAt": "2026-11-03T00:00:00Z"}]
        row = page(internal=checkpoint(github))
        saved = json.loads(na.read_text(row, "동기화 내부 상태"))
        snapshot = {"repository_node_id": REPO, "pulls": {901: ready, 903: draft}}
        expected_fingerprint = LEGACY["complex"]["baseline"]["facts_fingerprint"]
        expected_ready_hash = LEGACY["complex"]["review_pr_hash"]
        github.issues[0]["title"] = "Changed cosmetic title"
        github.issues[0]["updatedAt"] = "2026-11-05T01:00:00Z"
        github.issues[0]["linked_prs"].reverse()
        github.issues[0]["reopens"].reverse()
        # When:
        observed = state.issue_facts(github.issues[0], snapshot)
        last, previous, _, history = state.legacy_checkpoint(row, github.issues[0], CONFIG, observed)
        # Then:
        self.assertEqual(observed[1], expected_fingerprint)
        self.assertEqual(observed[2], expected_ready_hash)
        self.assertEqual(saved["baseline"]["facts_fingerprint"], expected_fingerprint)
        self.assertEqual(last, "백로그")
        self.assertEqual(previous, {"state": "OPEN", "reason": None, "ready": True})
        self.assertEqual(history, "matching_fingerprint")

    def test_reopen_and_ready_transition_reconstruction_table(self):
        # Given:
        for last, old_ready, ready_now, target in (
                ("완료", False, False, "백로그"), ("완료", False, True, "검토 중"),
                ("진행 중", False, True, "검토 중"), ("검토 중", True, False, "진행 중")):
            with self.subTest(last=last, old_ready=old_ready, ready_now=ready_now):
                github = GitHub()
                raw = json.loads(checkpoint(github, last))
                raw["baseline"]["facts_fingerprint"] = "0" * 64
                raw["review_pr_hash"] = state.digest(["PR_old"] if old_ready else [])
                notion = Notion(last, json.dumps(raw))
                if ready_now:
                    pr = pull()
                    github.pulls = [pr]
                    github.issues[0]["closedByPullRequestsReferences"] = conn([pr])
                # When:
                report = ss.sync(github, REST(github), Project(last), notion, CONFIG)
                # Then:
                self.assertEqual(report["items"][0]["target"], target)
                self.assertEqual(report["items"][0]["history"], "reconstructed")

    def test_foreign_internal_binding_is_initialization(self):
        # Given:
        github = GitHub()
        raw = json.loads(checkpoint(github))
        raw["issue_id"] = 999
        row = page(internal=json.dumps(raw))
        # When:
        result = state.legacy_checkpoint(row, github.issues[0], CONFIG,
                                         state.issue_facts(github.issues[0],
                                                          {"repository_node_id": REPO, "pulls": {}}))
        # Then:
        self.assertEqual(result, (None, None, None, "initialization"))
