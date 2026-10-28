"""PM-approved Draft observation in memory, plus an optional labelled test POST."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import discord_notify as dn
import discord_transport as dt

RESULT = Path("probe-result.json")


class ReadOnlyGitHub(dt.GitHub):
    def call(self, path, *, method="GET", payload=None):
        if method != "GET" or payload is not None:
            raise dt.Error("Probe GitHub mutation forbidden")
        return super().call(path, method=method, payload=payload)


class MemoryLedger:
    def __init__(self, state):
        if state is None:
            raise dt.Error("Probe requires existing ledger")
        self.state = copy.deepcopy(dt.validate_state(state))

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, state, *, bootstrap=False):
        if bootstrap:
            raise dt.Error("Probe bootstrap forbidden")
        data = dt.canonical(dt.validate_state(state))
        if len(data) > dt.MAX_STATE:
            raise dt.Error("Ledger size limit exceeded")
        self.state = copy.deepcopy(state)


class PreviewDiscord:
    def __init__(self, real, state):
        self.real = real
        self.ids = {item["message_id"] for item in state["outbox"].values() if item["message_id"]}
        self.next_id = 10**19
        self.count = 0

    def verify(self):
        self.real.verify()

    def send(self, payload):
        while str(self.next_id) in self.ids:
            self.next_id += 1
        if self.next_id >= 10**20:
            raise dt.Error("Synthetic receipt limit exceeded")
        mid = str(self.next_id)
        self.ids.add(mid)
        self.next_id += 1
        self.count += 1
        return mid


def context(env):
    sha = env.get("GITHUB_SHA", "")
    ref = env.get("GITHUB_REF", "")
    actor = env.get("GITHUB_ACTOR", "")
    if (env.get("GITHUB_REPOSITORY") != dt.REPO or
            env.get("GITHUB_REPOSITORY_ID") != str(dt.REPO_ID) or
            env.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or
            env.get("DISCORD_MODE") != "probe" or not ref.startswith("refs/heads/") or
            ref == "refs/heads/main" or not dn.valid_sha(sha) or
            env.get("DISCORD_APPROVED_SHA") != sha or
            env.get("GITHUB_ACTOR_ID") != dn.PM_ID or not actor or
            actor != env.get("GITHUB_TRIGGERING_ACTOR") or env.get("GITHUB_RUN_ATTEMPT") != "1" or
            not re.fullmatch(r"[1-9][0-9]{0,19}", env.get("GITHUB_RUN_ID", "")) or
            not re.fullmatch(r"[1-9][0-9]{0,9}", env.get("DISCORD_DRAFT_PR", "")) or
            env.get("DISCORD_SEND_TEST") not in {"true", "false"} or
            any(env.get(key) for key in ("DISCORD_RESOLVE_KEY", "DISCORD_RESOLVE_MESSAGE_ID"))):
        raise dt.Error("Invalid Draft dispatch")
    return {"sha": sha, "branch": ref.removeprefix("refs/heads/"),
            "pr": int(env["DISCORD_DRAFT_PR"]), "run_id": int(env["GITHUB_RUN_ID"])}


def verify_pr(gh, ctx):
    pull = gh.repo(f"/pulls/{ctx['pr']}")
    head, base = pull.get("head") or {}, pull.get("base") or {}
    for repo in (head.get("repo") or {}, base.get("repo") or {}):
        if type(repo.get("id")) is not int or repo["id"] != dt.REPO_ID:
            raise dt.Error("Draft repository mismatch")
    if (type(pull.get("number")) is not int or pull["number"] != ctx["pr"] or
            pull.get("state") != "open" or pull.get("draft") is not True or
            head.get("sha") != ctx["sha"] or head.get("ref") != ctx["branch"] or base.get("ref") != "main"):
        raise dt.Error("Draft state changed")


def ledger_commit(gh):
    sha = gh.repo(f"/git/ref/heads/{dt.BRANCH}")["object"]["sha"]
    if not dn.valid_sha(sha):
        raise dt.Error("Invalid ledger commit")
    return sha


def save_result(result):
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(env=None):
    env = dict(os.environ if env is None else env)
    # Optional Project lookup creates an independent GraphQL client: exclude it too.
    for key in ("DISCORD_PROJECT_READ_TOKEN", "PROJECT_TOKEN", "NOTION_TOKEN"):
        env[key] = ""
    result = {"schema": 1, "stage": "preflight", "observation": "not_started",
              "simulated_deliveries": 0, "ledger_unchanged": None,
              "test": {"status": "not_requested", "message_id": None}}
    try:
        ctx = context(env)
        checked_out = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        if checked_out != ctx["sha"]:
            raise dt.Error("Draft checkout mismatch")
        gh = ReadOnlyGitHub(env.get("GITHUB_TOKEN", ""))
        verify_pr(gh, ctx)
        result.update(pr=ctx["pr"], sha=ctx["sha"], run_id=ctx["run_id"])
        before = ledger_commit(gh)
        memory = MemoryLedger(dt.Ledger(gh).load())
        real = dt.Discord(env.get("DISCORD_WEBHOOK_URL", ""), env.get("DISCORD_CHANNEL_ID", ""))
        preview = PreviewDiscord(real, memory.state)
        result["stage"] = "observation"
        env["DISCORD_NOTIFICATIONS_ENABLED"] = "true"  # Private copy, no Actions setting write.
        code = dn.main([], env=env, github_factory=lambda token: gh,
                       ledger_factory=lambda github: memory,
                       discord_factory=lambda url, channel: preview)
        result["simulated_deliveries"] = preview.count
        result["observation"] = "passed" if code == 0 else "failed"
        result["ledger_unchanged"] = before == ledger_commit(gh)
        if code or not result["ledger_unchanged"]:
            raise dt.Error("Probe observation failed")
        if env["DISCORD_SEND_TEST"] == "true":
            result["stage"] = "test_preflight"
            mapping = dn.user_map(env.get("DISCORD_USER_MAP", "{}"))
            if dn.PM.casefold() not in mapping:
                raise dt.Error("Test PM mapping missing")
            # Observation may take minutes: bind current Draft again immediately before POST.
            real.verify()
            verify_pr(gh, ctx)
            key = f"probe-test:{ctx['pr']}:{ctx['sha']}:{ctx['run_id']}:1"
            message = dn.seal_message(key, dn.payload(
                f"Draft PR #{ctx['pr']} 검증 · 테스트 알림",
                f"PM 멘션 확인\n검증 SHA: {ctx['sha']}\n실행 ID: {ctx['run_id']}",
                dn.object_url(ctx["pr"], pull=True), mapping, recipients=[dn.PM]))
            result["stage"] = "test_delivery"
            result["test"].update(status="pending", payload_hash=dt.digest(message))
            save_result(result)  # Preserve local uncertainty before POST, always upload separately.
            try:
                mid = real.send(message, retry_rate_limit=False)
            except dt.HTTPError:
                result["test"]["status"] = "rejected"
                raise
            except Exception:
                result["test"]["status"] = "unknown"
                raise
            result["test"].update(status="confirmed", message_id=mid)
            save_result(result)
            result["ledger_unchanged"] = before == ledger_commit(gh)
            if not result["ledger_unchanged"]:
                raise dt.Error("Probe ledger changed")
        result["stage"] = "complete"
        return 0
    except Exception as error:
        result["reason"] = dn.diagnostic_reason(error)
        print(f"::error::Draft probe failed: stage={result['stage']}; reason={result['reason']}")
        return 1
    finally:
        save_result(result)


if __name__ == "__main__":
    sys.exit(main())
