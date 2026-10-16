"""Locally authorized general Project change with durable shared relay fencing.

Supported host platforms: macOS/Linux (fcntl locks). Existing caller credentials
stay local; no offline mutation fallback or automatic uncertain-fence release.
"""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
from uuid import uuid4

import github_project as gp
import sync_engine as se
from ordinary_sync_request import PinnedRelay, canonical

SOURCE_ID = "398c7ff6-9c66-43de-ba4d-3f2670b25a87"
STATUS_FIELD_ID = "PVTSSF_lAHOBda_2c4BmKKVzhk0RA4"


class MutationRejected(se.SyncError):
    pass


class HelperRelay(PinnedRelay):
    domain = "replica-helper-v1"
    key_name = "HELPER_REQUEST_KEY"
    paths = {"/agent-prepare", "/agent-attempt", "/agent-abort", "/agent-outcome", "/agent-request"}


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class LocalJournal:
    def __init__(self, directory, operation_id):
        self.directory = Path(directory)
        se.require(not self.directory.is_symlink(), "helper 기록 디렉터리 결속 오류")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.operation_id = se.identifier(operation_id)
        se.require(self.operation_id == operation_id, "helper operation UUID 형식 오류")
        self.path = self.directory / (operation_id + ".json")
        self.lock_path = self.directory / (operation_id + ".lock")

    @contextmanager
    def locked(self):
        se.require(not self.lock_path.is_symlink() and not self.path.is_symlink(), "helper 기록 symlink 거절")
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise se.SyncError("같은 helper operation이 다른 프로세스에서 실행 중입니다") from None
            yield self
        finally:
            os.close(fd)

    def load(self):
        if not self.path.exists():
            return None
        se.require(self.path.stat().st_size <= 65536, "helper 기록 크기 초과")
        with self.path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        se.require(isinstance(value, dict) and value.get("schema") == 1 and
                   value.get("body", {}).get("operation_id") == self.operation_id,
                   "helper 기록 결속 오류")
        return value

    def save(self, value):
        raw = canonical(value).encode()
        se.require(len(raw) <= 65536, "helper 기록 크기 초과")
        temporary = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class SnapshotReader:
    def __init__(self, github, rest, project_client, config):
        self.github, self.rest, self.project_client, self.config = github, rest, project_client, config

    def __call__(self, issue_number):
        facts = gp.fetch_repository_facts(self.github, self.rest)
        matches = [issue for issue in facts["issues"].values() if issue["number"] == issue_number]
        se.require(len(matches) == 1, "helper Issue가 고정 저장소에서 확인되지 않습니다")
        issue = matches[0]
        project = se._project_snapshot(self.project_client, self.config, facts)
        item = project["items"].get(issue["databaseId"])
        se.require(issue["state"] == "OPEN" and item is not None and
                   not project.get("archived_items", {}).get(issue["databaseId"]) and
                   not item.get("is_archived"), "helper는 열린 Issue의 기존 활성 item만 처리합니다")
        refs, eligible = se._linked_pr_facts(issue, facts)
        se.require(not eligible and item["status_option_id"] in
                   {self.config["status_options"][name] for name in se.GENERAL_STATES},
                   "helper 상태 또는 검토 PR이 일반 변경과 충돌합니다")
        return {"issue_id": issue["databaseId"], "item_id": item["id"],
                "stamp": se._status_stamp(item, self.config),
                "source_fingerprint": se._status_facts_fingerprint(issue, refs)}


def mutate_once(client, body, options):
    # One fixed mutation, one transport attempt, exact returned item identity.
    payload = {"query": gp.SET_STATUS_MUTATION, "variables": {
        "project": body["project_id"], "item": body["item_id"],
        "field": body["status_field_id"], "option": options[body["target"]]}}
    request = urllib.request.Request(gp.GRAPHQL_URL, data=canonical(payload).encode(),
                                     headers=client.headers, method="POST")
    try:
        with client.opener.open(request, timeout=30) as response:
            raw = response.read(65537)
            se.require(len(raw) <= 65536, "helper 변경 응답 크기 초과")
            value = json.loads(raw)
    except urllib.error.HTTPError as error:
        status = error.code; error.close()
        if status in {401, 403}:
            raise MutationRejected("helper 인증/권한 단계에서 변경이 거절됐습니다") from None
        raise se.SyncError("helper API 변경 결과 미확인") from None
    except Exception:
        raise se.SyncError("helper API 변경 결과 미확인") from None
    se.require(isinstance(value, dict) and not value.get("errors") and
               value.get("data", {}).get("updateProjectV2ItemFieldValue", {}).get(
                   "projectV2Item", {}).get("id") == body["item_id"],
               "helper 변경 응답 item 결속 미확인")


def evidence(document, endpoint, payload, outcome):
    document.update(phase=outcome, evidence_endpoint=endpoint, evidence=payload)


def deliver(journal, document, relay):
    try:
        response = relay.post(document["evidence_endpoint"], document["evidence"])
        if document["evidence_endpoint"] == "/agent-request":
            state = "CONFIRMED" if document["evidence"]["kind"] == "mutation" else "CONFIRMED_NOOP"
            se.require(response.get("sync_queued") is True, "helper 수신 기록 미확인")
        else:
            state = ("ABORTED" if document["evidence_endpoint"] == "/agent-abort" else
                     "REJECTED_FENCED" if document["evidence"]["outcome"] == "rejected" else "UNKNOWN_FENCED")
        se.require(response.get("operation_id") == document["body"]["operation_id"] and
                   response.get("binding") == document["binding"] and response.get("state") == state,
                   "helper 결과 응답 결속 오류")
        document["phase"] = state; journal.save(document)
        return {"operation_id": document["body"]["operation_id"], "outcome": state,
                "project_confirmed": state in {"CONFIRMED", "CONFIRMED_NOOP"},
                "sync_queued": state in {"CONFIRMED", "CONFIRMED_NOOP"}, "notion_reflected": False}
    except Exception:
        return {"operation_id": document["body"]["operation_id"], "outcome": "DELIVERY_UNKNOWN",
                "project_confirmed": document["evidence_endpoint"] == "/agent-request",
                "sync_queued": False, "notion_reflected": False}


def run_helper(journal, relay, reader, mutate, *, issue_number, page_id, target, options, abort=False):
    se.require(type(issue_number) is int and issue_number > 0 and target in se.GENERAL_STATES,
               "helper는 일반 세 상태의 기존 Issue만 처리합니다")
    page_id = se.identifier(page_id)
    with journal.locked():
        document = journal.load()
        if document is None:
            se.require(not abort, "helper 안전 취소에는 기존 operation 기록이 필요합니다")
            snapshot = reader(issue_number)
            body = {"operation_id": journal.operation_id, "repository_id": gp.REPOSITORY_ID,
                "project_id": gp.EXPECTED_PROJECT_ID, "item_id": snapshot["item_id"],
                "status_field_id": STATUS_FIELD_ID, "issue_id": snapshot["issue_id"],
                "page_id": page_id, "sync_key": se.key_for(gp.REPOSITORY_ID, snapshot["issue_id"]),
                "expected_before": snapshot["stamp"], "target": target,
                "source_fingerprint": snapshot["source_fingerprint"]}
            document = {"schema": 1, "relay": relay.base, "issue_number": issue_number,
                "body": body, "binding": digest(body), "phase": "INTENT", "may_have_sent": False,
                "evidence_endpoint": None, "evidence": None}
            journal.save(document)
        body = document["body"]
        se.require(document["relay"] == relay.base and document["issue_number"] == issue_number and
                   body["page_id"] == page_id and body["target"] == target and
                   document["binding"] == digest(body), "helper 재개 입력 또는 relay 결속이 달라졌습니다")
        bound = {"operation_id": body["operation_id"], "binding": document["binding"]}
        if abort:
            # Only the remote PREPARED CAS can prove safe abort, even after local ACK loss.
            response = relay.post("/agent-abort", bound)
            se.require(response.get("state") == "ABORTED" and response.get("binding") == document["binding"] and
                       response.get("operation_id") == journal.operation_id, "helper 안전 취소 미확인")
            evidence(document, "/agent-abort", bound, "ABORTED"); journal.save(document)
            return deliver(journal, document, relay)
        if document["evidence_endpoint"] is not None:
            return deliver(journal, document, relay)
        if document["may_have_sent"]:
            payload = {**bound, "outcome": "unknown", "code": "ACK_LOST",
                       "evidence_digest": digest({"operation_id": journal.operation_id, "code": "ACK_LOST"})}
            evidence(document, "/agent-outcome", payload, "MUTATION_AMBIGUOUS"); journal.save(document)
            return deliver(journal, document, relay)
        prepared = relay.post("/agent-prepare", body)
        se.require(prepared.get("binding") == document["binding"] and prepared.get("operation_id") == journal.operation_id and
                   prepared.get("state") == "PREPARED", "helper 예약 미확인")
        document["phase"] = "PREPARED"; journal.save(document)
        try:
            current = reader(issue_number)
            se.require(current["issue_id"] == body["issue_id"] and current["item_id"] == body["item_id"] and
                       current["stamp"] == body["expected_before"] and
                       current["source_fingerprint"] == body["source_fingerprint"], "helper 변경 직전 사실이 달라졌습니다")
        except Exception:
            evidence(document, "/agent-abort", bound, "ABORT_PENDING"); journal.save(document)
            return deliver(journal, document, relay)
        if current["stamp"]["option_id"] == options[target]:
            payload = {**bound, "kind": "noop", "readback_stamp": current["stamp"],
                "source_fingerprint": current["source_fingerprint"], "evidence_digest": digest(current)}
            evidence(document, "/agent-request", payload, "PROJECT_NOOP_CONFIRMED"); journal.save(document)
            return deliver(journal, document, relay)
        document.update(may_have_sent=True, phase="MAY_HAVE_SENT"); journal.save(document)
        code = "ACK_LOST"; outcome = "unknown"
        try:
            permission = relay.post("/agent-attempt", bound)
            se.require(permission.get("operation_id") == journal.operation_id and
                       permission.get("binding") == document["binding"] and
                       permission.get("state") == "ATTEMPTING" and permission.get("newly_authorized") is True,
                       "helper 신규 변경 허가 미확인")
            code = "API_OUTCOME_UNKNOWN"
            mutate(body)
            code = "READBACK_MISMATCH"
            readback = reader(issue_number)
            se.require(readback["issue_id"] == body["issue_id"] and readback["item_id"] == body["item_id"] and
                       readback["stamp"]["field_id"] == STATUS_FIELD_ID and
                       readback["stamp"]["option_id"] == options[target] and
                       readback["source_fingerprint"] == body["source_fingerprint"], "helper 변경 readback 미확인")
            payload = {**bound, "kind": "mutation", "readback_stamp": readback["stamp"],
                "source_fingerprint": readback["source_fingerprint"], "evidence_digest": digest(readback)}
            evidence(document, "/agent-request", payload, "PROJECT_CONFIRMED")
        except MutationRejected:
            outcome = "rejected"; code = "API_REJECTED"
            payload = {**bound, "outcome": outcome, "code": code,
                       "evidence_digest": digest({"operation_id": journal.operation_id, "code": code})}
            evidence(document, "/agent-outcome", payload, "REJECTED_FENCED")
        except Exception:
            payload = {**bound, "outcome": outcome, "code": code,
                       "evidence_digest": digest({"operation_id": journal.operation_id, "code": code})}
            evidence(document, "/agent-outcome", payload, "MUTATION_AMBIGUOUS")
        journal.save(document)
        return deliver(journal, document, relay)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--page-id", required=True)
    parser.add_argument("--target", choices=se.GENERAL_STATES, required=True)
    parser.add_argument("--operation-id", default="")
    parser.add_argument("--journal", default=".replica-status-requests")
    parser.add_argument("--abort", action="store_true")
    args = parser.parse_args(argv)
    operation_id = args.operation_id or str(uuid4())
    try:
        env = os.environ
        se.require(env.get("PROJECT_ID") == gp.EXPECTED_PROJECT_ID and
                   env.get("PROJECT_OWNER_ID") == gp.EXPECTED_PROJECT_OWNER_ID and
                   env.get("PROJECT_STATUS_FIELD_ID") == STATUS_FIELD_ID and
                   env.get("NOTION_DATA_SOURCE_ID") == SOURCE_ID, "helper 고정 자원 설정 오류")
        config = {"project_id": gp.EXPECTED_PROJECT_ID, "owner_id": gp.EXPECTED_PROJECT_OWNER_ID,
            "status_field_id": STATUS_FIELD_ID, "status_options": gp.parse_status_options(env.get("PROJECT_STATUS_OPTIONS", ""))}
        project_client = gp.GraphQLClient(env.get("PROJECT_TOKEN", ""))
        reader = SnapshotReader(gp.GraphQLClient(env.get("GITHUB_TOKEN", "")),
                                gp.RESTClient(env.get("GITHUB_TOKEN", "")), project_client, config)
        result = run_helper(LocalJournal(args.journal, operation_id), HelperRelay(env), reader,
            lambda body: mutate_once(project_client, body, config["status_options"]),
            issue_number=args.issue, page_id=args.page_id, target=args.target,
            options=config["status_options"], abort=args.abort)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["outcome"] in {"CONFIRMED", "CONFIRMED_NOOP", "ABORTED"} else 1
    except Exception:
        print(json.dumps({"outcome": "INCOMPLETE", "operation_id": operation_id}, sort_keys=True), file=sys.stderr)
        print("helper 미완료: 로컬 operation과 relay 기록을 확인하세요. 새 UUID로 우회하지 마세요.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
