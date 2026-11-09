"""Trusted-main, one-time claimed ordinary sync; no PM resume inputs."""

from dataclasses import asdict
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit

import github_project as gp
import notification_report as nr
import notion_api as na
import sync_simple as ss
from sync_simple_apply import project_snapshot
from sync_simple_state import CHECKPOINT, issue_facts, read_checkpoint
from ordinary_claim import OrdinaryClaim

WORKFLOW_PATH = ".github/workflows/notion-sync-request.yml"
GENESIS = "0" * 64
EVIDENCE_FILE = "notion-relay-evidence.json"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))




class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PinnedRelay:
    domain = "replica-workflow-v1"
    key_name = "WORKFLOW_CALLBACK_KEY"
    paths = {"/claim", "/result/chunk", "/result"}
    def __init__(self, env, *, opener=None, clock=time.time):
        parsed = urlsplit(env.get("NOTION_RELAY_URL", ""))
        gp.require(parsed.scheme == "https" and
                   re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev", parsed.hostname or "") and
                   parsed.hostname == env.get("NOTION_RELAY_HOST") and
                   parsed.port is None and not parsed.username and not parsed.password and
                   parsed.path in ("", "/") and not parsed.query and not parsed.fragment,
                   "일반 실행 요청 relay 주소 결속 오류")
        self.base = f"https://{parsed.hostname}"
        self.key = env.get(self.key_name, "")
        gp.require(isinstance(self.key, str) and len(self.key) >= 32,
                   "일반 실행 결과 키가 설정되지 않았습니다")
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.clock = clock

    def post(self, path, value):
        gp.require(path in self.paths, "일반 실행 API 경로 오류")
        raw = canonical(value).encode("utf-8")
        gp.require(len(raw) <= 65536, "일반 실행 API 본문 크기 초과")
        stamp = str(int(self.clock()))
        signature = hmac.new(self.key.encode(),
            f"{self.domain}\nPOST\n{path}\n{stamp}\n".encode() + raw,
            hashlib.sha256).hexdigest()
        request = urllib.request.Request(self.base + path, data=raw, method="POST", headers={
            "Content-Type": "application/json", "X-Replica-Timestamp": stamp,
            # workers.dev rejects the default Python-urllib agent (Cloudflare error 1010).
            "User-Agent": "replica-sync-client", "X-Replica-Signature": signature})
        try:
            with self.opener.open(request, timeout=30) as response:
                gp.require(response.status == 200, "일반 실행 결과 수신 실패")
                body = response.read(65537)
                gp.require(len(body) <= 65536, "일반 실행 응답 크기 초과")
                value = json.loads(body)
                gp.require(isinstance(value, dict), "일반 실행 응답 형식 오류")
                return value
        except Exception:
            raise gp.SyncError("일반 실행 전달 결과 미확인; 같은 실행을 재실행하지 마세요") from None


def claim_context(env, rest, relay, checkout_sha):
    request_id = env.get("RELAY_REQUEST_ID", "")
    gp.require(na.identifier(request_id) == request_id, "일반 요청 UUID 형식 오류")
    for name in ("RELAY_GENERATION", "GITHUB_RUN_ID", "GITHUB_DISPATCH_ACTOR_ID"):
        gp.require(re.fullmatch(r"[1-9][0-9]*", env.get(name, "")) is not None,
                   "일반 실행 번호 형식 오류")
    sha = env.get("GITHUB_SHA", "")
    gp.require(re.fullmatch(r"[0-9a-f]{40}", sha) is not None and checkout_sha == sha,
               "일반 실행 checkout SHA 불일치")
    context = OrdinaryClaim(request_id, int(env["RELAY_GENERATION"]), int(env["GITHUB_RUN_ID"]),
                            sha, int(env["GITHUB_DISPATCH_ACTOR_ID"]))
    context.validate(env)
    run = rest.request(f"/repos/{gp.REPOSITORY}/actions/runs/{context.run_id}")
    gp.require(run.get("id") == context.run_id and run.get("repository", {}).get("id") == gp.REPOSITORY_ID and
               run.get("head_repository", {}).get("id") == gp.REPOSITORY_ID and
               run.get("head_branch") == "main" and run.get("head_sha") == sha and
               run.get("event") == "workflow_dispatch" and run.get("run_attempt") == 1 and
               run.get("path") == WORKFLOW_PATH and
               str(run.get("workflow_id")) == env.get("GITHUB_WORKFLOW_ID") and
               run.get("display_title") == f"replica-sync-request:{request_id}:{context.generation}" and
               run.get("actor", {}).get("id") == context.actor_id and
               run.get("triggering_actor", {}).get("id") == context.actor_id,
               "일반 실행의 GitHub 독립 결속 검증 실패")
    response = relay.post("/claim", context.body())  # Exactly one authorization attempt.
    gp.require(set(response) == {"request_id", "newly_authorized", "receipts", "project_id", "repository_id"} and
               response["request_id"] == request_id and response["newly_authorized"] is True and
               response["repository_id"] == gp.REPOSITORY_ID and response["project_id"] == gp.EXPECTED_PROJECT_ID and
               isinstance(response["receipts"], list) and 0 <= len(response["receipts"]) <= 10 and
               all(isinstance(item, str) and len(item) <= 128 for item in response["receipts"]) and
               len(set(response["receipts"])) == len(response["receipts"]),
               "일반 실행 claim은 신규 허가가 아닙니다; 재실행하지 마세요")
    return context


def collect(entries, notion, facts, project, config):
    """Recheck only engine-confirmed entries; never learn a new latest value."""
    result = []
    for entry in sorted(entries, key=lambda value: value["page_id"]):
        try:
            page = notion.request("GET", f"/pages/{entry['page_id']}")
            na.bound_page(page, config["notion_source_id"])
            if (na.identifier(page["id"]) != entry["page_id"] or
                    any(page.get(name) for name in ("archived", "in_trash", "is_archived")) or
                    na.read_select(page, "종류") != "Issue" or
                    na.read_text(page, "동기화 키") != entry["key"] or
                    na.read_select(page, "작업 상태") != entry["status"] or
                    hashlib.sha256(na.read_text(page, CHECKPOINT).encode()).hexdigest() != entry["state_sha256"]):
                continue
            issue_id = int(entry["key"].rsplit(":", 1)[1])
            issue = facts["issues"].get(issue_id)
            item = project["items"].get(issue_id)
            saved = read_checkpoint(page)
            reflection = bool(issue and item and saved and
                not project["archived_items"].get(issue_id) and item["id"] == entry["item_id"] and
                item["content_id"] == issue["id"] and
                item["status_option_id"] == config["status_options"][entry["status"]] and
                saved == (entry["status"], issue_facts(issue, facts)[0]))
            result.append({**entry, "reflection": reflection})
        except (gp.SyncError, KeyError, TypeError, AttributeError, ValueError):
            continue
    return result


def evidence_payloads(context, entries, report):
    nr.validate(report, run_id=context.run_id, attempt=1, sha=context.sha)
    gp.require(len(entries) <= 20000, "일반 실행 관측 기록 용량 초과")
    count = (len(entries) + 9) // 10
    previous = GENESIS
    chunks = []
    for index in range(count):
        value = {**context.body(), "index": index, "chunk_count": count,
                 "previous_digest": previous, "entries": entries[index * 10:(index + 1) * 10]}
        previous = hashlib.sha256(canonical(value).encode()).hexdigest()
        value["chunk_digest"] = previous
        gp.require(len(canonical(value).encode()) <= 65536, "일반 실행 관측 조각 크기 초과")
        chunks.append(value)
    result = {**context.body(), "chunk_count": count, "entry_count": len(entries),
              "final_digest": previous, "report_digest": hashlib.sha256(canonical(report).encode()).hexdigest(),
              "scan_complete": report["scan_complete"], "failed": report["kind"] in {"failed", "skipped"}}
    return chunks, result


def save_evidence(context, entries, report):
    value = {"schema": 1, "context": asdict(context), "entries": entries, "report": report}
    evidence_payloads(context, entries, report)
    raw = canonical(value).encode()
    gp.require(len(raw) <= 16000000, "일반 실행 결과 기록 용량 초과")
    destination = Path(EVIDENCE_FILE)
    temporary = destination.with_name(destination.name + ".tmp")
    with open(temporary, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)


def deliver_evidence(relay, context, entries, report, *, sleep=time.sleep):
    chunks, seal = evidence_payloads(context, entries, report)
    for path, value in [("/result/chunk", value) for value in chunks] + [("/result", seal)]:
        for attempt in range(3):
            try:
                response = relay.post(path, value)
                gp.require(response.get("stored" if path.endswith("chunk") else "sealed") is True,
                           "일반 실행 결과 확인 형식 오류")
                break
            except Exception:
                if attempt == 2:
                    raise
                sleep(2 ** attempt)


def recover_evidence(path, env):
    path = Path(path)
    gp.require(path.name == EVIDENCE_FILE and not path.is_symlink() and
               path.stat().st_size <= 16000000, "일반 실행 결과 파일 형식 오류")
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    gp.require(isinstance(value, dict) and set(value) == {"schema", "context", "entries", "report"} and
               value["schema"] == 1 and isinstance(value["context"], dict) and
               set(value["context"]) == set(OrdinaryClaim.__dataclass_fields__),
               "일반 실행 결과 기록 형식 오류")
    context = OrdinaryClaim(**value["context"])
    gp.require(na.identifier(context.request_id) == context.request_id and
               type(context.generation) is int and context.generation > 0 and
               type(context.run_id) is int and context.run_id > 0 and
               isinstance(value["entries"], list), "일반 실행 결과 결속 오류")
    # Evidence-only retry. The Worker independently verifies the original run;
    # no claim or engine invocation appears on this recovery path.
    deliver_evidence(PinnedRelay(env), context, value["entries"], value["report"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deliver-evidence", default="")
    args = parser.parse_args(argv)
    env = os.environ
    context = None
    observations = None
    try:
        if args.deliver_evidence:
            recover_evidence(args.deliver_evidence, env)
            return 0
        config = ss.load_config(env)
        github = gp.GraphQLClient(env["GITHUB_TOKEN"])
        rest = gp.RESTClient(env["GITHUB_TOKEN"])
        project_client = gp.GraphQLClient(env["PROJECT_TOKEN"])
        notion = na.NotionClient(env["NOTION_TOKEN"], allow_apply=True)
        relay = PinnedRelay(env)
        checkout = subprocess.run(["git", "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
        context = claim_context(env, rest, relay, checkout)
        observations = []
        result = ss.sync(github, rest, project_client, notion, config, apply=True, env=env,
                         evidence=observations, ordinary_claim=context)
        failed = result["counts"]["failed"] > 0
        nr.write(nr.FILE_NAME, env, kind=ss.notification_kind(result),
                 scan_complete=not failed, holds=[])
        with open(nr.FILE_NAME, encoding="utf-8") as source:
            report = nr.validate(json.load(source), run_id=context.run_id, attempt=1, sha=context.sha)
        facts = gp.fetch_repository_facts(github, rest)
        project = project_snapshot(project_client, config, facts, refresh=True)
        entries = collect(observations, notion, facts, project, config)
        save_evidence(context, entries, report)
        deliver_evidence(relay, context, entries, report)
        Path("sync-simple-plan.json").write_text(json.dumps(result, ensure_ascii=False) + "\n",
                                                encoding="utf-8")
        print(json.dumps(result["counts"], ensure_ascii=False, sort_keys=True))
        return 1 if failed else 0
    except Exception:
        if context is not None and observations is not None and not Path(EVIDENCE_FILE).exists():
            try:
                nr.write(nr.FILE_NAME, env, kind="failed")
                with open(nr.FILE_NAME, encoding="utf-8") as stream:
                    failed = json.load(stream)
                entries = [{**value, "reflection": False} for value in observations]
                save_evidence(context, entries, failed)
                deliver_evidence(relay, context, entries, failed)
            except Exception:
                pass
        print("일반 동기화 미완료: 수신 기록과 실행 결과를 확인하세요. 실행을 자동 재시도하지 않습니다.", file=sys.stderr)
        # Any claim/engine/callback uncertainty keeps the durable request fenced.
        # Publishing result evidence after an interrupted process never replays sync.
        return 1


if __name__ == "__main__":
    sys.exit(main())
