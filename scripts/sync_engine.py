"""Fail-closed GitHub Project → Notion synchronization core."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import github_project as gp

REPOSITORY = gp.REPOSITORY
REPOSITORY_ID = gp.REPOSITORY_ID
CONTROL_KEY = "replica-sync-control"
ACTIONS_WORKFLOW_URL = "https://github.com/Aurelia-aurity/Replica/actions/workflows/notion-sync.yml"
KEY_RE = re.compile(r"gh:([1-9][0-9]*):issue:([1-9][0-9]*)\Z")
MAX_PROPERTY_TEXT = 20000
MAX_INTERNAL_TEXT = 18000
NOTION_QUERY_PAGE_SIZE = 100
NOTION_QUERY_MAX_RESULTS = 10000
BOUNDARY_SECONDS = 1.0
GENERAL_STATES = ("백로그", "준비 중", "진행 중")
RESUMABLE_HOLD_CODES = {
    "MIGRATION_CONFLICT", "MIGRATION_UNSET", "MIGRATION_INPUT_CHANGED",
    "OPEN_PROJECT_COMPLETED", "REVIEW_WITHOUT_CYCLE", "RESUME_CHECKPOINT_CHANGED",
    "CLOSURE_DUPLICATE_CONFLICT", "OPEN_DUPLICATE_CONFLICT", "REOPEN_CLOSE_ORDER",
    "REOPEN_ORDER_AMBIGUOUS", "SOURCE_CHANGED_BEFORE_WRITE", "PROJECT_ADD_UNCERTAIN",
    "PROJECT_ITEM_RACE", "PROJECT_ITEM_ARCHIVED", "PENDING_RESULT_UNCLEAR",
    "PENDING_FACTS_CHANGED", "PROJECTION_CHECKPOINT_CHANGED", "PROJECT_STATUS_UNSET",
}

SCHEMA = {
    "제목": "title", "종류": "select", "번호": "number", "GitHub URL": "url",
    "GitHub 상태": "select", "작성자": "rich_text", "담당자": "rich_text",
    "라벨": "rich_text", "GitHub 수정": "date", "동기화 키": "rich_text",
    "동기화 시각": "date", "Pending create": "rich_text", "작업 상태": "select",
    "일정": "date", "메모": "rich_text", "종료 사유": "select", "대표 이슈": "url",
    "확인 필요": "rich_text", "동기화 내부 상태": "rich_text",
}
OPTIONS = {
    "종류": {"Issue", "PR", "Sync"},
    "GitHub 상태": {"Open", "Closed", "Draft", "Merged"},
    "작업 상태": {"백로그", "준비 중", "진행 중", "검토 중", "완료"},
    "종료 사유": {"완료", "미계획", "중복", "확인 필요"},
}
ISSUE_STATE_KEYS = {
    "v", "repository_id", "issue_id", "project_id", "project_item_id", "migration_complete",
    "reopen_baseline_id", "reopen_last_id", "review_cycle", "review_return_cycle",
    "review_pr_hash", "pending", "hold", "resume", "projection",
}
CONTROL_STATE_KEYS = {"v", "project_id", "migration_cutoff", "last_success_at", "run_id", "last_result"}
LEGACY_RESUME_KEYS = {"actor_id", "run_id", "approved_option_id", "fingerprint", "display_pending"}
RESUME_KEYS = LEGACY_RESUME_KEYS | {"expected_option_id", "expected_fingerprint"}
STATUS_CHECKPOINT_KEYS = {"migration_complete", "review_cycle", "review_return_cycle",
                          "reopen_last_id", "review_pr_hash"}
PROJECTION_KEYS = {"expected_option_id", "expected_fingerprint", "checkpoint"}


class SyncError(Exception):
    """Fixed diagnostics only; never include remote data, API bodies or credentials."""


def require(condition, message="원격 데이터 검증 실패"):
    if not condition:
        raise SyncError(message)


def identifier(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise SyncError("Notion ID 형식 오류") from None


def positive(value, message="GitHub ID/번호 형식 오류"):
    require(type(value) is int and value > 0, message)
    return value


def timestamp(value):
    return gp.parse_time(value)


def text_property(value, kind="rich_text"):
    require(isinstance(value, str) and len(value) <= MAX_PROPERTY_TEXT, "Notion 텍스트 크기/형식 오류")
    return {kind: [{"type": "text", "text": {"content": value[i:i + 2000]}}
                   for i in range(0, len(value), 2000)]}


def read_text(page, name):
    prop = page["properties"][name]
    return "".join(part.get("plain_text", part.get("text", {}).get("content", ""))
                   for part in prop["rich_text"])


def read_select(page, name):
    value = page["properties"][name]["select"]
    return value.get("name") if value else None


def read_date(page, name):
    value = page["properties"][name]["date"]
    return value.get("start") if value else None


def array_text(values):
    require(isinstance(values, list) and all(isinstance(v, str) for v in values), "GitHub 목록 형식 오류")
    return json.dumps(sorted(set(values)), ensure_ascii=False, separators=(",", ":"))


def key_for(repo_id, issue_id):
    return f"gh:{positive(repo_id)}:issue:{positive(issue_id)}"


def valid_key(value):
    match = KEY_RE.fullmatch(value)
    return bool(match and int(match[1]) == REPOSITORY_ID)


def canonical_json(value):
    result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    require(len(result) <= MAX_INTERNAL_TEXT, "동기화 내부 상태 크기 초과")
    return result


def decode_internal(value, *, kind, project_id, object_id=None):
    if value == "":
        return None
    require(len(value) <= MAX_INTERNAL_TEXT, "동기화 내부 상태 크기 초과")
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        raise SyncError("동기화 내부 상태 JSON 오류") from None
    require(isinstance(parsed, dict) and type(parsed.get("v")) is int and parsed["v"] == 1,
            "동기화 내부 상태 버전/형식 오류")
    if kind == "issue":
        require(set(parsed) == ISSUE_STATE_KEYS, "Issue 내부 상태 속성 집합 오류")
        require(parsed.get("repository_id") == REPOSITORY_ID and parsed.get("project_id") == project_id,
                "Issue 내부 상태 저장소/Project ID 불일치")
        require(type(parsed.get("issue_id")) is int and parsed["issue_id"] > 0 and
                (object_id is None or parsed["issue_id"] == object_id), "Issue 내부 상태 ID 불일치")
        require(parsed["project_item_id"] is None or isinstance(parsed["project_item_id"], str),
                "Issue Project item ID 형식 오류")
        require(type(parsed.get("migration_complete")) is bool, "Issue 이관 상태 형식 오류")
        for field in ("review_cycle", "review_return_cycle"):
            require(type(parsed.get(field)) is int and parsed[field] >= 0, "Issue 검토 주기 형식 오류")
        require(parsed["review_return_cycle"] <= parsed["review_cycle"], "Issue 검토 복귀 주기 역전")
        for field in ("reopen_baseline_id", "reopen_last_id"):
            require(parsed[field] is None or isinstance(parsed[field], str), "Issue 재오픈 ID 형식 오류")
        require(isinstance(parsed["review_pr_hash"], str), "Issue PR hash 형식 오류")
        require(parsed["pending"] is None or isinstance(parsed["pending"], dict), "Issue pending 형식 오류")
        require(parsed["hold"] is None or isinstance(parsed["hold"], dict), "Issue hold 형식 오류")
        require(parsed["resume"] is None or isinstance(parsed["resume"], dict), "Issue resume 형식 오류")
        require(parsed["projection"] is None or isinstance(parsed["projection"], dict),
                "Issue projection 형식 오류")
        _validate_pending(parsed["pending"], project_id, parsed["issue_id"])
        _validate_hold(parsed["hold"])
        _validate_resume(parsed["resume"])
        _validate_projection(parsed["projection"])
        require(parsed["pending"] is None or parsed["projection"] is None,
                "Issue pending/projection 동시 보유 오류")
        if parsed["projection"] is not None:
            require(isinstance(parsed["project_item_id"], str) and parsed["project_item_id"],
                    "Issue projection의 고정 Project item ID 누락")
    elif kind == "control":
        require(set(parsed) == CONTROL_STATE_KEYS and parsed.get("project_id") == project_id,
                "동기화 관리 내부 상태 속성/Project ID 오류")
        cutoff = parsed.get("migration_cutoff")
        require(isinstance(cutoff, str), "동기화 기준선 누락")
        timestamp(cutoff)
        if parsed.get("last_success_at") is not None:
            require(isinstance(parsed["last_success_at"], str), "최근 성공 시각 형식 오류")
            timestamp(parsed["last_success_at"])
        require(parsed.get("run_id") is None or isinstance(parsed.get("run_id"), str),
                "동기화 run ID 형식 오류")
        require(parsed.get("last_result") is None or isinstance(parsed.get("last_result"), dict),
                "최근 결과 형식 오류")
    else:
        raise SyncError("내부 상태 종류 오류")
    return parsed


def _validate_pending(pending, project_id, issue_id):
    if pending is None:
        return
    keys = {"kind", "issue_id", "item_id", "event_id", "before_option_id", "target_option_id",
            "facts_fingerprint", "migration_fingerprint", "notion_status", "confirmed",
            "project_item_id", "checkpoint"}
    require(set(pending) == keys and type(pending.get("issue_id")) is int and
            pending.get("issue_id") == issue_id and
            pending.get("kind") in {"status", "add"}, "Issue pending 구조 오류")
    require(pending.get("item_id") is None or isinstance(pending.get("item_id"), str), "pending item ID 오류")
    require(pending.get("project_item_id") is None or isinstance(pending.get("project_item_id"), str),
            "pending Project item ID 오류")
    require(pending.get("event_id") is None or isinstance(pending.get("event_id"), str), "pending event ID 오류")
    for field in ("before_option_id", "target_option_id"):
        require(pending.get(field) is None or isinstance(pending[field], str), "pending option ID 오류")
        require(pending.get(field) is None or pending[field] in gp.EXPECTED_STATUS_OPTIONS.values(),
                "pending option ID 미등록")
    require(isinstance(pending.get("facts_fingerprint"), str) and len(pending["facts_fingerprint"]) == 64,
            "pending fingerprint 오류")
    require(pending.get("migration_fingerprint") is None or
            (isinstance(pending.get("migration_fingerprint"), str) and
             len(pending["migration_fingerprint"]) == 64), "pending 최초 이관 fingerprint 오류")
    require(pending.get("notion_status") is None or isinstance(pending["notion_status"], str),
            "pending Notion 상태 오류")
    require(type(pending.get("confirmed")) is bool and isinstance(pending.get("checkpoint"), dict),
            "pending checkpoint 오류")
    require(pending["notion_status"] is None or
            pending["notion_status"] in OPTIONS["작업 상태"],
            "pending Notion 상태 미등록")
    checkpoint = pending["checkpoint"]
    if pending["kind"] == "status":
        _validate_status_checkpoint(checkpoint)
        require(isinstance(pending.get("item_id"), str) and pending["item_id"] and
                pending.get("project_item_id") == pending["item_id"] and
                (pending.get("event_id") is None or
                 isinstance(pending.get("event_id"), str) and pending["event_id"]),
                "pending status item/event ID 관계 오류")
        if pending["event_id"] is not None:
            require(checkpoint["reopen_last_id"] == pending["event_id"],
                    "pending status event/checkpoint ID 불일치")
    else:
        require(pending.get("item_id") is None and pending.get("event_id") is None and
                pending.get("before_option_id") is None and pending.get("target_option_id") is None,
                "pending add status/event 속성은 비어 있어야 합니다")
        require(isinstance(checkpoint.get("content_id"), str) and checkpoint["content_id"],
                "pending add content ID 오류")
        if pending["confirmed"]:
            require(set(checkpoint) == {"content_id", "item_id"} and
                    isinstance(checkpoint.get("item_id"), str) and checkpoint["item_id"] and
                    pending.get("project_item_id") == checkpoint["item_id"],
                    "pending add confirmed checkpoint 관계 오류")
        else:
            require(set(checkpoint) == {"content_id"} and
                    pending.get("project_item_id") is None,
                    "pending add prepare checkpoint 관계 오류")
    canonical_json(pending)


def _validate_status_checkpoint(checkpoint):
    require(isinstance(checkpoint, dict) and set(checkpoint) == STATUS_CHECKPOINT_KEYS and
            type(checkpoint.get("migration_complete")) is bool and
            type(checkpoint.get("review_cycle")) is int and checkpoint["review_cycle"] >= 0 and
            type(checkpoint.get("review_return_cycle")) is int and
            0 <= checkpoint["review_return_cycle"] <= checkpoint["review_cycle"] and
            (checkpoint.get("reopen_last_id") is None or
             isinstance(checkpoint.get("reopen_last_id"), str) and checkpoint["reopen_last_id"]) and
            isinstance(checkpoint.get("review_pr_hash"), str) and
            len(checkpoint["review_pr_hash"]) == 64,
            "status checkpoint 구조/값 오류")


def _apply_status_checkpoint(state, checkpoint):
    _validate_status_checkpoint(checkpoint)
    for field in STATUS_CHECKPOINT_KEYS:
        state[field] = checkpoint[field]


def _validate_projection(projection):
    if projection is None:
        return
    require(set(projection) == PROJECTION_KEYS and
            (projection.get("expected_option_id") is None or
             projection["expected_option_id"] in gp.EXPECTED_STATUS_OPTIONS.values()) and
            isinstance(projection.get("expected_fingerprint"), str) and
            len(projection["expected_fingerprint"]) == 64,
            "Issue projection checkpoint 형식 오류")
    _validate_status_checkpoint(projection.get("checkpoint"))


def _validate_hold(hold):
    if hold is None:
        return
    require(set(hold) == {"code", "message", "fingerprint"} and
            isinstance(hold.get("code"), str) and isinstance(hold.get("message"), str) and
            isinstance(hold.get("fingerprint"), str) and len(hold["fingerprint"]) == 64,
            "Issue hold 형식 오류")


def _validate_resume(resume):
    if resume is None:
        return
    keys = set(resume)
    require(keys in (LEGACY_RESUME_KEYS, RESUME_KEYS) and
            type(resume.get("actor_id")) is int and resume["actor_id"] == gp.EXPECTED_PM_USER_ID and
            isinstance(resume.get("run_id"), str) and
            (resume.get("approved_option_id") is None or isinstance(resume["approved_option_id"], str)) and
            isinstance(resume.get("fingerprint"), str) and len(resume["fingerprint"]) == 64 and
            type(resume.get("display_pending")) is bool, "Issue resume 형식 오류")
    require(resume.get("approved_option_id") is None or
            resume["approved_option_id"] in gp.EXPECTED_STATUS_OPTIONS.values(),
            "Issue resume option ID 미등록")
    if keys == RESUME_KEYS:
        expected_option = resume.get("expected_option_id")
        expected_fingerprint = resume.get("expected_fingerprint")
        require(expected_option is None or
                (isinstance(expected_option, str) and
                 expected_option in gp.EXPECTED_STATUS_OPTIONS.values()),
                "Issue resume checkpoint option ID 미등록")
        require((expected_option is None and expected_fingerprint is None) or
                (isinstance(expected_fingerprint, str) and len(expected_fingerprint) == 64),
                "Issue resume checkpoint fingerprint 오류")


def new_issue_state(issue_id, project_id, baseline_id=None):
    state = {
        "v": 1, "repository_id": REPOSITORY_ID, "issue_id": issue_id, "project_id": project_id,
        "project_item_id": None, "migration_complete": False,
        "reopen_baseline_id": baseline_id, "reopen_last_id": baseline_id,
        "review_cycle": 0, "review_return_cycle": 0, "review_pr_hash": "",
        "pending": None, "hold": None, "resume": None, "projection": None,
    }
    return state


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NotionClient:
    def __init__(self, token, *, opener=None, sleep=time.sleep):
        require(isinstance(token, str) and token, "NOTION_TOKEN 설정이 없습니다")
        self.base = "https://api.notion.com/v1"
        self.sleep = sleep
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                        "Notion-Version": "2026-03-11"}

    def request(self, method, path, payload=None, *, write=False, create=False):
        require(path.startswith("/") and not path.startswith("//"), "Notion API 경로 오류")
        url = self.base + path
        require(urllib.parse.urlsplit(url).hostname == "api.notion.com", "Notion API host 오류")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        read_query = method == "POST" and path.endswith("/query") and not write and not create
        attempts = 1 if write or create else 4
        for attempt in range(attempts):
            self.sleep(0.4)
            request = urllib.request.Request(url, data=data, headers=self.headers, method=method)
            try:
                with self.opener.open(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                status = exc.code
                blocked = False
                if status == 429 and read_query:
                    try:
                        body = json.loads(exc.read(16384))
                        blocked = body.get("additional_data", {}).get("rate_limit_reason") == "public_api_request_blocked"
                    except (ValueError, AttributeError):
                        pass
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                retryable = status in {429, 500, 502, 503, 504, 529} and not blocked
                if write or create or not retryable or attempt == attempts - 1:
                    raise SyncError(f"Notion HTTP {status}; 실행 중단") from None
                try:
                    delay = int(retry_after) if retry_after is not None else 2 ** attempt + random.random()
                except ValueError:
                    raise SyncError("Notion Retry-After 형식 오류") from None
                require(0 <= delay <= 60, "Notion Retry-After 초과; 다음 실행에서 재시도")
                self.sleep(delay)
            except (urllib.error.URLError, TimeoutError, OSError):
                if write or create or attempt == attempts - 1:
                    raise SyncError("Notion 연결/응답 실패; 실행 중단") from None
                self.sleep(2 ** attempt + random.random())
            except (ValueError, UnicodeError):
                raise SyncError("Notion 응답 형식 오류") from None


def _notion_query_diagnostic(response, *, archived, page_number, seen_cursors):
    """Emit allowlisted response-shape facts without remote values or identifiers."""
    if not isinstance(response, dict):
        print("[notion-query] "
              f"archived={str(archived).lower()} page={page_number} response=invalid",
              file=sys.stderr)
        return

    object_value = response.get("object")
    object_kind = "list" if object_value == "list" else (
        "missing" if "object" not in response else "other")
    type_value = response.get("type")
    response_type = "page_or_data_source" if type_value == "page_or_data_source" else (
        "missing" if "type" not in response else "other")
    parent_kind = "object" if isinstance(response.get("page_or_data_source"), dict) else (
        "missing" if "page_or_data_source" not in response else "other")

    results = response.get("results")
    results_count = str(len(results)) if isinstance(results, list) else (
        "missing" if "results" not in response else "invalid")
    has_more_value = response.get("has_more")
    has_more = str(has_more_value).lower() if type(has_more_value) is bool else (
        "missing" if "has_more" not in response else "invalid")
    cursor_present = "next_cursor" in response
    cursor = response.get("next_cursor")
    cursor_kind = "null" if cursor is None else "string" if isinstance(cursor, str) else "other"
    if has_more_value is True:
        cursor_valid = isinstance(cursor, str) and bool(cursor) and cursor not in seen_cursors
    elif has_more_value is False:
        cursor_valid = cursor is None
    else:
        cursor_valid = False

    status = response.get("request_status")
    if "request_status" not in response:
        status_kind = "missing"
        reason_kind = "unavailable"
    elif not isinstance(status, dict):
        status_kind = "malformed"
        reason_kind = "unavailable"
    else:
        status_value = status.get("type")
        status_kind = status_value if isinstance(status_value, str) and status_value in {
            "complete", "incomplete"} else (
            "missing" if "type" not in status else "other")
        if "incomplete_reason" not in status:
            reason_kind = "absent"
        else:
            reason = status.get("incomplete_reason")
            reason_kind = "query_result_limit_reached" if reason == "query_result_limit_reached" else (
                "other" if isinstance(reason, str) else "malformed")

    print("[notion-query] "
          f"archived={str(archived).lower()} page={page_number} "
          f"object={object_kind} type={response_type} page_or_data_source={parent_kind} "
          f"results_count={results_count} has_more={has_more} "
          f"next_cursor_present={str(cursor_present).lower()} next_cursor_type={cursor_kind} "
          f"next_cursor_valid={str(cursor_valid).lower()} "
          f"request_status={status_kind} incomplete_reason={reason_kind}",
          file=sys.stderr)


def query_all(notion, source_id, archived, *, diagnostics=False):
    rows, cursor, seen = [], None, set()
    page_number = 0
    while True:
        payload = {"page_size": NOTION_QUERY_PAGE_SIZE, "is_archived": archived}
        if cursor is not None:
            payload["start_cursor"] = cursor
        response = notion.request("POST", f"/data_sources/{source_id}/query", payload)
        page_number += 1
        if diagnostics:
            _notion_query_diagnostic(response, archived=archived, page_number=page_number,
                                     seen_cursors=seen)
        require(isinstance(response, dict), "Notion query 응답 object 형식 오류")
        require(response.get("object") == "list" and
                response.get("type") == "page_or_data_source" and
                isinstance(response.get("page_or_data_source"), dict),
                "Notion query 응답 envelope 형식 오류")
        batch = response.get("results")
        require(isinstance(batch, list), "Notion query results 형식 오류")
        has_more = response.get("has_more")
        require(type(has_more) is bool, "Notion has_more 형식 오류")
        require("next_cursor" in response, "Notion next_cursor 필드 누락")
        cursor = response.get("next_cursor")
        if has_more:
            require(isinstance(cursor, str) and cursor, "Notion 다음 페이지 cursor 누락/형식 오류")
            require(cursor not in seen, "Notion cursor 반복")
        else:
            require(cursor is None, "Notion terminal cursor는 null이어야 합니다")

        if "request_status" in response:
            request_status = response["request_status"]
            require(isinstance(request_status, dict), "Notion request_status 형식 오류")
            status_type = request_status.get("type")
            require(isinstance(status_type, str) and status_type in {"complete", "incomplete"},
                    "Notion request_status type 형식/값 오류")
            if "incomplete_reason" in request_status:
                require(request_status["incomplete_reason"] == "query_result_limit_reached",
                        "Notion incomplete_reason 형식/값 오류")
            require(status_type == "complete", "Notion query 결과가 불완전합니다")

        total = len(rows) + len(batch)
        require(total < NOTION_QUERY_MAX_RESULTS,
                "Notion query 결과가 10,000건 경계에 도달했습니다; 전체 조회를 보장할 수 없습니다")
        rows.extend(batch)
        if not has_more:
            return rows
        seen.add(cursor)


def bound_page(page, source_id):
    parent = page.get("parent")
    require(isinstance(parent, dict) and identifier(parent.get("data_source_id")) == source_id,
            "Notion 부모 데이터 소스 불일치")
    identifier(page.get("id"))


def is_archived(page):
    return bool(page.get("is_archived", page.get("archived", False)) or
                page.get("archived", False) or page.get("in_trash", False))


def _schema(notion, source_id):
    schema = notion.request("GET", f"/data_sources/{source_id}")
    require(identifier(schema.get("id")) == source_id, "Notion data source ID 불일치")
    properties = schema.get("properties")
    require(isinstance(properties, dict) and set(properties) == set(SCHEMA), "Notion 19개 속성 집합 불일치")
    for name, kind in SCHEMA.items():
        require(properties.get(name, {}).get("type") == kind, "Notion 속성 타입 불일치")
    for name, expected in OPTIONS.items():
        actual = {option.get("name") for option in properties[name]["select"]["options"]}
        require(actual == expected, "Notion select 옵션 불일치")
    return schema


def check_control(page, source_id, control_id):
    bound_page(page, source_id)
    require(identifier(page["id"]) == control_id and not is_archived(page),
            "동기화 관리 행 ID/활성 상태 불일치")
    require(read_text(page, "동기화 키") == CONTROL_KEY and read_select(page, "종류") == "Sync",
            "동기화 관리 행 키/종류 불일치")
    workflow_url = page["properties"]["GitHub URL"]["url"]
    require(workflow_url is None or workflow_url == ACTIONS_WORKFLOW_URL,
            "동기화 관리행 Actions URL 형식 오류")
    for name, kind in {"번호": "number", "GitHub 상태": "select",
                       "GitHub 수정": "date"}.items():
        require(page["properties"][name][kind] is None, "동기화 관리 행 고정 필드는 비어 있어야 합니다")


def preflight_notion(notion, source_id, control_id, project_id, *, diagnostics=False):
    source_id, control_id = identifier(source_id), identifier(control_id)
    _schema(notion, source_id)
    direct_control = notion.request("GET", f"/pages/{control_id}")
    check_control(direct_control, source_id, control_id)
    index, archived_keys = {}, set()
    for archived in (False, True):
        for row in query_all(notion, source_id, archived, diagnostics=diagnostics):
            bound_page(row, source_id)
            key = read_text(row, "동기화 키")
            if not key:
                continue
            require(key == CONTROL_KEY or valid_key(key), "Notion 동기화 키 형식/저장소 불일치")
            if archived or is_archived(row):
                archived_keys.add(key)
                continue
            require(key not in index, "Notion 동기화 키 중복; 수동 확인 필요")
            kind = read_select(row, "종류")
            require(kind in {"Issue", "PR", "Sync"}, "Notion 관리 행 종류 오류")
            if kind == "Issue":
                match = KEY_RE.fullmatch(key)
                issue_id = positive(int(match[2]))
                state_text = read_text(row, "동기화 내부 상태")
                state = decode_internal(state_text, kind="issue", project_id=project_id,
                                        object_id=issue_id) if state_text else None
                task = read_select(row, "작업 상태")
                require(task is None or task in OPTIONS["작업 상태"], "Notion 작업 상태 옵션 오류")
                positive(row["properties"]["번호"]["number"], "Notion Issue 번호 오류")
            elif kind == "PR":
                require(not read_text(row, "동기화 내부 상태"), "PR 행에 Issue 내부 상태가 있습니다")
                positive(row["properties"]["번호"]["number"], "Notion PR 번호 오류")
            date = read_date(row, "GitHub 수정")
            if date:
                timestamp(date)
            index[key] = row
    require(CONTROL_KEY not in archived_keys and CONTROL_KEY in index,
            "동기화 관리 행이 조회되지 않거나 보관 상태입니다")
    check_control(index[CONTROL_KEY], source_id, control_id)
    pending = read_text(direct_control, "Pending create")
    require(read_text(index[CONTROL_KEY], "Pending create") == pending, "관리 행 snapshot 불일치")
    require(read_text(index[CONTROL_KEY], "동기화 내부 상태") ==
            read_text(direct_control, "동기화 내부 상태") and
            read_date(index[CONTROL_KEY], "동기화 시각") == read_date(direct_control, "동기화 시각") and
            read_text(index[CONTROL_KEY], "확인 필요") == read_text(direct_control, "확인 필요"),
            "관리행 내부 상태/표시 snapshot 불일치")
    require(not any(key in archived_keys for key in index), "Notion 보관 키 상태 오류")
    if pending:
        require(valid_key(pending) and pending in index, "Pending create 결과 불명; 수동 조사 필요")
    control_text = read_text(direct_control, "동기화 내부 상태")
    control_state = decode_internal(control_text, kind="control", project_id=project_id) if control_text else None
    visible_date = read_date(direct_control, "동기화 시각")
    if control_state and control_state["last_success_at"]:
        require(visible_date and timestamp(visible_date) == timestamp(control_state["last_success_at"]),
                "관리행 성공 시각/internal checkpoint 불일치")
    return {"source_id": source_id, "control_id": control_id, "index": index,
            "control": direct_control, "control_state": control_state, "pending_create": pending,
            "archived_keys": archived_keys}


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _notion_value(page, name, kind):
    prop = page["properties"][name]
    if kind in {"title", "rich_text"}:
        return "".join(part.get("plain_text", part.get("text", {}).get("content", ""))
                       for part in prop[kind])
    if kind == "select":
        return prop[kind].get("name") if prop[kind] else None
    if kind == "date":
        return prop[kind].get("start") if prop[kind] else None
    if kind == "url":
        return prop[kind]
    if kind == "number":
        return prop[kind]
    raise SyncError("Notion readback 속성 형식 오류")


def _verify_properties(page, expected):
    for name, value in expected.items():
        kind, body = next(iter(value.items()))
        if kind in {"title", "rich_text"}:
            wanted = "".join(part["text"]["content"] for part in body)
        elif kind == "select":
            wanted = body.get("name") if body else None
        elif kind == "date":
            wanted = body.get("start") if body else None
        else:
            wanted = body
        require(_notion_value(page, name, kind) == wanted,
                "Notion write readback 불일치; 다음 실행에서 복구 필요")


def _patch_page(notion, source_id, page, properties):
    page_id = identifier(page.get("id"))
    notion.request("PATCH", f"/pages/{page_id}", {"properties": properties}, write=True)
    confirmed = notion.request("GET", f"/pages/{page_id}")
    bound_page(confirmed, source_id)
    require(identifier(confirmed.get("id")) == page_id and not is_archived(confirmed),
            "Notion 변경 대상 페이지 readback 오류")
    _verify_properties(confirmed, properties)
    page.clear()
    page.update(confirmed)
    return page


def _save_issue_state(notion, source_id, row, state):
    raw = canonical_json(state)
    decoded = decode_internal(raw, kind="issue", project_id=state["project_id"],
                              object_id=state["issue_id"])
    _patch_page(notion, source_id, row, {"동기화 내부 상태": text_property(canonical_json(decoded))})


def _save_control_state(notion, source_id, control, state):
    raw = canonical_json(state)
    decoded = decode_internal(raw, kind="control", project_id=state["project_id"])
    _patch_page(notion, source_id, control,
                {"동기화 내부 상태": text_property(canonical_json(decoded))})


def _make_metadata(kind, row, key):
    number = positive(row.get("number"), "GitHub 번호 오류")
    updated = row.get("updatedAt")
    timestamp(updated)
    author = row.get("author") or {}
    require(isinstance(author, dict), "GitHub 작성자 형식 오류")
    assignees = row.get("assignees", {}).get("nodes", [])
    labels = row.get("labels", {}).get("nodes", [])
    require(isinstance(assignees, list) and isinstance(labels, list), "GitHub 메타데이터 목록 형식 오류")
    assignee_names = sorted({a.get("login") for a in assignees if isinstance(a, dict)})
    label_names = sorted({a.get("name") for a in labels if isinstance(a, dict)})
    require(all(isinstance(v, str) for v in assignee_names + label_names), "GitHub 메타데이터 값 오류")
    if kind == "Issue":
        github_state = "Open" if row.get("state") == "OPEN" else "Closed"
        path_kind = "issues"
        title = f"#{number} {row.get('title', '')}"
    else:
        github_state = ("Merged" if row.get("state") == "MERGED" or row.get("mergedAt") else
                        "Draft" if row.get("state") == "OPEN" and row.get("isDraft") else
                        "Open" if row.get("state") == "OPEN" else "Closed")
        path_kind = "pull"
        title = row.get("title", "")
    require(isinstance(title, str) and title.strip(), "GitHub 제목 누락")
    url = row.get("url")
    require(isinstance(url, str) and url == f"https://github.com/{REPOSITORY}/{path_kind}/{number}",
            "GitHub URL/번호 불일치")
    return {
        "제목": text_property(title, "title"), "종류": {"select": {"name": kind}},
        "번호": {"number": number}, "GitHub URL": {"url": url},
        "GitHub 상태": {"select": {"name": github_state}},
        "작성자": text_property(author.get("login") or ""),
        "담당자": text_property(", ".join(assignee_names)),
        "라벨": text_property(", ".join(label_names)),
        "GitHub 수정": {"date": {"start": updated}}, "동기화 키": text_property(key),
    }


def _issue_reopens(issue):
    events = issue.get("reopens")
    require(isinstance(events, list), "GitHub 재오픈 기록 목록 누락")
    result, ids = [], {}
    for event in events:
        require(isinstance(event, dict) and isinstance(event.get("id"), str) and event["id"],
                "GitHub 재오픈 이벤트 ID 오류")
        previous = ids.get(event["id"])
        if previous is not None:
            require(previous == event.get("createdAt"), "같은 재오픈 ID의 시각이 서로 다릅니다")
            continue
        ids[event["id"]] = event.get("createdAt")
        result.append((timestamp(event.get("createdAt")), event))
    return sorted(result, key=lambda entry: (entry[0], entry[1]["id"]))


def _valid_forward_closed_checkpoint(issue, timeline, checkpoint_id, prior_id, cutoff_time):
    """Recognize only a canonical CLOSED reopen checkpoint that advanced past prior state."""
    if issue.get("state") != "CLOSED" or not checkpoint_id:
        return False
    checkpoint_time = timeline.get(checkpoint_id)
    if checkpoint_time is None or checkpoint_time <= cutoff_time + timedelta(
            seconds=BOUNDARY_SECONDS):
        return False
    prior_time = timeline.get(prior_id) if prior_id is not None else None
    if prior_id is not None and (prior_time is None or checkpoint_time <= prior_time):
        return False

    closed_at = issue.get("closedAt")
    if not closed_at:
        return False
    closed_time = timestamp(closed_at)
    if ((closed_time - checkpoint_time).total_seconds() <= BOUNDARY_SECONDS):
        return False

    events = _issue_reopens(issue)
    seen_times = {}
    candidates = []
    cutoff_limit = cutoff_time + timedelta(seconds=BOUNDARY_SECONDS)
    for at, event in events:
        if abs((at - cutoff_time).total_seconds()) <= BOUNDARY_SECONDS:
            return False
        if at in seen_times and seen_times[at] != event["id"]:
            return False
        seen_times[at] = event["id"]
        if at > cutoff_limit and (prior_time is None or at > prior_time):
            candidates.append((at, event))
    if not candidates:
        return False
    latest_time, latest_event = max(candidates, key=lambda entry: (entry[0], entry[1]["id"]))
    return (latest_event["id"] == checkpoint_id and latest_time < closed_time and
            (closed_time - latest_time).total_seconds() > BOUNDARY_SECONDS)


def _linked_pr_facts(issue, facts, *, allow_snapshot_drift=False):
    refs = issue.get("linked_prs")
    require(isinstance(refs, list), "GitHub Issue 종료 PR 참조 목록 누락")
    pulls_by_node = {pr["id"]: pr for pr in facts["pulls"].values()}
    all_refs, eligible = [], []
    seen = set()
    for ref in refs:
        require(isinstance(ref, dict) and isinstance(ref.get("id"), str) and ref["id"],
                "GitHub 종료 PR 참조 ID 오류")
        require(ref["id"] not in seen, "GitHub 종료 PR 참조 중복")
        seen.add(ref["id"])
        base = ref.get("baseRepository") or {}
        repository = ref.get("repository") or {}
        require(isinstance(base, dict) and isinstance(repository, dict), "GitHub PR 저장소 정보 누락")
        require(isinstance(base.get("id"), str) and base["id"] and
                isinstance(repository.get("id"), str) and repository["id"] and
                (ref.get("baseRefName") is None or isinstance(ref.get("baseRefName"), str)),
                "GitHub 종료 PR 저장소/기준 branch 형식 오류")
        state = ref.get("state")
        require(state in {"OPEN", "CLOSED", "MERGED"} and type(ref.get("isDraft")) is bool,
                "GitHub 종료 PR 상태 형식 오류")
        ref_facts = {"id": ref["id"], "number": positive(ref.get("number")), "state": state,
                     "isDraft": ref["isDraft"], "baseRefName": ref.get("baseRefName"),
                     "baseRepository": base.get("id"), "repository": repository.get("id")}
        all_refs.append(ref_facts)
        if repository.get("id") == facts["repository_node_id"]:
            pull = pulls_by_node.get(ref["id"])
            matches_snapshot = (pull is not None and pull.get("number") == ref_facts["number"] and
                                pull.get("state") == state and pull.get("isDraft") == ref["isDraft"] and
                                pull.get("baseRefName") == ref.get("baseRefName") and
                                (pull.get("baseRepository") or {}).get("id") == base.get("id"))
            if not matches_snapshot:
                require(allow_snapshot_drift, "Issue 종료 PR 참조와 전체 PR snapshot 불일치")
                ref_facts["snapshot_changed"] = True
        if (repository.get("id") == facts["repository_node_id"] and
                base.get("id") == facts["repository_node_id"] and
                ref.get("baseRefName") == "main" and state == "OPEN" and not ref["isDraft"]):
            eligible.append(ref_facts)
    all_refs.sort(key=lambda value: value["id"])
    eligible.sort(key=lambda value: value["id"])
    return all_refs, eligible


def _source_fingerprint(issue, linked_refs, item):
    duplicate = issue.get("duplicateOf") or {}
    reopens = [{"id": event["id"], "createdAt": event["createdAt"]}
               for _, event in _issue_reopens(issue)]
    return _digest({
        "issue_id": issue.get("databaseId"), "issue_node_id": issue.get("id"),
        "state": issue.get("state"), "stateReason": issue.get("stateReason"),
        "updatedAt": issue.get("updatedAt"), "closedAt": issue.get("closedAt"),
        "duplicate_id": duplicate.get("id"), "reopens": reopens, "linked_prs": linked_refs,
        "project_item_id": item.get("id") if item else None,
        "project_option_id": item.get("status_option_id") if item else None,
    })


def _resume_checkpoint_matches(resume, issue, facts, item):
    """Only a checkpointed, exact current Project/source snapshot can recover display."""
    if (not isinstance(resume, dict) or set(resume) != RESUME_KEYS or
            not resume.get("display_pending") or resume.get("expected_fingerprint") is None or
            item is None or item.get("status_option_id") != resume.get("expected_option_id")):
        return False
    linked, _ = _linked_pr_facts(issue, facts)
    return _source_fingerprint(issue, linked, item) == resume["expected_fingerprint"]


def _resume_approval_matches(resume, issue, facts, item):
    """An uncheckpointed saved approval may continue only from its original exact snapshot."""
    if (not isinstance(resume, dict) or set(resume) != RESUME_KEYS or
            not resume.get("display_pending") or resume.get("expected_fingerprint") is not None or
            item is None or item.get("status_option_id") != resume.get("approved_option_id")):
        return False
    linked, _ = _linked_pr_facts(issue, facts)
    return _source_fingerprint(issue, linked, item) == resume["fingerprint"]


def _resume_matches(resume, issue, facts, item):
    return (_resume_checkpoint_matches(resume, issue, facts, item) or
            _resume_approval_matches(resume, issue, facts, item))


def _migration_input_fingerprint(page, state):
    """Fingerprint only first-migration inputs; a run's own pending field is excluded on reread."""
    return _digest({
        "notion_page_id": page.get("id"),
        "notion_status": read_select(page, "작업 상태"),
        "migration_complete": state["migration_complete"],
        "project_item_id": state["project_item_id"],
        "pending": state["pending"],
        "hold": state["hold"],
        "resume": state["resume"],
    })


def _migration_input_unchanged(notion, source_id, row, pending, project_id):
    """Read back Notion after our pending write and compare with the pre-pending input."""
    latest = notion.request("GET", f"/pages/{identifier(row.get('id'))}")
    bound_page(latest, source_id)
    require(not is_archived(latest), "최초 이관 중 Notion 행이 보관되었습니다")
    latest_state_text = read_text(latest, "동기화 내부 상태")
    latest_state = decode_internal(latest_state_text, kind="issue",
                                   project_id=project_id,
                                   object_id=pending["issue_id"])
    if latest_state is None or latest_state.get("pending") != pending:
        return False, latest
    pre_pending_state = json.loads(json.dumps(latest_state))
    pre_pending_state["pending"] = None
    if (pending["kind"] == "add" and (pre_pending_state.get("hold") or {}).get("code") ==
            "PROJECT_ADD_UNCERTAIN"):
        # This visible hold is our recovery marker, not a change to the migration input.
        pre_pending_state["hold"] = None
    return (_migration_input_fingerprint(latest, pre_pending_state) ==
            pending["migration_fingerprint"]), latest


def _new_hold(code, message, fingerprint):
    return {"code": code, "message": message, "fingerprint": fingerprint}


def _plan_issue(issue, item, notion_row, state, facts, cutoff, project_id, status_options,
                *, approved_resume=False):
    fingerprint_refs, eligible = _linked_pr_facts(issue, facts)
    fingerprint = _source_fingerprint(issue, fingerprint_refs, item)
    project_by_id = {value: name for name, value in status_options.items()}
    current = project_by_id.get(item["status_option_id"]) if item else None
    require(item is not None, "Issue Project 항목이 없습니다")
    duplicate = issue.get("duplicateOf")
    if duplicate is not None:
        require(isinstance(duplicate, dict) and isinstance(duplicate.get("id"), str) and
                isinstance(duplicate.get("url"), str) and duplicate["url"].startswith("https://github.com/"),
                "GitHub duplicateOf 관계 형식 오류")
    events = _issue_reopens(issue)
    event_times = {}
    for at, event in events:
        event_times.setdefault(at, []).append(event["id"])
    if any(len(ids) > 1 for ids in event_times.values()):
        return {"hold": _new_hold("REOPEN_ORDER_AMBIGUOUS",
                                  "같은 시각의 재오픈 순서를 확인할 수 없습니다.", fingerprint),
                "fingerprint": fingerprint}
    cutoff_dt = timestamp(cutoff)
    latest_before = None
    unseen_after = []
    state_has_checkpoint = bool(state.get("reopen_baseline_id") or state.get("reopen_last_id"))
    prior_last = state.get("reopen_last_id")
    event_ids = {event["id"] for _, event in events}
    if prior_last is not None:
        require(prior_last in event_ids, "저장된 재오픈 checkpoint가 GitHub timeline에서 사라졌습니다")
    for at, event in events:
        delta = abs((at - cutoff_dt).total_seconds())
        if delta <= BOUNDARY_SECONDS:
            return {"hold": _new_hold("AMBIGUOUS_CUTOFF", "기준 시각 경계의 재오픈을 판정할 수 없습니다.", fingerprint),
                    "fingerprint": fingerprint}
        if at < cutoff_dt:
            latest_before = event
        elif prior_last is None or event["id"] != prior_last:
            if prior_last is None or at > next(t for t, e in events if e["id"] == prior_last):
                unseen_after.append((at, event))
    if not state_has_checkpoint:
        state["reopen_baseline_id"] = latest_before["id"] if latest_before else None
        state["reopen_last_id"] = state["reopen_baseline_id"]
    latest_new = max(unseen_after, default=None, key=lambda pair: (pair[0], pair[1]["id"]))
    reopened = latest_new is not None and issue.get("state") == "OPEN"
    if latest_new and issue.get("state") == "CLOSED":
        closed_at = issue.get("closedAt")
        if not closed_at:
            return {"hold": _new_hold("REOPEN_CLOSE_ORDER", "종료와 재오픈의 순서를 검증할 수 없습니다.", fingerprint),
                    "fingerprint": fingerprint}
        close_dt = timestamp(closed_at)
        delta = abs((close_dt - latest_new[0]).total_seconds())
        if delta <= BOUNDARY_SECONDS:
            return {"hold": _new_hold("REOPEN_CLOSE_ORDER", "종료와 재오픈 시각 경계가 모호합니다.", fingerprint),
                    "fingerprint": fingerprint}
        if latest_new[0] > close_dt:
            return {"hold": _new_hold("REOPEN_CLOSE_ORDER", "종료된 이슈 뒤의 재오픈 기록이 모순됩니다.", fingerprint),
                    "fingerprint": fingerprint}
        state["reopen_last_id"] = latest_new[1]["id"]

    state["review_pr_hash"] = _digest([entry["id"] for entry in eligible])
    state["project_item_id"] = item["id"]
    if issue.get("state") == "CLOSED":
        state["migration_complete"] = True
        reason = issue.get("stateReason")
        if duplicate and reason in {"COMPLETED", "NOT_PLANNED"}:
            return {"hold": _new_hold("CLOSURE_DUPLICATE_CONFLICT", "종료 사유와 duplicateOf 관계가 모순됩니다.", fingerprint),
                    "fingerprint": fingerprint}
        if reason == "COMPLETED":
            return {"target": "완료", "end_reason": "완료", "representative": None,
                    "confirmation": "", "state": state, "fingerprint": fingerprint, "event_id": None,
                    "automatic": True, "migration_complete": True}
        if reason == "NOT_PLANNED":
            return {"target": None, "end_reason": "미계획", "representative": None,
                    "confirmation": "", "state": state, "fingerprint": fingerprint, "event_id": None,
                    "automatic": True, "migration_complete": True}
        if reason == "DUPLICATE":
            note = "대표 이슈 지정 필요" if not duplicate else ""
            return {"target": None, "end_reason": "중복", "representative": duplicate.get("url") if duplicate else None,
                    "confirmation": note, "state": state, "fingerprint": fingerprint, "event_id": None,
                    "automatic": True, "migration_complete": True}
        return {"target": None, "end_reason": "확인 필요", "representative": None,
                "confirmation": "GitHub 종료 사유를 확인해야 합니다.", "state": state,
                "fingerprint": fingerprint, "event_id": None, "automatic": True,
                "migration_complete": True}

    if issue.get("state") != "OPEN":
        return {"hold": _new_hold("ISSUE_STATE_UNKNOWN", "GitHub Issue 상태를 판정할 수 없습니다.", fingerprint),
                "fingerprint": fingerprint}
    if duplicate:
        return {"hold": _new_hold("OPEN_DUPLICATE_CONFLICT", "열린 이슈에 duplicateOf 관계가 있습니다.", fingerprint),
                "fingerprint": fingerprint}
    if reopened:
        state["reopen_last_id"] = latest_new[1]["id"]
        target = "검토 중" if eligible else "백로그"
        if target == "검토 중" and state["review_cycle"] <= state["review_return_cycle"]:
            state["review_cycle"] += 1
        state["migration_complete"] = True
        return {"target": target, "end_reason": None, "representative": None, "confirmation": "",
                "state": state, "fingerprint": fingerprint, "event_id": latest_new[1]["id"],
                "automatic": True, "migration_complete": True}

    if eligible:
        if state["review_cycle"] <= state["review_return_cycle"]:
            state["review_cycle"] += 1
        state["review_pr_hash"] = _digest([entry["id"] for entry in eligible])
        state["migration_complete"] = True
        return {"target": "검토 중", "end_reason": None, "representative": None, "confirmation": "",
                "state": state, "fingerprint": fingerprint, "event_id": None,
                "automatic": True, "migration_complete": True}

    if current == "완료":
        return {"hold": _new_hold("OPEN_PROJECT_COMPLETED", "열린 이슈의 Project 상태가 완료입니다.", fingerprint),
                "fingerprint": fingerprint}

    if current == "검토 중":
        if state["review_cycle"] > state["review_return_cycle"]:
            state["review_return_cycle"] = state["review_cycle"]
            state["migration_complete"] = True
            return {"target": "진행 중", "end_reason": None, "representative": None, "confirmation": "",
                    "state": state, "fingerprint": fingerprint, "event_id": None,
                    "automatic": True, "migration_complete": True}
        return {"hold": _new_hold("REVIEW_WITHOUT_CYCLE", "검토 중 상태의 자동 전환 기록이 없습니다.", fingerprint),
                "fingerprint": fingerprint}

    if current in GENERAL_STATES and state["review_cycle"] > state["review_return_cycle"]:
        # A human-selected general state closes the previous automatic PR review cycle.
        state["review_return_cycle"] = state["review_cycle"]
        state["review_pr_hash"] = _digest([])

    age = (timestamp(issue["createdAt"]) - cutoff_dt).total_seconds()
    if abs(age) <= BOUNDARY_SECONDS:
        return {"hold": _new_hold("AMBIGUOUS_CUTOFF", "이슈 생성 시각이 기준선 경계에 있습니다.", fingerprint),
                "fingerprint": fingerprint}
    is_new = age > BOUNDARY_SECONDS
    if not state["migration_complete"] and not is_new:
        notion_status = read_select(notion_row, "작업 상태") if notion_row else None
        if approved_resume:
            if current in GENERAL_STATES:
                state["migration_complete"] = True
                return {"target": current, "end_reason": None, "representative": None,
                        "confirmation": "", "state": state, "fingerprint": fingerprint,
                        "event_id": None, "automatic": False, "migration_complete": True,
                        "initial_migration": True}
            return {"hold": _new_hold("MIGRATION_UNSET", "수동 재개 전에 Project에서 상태를 정해야 합니다.", fingerprint),
                    "fingerprint": fingerprint}
        if current in GENERAL_STATES and notion_status in GENERAL_STATES and current == notion_status:
            state["migration_complete"] = True
            return {"target": current, "end_reason": None, "representative": None, "confirmation": "",
                    "state": state, "fingerprint": fingerprint, "event_id": None,
                    "automatic": False, "migration_complete": True, "initial_migration": True}
        if current is None and notion_status in GENERAL_STATES:
            state["migration_complete"] = True
            return {"target": notion_status, "end_reason": None, "representative": None, "confirmation": "",
                    "state": state, "fingerprint": fingerprint, "event_id": None,
                    "automatic": False, "migration_complete": True, "initial_migration": True}
        if current in GENERAL_STATES and notion_status is None:
            state["migration_complete"] = True
            return {"target": current, "end_reason": None, "representative": None, "confirmation": "",
                    "state": state, "fingerprint": fingerprint, "event_id": None,
                    "automatic": False, "migration_complete": True, "initial_migration": True}
        code = "MIGRATION_CONFLICT" if current in GENERAL_STATES and notion_status in GENERAL_STATES else "MIGRATION_UNSET"
        message = ("최초 이관에서 Project와 Notion 상태가 다릅니다." if code == "MIGRATION_CONFLICT"
                   else "최초 이관에서 Project 또는 Notion 상태가 비어 있습니다.")
        return {"hold": _new_hold(code, message, fingerprint), "fingerprint": fingerprint}

    if not state["migration_complete"] and is_new:
        state["migration_complete"] = True
        return {"target": "백로그", "end_reason": None, "representative": None, "confirmation": "",
                "state": state, "fingerprint": fingerprint, "event_id": None,
                "automatic": True, "migration_complete": True}
    if current is None:
        return {"hold": _new_hold("PROJECT_STATUS_UNSET",
                                  "이관된 열린 이슈의 Project Status가 비어 있습니다. 일반 상태를 선택해야 합니다.",
                                  fingerprint),
                "fingerprint": fingerprint}
    if current not in set(status_options) - {"완료"} and current is not None:
        return {"hold": _new_hold("PROJECT_STATUS_UNKNOWN", "Project 상태를 판정할 수 없습니다.", fingerprint),
                "fingerprint": fingerprint}
    state["migration_complete"] = True
    return {"target": current, "end_reason": None, "representative": None, "confirmation": "",
            "state": state, "fingerprint": fingerprint, "event_id": None,
            "automatic": False, "migration_complete": True}


def _pending_saved_migration_approval(state, pending):
    resume = state.get("resume")
    hold = state.get("hold")
    return bool(
        pending["migration_fingerprint"] is not None and hold and
        hold.get("code") in RESUMABLE_HOLD_CODES and resume and
        resume.get("display_pending") and
        resume.get("approved_option_id") == pending["before_option_id"] and
        resume.get("fingerprint") == pending["facts_fingerprint"])


def _canonical_pending_status_plan(issue, state, facts, cutoff, config, pending, *,
                                   replay_applied_reopen=False,
                                   restore_review_return=False):
    """Rebuild a pending plan from captured inputs and an explicit checkpoint phase."""
    plan_state = json.loads(json.dumps(state))
    plan_state["pending"] = None
    plan_state["projection"] = None
    item = {"id": pending["item_id"],
            "status_option_id": pending["before_option_id"]}

    # A confirmed reopen checkpoint has already advanced reopen_last_id. Rewind only
    # the synthetic planning copy so the source event can be replayed canonically.
    if replay_applied_reopen:
        replay_event_id = pending["event_id"]
        events = _issue_reopens(issue)
        event_time = next((at for at, event in events
                           if event["id"] == replay_event_id), None)
        require(replay_event_id is not None and event_time is not None and
                event_time.timestamp() > timestamp(cutoff).timestamp() + BOUNDARY_SECONDS and
                state["reopen_last_id"] == replay_event_id,
                "적용된 pending 재오픈 이벤트가 timeline checkpoint와 불일치합니다")
        prior_events = [(at, event["id"]) for at, event in events if at < event_time]
        prior = max(prior_events, default=None, key=lambda entry: (entry[0], entry[1]))
        plan_state["reopen_last_id"] = prior[1] if prior else plan_state["reopen_baseline_id"]

    before_name = {value: name for name, value in config["status_options"].items()}.get(
        pending["before_option_id"])
    target_name = {value: name for name, value in config["status_options"].items()}.get(
        pending["target_option_id"])
    if (restore_review_return and before_name == "검토 중" and
            target_name == "진행 중" and
            plan_state["review_cycle"] == plan_state["review_return_cycle"] and
            plan_state["review_cycle"] > 0):
        # The canonical review-return transition stores review_return_cycle=review_cycle.
        # Restore a valid pre-transition value for the pure re-plan.
        plan_state["review_return_cycle"] -= 1

    captured_notion_status = pending["notion_status"]
    captured_row = {"properties": {"작업 상태": {
        "select": {"name": captured_notion_status} if captured_notion_status else None}}}
    approved_resume = _pending_saved_migration_approval(state, pending)

    return _plan_issue(issue, item, captured_row, plan_state, facts, cutoff,
                       config["project_id"], config["status_options"],
                       approved_resume=approved_resume)


def _control_state(project_id, cutoff, run_id):
    return {"v": 1, "project_id": project_id, "migration_cutoff": cutoff,
            "last_success_at": None, "run_id": run_id, "last_result": None}


def _ensure_notion_row(notion, source_id, control, index, kind, row, canonical_id, now):
    key = key_for(REPOSITORY_ID, canonical_id)
    existing = index.get(key)
    metadata = _make_metadata(kind, row, key)
    if existing:
        require(read_select(existing, "종류") == kind and
                existing["properties"]["번호"]["number"] == row["number"],
                "기존 canonical Notion 행 종류/번호가 source와 다릅니다")
        return existing, False
    if key in index:
        raise SyncError("Notion canonical key collision")
    update_control_pending(notion, source_id, control, key)
    properties = {**metadata, "동기화 시각": {"date": {"start": now}},
                  "Pending create": text_property("")}
    if kind == "Issue":
        properties["작업 상태"] = {"select": None}
        properties["종료 사유"] = {"select": None}
        properties["대표 이슈"] = {"url": None}
        properties["확인 필요"] = text_property("")
        properties["동기화 내부 상태"] = text_property("")
    else:
        properties["동기화 내부 상태"] = text_property("")
    created = notion.request("POST", "/pages", {"parent": {"data_source_id": source_id},
                           "properties": properties}, create=True)
    require(isinstance(created, dict) and isinstance(created.get("id"), str),
            "Notion 생성 응답 ID 누락; 생성 fence 유지")
    page_id = identifier(created["id"])
    confirmed = notion.request("GET", f"/pages/{page_id}")
    bound_page(confirmed, source_id)
    require(not is_archived(confirmed) and read_text(confirmed, "동기화 키") == key and
            read_select(confirmed, "종류") == kind, "Notion 생성행 식별 readback 오류; fence 유지")
    _verify_properties(confirmed, properties)
    update_control_pending(notion, source_id, control, "")
    index[key] = confirmed
    return confirmed, True


def update_control_pending(notion, source_id, control, value):
    _patch_page(notion, source_id, control, {"Pending create": text_property(value)})


def _project_snapshot(client, config, facts):
    project = gp.fetch_project(client, project_id=config["project_id"], owner_id=config["owner_id"],
                               status_field_id=config["status_field_id"],
                               status_options=config["status_options"],
                               repository_node_id=facts["repository_node_id"])
    for issue_id, item in project["items"].items():
        issue = facts["issues"].get(issue_id)
        require(issue is not None, "Project 대상 저장소 Issue가 GraphQL 전체 목록에 없습니다")
        require(item["content_id"] == issue["id"], "Project Issue ID/content node ID 관계 불일치")
    for issue_id, archived_items in project.get("archived_items", {}).items():
        issue = facts["issues"].get(issue_id)
        require(issue is not None and isinstance(archived_items, list) and archived_items,
                "보관 Project Issue가 source snapshot에서 확인되지 않습니다")
        require(all(item.get("content_id") == issue["id"] for item in archived_items),
                "보관 Project item의 Issue content node ID 불일치")
    return project


def _status_option(project, name):
    return project["status_options"][name] if name else None


def _verify_issue_state_write(notion, source_id, row, state):
    _save_issue_state(notion, source_id, row, state)


def _resume_numbers(raw):
    if raw is None or raw == "":
        return []
    require(isinstance(raw, str) and len(raw) <= 4000, "resolve_issue_numbers 입력 크기 오류")
    parts = raw.split(",")
    require(parts and all(re.fullmatch(r"\s*[1-9][0-9]*\s*", part) for part in parts),
            "resolve_issue_numbers는 양의 이슈 번호를 comma-separated로 입력해야 합니다")
    numbers = {int(part.strip()) for part in parts}
    require(all(n <= 2147483647 for n in numbers), "resolve_issue_numbers 범위 초과")
    return sorted(numbers)


def _validate_manual_resume(github, raw, env):
    numbers = _resume_numbers(raw)
    if not numbers:
        return set(), None
    require(env.get("GITHUB_EVENT_NAME") == "workflow_dispatch",
            "수동 보류 재개는 workflow_dispatch에서만 허용됩니다")
    actor = env.get("GITHUB_ACTOR")
    triggering_actor = env.get("GITHUB_TRIGGERING_ACTOR")
    run_id = env.get("GITHUB_RUN_ID")
    require(env.get("GITHUB_RUN_ATTEMPT") == "1",
            "수동 보류 재개는 새 workflow_dispatch의 첫 실행 시도에서만 허용됩니다")
    require(isinstance(run_id, str) and re.fullmatch(r"[1-9][0-9]*", run_id),
            "GitHub workflow run ID 형식 오류")
    actor_id = gp.resolve_actor(github, actor)
    triggering_actor_id = gp.resolve_actor(github, triggering_actor)
    require(triggering_actor_id == actor_id,
            "실제 workflow 재실행 요청자가 원래 PM actor와 다릅니다")
    return set(numbers), {"actor_id": actor_id, "run_id": run_id}


def _preview_resolutions(numbers, source_by_number, index, project, facts, config, cutoff):
    preview = []
    for number in sorted(numbers):
        kind, issue_id, issue = source_by_number[number]
        key = key_for(REPOSITORY_ID, issue_id)
        row = index[key]
        state = json.loads(read_text(row, "동기화 내부 상태"))
        item = project["items"].get(issue_id)
        prior_hold = state["hold"]
        if item is None or project.get("archived_items", {}).get(issue_id):
            preview.append({"issue_number": number, "result": "still_held",
                            "reason": "Project 항목이 활성 상태로 확인되지 않습니다."})
            continue
        plan_state = json.loads(json.dumps(state))
        approved = prior_hold["code"] in RESUMABLE_HOLD_CODES
        plan = _plan_issue(issue, item, row, plan_state, facts, cutoff, config["project_id"],
                           config["status_options"], approved_resume=approved)
        if plan.get("hold"):
            preview.append({"issue_number": number, "result": "still_held",
                            "reason": plan["hold"]["message"]})
            continue
        before = {value: name for name, value in project["status_options"].items()}.get(
            item["status_option_id"])
        preview.append({"issue_number": number, "result": "would_resume",
                        "prior_hold": prior_hold["code"], "project_before": before,
                        "project_after": plan.get("target"),
                        "project_change": before != plan.get("target")})
    return preview


def _projection_properties(metadata, *, target, end_reason, representative, confirmation,
                           state, now, kind):
    properties = dict(metadata)
    properties["동기화 시각"] = {"date": {"start": now}}
    if kind == "Issue":
        properties.update({
            "작업 상태": {"select": {"name": target} if target else None},
            "종료 사유": {"select": {"name": end_reason} if end_reason else None},
            "대표 이슈": {"url": representative},
            "확인 필요": text_property(confirmation),
            "동기화 내부 상태": text_property(canonical_json(state)),
        })
    return properties


def _display_hold(notion, source_id, row, metadata, state, hold, now, *, preserve_task_status=False):
    state["hold"] = hold
    state["resume"] = None
    _verify_issue_state_write(notion, source_id, row, state)
    visible = "보류: " + hold["message"]
    if preserve_task_status:
        props = {**metadata, "동기화 시각": {"date": {"start": now}},
                 "확인 필요": text_property(visible),
                 "동기화 내부 상태": text_property(canonical_json(state))}
    else:
        props = _projection_properties(metadata, target=None, end_reason=None, representative=None,
                                       confirmation=visible, state=state, now=now, kind="Issue")
    _patch_page(notion, source_id, row, props)


def _resolve_pending_add(notion, github, source_id, row, state, project, facts, issue):
    item = project["items"].get(issue["databaseId"])
    pending = state["pending"]
    require(pending and pending["kind"] == "add", "Project add pending 복구 종류 오류")
    fingerprint = _source_fingerprint(issue, _linked_pr_facts(issue, facts)[0], None)
    if item is None:
        return None, _new_hold("PROJECT_ADD_UNCERTAIN", "Project 추가 결과를 확인할 수 없습니다.",
                               fingerprint)
    require(item["content_id"] == issue["id"], "pending Project item content ID 불일치")
    latest = gp.fetch_issue_detail(github, issue["id"])
    latest_refs, _ = _linked_pr_facts(latest, facts, allow_snapshot_drift=True)
    latest_fingerprint = _source_fingerprint(latest, latest_refs, None)
    if latest_fingerprint != pending["facts_fingerprint"]:
        state["pending"] = None
        state["project_item_id"] = item["id"]
        return item, _new_hold("SOURCE_CHANGED_BEFORE_WRITE",
                               "Project 추가 결과를 확인하는 중 GitHub 원본이 달라졌습니다.",
                               _source_fingerprint(latest, latest_refs, item))
    if pending["migration_fingerprint"] is not None:
        unchanged, latest = _migration_input_unchanged(
            notion, source_id, row, pending, state["project_id"])
        if not unchanged:
            state["pending"] = None
            state["project_item_id"] = item["id"]
            state["hold"] = _new_hold(
                "MIGRATION_INPUT_CHANGED",
                "Project 항목 추가 중 최초 이관 입력이 달라져 수동 확인이 필요합니다.",
                _digest({"facts": pending["facts_fingerprint"],
                                 "migration_input": _migration_input_fingerprint(
                                     latest, decode_internal(read_text(latest, "동기화 내부 상태"),
                                 kind="issue", project_id=state["project_id"],
                                 object_id=issue["databaseId"]))}))
            _verify_issue_state_write(notion, source_id, row, state)
            return item, state["hold"]
    state["pending"]["confirmed"] = True
    state["pending"]["project_item_id"] = item["id"]
    state["pending"]["checkpoint"] = {"content_id": issue["id"], "item_id": item["id"]}
    _verify_issue_state_write(notion, source_id, row, state)
    state["project_item_id"] = item["id"]
    state["pending"] = None
    _verify_issue_state_write(notion, source_id, row, state)
    return item, None


def _pending_status_resolution(issue, item, notion_row, state, facts, config, cutoff, *,
                               manual_resume=False, snapshot_issue=None):
    """Purely decide whether a status pending is checkpointable or needs fresh PM review."""
    resolved_state = json.loads(json.dumps(state))
    pending = resolved_state["pending"]
    require(pending and pending["kind"] == "status", "Project status pending 복구 종류 오류")
    require(item is not None and item["id"] == pending["item_id"],
            "Project status pending 항목 식별 불일치")
    linked, _ = _linked_pr_facts(issue, facts, allow_snapshot_drift=True)
    before_item = {"id": item["id"], "status_option_id": pending["before_option_id"]}
    current_fingerprint = _source_fingerprint(issue, linked, before_item)

    # A difference between the initial full snapshot and this per-issue reread is a
    # current-run race. Keep it issue-local and never authorize it as a historical delta.
    if snapshot_issue is not None:
        snapshot_linked, _ = _linked_pr_facts(snapshot_issue, facts)
        snapshot_fingerprint = _source_fingerprint(snapshot_issue, snapshot_linked, before_item)
        if current_fingerprint != snapshot_fingerprint:
            resolved_state["pending"] = None
            return {"mode": "hold", "state": resolved_state,
                    "hold": _new_hold(
                        "SOURCE_CHANGED_BEFORE_WRITE",
                        "전체 조회 뒤 GitHub 원본이 달라져 이 이슈의 복구를 보류했습니다.",
                        _source_fingerprint(issue, linked, item))}

    facts_match = current_fingerprint == pending["facts_fingerprint"]
    target_match = item["status_option_id"] == pending["target_option_id"]
    if manual_resume and (not facts_match or not target_match):
        # A fresh PM dispatch adopts only the active Project value when it agrees
        # with a canonical plan over today's source. The old checkpoint is discarded.
        resolved_state["pending"] = None
        resolved_state["projection"] = None
        plan_state = json.loads(json.dumps(resolved_state))
        plan = _plan_issue(issue, item, notion_row, plan_state, facts, cutoff,
                           config["project_id"], config["status_options"],
                           approved_resume=True)
        if plan.get("hold"):
            return {"mode": "hold", "state": resolved_state, "hold": plan["hold"]}
        expected = config["status_options"].get(plan.get("target"))
        if expected != item["status_option_id"]:
            return {"mode": "hold", "state": resolved_state,
                    "hold": _new_hold(
                        "PENDING_RESULT_UNCLEAR",
                        "PM 확인한 Project 상태가 현재 GitHub 사실의 상태 판정과 다릅니다. 값을 정리한 뒤 새 PM 재개가 필요합니다.",
                        _source_fingerprint(issue, linked, item))}
        return {"mode": "fresh_resume", "state": resolved_state, "plan": plan}

    if not facts_match:
        resolved_state["pending"] = None
        return {"mode": "hold", "state": resolved_state,
                "hold": _new_hold("PENDING_FACTS_CHANGED", "pending 이후 원본 사실이 달라졌습니다.",
                                   _source_fingerprint(issue, linked, item))}
    if not target_match:
        return {"mode": "hold", "state": resolved_state,
                "hold": _new_hold("PENDING_RESULT_UNCLEAR",
                                   "이전 Project 전환 결과가 확인되지 않아 재시도하지 않습니다.",
                                   _source_fingerprint(issue, linked, item))}
    return {"mode": "checkpoint", "state": resolved_state}


def _recover_pending_status(notion, source_id, row, state, project, github, facts, issue,
                           config, cutoff, *, manual_resume=False):
    pending = state["pending"]
    require(pending and pending["kind"] == "status", "Project status pending 복구 종류 오류")
    item = project["items"].get(issue["databaseId"])
    require(item is not None and item["id"] == pending["item_id"],
            "Project status pending 항목 식별 불일치")
    latest = gp.fetch_issue_detail(github, issue["id"])
    decision = _pending_status_resolution(
        latest, item, row, state, facts, config, cutoff, manual_resume=manual_resume,
        snapshot_issue=issue)
    state.update(decision["state"])
    if decision["mode"] == "hold":
        return decision["hold"]
    if decision["mode"] == "fresh_resume":
        return None
    linked, _ = _linked_pr_facts(latest, facts, allow_snapshot_drift=True)
    source_fingerprint = _source_fingerprint(latest, linked,
                                             {"id": item["id"],
                                              "status_option_id": pending["before_option_id"]})
    if pending["migration_fingerprint"] is not None:
        unchanged, latest_page = _migration_input_unchanged(
            notion, source_id, row, pending, config["project_id"])
        if not unchanged:
            state["pending"] = None
            return _new_hold("MIGRATION_INPUT_CHANGED",
                             "pending 처리 중 최초 이관 입력이 달라져 수동 확인이 필요합니다.",
                             _digest({"source": source_fingerprint,
                                      "migration_input": _migration_input_fingerprint(
                                          latest_page,
                                          decode_internal(read_text(latest_page, "동기화 내부 상태"),
                                              kind="issue", project_id=config["project_id"],
                                              object_id=issue["databaseId"]))}))
    checkpoint = pending["checkpoint"]
    if not state["migration_complete"] and state["reopen_baseline_id"] is None:
        cutoff_time = timestamp(cutoff)
        prior_events = [(at, event["id"]) for at, event in _issue_reopens(latest)
                        if at.timestamp() < cutoff_time.timestamp() - BOUNDARY_SECONDS]
        initial_baseline = max(prior_events, default=None, key=lambda entry: (entry[0], entry[1]))
        if initial_baseline is not None:
            baseline_id = initial_baseline[1]
            require(state["reopen_last_id"] in {None, baseline_id},
                    "최초 이관 복구의 재오픈 baseline/처리 ID가 source와 불일치합니다")
            state["reopen_baseline_id"] = baseline_id
    _apply_status_checkpoint(state, checkpoint)
    state["project_item_id"] = item["id"]
    expected_fingerprint = _source_fingerprint(latest, linked, item)
    if state["resume"] and state["resume"]["display_pending"]:
        state["resume"]["expected_option_id"] = item["status_option_id"]
        state["resume"]["expected_fingerprint"] = expected_fingerprint
    state["pending"]["confirmed"] = True
    _verify_issue_state_write(notion, source_id, row, state)
    _apply_status_checkpoint(state, checkpoint)
    state["pending"] = None
    _verify_issue_state_write(notion, source_id, row, state)
    return None


def _add_project_item_once(notion, github, project_client, source_id, row, state, project, facts,
                           issue, config, fingerprint):
    pending = {"kind": "add", "issue_id": issue["databaseId"], "item_id": None,
               "event_id": None, "before_option_id": None, "target_option_id": None,
               "facts_fingerprint": fingerprint,
               "migration_fingerprint": (_migration_input_fingerprint(row, state)
                                         if not state["migration_complete"] else None),
               "notion_status": read_select(row, "작업 상태"),
               "confirmed": False, "project_item_id": None,
               "checkpoint": {"content_id": issue["id"]}}
    state["pending"] = pending
    _verify_issue_state_write(notion, source_id, row, state)
    latest = gp.fetch_issue_detail(github, issue["id"])
    latest_refs, _ = _linked_pr_facts(latest, facts, allow_snapshot_drift=True)
    latest_fp = _source_fingerprint(latest, latest_refs, None)
    if latest_fp != fingerprint:
        state["pending"] = None
        hold = _new_hold("SOURCE_CHANGED_BEFORE_WRITE",
                         "Project 추가 직전 GitHub 원본이 달라져 항목 추가를 멈췄습니다.",
                         _source_fingerprint(latest, latest_refs, None))
        state["hold"] = hold
        return project, None, hold
    if state["pending"]["migration_fingerprint"] is not None:
        unchanged, latest_page = _migration_input_unchanged(
            notion, source_id, row, state["pending"], config["project_id"])
        if not unchanged:
            latest_state = decode_internal(read_text(latest_page, "동기화 내부 상태"),
                                           kind="issue", project_id=config["project_id"],
                                           object_id=issue["databaseId"])
            state["pending"] = None
            hold = _new_hold("MIGRATION_INPUT_CHANGED",
                             "Project 추가 직전 최초 이관 입력이 달라져 항목 추가를 멈췄습니다.",
                             _digest({"source": latest_fp,
                                      "migration_input": _migration_input_fingerprint(
                                          latest_page, latest_state)}))
            state["hold"] = hold
            return project, None, hold

    # Recheck the full Project index immediately before the add. A newly appeared
    # active or archived item is an issue-local race, never an invitation to duplicate it.
    latest_project = _project_snapshot(project_client, config, facts)
    raced_item = latest_project["items"].get(issue["databaseId"])
    archived = latest_project.get("archived_items", {}).get(issue["databaseId"], [])
    if raced_item is not None or archived:
        state["pending"] = None
        fingerprint_item = raced_item
        hold_code = "PROJECT_ITEM_RACE" if raced_item is not None else "PROJECT_ITEM_ARCHIVED"
        hold = _new_hold(hold_code,
                         "Project 추가 직전 기존 항목이 확인되어 자동 추가를 멈췄습니다.",
                         _source_fingerprint(latest, latest_refs, fingerprint_item))
        state["hold"] = hold
        return latest_project, raced_item, hold
    try:
        gp.add_project_issue(project_client, config["project_id"], issue["id"])
    except gp.SyncError:
        # The request may have committed. Leave the durable pending marker for a
        # later PM-confirmed read; do not read back and write again in this run.
        raise SyncError("Project 추가 결과 불명; pending을 유지하고 수동 확인 필요") from None
    project = _project_snapshot(project_client, config, facts)
    item = project["items"].get(issue["databaseId"])
    require(item is not None and item["content_id"] == issue["id"],
            "Project 추가 결과 0개/대상 불일치; pending 유지")
    if pending["migration_fingerprint"] is not None:
        unchanged, latest_page = _migration_input_unchanged(
            notion, source_id, row, pending, config["project_id"])
        if not unchanged:
            state["pending"] = None
            state["project_item_id"] = item["id"]
            state["hold"] = _new_hold(
                "MIGRATION_INPUT_CHANGED",
                "Project 항목 추가 중 최초 이관 입력이 달라져 수동 확인이 필요합니다.",
                _digest({"facts": pending["facts_fingerprint"],
                         "migration_input": _migration_input_fingerprint(
                             latest_page, decode_internal(read_text(latest_page, "동기화 내부 상태"),
                                 kind="issue", project_id=config["project_id"],
                                 object_id=issue["databaseId"]))}))
            _verify_issue_state_write(notion, source_id, row, state)
            return project, item, state["hold"]
    state["pending"]["confirmed"] = True
    state["pending"]["project_item_id"] = item["id"]
    state["pending"]["checkpoint"] = {"content_id": issue["id"], "item_id": item["id"]}
    _verify_issue_state_write(notion, source_id, row, state)
    state["project_item_id"] = item["id"]
    state["pending"] = None
    _verify_issue_state_write(notion, source_id, row, state)
    return project, item, None


def _apply_project_status(notion, github, project_client, source_id, row, state, project,
                          facts, issue, item, plan, config):
    target_id = _status_option(project, plan["target"])
    before = item["status_option_id"]
    notion_status = read_select(row, "작업 상태")
    pending = {"kind": "status", "issue_id": issue["databaseId"], "item_id": item["id"],
               "event_id": plan.get("event_id"), "before_option_id": before,
               "target_option_id": target_id, "facts_fingerprint": plan["fingerprint"],
               "migration_fingerprint": (_migration_input_fingerprint(row, state)
                                         if plan.get("initial_migration") else None),
               "notion_status": notion_status, "confirmed": False,
               "project_item_id": item["id"],
               "checkpoint": {"migration_complete": bool(plan.get("migration_complete")),
                              "review_cycle": plan["state"]["review_cycle"],
                              "review_return_cycle": plan["state"]["review_return_cycle"],
                              "reopen_last_id": plan["state"]["reopen_last_id"],
                              "review_pr_hash": plan["state"]["review_pr_hash"]}}
    state["pending"] = pending
    _verify_issue_state_write(notion, source_id, row, state)

    latest = gp.fetch_issue_detail(github, issue["id"])
    latest_item = gp.fetch_project_item(project_client, config["project_id"], item["id"],
                                        config["status_field_id"], allow_archived=True)
    latest_refs, _ = _linked_pr_facts(latest, facts, allow_snapshot_drift=True)
    if latest_item.get("is_archived"):
        state["pending"] = None
        hold = _new_hold("PROJECT_ITEM_ARCHIVED",
                         "Project 항목이 쓰기 직전에 보관되어 자동 변경을 멈췄습니다.",
                         _source_fingerprint(latest, latest_refs, latest_item))
        updated_project = dict(project)
        updated_items = dict(project["items"])
        updated_items.pop(issue["databaseId"], None)
        updated_project["items"] = updated_items
        archived_items = dict(project.get("archived_items", {}))
        archived_items[issue["databaseId"]] = [{"id": latest_item["id"],
                                                 "content_id": latest_item["content_id"],
                                                 "is_archived": True}]
        updated_project["archived_items"] = archived_items
        return updated_project, None, hold
    latest_fingerprint = _source_fingerprint(latest, latest_refs, latest_item)
    if latest_fingerprint != plan["fingerprint"]:
        state["pending"] = None
        hold = _new_hold("SOURCE_CHANGED_BEFORE_WRITE",
                         "Project 변경 직전 GitHub 또는 Project 값이 달라져 자동 변경을 멈췄습니다.",
                         latest_fingerprint)
        updated_project = dict(project)
        updated_items = dict(project["items"])
        updated_items[issue["databaseId"]] = latest_item
        updated_project["items"] = updated_items
        return updated_project, latest_item, hold
    if pending["migration_fingerprint"] is not None:
        unchanged, latest_page = _migration_input_unchanged(notion, source_id, row, pending,
                                                             config["project_id"])
        if not unchanged:
            state["pending"] = None
            hold_fingerprint = _digest({"source": plan["fingerprint"],
                                        "migration_input": _migration_input_fingerprint(
                                            latest_page,
                                            decode_internal(read_text(latest_page, "동기화 내부 상태"),
                                                kind="issue", project_id=config["project_id"],
                                                object_id=issue["databaseId"]))})
            hold = _new_hold("MIGRATION_INPUT_CHANGED",
                             "최초 이관 중 Notion 상태/내부 입력이 달라져 자동 반영을 멈췄습니다.",
                             hold_fingerprint)
            updated_project = dict(project)
            updated_items = dict(project["items"])
            updated_items[issue["databaseId"]] = latest_item
            updated_project["items"] = updated_items
            return updated_project, latest_item, hold

    if target_id != before:
        if target_id is None:
            gp.clear_project_status(project_client, config["project_id"], item["id"], config["status_field_id"])
        else:
            gp.set_project_status(project_client, config["project_id"], item["id"],
                                  config["status_field_id"], target_id)
        verified = gp.fetch_project_item(project_client, config["project_id"], item["id"],
                                         config["status_field_id"])
        require(verified["status_option_id"] == target_id,
                "Project 상태 변경 readback 불일치; pending 유지")
    else:
        verified = latest_item

    if pending["migration_fingerprint"] is not None:
        unchanged, latest_page = _migration_input_unchanged(notion, source_id, row, pending,
                                                             config["project_id"])
        if not unchanged:
            state["pending"] = None
            hold_fingerprint = _digest({"source": plan["fingerprint"],
                                        "migration_input": _migration_input_fingerprint(
                                            latest_page,
                                            decode_internal(read_text(latest_page, "동기화 내부 상태"),
                                                kind="issue", project_id=config["project_id"],
                                                object_id=issue["databaseId"]))})
            hold = _new_hold("MIGRATION_INPUT_CHANGED",
                             "Project 처리 중 최초 이관 입력이 달라져 수동 확인이 필요합니다.",
                             hold_fingerprint)
            updated_project = dict(project)
            updated_items = dict(project["items"])
            updated_items[issue["databaseId"]] = verified
            updated_project["items"] = updated_items
            return updated_project, verified, hold

    state["pending"] = pending
    state["pending"]["confirmed"] = True
    state["pending"]["project_item_id"] = verified["id"]
    resume_checkpoint = None
    if state["resume"] and state["resume"]["display_pending"]:
        resume_checkpoint = {
            "expected_option_id": verified["status_option_id"],
            "expected_fingerprint": _source_fingerprint(latest, latest_refs, verified),
        }
        state["resume"].update(resume_checkpoint)
    _verify_issue_state_write(notion, source_id, row, state)
    state.update(plan["state"])
    _apply_status_checkpoint(state, pending["checkpoint"])
    state["pending"] = None
    state["project_item_id"] = verified["id"]
    state["projection"] = {
        "expected_option_id": target_id,
        "expected_fingerprint": _source_fingerprint(latest, latest_refs, verified),
        "checkpoint": pending["checkpoint"],
    }
    if resume_checkpoint is not None:
        state["resume"].update(resume_checkpoint)
    _verify_issue_state_write(notion, source_id, row, state)
    updated_project = dict(project)
    updated_items = dict(project["items"])
    updated_items[issue["databaseId"]] = verified
    updated_project["items"] = updated_items
    return updated_project, verified, None


def _update_row(notion, source_id, row, metadata, plan, state, now, *, kind):
    props = _projection_properties(metadata, target=plan.get("target"),
                                   end_reason=plan.get("end_reason"),
                                   representative=plan.get("representative"),
                                   confirmation=plan.get("confirmation", ""), state=state,
                                   now=now, kind=kind)
    _patch_page(notion, source_id, row, props)


def _update_control_summary(notion, source_id, control, control_state, issue_rows,
                            *, now, run_id, counts):
    holds = []
    for row, state in issue_rows:
        if state and (state.get("hold") or state.get("projection") or
                      (state.get("resume") or {}).get("display_pending")):
            number = row["properties"]["번호"]["number"]
            holds.append(positive(number, "보류 Issue 번호 오류"))
    holds = sorted(set(holds))
    success = not holds and counts.get("failed", 0) == 0
    control_state["run_id"] = run_id
    control_state["last_result"] = {"kind": "complete" if success else "partial",
                                     "at": now, "holds": holds,
                                     "counts": {key: counts.get(key, 0) for key in
                                                ("created", "updated", "project_changes", "held")}}
    result_label = "전체 완료" if success else "부분 반영"
    props = {"제목": text_property(f"Replica 동기화 · {result_label} · {now}", "title"),
             "GitHub URL": {"url": ACTIONS_WORKFLOW_URL},
             "확인 필요": text_property("" if not holds else
                                     "보류 " + str(len(holds)) + "건: " + ", ".join(map(str, holds))),
             "동기화 내부 상태": text_property(canonical_json(control_state))}
    if success:
        control_state["last_success_at"] = now
        props["동기화 내부 상태"] = text_property(canonical_json(control_state))
        props["동기화 시각"] = {"date": {"start": now}}
    _patch_page(notion, source_id, control, props)
    if success:
        require(timestamp(read_date(control, "동기화 시각")) == timestamp(now),
                "전체 성공 시각 readback 오류")
    return holds


def _preview_issue_plans(notion, source_id, source, index, project, facts, config, cutoff, resume_numbers):
    """Calculate issue targets from the preflight snapshot without persisting markers."""
    plans = []
    status_by_id = {value: name for name, value in project["status_options"].items()}
    for key, (kind, issue_id, issue) in source.items():
        if kind != "Issue":
            continue
        number = issue["number"]
        row = index.get(key)
        if row:
            raw = read_text(row, "동기화 내부 상태")
            state = decode_internal(raw, kind="issue", project_id=config["project_id"],
                                    object_id=issue_id) if raw else new_issue_state(issue_id, config["project_id"])
            notion_row = row
        else:
            state = new_issue_state(issue_id, config["project_id"])
            notion_row = {"properties": {"작업 상태": {"select": None}}}
        item = project["items"].get(issue_id)
        archived = project.get("archived_items", {}).get(issue_id, [])
        current = status_by_id.get(item["status_option_id"]) if item else None
        result = {"issue_number": number, "project_before": current,
                  "project_add_required": False, "project_change": False,
                  "target": None, "hold": None,
                  "result": "still_held" if number in resume_numbers else "planned"}

        if archived:
            result["hold"] = {"code": "PROJECT_ITEM_ARCHIVED",
                              "message": "보관된 Project 항목이 있어 자동 추가/전환할 수 없습니다."}
            plans.append(result)
            continue
        if state["project_item_id"] is not None and (item is None or
                state["project_item_id"] != item["id"]):
            result["hold"] = {"code": "PROJECT_ITEM_MISSING" if item is None else "PROJECT_ITEM_ID_CHANGED",
                              "message": "기존 Project item 고정 식별자를 확인할 수 없습니다."}
            plans.append(result)
            continue
        if state["pending"]:
            pending = state["pending"]
            if pending["kind"] == "add":
                linked, _ = _linked_pr_facts(issue, facts)
                current_source_fingerprint = _source_fingerprint(issue, linked, None)
                if (number not in resume_numbers or item is None or
                        current_source_fingerprint != pending["facts_fingerprint"]):
                    result["hold"] = {"code": "PROJECT_ADD_UNCERTAIN",
                                      "message": "PM 확인 전에는 이전 Project 추가 결과를 재사용할 수 없습니다."}
                    plans.append(result)
                    continue
                if pending["migration_fingerprint"] is not None:
                    unchanged, _ = _migration_input_unchanged(
                        notion, source_id, row, pending, config["project_id"])
                    if not unchanged:
                        result["hold"] = {"code": "MIGRATION_INPUT_CHANGED",
                                          "message": "pending 처리 중 최초 이관 입력이 달라졌습니다."}
                        plans.append(result)
                        continue
                if state["hold"] is None:
                    state["hold"] = _new_hold("PROJECT_ADD_UNCERTAIN",
                                              "Project 추가 결과를 PM이 확인해야 합니다.",
                                              pending["facts_fingerprint"])
                state["pending"] = None
                state["project_item_id"] = item["id"]
            else:
                if item is None or item["id"] != pending["item_id"]:
                    result["hold"] = {"code": "PENDING_ITEM_MISSING",
                                      "message": "pending Project 항목을 확인할 수 없습니다."}
                    plans.append(result)
                    continue
                decision = _pending_status_resolution(
                    issue, item, notion_row, state, facts, config, cutoff,
                    manual_resume=number in resume_numbers, snapshot_issue=issue)
                state = decision["state"]
                if decision["mode"] == "hold":
                    result["hold"] = {"code": decision["hold"]["code"],
                                      "message": decision["hold"]["message"]}
                    plans.append(result)
                    continue
                if (decision["mode"] == "checkpoint" and
                        pending["migration_fingerprint"] is not None):
                    unchanged, _ = _migration_input_unchanged(
                        notion, source_id, row, pending, config["project_id"])
                    if not unchanged:
                        result["hold"] = {"code": "MIGRATION_INPUT_CHANGED",
                                          "message": "pending 처리 중 최초 이관 입력이 달라졌습니다."}
                        plans.append(result)
                        continue
                if decision["mode"] == "checkpoint":
                    _apply_status_checkpoint(state, pending["checkpoint"])
                    if state["resume"] and state["resume"]["display_pending"]:
                        linked, _ = _linked_pr_facts(issue, facts)
                        state["resume"]["expected_option_id"] = item["status_option_id"]
                        state["resume"]["expected_fingerprint"] = _source_fingerprint(issue, linked, item)
                    state["pending"] = None
        resume_candidate = _resume_matches(state["resume"], issue, facts, item)
        stale_resume = bool(state["resume"] and state["resume"]["display_pending"] and
                            number not in resume_numbers and not state["pending"] and
                            not resume_candidate)
        if stale_resume:
            result["hold"] = {"code": "RESUME_CHECKPOINT_CHANGED",
                              "message": "PM 승인 후 Project 또는 GitHub 상태가 달라져 재승인이 필요합니다."}
            plans.append(result)
            continue
        if state["hold"] and number not in resume_numbers and not resume_candidate:
            result["hold"] = {"code": state["hold"]["code"],
                              "message": state["hold"]["message"]}
            plans.append(result)
            continue
        if item is None:
            age = (timestamp(issue["createdAt"]) - timestamp(cutoff)).total_seconds()
            if (state["project_item_id"] is None and not state["migration_complete"] and
                    abs(age) > BOUNDARY_SECONDS):
                result["project_add_required"] = True
                item = {"id": None, "status_option_id": None}
            else:
                result["hold"] = {"code": "PROJECT_ITEM_MISSING",
                                  "message": "기존 이슈의 Project 항목이 없습니다."}
                plans.append(result)
                continue

        approved = bool(state["hold"] and state["hold"]["code"] in RESUMABLE_HOLD_CODES)
        if number in resume_numbers:
            approved = bool(state["hold"] and state["hold"]["code"] in RESUMABLE_HOLD_CODES)
        if resume_candidate:
            approved = approved or (state["hold"] is not None and
                                    state["hold"]["code"] in RESUMABLE_HOLD_CODES)
        plan = _plan_issue(issue, item, notion_row, state, facts, cutoff, config["project_id"],
                           config["status_options"], approved_resume=approved)
        if plan.get("hold"):
            result["hold"] = {"code": plan["hold"]["code"],
                              "message": plan["hold"]["message"]}
        elif state.get("hold") and number not in resume_numbers:
            result["hold"] = {"code": state["hold"]["code"],
                              "message": state["hold"]["message"]}
        else:
            result["target"] = plan.get("target")
            result["project_after"] = plan.get("target")
            result["end_reason"] = plan.get("end_reason")
            result["project_change"] = (not result["project_add_required"] and
                                         current != plan.get("target"))
            if number in resume_numbers:
                result["resume_preview"] = "would_resume"
                result["result"] = "would_resume"
        if result["hold"]:
            result["result"] = "still_held"
        plans.append(result)
    return plans


def sync(github, rest, project_client, notion, config, *, dry_run=False,
         resolve_issue_numbers="", env=None, now=None):
    """Run one complete local/API sync; all global reads finish before the first write."""
    env = os.environ if env is None else env
    source_id, control_id = identifier(config["notion_source_id"]), identifier(config["notion_control_id"])
    facts = gp.fetch_repository_facts(github, rest)
    project = _project_snapshot(project_client, config, facts)
    notion_state = preflight_notion(notion, source_id, control_id, config["project_id"],
                                    diagnostics=dry_run)
    cutoff = (notion_state["control_state"] or {}).get("migration_cutoff")
    if cutoff is None:
        cutoff = _iso(facts["server_time"])
    else:
        cutoff = _iso(timestamp(cutoff))
    cutoff_time = timestamp(cutoff)
    index = notion_state["index"]
    for key in notion_state["archived_keys"]:
        if key == CONTROL_KEY or key in index:
            raise SyncError("활성/보관 Notion 동기화 키 충돌")
    source = {}
    for issue_id, issue in facts["issues"].items():
        key = key_for(REPOSITORY_ID, issue_id)
        source[key] = ("Issue", issue_id, issue)
    for legacy_id, pull in facts["pulls"].items():
        key = key_for(REPOSITORY_ID, legacy_id)
        source[key] = ("PR", legacy_id, pull)
    require(len(source) == len(facts["issues"]) + len(facts["pulls"]), "GitHub canonical key collision")
    require(all(key == CONTROL_KEY or key in source for key in index),
            "Notion 관리 행이 GitHub 전체 snapshot에서 확인되지 않습니다")
    for issue_id, item in project["items"].items():
        require(issue_id in facts["issues"], "Project Issue가 source snapshot에 없습니다")
    for key in source:
        require(key not in notion_state["archived_keys"], "기존 동기화 행이 Notion 보관 상태입니다")
    for key, (kind, _, row_data) in source.items():
        _make_metadata(kind, row_data, key)
        existing = index.get(key)
        if existing:
            require(read_select(existing, "종류") == kind and
                    existing["properties"]["번호"]["number"] == row_data["number"],
                    "기존 canonical Notion 행 종류/번호가 source와 다릅니다")
        if kind == "Issue":
            _issue_reopens(row_data)
            linked, _ = _linked_pr_facts(row_data, facts)
            duplicate = row_data.get("duplicateOf")
            if duplicate is not None:
                require(isinstance(duplicate, dict) and isinstance(duplicate.get("id"), str) and
                        isinstance(duplicate.get("url"), str) and
                        duplicate["url"].startswith("https://github.com/"),
                        "GitHub duplicateOf 관계 형식 오류")
            require(row_data.get("stateReason") is None or isinstance(row_data.get("stateReason"), str),
                    "GitHub Issue stateReason 형식 오류")
            if row_data.get("closedAt") is not None:
                timestamp(row_data["closedAt"])
    # Bind pending checkpoints to the complete Issue snapshot before any writes.
    for key, (kind, issue_id, issue) in source.items():
        if kind != "Issue" or key not in index:
            continue
        state_text = read_text(index[key], "동기화 내부 상태")
        state = decode_internal(state_text, kind="issue", project_id=config["project_id"],
                                object_id=issue_id) if state_text else None
        if state:
            reopen_events = _issue_reopens(issue)
            timeline = {event["id"]: at for at, event in reopen_events}
            baseline_id = state["reopen_baseline_id"]
            last_id = state["reopen_last_id"]
            if baseline_id is not None:
                require(baseline_id in timeline,
                        "저장된 재오픈 baseline이 GitHub timeline에 없습니다")
                require(timeline[baseline_id].timestamp() <
                        cutoff_time.timestamp() - BOUNDARY_SECONDS,
                        "저장된 재오픈 baseline이 이관 cutoff 이전의 확정 이벤트가 아닙니다")
            if last_id is not None:
                require(last_id in timeline,
                        "저장된 재오픈 checkpoint가 GitHub timeline에 없습니다")
                if last_id != baseline_id:
                    require(timeline[last_id].timestamp() >
                            cutoff_time.timestamp() + BOUNDARY_SECONDS,
                            "저장된 재오픈 checkpoint가 cutoff 이후의 확정 이벤트가 아닙니다")
            if baseline_id is not None and last_id is not None:
                require(timeline[baseline_id] <= timeline[last_id],
                        "저장된 재오픈 baseline/checkpoint 순서가 역전되었습니다")
            pending = state["pending"]
            if pending and pending["kind"] == "add":
                require(pending["checkpoint"]["content_id"] == issue.get("id"),
                        "pending add content ID가 source Issue와 불일치")
            elif pending and pending["kind"] == "status":
                timestamp_ids = {}
                for at, event in reopen_events:
                    require(abs((at - cutoff_time).total_seconds()) > BOUNDARY_SECONDS,
                            "pending status Issue 재오픈 cutoff 경계가 모호합니다")
                    require(at not in timestamp_ids,
                            "pending status Issue 재오픈 순서가 같은 시각에 모호합니다")
                    timestamp_ids[at] = event["id"]
                pending_event = pending["event_id"]
                checkpoint_last = pending["checkpoint"]["reopen_last_id"]
                checkpoint = pending["checkpoint"]
                pending_refs, _ = _linked_pr_facts(issue, facts)
                pending_facts_match = (_source_fingerprint(
                    issue, pending_refs, {"id": pending["item_id"],
                                          "status_option_id": pending["before_option_id"]}) ==
                    pending["facts_fingerprint"])
                already_applied_event = (pending_event is not None and
                                         pending_event == last_id)
                if pending_facts_match:
                    canonical_event_id = None
                    if issue.get("state") == "OPEN":
                        prior_event_time = timeline.get(last_id) if last_id is not None else None
                        unprocessed_reopens = [
                            (at, event) for at, event in reopen_events
                            if at.timestamp() > cutoff_time.timestamp() + BOUNDARY_SECONDS and
                            (prior_event_time is None or at > prior_event_time)]
                        if unprocessed_reopens:
                            canonical_event_id = max(
                                unprocessed_reopens,
                                key=lambda entry: (entry[0], entry[1]["id"]))[1]["id"]
                    require(pending_event == canonical_event_id or
                            (already_applied_event and issue.get("state") == "OPEN" and
                             canonical_event_id is None),
                            "pending status event ID가 현재 source의 canonical 재오픈과 불일치합니다")
                if pending_facts_match:
                    checkpoint_state_matches = (
                        state["project_item_id"] == pending["item_id"] and
                        all(state[field] == checkpoint[field]
                            for field in STATUS_CHECKPOINT_KEYS))
                    candidates = [_canonical_pending_status_plan(
                        issue, state, facts, cutoff, config, pending)]
                    if checkpoint_state_matches and issue.get("state") == "OPEN":
                        checkpoint_event_id = checkpoint["reopen_last_id"]
                        checkpoint_time = timeline.get(checkpoint_event_id)
                        if (pending_event is not None and checkpoint_event_id is not None and
                                last_id == checkpoint_event_id and
                                checkpoint_time is not None and
                                checkpoint_time.timestamp() >
                                cutoff_time.timestamp() + BOUNDARY_SECONDS):
                            candidates.append(_canonical_pending_status_plan(
                                issue, state, facts, cutoff, config, pending,
                                replay_applied_reopen=True))
                        if (canonical_event_id is None and
                                {value: name for name, value in config["status_options"].items()}.get(
                                    pending["before_option_id"]) == "검토 중" and
                                config["status_options"].get(
                                    "진행 중") == pending["target_option_id"]):
                            candidates.append(_canonical_pending_status_plan(
                                issue, state, facts, cutoff, config, pending,
                                restore_review_return=True))
                    def canonical_match(candidate):
                        return (not candidate.get("hold") and
                                candidate.get("event_id") == pending_event and
                                config["status_options"].get(candidate.get("target")) ==
                                pending["target_option_id"] and
                                all(candidate["state"][field] == checkpoint[field]
                                    for field in STATUS_CHECKPOINT_KEYS))
                    require(any(canonical_match(candidate) for candidate in candidates),
                            "pending status가 현재 canonical 이벤트/target/checkpoint와 불일치합니다")
                if pending_event is not None:
                    require(pending_event in timeline,
                            "pending status 재오픈 이벤트가 실제 Issue timeline에 없습니다")
                    require(timeline[pending_event].timestamp() >
                            cutoff_time.timestamp() + BOUNDARY_SECONDS,
                            "pending status 재오픈 이벤트가 cutoff 이후의 확정 이벤트가 아닙니다")
                    if last_id is not None:
                        if pending_event == last_id:
                            require(pending["confirmed"] and
                                    all(state[field] == checkpoint[field]
                                        for field in STATUS_CHECKPOINT_KEYS) and
                                    state["project_item_id"] == pending["item_id"],
                                    "이미 처리된 재오픈 이벤트를 pending status가 재사용합니다")
                        else:
                            require(timeline[pending_event] > timeline[last_id],
                                    "pending status 재오픈 이벤트가 기존 checkpoint보다 새롭지 않습니다")
                if checkpoint_last is not None:
                    require(checkpoint_last in timeline,
                            "pending status checkpoint가 실제 Issue timeline에 없습니다")
                    if baseline_id is not None:
                        require(timeline[baseline_id] <= timeline[checkpoint_last],
                                "pending status baseline/checkpoint 순서가 역전되었습니다")
                if pending_event is not None:
                    require(checkpoint_last == pending_event,
                            "pending status 이벤트와 checkpoint ID가 일치하지 않습니다")
                else:
                    stored_prior_id = last_id or baseline_id
                    if pending_facts_match:
                        prior_id = stored_prior_id
                        if prior_id is None:
                            prior_events = [(at, event["id"]) for at, event in reopen_events
                                            if at.timestamp() <
                                            cutoff_time.timestamp() - BOUNDARY_SECONDS]
                            prior = max(prior_events, default=None,
                                        key=lambda entry: (entry[0], entry[1]))
                            prior_id = prior[1] if prior else None
                        require(checkpoint_last == prior_id or
                                _valid_forward_closed_checkpoint(issue, timeline,
                                    checkpoint_last, prior_id, cutoff_time),
                                "pending status checkpoint가 현재 CLOSED 재오픈 사실과 일치하지 않습니다")
                    else:
                        # Historical source drift is issue-local. Keep structural identity,
                        # cutoff ambiguity, and persisted-order checks; let recovery hold the
                        # stale checkpoint or let a fresh PM approval derive today's plan.
                        if checkpoint_last == stored_prior_id:
                            pass
                        elif stored_prior_id is not None:
                            require(checkpoint_last is not None and
                                    timeline[checkpoint_last] > timeline[stored_prior_id] and
                                    timeline[checkpoint_last].timestamp() >
                                    cutoff_time.timestamp() + BOUNDARY_SECONDS,
                                    "historical pending checkpoint가 cutoff 이전 또는 저장 순서 역행입니다")
                        elif checkpoint_last is not None:
                            checkpoint_time = timeline[checkpoint_last]
                            if checkpoint_time.timestamp() < cutoff_time.timestamp() - BOUNDARY_SECONDS:
                                prior_events = [(at, event["id"]) for at, event in reopen_events
                                                if at.timestamp() <
                                                cutoff_time.timestamp() - BOUNDARY_SECONDS]
                                latest_initial = max(prior_events, default=None,
                                                     key=lambda entry: (entry[0], entry[1]))
                                require(latest_initial is not None and
                                        checkpoint_last == latest_initial[1],
                                        "historical 최초 checkpoint가 실제 cutoff 이전 마지막 baseline이 아닙니다")
                            else:
                                require(checkpoint_time.timestamp() >
                                        cutoff_time.timestamp() + BOUNDARY_SECONDS,
                                        "historical pending checkpoint가 cutoff 경계에 있습니다")
    resume_numbers, resume_actor = _validate_manual_resume(github, resolve_issue_numbers, env)
    if now is None:
        now = _iso(datetime.now(timezone.utc))
    else:
        now = _iso(timestamp(now))
    run_id = env.get("GITHUB_RUN_ID") or "local"
    require(isinstance(run_id, str) and len(run_id) <= 128, "workflow run ID 형식 오류")
    counts = {"source_items": len(source), "created": 0, "updated": 0,
              "project_changes": 0, "held": 0, "failed": 0}
    # Validate that every requested number names an existing held Issue before any per-issue write.
    by_number = {entry[2]["number"]: entry for entry in source.values() if entry[0] == "Issue"}
    for number in resume_numbers:
        source_entry = by_number.get(number)
        require(source_entry is not None, "resolve_issue_numbers에 저장소 Issue가 없습니다")
        key = key_for(REPOSITORY_ID, source_entry[1])
        row = index.get(key)
        require(row is not None and read_select(row, "종류") == "Issue", "재개 대상 Notion Issue 행이 없습니다")
        state_text = read_text(row, "동기화 내부 상태")
        state = decode_internal(state_text, kind="issue", project_id=config["project_id"],
                                object_id=source_entry[1]) if state_text else None
        pending_add = bool(state and state["pending"] and state["pending"]["kind"] == "add")
        resumable_hold = bool(state and state["hold"] and
                              state["hold"]["code"] in RESUMABLE_HOLD_CODES)
        require(state is not None and (resumable_hold or
                                      (pending_add and state["hold"] is None)),
                "수동 재개 대상이 현재 보류 상태가 아닙니다")
        require(not state["hold"] or state["hold"]["code"] in RESUMABLE_HOLD_CODES,
                "GitHub 원본 모순은 수동 재개로 무시할 수 없습니다")

    if dry_run:
        counts["issue_plans"] = _preview_issue_plans(notion, source_id, source, index, project, facts,
                                                       config, cutoff, resume_numbers)
        if resume_numbers:
            counts["resolution_preview"] = [plan for plan in counts["issue_plans"]
                                             if plan["issue_number"] in resume_numbers]
        return counts

    control = notion_state["control"]
    control_state = notion_state["control_state"] or _control_state(config["project_id"], cutoff, run_id)
    if notion_state["control_state"] is None:
        _save_control_state(notion, source_id, control, control_state)
    require(control_state["migration_cutoff"] == cutoff, "최초 이관 기준선이 실행 중 변경됨")

    # Reconcile prior Notion creation fences without ever repeating an uncertain create.
    if notion_state["pending_create"]:
        pending_key = notion_state["pending_create"]
        require(pending_key in index, "Pending create 결과 행이 없습니다")
        update_control_pending(notion, source_id, control, "")

    pulls_by_node = {pull["id"]: pull for pull in facts["pulls"].values()}
    del pulls_by_node
    project_by_issue = project["items"]
    issue_rows_for_summary = []
    # Create/reuse all source rows before Project item additions and status projections.
    for key, (kind, canonical_id, row_data) in source.items():
        row, created = _ensure_notion_row(notion, source_id, control, index, kind,
                                          row_data, canonical_id, now)
        if created:
            counts["created"] += 1
        if kind == "PR":
            metadata = _make_metadata(kind, row_data, key)
            if not created:
                _patch_page(notion, source_id, row, {**metadata, "동기화 시각": {"date": {"start": now}}})
                counts["updated"] += 1
            continue

        issue_id = canonical_id
        internal = read_text(row, "동기화 내부 상태")
        state = decode_internal(internal, kind="issue", project_id=config["project_id"],
                                object_id=issue_id) if internal else new_issue_state(issue_id, config["project_id"])
        metadata = _make_metadata(kind, row_data, key)
        item = project_by_issue.get(issue_id)

        archived_items = project.get("archived_items", {}).get(issue_id, [])
        if archived_items:
            fingerprint = _source_fingerprint(row_data, _linked_pr_facts(row_data, facts)[0], item)
            hold = _new_hold("PROJECT_ITEM_ARCHIVED", "이슈에 보관된 Project 항목이 있어 자동 추가/전환을 멈췄습니다.",
                             fingerprint)
            _display_hold(notion, source_id, row, metadata, state, hold, now)
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue

        if state["project_item_id"] is not None and (item is None or
                state["project_item_id"] != item["id"]):
            code = "PROJECT_ITEM_MISSING" if item is None else "PROJECT_ITEM_ID_CHANGED"
            fingerprint = _source_fingerprint(row_data, _linked_pr_facts(row_data, facts)[0], item)
            hold = _new_hold(code, "기존 Project item 고정 식별자를 확인할 수 없습니다.", fingerprint)
            _display_hold(notion, source_id, row, metadata, state, hold, now)
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue

        issue_number = row_data["number"]
        projection = state["projection"]
        saved_pm_display = bool(state["resume"] and state["resume"]["display_pending"])
        if (projection is not None and
                (state["hold"] is None or issue_number in resume_numbers or saved_pm_display)):
            latest_issue = gp.fetch_issue_detail(github, row_data["id"])
            latest_refs, _ = _linked_pr_facts(latest_issue, facts, allow_snapshot_drift=True)
            latest_item = gp.fetch_project_item(project_client, config["project_id"], item["id"],
                                                config["status_field_id"], allow_archived=True)
            fresh_fingerprint = _source_fingerprint(latest_issue, latest_refs, latest_item)
            projection_hold = None
            if latest_item.get("is_archived"):
                projection_hold = _new_hold(
                    "PROJECT_ITEM_ARCHIVED", "표시 복구 전 Project 항목이 보관되었습니다.",
                    fresh_fingerprint)
            elif latest_item.get("content_id") != latest_issue.get("id"):
                projection_hold = _new_hold(
                    "PROJECT_ITEM_RACE", "표시 복구 전 Project 항목 연결이 달라졌습니다.",
                    fresh_fingerprint)
            elif (latest_item["status_option_id"] != projection["expected_option_id"] or
                  fresh_fingerprint != projection["expected_fingerprint"]):
                projection_hold = _new_hold(
                    "PROJECTION_CHECKPOINT_CHANGED",
                    "Project 또는 원본 사실이 checkpoint 이후 달라졌습니다. 현재 값을 확인하고 PM 재승인이 필요합니다.",
                    fresh_fingerprint)

            if projection_hold is None:
                projection_state = json.loads(json.dumps(state))
                _apply_status_checkpoint(projection_state, projection["checkpoint"])
                projection_plan = _plan_issue(
                    latest_issue, latest_item, row, projection_state, facts, cutoff,
                    config["project_id"], config["status_options"],
                    approved_resume=issue_number in resume_numbers or saved_pm_display)
                if (projection_plan.get("hold") or
                        _status_option(project, projection_plan.get("target")) !=
                        projection["expected_option_id"]):
                    projection_hold = _new_hold(
                        "PROJECTION_CHECKPOINT_CHANGED",
                        "저장된 Project checkpoint를 현재 원본 사실로 재구성할 수 없습니다. PM 확인이 필요합니다.",
                        fresh_fingerprint)
                else:
                    state.update(projection_plan["state"])
                    _apply_status_checkpoint(state, projection["checkpoint"])
                    state["project_item_id"] = latest_item["id"]
                    state["projection"] = None
                    state["pending"] = None
                    state["hold"] = None
                    state["resume"] = None
                    project_by_issue[issue_id] = latest_item
                    _update_row(notion, source_id, row, metadata, projection_plan, state, now,
                                kind="Issue")
                    if not created:
                        counts["updated"] += 1
                    issue_rows_for_summary.append((row, state))
                    continue

            if (projection_hold is not None and saved_pm_display and
                    projection_hold["code"] == "PROJECTION_CHECKPOINT_CHANGED"):
                projection_hold["code"] = "RESUME_CHECKPOINT_CHANGED"
            state["projection"] = None
            _display_hold(notion, source_id, row, metadata, state, projection_hold, now,
                          preserve_task_status=True)
            if latest_item.get("is_archived"):
                project_by_issue.pop(issue_id, None)
            else:
                project_by_issue[issue_id] = latest_item
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue

        resume_display_candidate = _resume_matches(state["resume"], row_data, facts, item)
        stale_resume = bool(state["resume"] and state["resume"]["display_pending"] and
                            issue_number not in resume_numbers and not state["pending"] and
                            not resume_display_candidate)
        if stale_resume:
            stale_fingerprint = _source_fingerprint(
                row_data, _linked_pr_facts(row_data, facts)[0], item)
            stale_hold = _new_hold("RESUME_CHECKPOINT_CHANGED",
                "PM 승인 후 Project 또는 GitHub 상태가 달라졌습니다. 현재 값을 확인한 뒤 PM이 다시 승인해야 합니다.",
                stale_fingerprint)
            _display_hold(notion, source_id, row, metadata, state, stale_hold, now,
                          preserve_task_status=True)
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue
        if (state["hold"] is not None and issue_number not in resume_numbers and
                not state["pending"] and not resume_display_candidate):
            _display_hold(notion, source_id, row, metadata, state, state["hold"], now,
                          preserve_task_status=state["hold"]["code"] in
                          {"MIGRATION_INPUT_CHANGED", "RESUME_CHECKPOINT_CHANGED",
                           "PENDING_RESULT_UNCLEAR", "PROJECT_STATUS_UNSET"})
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue

        # A possibly lost add response is never adopted or retried without a PM choice.
        if state["pending"] and state["pending"]["kind"] == "add":
            if issue_number not in resume_numbers:
                fingerprint = _source_fingerprint(row_data, _linked_pr_facts(row_data, facts)[0], None)
                hold = state["hold"] or _new_hold(
                    "PROJECT_ADD_UNCERTAIN", "Project 추가 결과를 확인할 수 없습니다.", fingerprint)
                _display_hold(notion, source_id, row, metadata, state, hold, now)
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue
            if state["hold"] is None:
                state["hold"] = _new_hold(
                    "PROJECT_ADD_UNCERTAIN", "Project 추가 결과를 PM이 확인해야 합니다.",
                    _source_fingerprint(row_data, _linked_pr_facts(row_data, facts)[0], None))
                _verify_issue_state_write(notion, source_id, row, state)
            item, pending_hold = _resolve_pending_add(
                notion, github, source_id, row, state, project, facts, row_data)
            if pending_hold:
                _display_hold(notion, source_id, row, metadata, state, pending_hold, now,
                              preserve_task_status=pending_hold["code"] in
                              {"MIGRATION_INPUT_CHANGED", "PENDING_RESULT_UNCLEAR",
                               "PROJECT_STATUS_UNSET"})
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue
            require(item is not None, "PM 승인 Project 항목 readback 누락")
            project_by_issue[issue_id] = item

        if item is None:
            if state["pending"] and state["pending"]["kind"] == "status":
                fingerprint = _source_fingerprint(row_data, _linked_pr_facts(row_data, facts)[0], None)
                hold = _new_hold("PENDING_ITEM_MISSING", "pending Project 항목이 사라져 자동 복구를 멈췄습니다.",
                                 fingerprint)
                _display_hold(notion, source_id, row, metadata, state, hold, now)
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue
            age = (timestamp(row_data["createdAt"]) - timestamp(cutoff)).total_seconds()
            if abs(age) <= BOUNDARY_SECONDS:
                fingerprint = _source_fingerprint(row_data, _linked_pr_facts(row_data, facts)[0], None)
                hold = _new_hold("AMBIGUOUS_CUTOFF", "이슈 생성 시각이 기준선 경계에 있습니다.", fingerprint)
                _display_hold(notion, source_id, row, metadata, state, hold, now)
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue
            if state["migration_complete"] or state["project_item_id"] is not None:
                fingerprint = _source_fingerprint(row_data, _linked_pr_facts(row_data, facts)[0], None)
                hold = _new_hold("PROJECT_ITEM_MISSING", "기존 이슈의 Project 항목이 없습니다.", fingerprint)
                _display_hold(notion, source_id, row, metadata, state, hold, now)
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue
            fingerprint = _source_fingerprint(row_data, _linked_pr_facts(row_data, facts)[0], None)
            try:
                project, item, add_hold = _add_project_item_once(
                    notion, github, project_client, source_id, row, state,
                    project, facts, row_data, config, fingerprint)
            except (SyncError, gp.SyncError):
                # The pending marker is durable; do not make a second add attempt.
                issue_rows_for_summary.append((row, state))
                raise
            if add_hold:
                _display_hold(notion, source_id, row, metadata, state, add_hold, now,
                              preserve_task_status=add_hold["code"] == "MIGRATION_INPUT_CHANGED")
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue
            project_by_issue[issue_id] = item

        pending_hold = None
        if state["pending"] and state["pending"]["kind"] == "status":
            pending_hold = _recover_pending_status(notion, source_id, row, state, project,
                                                   github, facts, row_data, config, cutoff,
                                                   manual_resume=issue_number in resume_numbers)
            if pending_hold:
                _display_hold(notion, source_id, row, metadata, state, pending_hold, now,
                          preserve_task_status=pending_hold["code"] in
                          {"MIGRATION_INPUT_CHANGED", "PENDING_RESULT_UNCLEAR",
                           "PROJECT_STATUS_UNSET"})
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue

        saved_resume_recovery = (issue_number not in resume_numbers and
                                 _resume_matches(state["resume"], row_data, facts, item))
        approved_resume = False
        if issue_number in resume_numbers:
            prior_hold = state["hold"]
            state["resume"] = {"actor_id": resume_actor["actor_id"], "run_id": resume_actor["run_id"],
                               "approved_option_id": item["status_option_id"],
                               "fingerprint": _source_fingerprint(row_data,
                                   _linked_pr_facts(row_data, facts)[0], item),
                               "display_pending": True,
                               "expected_option_id": None, "expected_fingerprint": None}
            _verify_issue_state_write(notion, source_id, row, state)
            approved_resume = prior_hold["code"] in RESUMABLE_HOLD_CODES
        elif saved_resume_recovery:
            approved_resume = bool(state["hold"] and
                                   state["hold"]["code"] in RESUMABLE_HOLD_CODES)

        plan_state = json.loads(json.dumps(state))
        plan = _plan_issue(row_data, item, row, plan_state, facts, cutoff, config["project_id"],
                           config["status_options"], approved_resume=approved_resume)
        if plan.get("hold"):
            _display_hold(notion, source_id, row, metadata, state, plan["hold"], now,
                          preserve_task_status=plan["hold"]["code"] in
                          {"MIGRATION_INPUT_CHANGED", "PROJECT_STATUS_UNSET"})
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue
        if state["hold"] is not None and issue_number not in resume_numbers and not saved_resume_recovery:
            # A durable hold cannot disappear merely because the source now looks consistent.
            _display_hold(notion, source_id, row, metadata, state, state["hold"], now,
                          preserve_task_status=state["hold"]["code"] in
                          {"MIGRATION_INPUT_CHANGED", "RESUME_CHECKPOINT_CHANGED",
                           "PENDING_RESULT_UNCLEAR", "PROJECT_STATUS_UNSET"})
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue

        old_option = item["status_option_id"]
        target_option = _status_option(project, plan["target"])
        project, item, migration_hold = _apply_project_status(
            notion, github, project_client, source_id, row, state, project, facts,
            row_data, item, plan, config)
        project_by_issue[issue_id] = item
        if migration_hold:
            _display_hold(notion, source_id, row, metadata, state, migration_hold, now,
                          preserve_task_status=True)
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue
        if old_option != target_option:
            counts["project_changes"] += 1
        # Checkpoint exists before any user-facing Notion projection.
        latest_issue = gp.fetch_issue_detail(github, row_data["id"])
        latest_refs, _ = _linked_pr_facts(latest_issue, facts, allow_snapshot_drift=True)
        latest_item = gp.fetch_project_item(project_client, config["project_id"], item["id"],
                                            config["status_field_id"], allow_archived=True)
        expected_projection = state["projection"]
        current_projection_fingerprint = _source_fingerprint(latest_issue, latest_refs, latest_item)
        if latest_item.get("is_archived"):
            projection_hold = _new_hold("PROJECT_ITEM_ARCHIVED",
                                        "Notion 표시 전에 Project 항목이 보관되었습니다.",
                                        current_projection_fingerprint)
        elif (latest_item.get("content_id") != latest_issue.get("id") or
              latest_item["status_option_id"] != target_option or
              expected_projection is None or
              expected_projection["expected_option_id"] != target_option or
              current_projection_fingerprint != expected_projection["expected_fingerprint"]):
            projection_hold = _new_hold(
                "PROJECTION_CHECKPOINT_CHANGED",
                "Project 또는 원본 사실이 checkpoint 이후 달라져 표시 복구를 보류했습니다. PM 확인이 필요합니다.",
                current_projection_fingerprint)
        else:
            projection_hold = None
        if projection_hold:
            state["projection"] = None
            if latest_item.get("is_archived"):
                project_by_issue.pop(issue_id, None)
            else:
                project_by_issue[issue_id] = latest_item
            _display_hold(notion, source_id, row, metadata, state, projection_hold, now,
                          preserve_task_status=True)
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue
        project_by_issue[issue_id] = latest_item
        state["hold"] = None
        state["resume"] = None
        state["projection"] = None
        _update_row(notion, source_id, row, metadata, plan, state, now, kind="Issue")
        if not created:
            counts["updated"] += 1
        issue_rows_for_summary.append((row, state))

    # Preserve/update PR metadata without touching Issue-only automation properties.
    holds = _update_control_summary(notion, source_id, control, control_state,
                                    issue_rows_for_summary, now=now, run_id=run_id, counts=counts)
    counts["held"] = max(counts["held"], len(holds))
    return counts


def _load_config(env):
    names = ("GITHUB_TOKEN", "PROJECT_TOKEN", "NOTION_TOKEN", "PROJECT_ID", "PROJECT_OWNER_ID",
             "PROJECT_STATUS_FIELD_ID", "PROJECT_STATUS_OPTIONS", "PM_GITHUB_USER_ID",
             "NOTION_DATA_SOURCE_ID", "NOTION_CONTROL_PAGE_ID")
    require(all(isinstance(env.get(name), str) and env[name] for name in names),
            "동기화 설정이 누락되었습니다. Actions variables/secrets를 확인하세요.")
    require(env["PROJECT_ID"] == gp.EXPECTED_PROJECT_ID and
            env["PROJECT_OWNER_ID"] == gp.EXPECTED_PROJECT_OWNER_ID and
            env["PM_GITHUB_USER_ID"] == str(gp.EXPECTED_PM_USER_ID),
            "고정 Project/PM 설정이 확인된 ID와 다릅니다")
    require(len(env["PROJECT_STATUS_FIELD_ID"]) <= 128, "Project Status field ID 형식 오류")
    return {"project_id": env["PROJECT_ID"], "owner_id": env["PROJECT_OWNER_ID"],
            "status_field_id": env["PROJECT_STATUS_FIELD_ID"],
            "status_options": gp.parse_status_options(env["PROJECT_STATUS_OPTIONS"]),
            "notion_source_id": identifier(env["NOTION_DATA_SOURCE_ID"]),
            "notion_control_id": identifier(env["NOTION_CONTROL_PAGE_ID"])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="전체 조회·검증만 하고 어떤 API에도 쓰지 않음")
    parser.add_argument("--resolve-issue-numbers", default="",
                        help="PM 수동 재개 대상 Issue 번호를 comma-separated로 지정")
    args = parser.parse_args(argv)
    env = os.environ
    event = env.get("GITHUB_EVENT_NAME")
    enabled = env.get("NOTION_SYNC_ENABLED") == "true"
    explicit_dispatch_dry_run = event == "workflow_dispatch" and args.dry_run
    if not enabled and not explicit_dispatch_dry_run:
        print("동기화 비활성: NOTION_SYNC_ENABLED=true 설정 후 실행하세요.")
        return 0
    try:
        config = _load_config(env)
        gh = gp.GraphQLClient(env["GITHUB_TOKEN"])
        rest = gp.RESTClient(env["GITHUB_TOKEN"])
        project_client = gp.GraphQLClient(env["PROJECT_TOKEN"])
        notion = NotionClient(env["NOTION_TOKEN"])
        counts = sync(gh, rest, project_client, notion, config, dry_run=args.dry_run,
                      resolve_issue_numbers=args.resolve_issue_numbers, env=env)
        print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
        return 0
    except (SyncError, gp.SyncError) as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        # Never emit request bodies, remote payloads or tracebacks that can contain secrets.
        print("동기화 실패: 원격 응답 또는 스키마를 확인하세요. 기존 데이터는 보존됩니다.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
