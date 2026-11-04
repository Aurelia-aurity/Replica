"""Synthetic payload and HTTP boundary tests; no external calls or real sleeps."""
import html
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discord_alerts as da
import discord_events as de

SHA = "a" * 40
UID = "111111111111111111"
MAP = {"reviewer": UID}
WEBHOOK = "https://discord.com/api/webhooks/123/fake"


def event(action="opened", *, pull=True, **changes):
    obj = {"number": 79, "title": "title @everyone <@123> *bold*\nnew",
           "draft": False, "merged": False, "requested_reviewers": [],
           "requested_teams": [], **changes}
    return {"repository": {"full_name": da.REPO}, "action": action,
            "pull_request" if pull else "issue": obj, "before": SHA}


class API:
    def __init__(self, reviews=(), *, error=False):
        self.reviews = reviews
        self.calls = []
        self.error = error

    def pages(self, path, key=None):
        self.calls.append(path)
        if self.error:
            raise da.Error("API unavailable")
        return iter(self.reviews)


class Events(unittest.TestCase):
    def message(self, value, *, pull=True, api=None):
        return de.event_message("pull_request_target" if pull else "issues",
                                value, MAP, api or API())

    def test_lifecycle_message_kinds_and_draft_inclusion(self):
        cases = [
            (False, "opened", {}, "Issue 생성\nProject: " + da.PROJECT_URL),
            (False, "closed", {"state_reason": "completed"}, "Issue 종료 · 완료"),
            (False, "closed", {"state_reason": "not_planned"}, "Issue 종료 · 미계획"),
            (False, "closed", {"state_reason": "duplicate"}, "Issue 종료 · 중복"),
            (False, "closed", {"state_reason": None}, "Issue 종료"),
            (False, "closed", {"state_reason": "other"}, "Issue 종료"),
            (False, "reopened", {}, "재오픈"),
            (True, "opened", {"draft": True}, "PR 생성"),
            (True, "closed", {"draft": True, "merged": False}, "병합 없이 PR 종료"),
            (True, "closed", {"merged": True}, "PR 병합"),
            (True, "reopened", {"draft": True}, "재오픈"),
        ]
        for pull, action, changes, expected in cases:
            with self.subTest(action=action, changes=changes, pull=pull):
                # Given a GitHub lifecycle payload, when rendered, then shipped copy matches.
                message = self.message(event(action, pull=pull, **changes), pull=pull)
                lines = message["content"].splitlines()
                self.assertEqual("\n".join(lines[1:-1]), expected)
                self.assertIn("Replica · " + ("PR" if pull else "Issue") + " #79", lines[0])
                self.assertNotIn("@everyone", message["content"])
                self.assertNotIn("<@123>", message["content"])
                self.assertLess(len(message["content"]), 1900)
                self.assertEqual(message["allowed_mentions"]["parse"], [])
                self.assertEqual(message["allowed_mentions"]["users"], [])

    def test_review_requested_uses_payload_without_refetch(self):
        value = event("review_requested")
        value["requested_reviewer"] = {"login": "Reviewer"}
        api = API(error=True)
        message = self.message(value, api=api)
        self.assertEqual(message["allowed_mentions"]["users"], [UID])
        self.assertEqual(message["content"].splitlines()[2],
                         "리뷰 요청 · 변경 사항을 확인하고 리뷰해주세요.")
        self.assertEqual(api.calls, [])

    def test_unmapped_user_and_team_never_allow_mentions(self):
        for field, identity in [("requested_reviewer", {"login": "unknown"}),
                                ("requested_team", {"name": "@everyone", "slug": "team"})]:
            with self.subTest(field=field):
                value = event("review_requested")
                value[field] = identity
                message = self.message(value)
                self.assertEqual(message["allowed_mentions"]["users"], [])
                self.assertNotIn("@everyone", message["content"])

    def test_ready_mentions_all_users_and_team_names(self):
        value = event("ready_for_review", requested_reviewers=[
            {"login": "reviewer"}, {"login": "unknown"}],
            requested_teams=[{"slug": "team", "name": "Team"}])
        message = self.message(value)
        self.assertEqual(message["allowed_mentions"]["users"], [UID])
        self.assertIn("unknown", message["content"])
        self.assertEqual(message["content"].splitlines()[2],
                         "리뷰 요청 · Ready로 전환됐습니다. 변경 사항을 확인하고 리뷰해주세요. 팀: Team")

    def test_empty_ready_and_all_draft_review_actions_are_silent(self):
        for action in ("review_requested", "ready_for_review", "synchronize"):
            with self.subTest(action=action):
                api = API(error=True)
                self.assertIsNone(self.message(event(action, draft=True), api=api))
                self.assertEqual(api.calls, [])
        self.assertIsNone(self.message(event("ready_for_review")))

    def test_first_push_matches_before_and_later_approval_overrides(self):
        reviews = [
            {"id": 4, "user": {"login": "reviewer"}, "state": "COMMENTED", "commit_id": SHA},
            {"id": 1, "user": {"login": "reviewer"}, "state": "CHANGES_REQUESTED", "commit_id": SHA},
            {"id": 2, "user": {"login": "old"}, "state": "CHANGES_REQUESTED", "commit_id": "b" * 40},
            {"id": 5, "user": {"login": "approved"}, "state": "APPROVED", "commit_id": SHA},
            {"id": 3, "user": {"login": "approved"}, "state": "CHANGES_REQUESTED", "commit_id": SHA},
        ]
        api = API(reviews)
        message = self.message(event("synchronize"), api=api)
        self.assertEqual(message["allowed_mentions"]["users"], [UID])
        self.assertEqual(message["content"].splitlines()[2],
                         "수정 반영 · 변경 요청에 대한 새 커밋이 올라왔습니다. 재검토해주세요.")
        self.assertEqual(api.calls, [f"/repos/{da.REPO}/pulls/79/reviews"])

    def test_later_push_and_dismissal_are_silent(self):
        for state, commit in [("CHANGES_REQUESTED", "b" * 40), ("DISMISSED", SHA)]:
            with self.subTest(state=state):
                reviews = [{"id": 1, "user": {"login": "reviewer"},
                            "state": state, "commit_id": commit}]
                self.assertIsNone(self.message(event("synchronize"), api=API(reviews)))

    def test_latest_review_is_submission_order_not_draft_creation_order(self):
        reviews = [
            {"id": 10, "user": {"login": "reviewer"}, "state": "CHANGES_REQUESTED",
             "commit_id": SHA, "submitted_at": "2026-11-02T10:00:00Z"},
            {"id": 9, "user": {"login": "reviewer"}, "state": "APPROVED",
             "commit_id": SHA, "submitted_at": "2026-11-02T11:00:00Z"},
        ]
        self.assertIsNone(self.message(event("synchronize"), api=API(reviews)))

    def test_api_failure_propagates_for_rerun(self):
        with self.assertRaises(da.Error):
            self.message(event("synchronize"), api=API(error=True))

    def test_github_api_request_has_explicit_user_agent(self):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return b"{}"

        def opener(request, timeout):
            requests.append(request)
            return Response()

        client = da.Client("token", opener=opener)
        self.assertEqual(client.download(f"/repos/{da.REPO}/pulls/79"), b"{}")
        self.assertEqual(requests[0].get_header("User-agent"), da.USER_AGENT)

    def test_main_modes_and_failure_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "event.json"
            source.write_text(json.dumps(event("synchronize")), encoding="utf-8")
            env = {"DISCORD_EVENTS_MODE": "live", "GITHUB_EVENT_PATH": str(source),
                   "GITHUB_REPOSITORY": da.REPO, "GITHUB_EVENT_NAME": "pull_request_target"}
            self.assertEqual(de.main(env, client=API(error=True)), 1)
            env["DISCORD_EVENTS_MODE"] = "off"
            self.assertEqual(de.main(env), 0)
            env["DISCORD_EVENTS_MODE"] = "typo"
            self.assertEqual(de.main(env), 1)

    def test_main_diagnostics_show_safe_errors_and_hide_raw_exception_details(self):
        token = "FAKE_EVENT_TOKEN"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "event.json"
            source.write_text(json.dumps(event()), encoding="utf-8")
            env = {"DISCORD_EVENTS_MODE": "live", "GITHUB_EVENT_PATH": str(source),
                   "GITHUB_REPOSITORY": da.REPO, "GITHUB_EVENT_NAME": "pull_request_target",
                   "DISCORD_WEBHOOK_URL": WEBHOOK, "GITHUB_TOKEN": token}
            for failure in (da.Error("Safe delivery failure"), ValueError(f"{WEBHOOK} {token}"),
                            KeyError(f"{WEBHOOK} {token}"), OSError(f"{WEBHOOK} {token}"),
                            TypeError(f"{WEBHOOK} {token}"), RuntimeError(f"{WEBHOOK} {token}"),
                            AttributeError(f"{WEBHOOK} {token}")):
                with self.subTest(exception=type(failure).__name__):
                    output = io.StringIO()

                    def send(message, environment):
                        raise failure

                    with redirect_stdout(output):
                        status = de.main(env, send=send)
                    self.assertEqual(status, 1)
                    detail = str(failure) if isinstance(failure, da.Error) else type(failure).__name__
                    self.assertIn(f"Discord event alert failed: {detail}", output.getvalue())
                    self.assertNotIn(WEBHOOK, output.getvalue())
                    self.assertNotIn(token, output.getvalue())


class Delivery(unittest.TestCase):
    def setUp(self):
        self.message = da.payload("title", "description",
                                  f"https://github.com/{da.REPO}/issues/79", MAP,
                                  recipients=("reviewer",))
        self.requests = []

    def opener(self, request, timeout):
        self.requests.append((request, timeout))
        return io.BytesIO(b'{"id":"1"}')

    def test_off_and_shadow_summary_never_use_live_webhook(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary"
            env = {"DISCORD_WEBHOOK_URL": WEBHOOK, "GITHUB_STEP_SUMMARY": str(summary)}
            da.deliver(self.message, env, opener=self.opener)
            self.assertFalse(summary.exists())
            env["DISCORD_EVENTS_MODE"] = "shadow"
            da.deliver(self.message, env, opener=self.opener)
            self.assertIn("&quot;allowed_mentions&quot;", summary.read_text())
            self.assertEqual(self.requests, [])

    def test_public_shadow_summary_hides_mention_and_allowed_user_ids(self):
        other_uid = "222222222222222222"
        message = da.payload("title " + UID, "description",
                             f"https://github.com/{da.REPO}/issues/79",
                             {**MAP, "other": other_uid}, recipients=("reviewer", "other"))
        original = json.dumps(message)
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary"
            env = {"DISCORD_EVENTS_MODE": "shadow", "GITHUB_STEP_SUMMARY": str(summary)}
            da.deliver(message, env, opener=self.opener)
            rendered = summary.read_text()
            self.assertNotIn(UID, rendered)
            self.assertNotIn(other_uid, rendered)
            decoded = json.loads(html.unescape(rendered).removeprefix("<pre>").removesuffix("</pre>\n"))
            self.assertEqual(decoded["allowed_mentions"]["users"],
                             ["[Discord user]", "[Discord user]"])
            self.assertNotIn("<@", decoded["content"])
            self.assertIn("[Discord user]", decoded["content"])
            self.assertEqual(json.dumps(message), original)
            self.assertEqual(self.requests, [])

    def test_shadow_summary_masks_prefix_overlapping_ids_as_complete_values(self):
        short_uid, long_uid = "11111111111111111", "11111111111111111222"
        mapping = {"short": short_uid, "long": long_uid}
        for recipients in (("short", "long"), ("long", "short")):
            with self.subTest(recipients=recipients), tempfile.TemporaryDirectory() as directory:
                message = da.payload("title", f"{short_uid} {long_uid}",
                                     f"https://github.com/{da.REPO}/issues/79",
                                     mapping, recipients=recipients)
                original = json.dumps(message)
                summary = Path(directory) / "summary"
                env = {"DISCORD_EVENTS_MODE": "shadow", "GITHUB_STEP_SUMMARY": str(summary)}
                da.deliver(message, env, opener=self.opener)
                decoded = json.loads(html.unescape(summary.read_text())
                                     .removeprefix("<pre>").removesuffix("</pre>\n"))
                self.assertEqual(decoded["allowed_mentions"]["users"],
                                 ["[Discord user]", "[Discord user]"])
                self.assertEqual(decoded["content"].splitlines()[0],
                                 "[Discord user] [Discord user]")
                self.assertEqual(decoded["content"].splitlines()[2],
                                 "[Discord user] [Discord user]")
                self.assertEqual(json.dumps(message), original)
                self.assertEqual(self.requests, [])

    def test_live_team_pm_fallback_and_shadow_routes(self):
        for pm, extra, expected in [
            (False, {}, WEBHOOK), (True, {}, WEBHOOK),
            (True, {"DISCORD_PM_WEBHOOK_URL": WEBHOOK + "pm"}, WEBHOOK + "pm"),
            (True, {"DISCORD_EVENTS_MODE": "shadow",
                    "DISCORD_SHADOW_WEBHOOK_URL": WEBHOOK + "shadow"}, WEBHOOK + "shadow")]:
            with self.subTest(pm=pm, extra=extra):
                env = {"DISCORD_EVENTS_MODE": "live", "DISCORD_WEBHOOK_URL": WEBHOOK, **extra}
                da.deliver(self.message, env, pm=pm, opener=self.opener)
                request, timeout = self.requests[-1]
                self.assertEqual(request.full_url, expected + "?wait=true")
                self.assertEqual(json.loads(request.data), self.message)
                self.assertEqual(request.get_header("User-agent"), da.USER_AGENT)
                self.assertEqual(timeout, 30)

    def test_retry_429_and_5xx_bounded_with_injected_sleep(self):
        calls, delays = [], []
        outcomes = [429, 502, 200]

        def opener(request, timeout):
            status = outcomes[len(calls)]
            calls.append(request)
            if status != 200:
                raise urllib.error.HTTPError(request.full_url, status, "error", {},
                                             io.BytesIO(b'{"retry_after":120}'))
            return io.BytesIO(b"{}")

        da.post(WEBHOOK, self.message, opener=opener, sleep=delays.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, [60, 1])

    def test_final_failure_and_nonretryable_http_and_network_errors(self):
        for status, count in [(500, 3), (429, 3), (400, 1), (401, 1), (None, 1)]:
            calls, delays = [], []

            def opener(request, timeout):
                calls.append(request)
                if status is None:
                    raise urllib.error.URLError("SECRET")
                raise urllib.error.HTTPError(request.full_url, status, "SECRET", {},
                                             io.BytesIO(b"{}"))

            with self.subTest(status=status), self.assertRaises(da.Error) as error:
                da.post(WEBHOOK, self.message, opener=opener, sleep=delays.append)
            self.assertEqual(len(calls), count)
            self.assertNotIn("SECRET", str(error.exception))
            self.assertNotIn(WEBHOOK, str(error.exception))

    def test_mapping_and_payload_boundaries(self):
        self.assertEqual(da.user_map('{"Reviewer":"' + UID + '"}'), MAP)
        for raw in ('[]', '{"reviewer":"<@123>"}', '{"A":"' + UID + '","a":"' + UID + '"}'):
            with self.subTest(raw=raw), self.assertRaises(da.Error):
                da.user_map(raw)
        with self.assertRaises(da.Error):
            da.payload("x" * 1900, "", f"https://github.com/{da.REPO}/issues/79", {})
        with self.assertRaises(da.Error):
            da.payload("title", "", "https://example.com", {})

    def test_main_live_and_shadow_and_delivery_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps(event()), encoding="utf-8")
            env = {"GITHUB_EVENT_PATH": str(path), "GITHUB_REPOSITORY": da.REPO,
                   "GITHUB_EVENT_NAME": "pull_request_target", "DISCORD_EVENTS_MODE": "live"}
            sent = []
            self.assertEqual(de.main(env, send=lambda message, env: sent.append(message)), 0)
            self.assertEqual(len(sent), 1)
            env.update(DISCORD_EVENTS_MODE="shadow", GITHUB_STEP_SUMMARY=str(Path(directory) / "summary"))
            self.assertEqual(de.main(env), 0)
            env.update(DISCORD_EVENTS_MODE="live")
            self.assertEqual(de.main(env), 1)


class GitHubClient(unittest.TestCase):
    def test_ref_existence_distinguishes_404_and_caches_completed_results(self):
        path = f"/repos/{da.REPO}/git/ref/tags/main"
        for status, expected in ((404, False), (200, True)):
            with self.subTest(status=status):
                calls, bodies = [], []

                def opener(request, timeout):
                    calls.append(request)
                    body = io.BytesIO(b'{"ref":"refs/tags/main"}')
                    bodies.append(body)
                    if status == 404:
                        raise urllib.error.HTTPError(request.full_url, status, "SECRET", {}, body)
                    return body

                client = da.Client("synthetic-token", opener=opener)
                results = [client.exists(path), client.exists(path)]
                self.assertEqual(results, [expected, expected])
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].full_url, "https://api.github.com" + path)
                self.assertEqual(calls[0].get_method(), "GET")
                self.assertTrue(all(body.closed for body in bodies))

    def test_ref_lookup_errors_raise_safe_error_instead_of_implying_absence(self):
        path = f"/repos/{da.REPO}/git/ref/tags/main"
        for status in (403, 429, 503, None):
            with self.subTest(status=status):
                body = io.BytesIO(b"SECRET")
                self.addCleanup(body.close)

                def opener(request, timeout):
                    if status is None:
                        raise urllib.error.URLError("SECRET")
                    raise urllib.error.HTTPError(request.full_url, status, "SECRET", {}, body)

                with self.assertRaises(da.Error) as error:
                    da.Client("SECRET", opener=opener).exists(path)
                self.assertNotIn("SECRET", str(error.exception))
                if status is not None:
                    self.assertTrue(body.closed)

    def test_paginated_reviews_and_authenticated_get_only(self):
        calls = []

        def opener(request, timeout):
            calls.append(request)
            rows = [{"id": n} for n in range(100)] if len(calls) == 1 else [{"id": 100}]
            return io.BytesIO(json.dumps(rows).encode())

        api = da.Client("synthetic-token", opener=opener)
        rows = list(api.pages(f"/repos/{da.REPO}/pulls/79/reviews"))
        self.assertEqual(len(rows), 101)
        self.assertIn("page=2", calls[1].full_url)
        self.assertEqual(calls[0].get_method(), "GET")
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer synthetic-token")

    def test_artifact_redirect_strips_authorization(self):
        request = urllib.request.Request("https://api.github.com/artifact", headers={
            "Authorization": "Bearer synthetic-token"})
        redirected = da.ArtifactRedirect().redirect_request(
            request, None, 302, "", {}, "https://example.com/signed")
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaises(da.Error):
            da.ArtifactRedirect().redirect_request(request, None, 302, "", {}, "http://example.com")

    def test_api_http_failure_never_exposes_token_or_response_text(self):
        body = io.BytesIO(b"SECRET")

        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "SECRET", {},
                                         body)

        with self.assertRaises(da.Error) as error:
            da.Client("SECRET", opener=opener).get(f"/repos/{da.REPO}/pulls/79")
        self.assertNotIn("SECRET", str(error.exception))
        self.assertTrue(body.closed)


class Workflows(unittest.TestCase):
    def test_privileged_alert_workflows_only_check_out_base_sha(self):
        root = Path(__file__).resolve().parents[2]
        for filename in ("discord-events.yml", "discord-run-alerts.yml"):
            with self.subTest(filename=filename):
                content = (root / ".github/workflows" / filename).read_text()
                self.assertRegex(content, r"actions/checkout@[0-9a-f]{40} # v4\n"
                                 r"        with:\n"
                                 r"          ref: \$\{\{ github.sha \}\}\n"
                                 r"          persist-credentials: false")
                self.assertNotIn("concurrency:", content)
                self.assertNotIn(": write", content)
                for line in content.splitlines():
                    if line.strip().startswith("run:"):
                        self.assertNotIn("${{", line)


if __name__ == "__main__":
    unittest.main()
