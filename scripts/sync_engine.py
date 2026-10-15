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
from uuid import UUID, uuid4

import github_project as gp
import notification_report
import observation_clock
import issue_status_sync as status_sync

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
PROJECT_ADD_READBACK_MAX_ATTEMPTS = 3
PROJECT_ADD_READBACK_BACKOFF_SECONDS = (0.25, 0.5)
GENERAL_STATES = ("백로그", "준비 중", "진행 중")
RESUMABLE_HOLD_CODES = {
    "MIGRATION_CONFLICT", "MIGRATION_UNSET", "MIGRATION_INPUT_CHANGED",
    "OPEN_PROJECT_COMPLETED", "REVIEW_WITHOUT_CYCLE", "RESUME_CHECKPOINT_CHANGED",
    "CLOSURE_DUPLICATE_CONFLICT", "OPEN_DUPLICATE_CONFLICT", "REOPEN_CLOSE_ORDER",
    "REOPEN_ORDER_AMBIGUOUS", "SOURCE_CHANGED_BEFORE_WRITE", "PROJECT_ADD_UNCERTAIN",
    "PROJECT_ITEM_RACE", "PROJECT_ITEM_ARCHIVED", "PENDING_RESULT_UNCLEAR",
    "PENDING_FACTS_CHANGED", "PROJECTION_CHECKPOINT_CHANGED", "PROJECT_STATUS_UNSET",
    "STATUS_CONFLICT", "STATUS_ORDER_UNKNOWN", "STATUS_REQUEST_INVALID",
    "BIDIRECTIONAL_CONFLICT", "REQUEST_RACE", "ADD_READBACK_EXHAUSTED",
    "GITHUB_FACTS_CHANGED", "DEFERRED_REQUEST_PENDING", "ADD_READBACK_CONTRADICTION",
}

SCHEMA = {
    "제목": "title", "종류": "select", "번호": "number", "GitHub URL": "url",
    "GitHub 상태": "select", "작성자": "rich_text", "담당자": "rich_text",
    "라벨": "rich_text", "GitHub 수정": "date", "동기화 키": "rich_text",
    "동기화 시각": "date", "Pending create": "rich_text", "작업 상태": "select",
    "일정": "date", "메모": "rich_text", "종료 사유": "select", "대표 이슈": "url",
    "확인 필요": "rich_text", "동기화 내부 상태": "rich_text",
    "요청 처리": "select", "요청 상태": "select",
}
DATE_DIAGNOSTIC_METADATA_WHITELIST = (
    "제목", "종류", "번호", "GitHub URL", "GitHub 상태", "작성자", "담당자",
    "라벨", "GitHub 수정", "동기화 키",
)
DISPLAY_MINUTE_DATE_PROPERTIES = frozenset({"GitHub 수정", "동기화 시각"})
OPTIONS = {
    "종류": {"Issue", "PR", "Sync"},
    "GitHub 상태": {"Open", "Closed", "Draft", "Merged"},
    "작업 상태": {"백로그", "준비 중", "진행 중", "검토 중", "완료"},
    "요청 처리": set(status_sync.REQUEST_UI_STATES),
    "요청 상태": set(status_sync.ALL_STATES),
    "종료 사유": {"완료", "미계획", "중복", "확인 필요"},
}
ISSUE_STATE_V1_KEYS = {
    "v", "repository_id", "issue_id", "project_id", "project_item_id", "migration_complete",
    "reopen_baseline_id", "reopen_last_id", "review_cycle", "review_return_cycle",
    "review_pr_hash", "pending", "hold", "resume", "projection",
}
ISSUE_STATE_V2_LEGACY_KEYS = ISSUE_STATE_V1_KEYS | {"baseline", "request", "readback", "notion_write"}
ISSUE_STATE_V2_KEYS = ISSUE_STATE_V2_LEGACY_KEYS | {"deferred_request"}
ISSUE_STATE_KEYS = ISSUE_STATE_V2_KEYS
CONTROL_STATE_KEYS = {"v", "project_id", "migration_cutoff", "last_success_at", "run_id", "last_result"}
LEGACY_RESUME_KEYS = {"actor_id", "run_id", "approved_option_id", "fingerprint", "display_pending"}
CHECKPOINT_RESUME_KEYS = LEGACY_RESUME_KEYS | {"expected_option_id", "expected_fingerprint"}
RESUME_KEYS = CHECKPOINT_RESUME_KEYS | {
    "request_id", "facts_fingerprint", "fact_contract", "approved_notion_status"}
STATUS_CHECKPOINT_KEYS = {"migration_complete", "review_cycle", "review_return_cycle",
                          "reopen_last_id", "review_pr_hash"}
PROJECTION_KEYS = {"expected_option_id", "expected_fingerprint", "checkpoint"}
SEMANTIC_PROJECTION_KEYS = PROJECTION_KEYS | {
    "fact_contract", "request_id", "expected_notion_status", "result_notion_status"}
CHECKPOINTED_PROJECTION_KEYS = SEMANTIC_PROJECTION_KEYS | {"display_checkpoint"}


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
    require(isinstance(parsed, dict) and type(parsed.get("v")) is int and parsed["v"] in {1, 2},
            "동기화 내부 상태 버전/형식 오류")
    if kind == "issue":
        version = parsed["v"]
        require(set(parsed) == (ISSUE_STATE_V1_KEYS if version == 1 else
                                ISSUE_STATE_V2_KEYS if "deferred_request" in parsed else
                                ISSUE_STATE_V2_LEGACY_KEYS),
                "Issue 내부 상태 속성 집합 오류")
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
        if version == 1:
            parsed.update({"v": 2, "baseline": None, "request": None,
                           "readback": None, "notion_write": None,
                           "deferred_request": None})
        elif "deferred_request" not in parsed:
            parsed["deferred_request"] = None
        _validate_issue_v2_state(parsed)
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
    legacy_keys = {"kind", "issue_id", "item_id", "event_id", "before_option_id", "target_option_id",
            "facts_fingerprint", "migration_fingerprint", "notion_status", "confirmed",
            "project_item_id", "checkpoint"}
    v2_keys = legacy_keys | {"fact_contract", "semantic_fingerprint"}
    semantic_request_keys = v2_keys | {"request_id"}
    keys = set(pending)
    stamped_request_keys = semantic_request_keys | {"before_status_stamp"}
    stamped_legacy_request_keys = v2_keys | {"before_status_stamp"}
    require(keys in (legacy_keys, v2_keys, semantic_request_keys, stamped_request_keys,
                     stamped_legacy_request_keys) and
            type(pending.get("issue_id")) is int and pending.get("issue_id") == issue_id and
            pending.get("kind") in {"status", "add"}, "Issue pending 구조 오류")
    if keys in (v2_keys, semantic_request_keys, stamped_request_keys,
                stamped_legacy_request_keys):
        require(pending.get("fact_contract") == "semantic_v2" and
                isinstance(pending.get("semantic_fingerprint"), str) and
                len(pending["semantic_fingerprint"]) == 64,
                "pending semantic fingerprint 계약 오류")
    if keys in (semantic_request_keys, stamped_request_keys):
        require(pending.get("request_id") is None or
                isinstance(pending.get("request_id"), str) and pending["request_id"],
                "pending status request ID 오류")
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
        if keys in (semantic_request_keys, stamped_request_keys):
            require(pending.get("request_id") is None or
                    isinstance(pending.get("request_id"), str) and pending["request_id"],
                    "pending status request ID 오류")
        if keys in (stamped_request_keys, stamped_legacy_request_keys):
            stamp = pending.get("before_status_stamp")
            require(isinstance(stamp, dict) and set(stamp) ==
                    {"field_id", "option_id", "value_id", "updated_at"} and
                    stamp.get("option_id") == pending.get("before_option_id") and
                    isinstance(stamp.get("field_id"), str) and stamp["field_id"] and
                    (stamp.get("value_id") is None or isinstance(stamp["value_id"], str)) and
                    (stamp.get("updated_at") is None or isinstance(stamp["updated_at"], str)),
                    "pending before Project status stamp 오류")
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
    keys = set(projection)
    require(keys in (PROJECTION_KEYS, SEMANTIC_PROJECTION_KEYS,
                     CHECKPOINTED_PROJECTION_KEYS) and
            (projection.get("expected_option_id") is None or
             projection["expected_option_id"] in gp.EXPECTED_STATUS_OPTIONS.values()) and
            isinstance(projection.get("expected_fingerprint"), str) and
            len(projection["expected_fingerprint"]) == 64,
            "Issue projection checkpoint 형식 오류")
    if keys in (SEMANTIC_PROJECTION_KEYS, CHECKPOINTED_PROJECTION_KEYS):
        require(projection.get("fact_contract") == "semantic_v2" and
                (projection.get("request_id") is None or
                 isinstance(projection.get("request_id"), str) and projection["request_id"]) and
                (projection.get("expected_notion_status") is None or
                 projection.get("expected_notion_status") in OPTIONS["작업 상태"]) and
                (projection.get("result_notion_status") is None or
                 projection.get("result_notion_status") in OPTIONS["작업 상태"]),
                "Issue projection semantic fingerprint 계약 오류")
    if keys == CHECKPOINTED_PROJECTION_KEYS:
        display = projection["display_checkpoint"]
        require(isinstance(display, dict) and set(display) ==
                {"phase", "expected_fields", "expected_fingerprint"} and
                display.get("phase") in {"display_sent", "completion_pending"} and
                isinstance(display.get("expected_fields"), dict) and
                bool(display["expected_fields"]) and
                all(name in SCHEMA and kind == SCHEMA[name] and
                    name != "동기화 내부 상태"
                    for name, kind in display["expected_fields"].items()) and
                isinstance(display.get("expected_fingerprint"), str) and
                re.fullmatch(r"[0-9a-f]{64}", display["expected_fingerprint"]) is not None,
                "Issue projection display checkpoint 오류")
    _validate_status_checkpoint(projection.get("checkpoint"))


def _validate_issue_v2_state(state):
    require(state.get("v") == 2, "Issue 내부 상태 v2 누락")
    baseline = state.get("baseline")
    if baseline is not None:
        require(isinstance(baseline, dict) and set(baseline) ==
                {"notion_status", "project", "project_item_id", "facts_fingerprint",
                 "observed_at"}, "Status baseline 구조 오류")
        try:
            status_sync.make_baseline(baseline["notion_status"], baseline["project"],
                                      baseline["project_item_id"],
                                      baseline["facts_fingerprint"], baseline["observed_at"])
        except status_sync.StatusSyncError as exc:
            raise SyncError("Status baseline 값 오류") from exc
        require(state.get("project_item_id") == baseline["project_item_id"],
                "Status baseline과 Issue 고정 Project item ID 불일치")
    request = state.get("request")
    if request is not None:
        keys = {"id", "target", "phase", "observed_from", "observed_to",
                "prior_notion_status", "project_option_id", "requested_by", "reason"}
        require(isinstance(request, dict) and set(request) == keys,
                "Status request 구조 오류")
        try:
            validated = status_sync.request_record(
                request["id"], request["target"], observed_from=request["observed_from"],
                observed_to=request["observed_to"],
                prior_notion_status=request["prior_notion_status"],
                project_option_id=request["project_option_id"], phase=request["phase"],
                reason=request["reason"])
        except (status_sync.StatusSyncError, KeyError) as exc:
            raise SyncError("Status request 값 오류") from exc
        require(request["requested_by"] is None and validated["id"] == request["id"],
                "Status request actor 추정/구조 오류")
        if request["phase"] in {"prepared", "sent", "uncertain", "confirmed"}:
            notion_write = state.get("notion_write")
            require(isinstance(notion_write, dict) and
                    notion_write.get("request_id") == request["id"] and
                    notion_write.get("target") == request["target"] and
                    notion_write.get("kind") == "request" and
                    notion_write.get("phase") == request["phase"],
                    "활성 status request/notion_write 결속 불일치")
            pending = state.get("pending")
            projection = state.get("projection")
            if pending and pending.get("kind") == "status" and pending.get("fact_contract") == "semantic_v2":
                request_id_matches = (pending.get("request_id") == request["id"]
                                      if "request_id" in pending else True)
                require(request_id_matches and
                        gp.EXPECTED_STATUS_OPTIONS.get(request["target"]) ==
                        pending.get("target_option_id") and
                        pending.get("notion_status") == request["target"],
                        "활성 status request/pending ID 또는 target 결속 불일치")
            elif projection and projection.get("fact_contract") == "semantic_v2":
                require(projection.get("request_id") == request["id"] and
                        projection.get("result_notion_status") == request["target"],
                        "활성 status request/projection ID 또는 target 결속 불일치")
            elif request["phase"] == "prepared" and pending is None and projection is None:
                # Compatibility for v2 prepare markers written immediately before the
                # pending checkpoint was introduced; prepared is safe to reconstruct.
                pass
        elif request["phase"] in {"accepted", "waiting", "held", "rejected", "completed"}:
            pass
        else:
            raise SyncError("활성 status request에 연결된 pending/projection이 없습니다")
    deferred = state.get("deferred_request")
    if deferred is not None:
        keys = {"id", "target", "phase", "observed_from", "observed_to",
                "prior_notion_status", "project_option_id", "requested_by", "reason"}
        require(isinstance(deferred, dict) and set(deferred) == keys and
                deferred.get("phase") in {"waiting", "held", "rejected"},
                "Deferred status request 구조 오류")
        try:
            validated = status_sync.request_record(
                deferred["id"], deferred["target"],
                observed_from=deferred["observed_from"], observed_to=deferred["observed_to"],
                prior_notion_status=deferred["prior_notion_status"],
                project_option_id=deferred["project_option_id"], phase=deferred["phase"],
                reason=deferred["reason"])
        except (status_sync.StatusSyncError, KeyError) as exc:
            raise SyncError("Deferred status request 값 오류") from exc
        require(deferred["requested_by"] is None and validated["id"] == deferred["id"],
                "Deferred status request actor/ID 오류")
    readback = state.get("readback")
    if readback is not None:
        keys = {"returned_item_id", "validated", "attempts", "reservation", "last_result"}
        reservation = readback.get("reservation") if isinstance(readback, dict) else None
        reservation_valid = (reservation is None or
            isinstance(reservation, dict) and set(reservation) ==
            {"token", "run_id", "run_attempt", "attempt"} and
            isinstance(reservation["token"], str) and reservation["token"] and
            isinstance(reservation["run_id"], str) and reservation["run_id"] and
            isinstance(reservation["run_attempt"], str) and reservation["run_attempt"] and
            type(reservation["attempt"]) is int and
            1 <= reservation["attempt"] <= PROJECT_ADD_READBACK_MAX_ATTEMPTS)
        require(isinstance(readback, dict) and set(readback) == keys and
                (readback["returned_item_id"] is None or
                 isinstance(readback["returned_item_id"], str) and readback["returned_item_id"]) and
                type(readback["validated"]) is bool and type(readback["attempts"]) is int and
                0 <= readback["attempts"] <= PROJECT_ADD_READBACK_MAX_ATTEMPTS and
                reservation_valid and
                (readback["last_result"] is None or readback["last_result"] in
                 {"not_visible", "network_error", "confirmed", "contradiction"}),
                "Project add readback v2 구조 오류")
        require(not readback["validated"] or readback["returned_item_id"] is not None,
                "Project add validated ID 누락")
        require(readback["attempts"] == 0 or readback["reservation"] is not None,
                "Project add readback reservation 누락")
        if readback["attempts"]:
            require(readback["reservation"]["attempt"] == readback["attempts"],
                    "Project add readback reservation/횟수 불일치")
        if readback["returned_item_id"] is not None:
            pending = state.get("pending")
            require((pending is not None and pending.get("kind") == "add" and
                     pending.get("project_item_id") in {None, readback["returned_item_id"]}) or
                    (state.get("project_item_id") == readback["returned_item_id"] and
                     (pending is None or pending.get("kind") == "status")),
                    "Project add readback ID와 pending/item checkpoint 불일치")
        require(not readback["validated"] or
                readback["last_result"] == "confirmed",
                "Project add validated/readback 결과 불일치")
    notion_write = state.get("notion_write")
    if notion_write is not None:
        legacy_keys = {"request_id", "expected_before", "target", "kind", "phase"}
        restore_keys = legacy_keys | {"project_item_id", "project_stamp", "facts_fingerprint"}
        verified_restore_keys = restore_keys | {"expected_fields", "expected_fingerprint"}
        keys_ok = (isinstance(notion_write, dict) and
                   (set(notion_write) == legacy_keys or
                    notion_write.get("kind") == "restore" and
                    frozenset(notion_write) in {frozenset(restore_keys),
                                                frozenset(verified_restore_keys)}))
        require(keys_ok and
                (notion_write["request_id"] is None or
                 isinstance(notion_write["request_id"], str)) and
                (notion_write["expected_before"] is None or
                 notion_write["expected_before"] in status_sync.ALL_STATES) and
                (notion_write["target"] is None or
                 notion_write["target"] in status_sync.ALL_STATES) and
                notion_write["kind"] in {"request", "restore", "projection"} and
                notion_write["phase"] in {"prepared", "sent", "uncertain", "confirmed"},
                "Notion write v2 구조 오류")
        if frozenset(notion_write) in {frozenset(restore_keys),
                                      frozenset(verified_restore_keys)}:
            status_sync.validate_stamp(notion_write["project_stamp"])
            require(isinstance(notion_write["project_item_id"], str) and
                    bool(notion_write["project_item_id"]) and
                    isinstance(notion_write["facts_fingerprint"], str) and
                    len(notion_write["facts_fingerprint"]) == 64,
                    "Notion restore checkpoint 구조 오류")
            if set(notion_write) == verified_restore_keys:
                fields = notion_write["expected_fields"]
                require(isinstance(fields, dict) and fields and
                        all(name in SCHEMA and kind == SCHEMA[name] and
                            name != "동기화 내부 상태"
                            for name, kind in fields.items()) and
                        isinstance(notion_write["expected_fingerprint"], str) and
                        re.fullmatch(r"[0-9a-f]{64}",
                                     notion_write["expected_fingerprint"]) is not None,
                        "Notion restore projection readback checkpoint 오류")


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
    require(keys in (LEGACY_RESUME_KEYS, CHECKPOINT_RESUME_KEYS, RESUME_KEYS) and
            type(resume.get("actor_id")) is int and resume["actor_id"] == gp.EXPECTED_PM_USER_ID and
            isinstance(resume.get("run_id"), str) and
            (resume.get("approved_option_id") is None or isinstance(resume["approved_option_id"], str)) and
            isinstance(resume.get("fingerprint"), str) and len(resume["fingerprint"]) == 64 and
            type(resume.get("display_pending")) is bool, "Issue resume 형식 오류")
    require(resume.get("approved_option_id") is None or
            resume["approved_option_id"] in gp.EXPECTED_STATUS_OPTIONS.values(),
            "Issue resume option ID 미등록")
    if keys in (CHECKPOINT_RESUME_KEYS, RESUME_KEYS):
        expected_option = resume.get("expected_option_id")
        expected_fingerprint = resume.get("expected_fingerprint")
        require(expected_option is None or
                (isinstance(expected_option, str) and
                 expected_option in gp.EXPECTED_STATUS_OPTIONS.values()),
                "Issue resume checkpoint option ID 미등록")
        require((expected_option is None and expected_fingerprint is None) or
                (isinstance(expected_fingerprint, str) and len(expected_fingerprint) == 64),
                "Issue resume checkpoint fingerprint 오류")
    if keys == RESUME_KEYS:
        require((resume.get("request_id") is None or
                 isinstance(resume.get("request_id"), str) and resume["request_id"]) and
                isinstance(resume.get("facts_fingerprint"), str) and
                len(resume["facts_fingerprint"]) == 64 and
                resume.get("fact_contract") == "semantic_v2" and
                (resume.get("approved_notion_status") is None or
                 resume.get("approved_notion_status") in OPTIONS["작업 상태"]),
                "Issue resume request/facts binding 오류")


def new_issue_state(issue_id, project_id, baseline_id=None):
    state = {
        "v": 2, "repository_id": REPOSITORY_ID, "issue_id": issue_id, "project_id": project_id,
        "project_item_id": None, "migration_complete": False,
        "reopen_baseline_id": baseline_id, "reopen_last_id": baseline_id,
        "review_cycle": 0, "review_return_cycle": 0, "review_pr_hash": "",
        "pending": None, "hold": None, "resume": None, "projection": None,
        "baseline": None, "request": None, "deferred_request": None,
        "readback": None, "notion_write": None,
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
    require(isinstance(properties, dict) and set(properties) == set(SCHEMA), "Notion 21개 속성 집합 불일치")
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
            _display_date_instant(row, "GitHub 수정", allow_null=True)
            index[key] = row
    require(CONTROL_KEY not in archived_keys and CONTROL_KEY in index,
            "동기화 관리 행이 조회되지 않거나 보관 상태입니다")
    check_control(index[CONTROL_KEY], source_id, control_id)
    pending = read_text(direct_control, "Pending create")
    require(read_text(index[CONTROL_KEY], "Pending create") == pending, "관리 행 snapshot 불일치")
    control_text = read_text(direct_control, "동기화 내부 상태")
    control_state = decode_internal(control_text, kind="control", project_id=project_id) if control_text else None
    listed_date = _display_date_instant(index[CONTROL_KEY], "동기화 시각", allow_null=True)
    direct_date = _display_date_instant(direct_control, "동기화 시각", allow_null=True)
    require(read_text(index[CONTROL_KEY], "동기화 내부 상태") == control_text and
            listed_date == direct_date and
            read_text(index[CONTROL_KEY], "확인 필요") == read_text(direct_control, "확인 필요"),
            "관리행 내부 상태/표시 snapshot 불일치")
    require(not any(key in archived_keys for key in index), "Notion 보관 키 상태 오류")
    if pending:
        require(valid_key(pending) and pending in index, "Pending create 결과 불명; 수동 조사 필요")
    visible_date = direct_date
    if control_state and control_state["last_success_at"]:
        require(visible_date is not None and
                visible_date == timestamp(_minute_iso(control_state["last_success_at"])),
                "관리행 성공 시각/internal checkpoint 불일치")
    return {"source_id": source_id, "control_id": control_id, "index": index,
            "control": direct_control, "control_state": control_state, "pending_create": pending,
            "archived_keys": archived_keys}


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _minute_iso(value):
    parsed = timestamp(value)
    return _iso(parsed.replace(second=0, microsecond=0))


def _display_date_instant(page, name, *, allow_null=False):
    properties = page.get("properties") if isinstance(page, dict) else None
    require(isinstance(properties, dict), "Notion 날짜 readback 형식 오류")
    prop = properties.get(name)
    require(isinstance(prop, dict) and "date" in prop,
            "Notion 날짜 readback 형식 오류")
    date_value = prop["date"]
    if date_value is None:
        require(allow_null, "Notion 날짜 readback 누락")
        return None
    require(isinstance(date_value, dict) and date_value.get("end") is None and
            date_value.get("time_zone") is None,
            "Notion 날짜 readback range/timezone 불일치")
    return timestamp(date_value.get("start"))


def _expected_display_date_instant(name, body):
    require(name in DISPLAY_MINUTE_DATE_PROPERTIES and isinstance(body, dict) and
            body.get("end") is None and body.get("time_zone") is None,
            "표시 날짜 expected 형식 오류")
    start = body.get("start")
    expected = timestamp(start)
    require(start == _iso(expected.replace(second=0, microsecond=0)),
            "표시 날짜 expected UTC minute projection 오류")
    return expected


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
        if name in DISPLAY_MINUTE_DATE_PROPERTIES:
            require(kind == "date", "표시 날짜 속성 타입 오류")
            wanted = _expected_display_date_instant(name, body)
            actual = _display_date_instant(page, name)
            require(actual == wanted,
                    "Notion write readback 불일치; 다음 실행에서 복구 필요")
            continue
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


def _properties_match(page, expected):
    try:
        _verify_properties(page, expected)
    except SyncError:
        return False
    return True


def _projection_property_value(name, kind, body):
    if kind in {"title", "rich_text"}:
        return "".join(part["text"]["content"] for part in body)
    if kind == "select":
        return body.get("name") if body else None
    if kind == "date":
        return (_iso(_expected_display_date_instant(name, body))
                if name in DISPLAY_MINUTE_DATE_PROPERTIES else
                _iso(timestamp(body["start"])) if body else None)
    return body


def _restore_projection_checkpoint(properties):
    fields = {name: next(iter(value)) for name, value in properties.items()
              if name != "동기화 내부 상태"}
    normalized = {name: _projection_property_value(name, kind, properties[name][kind])
                  for name, kind in fields.items()}
    return fields, _digest(normalized)


def _restore_projection_matches(page, marker):
    fields = marker.get("expected_fields")
    if not isinstance(fields, dict):
        return False
    try:
        normalized = {}
        for name, kind in fields.items():
            if name in DISPLAY_MINUTE_DATE_PROPERTIES:
                value = _display_date_instant(page, name, allow_null=True)
                normalized[name] = _iso(value) if value is not None else None
            else:
                normalized[name] = _notion_value(page, name, kind)
        return _digest(normalized) == marker.get("expected_fingerprint")
    except (KeyError, TypeError, ValueError, SyncError):
        return False


def _projection_display_matches(page, projection):
    display = projection.get("display_checkpoint") or {}
    return _restore_projection_matches(page, {
        "expected_fields": display.get("expected_fields"),
        "expected_fingerprint": display.get("expected_fingerprint"),
    })


def _projection_display_matches_except_sync_clock(page, properties):
    """Match the saved legacy display while ignoring only its refreshed sync clock."""
    fields, _ = _restore_projection_checkpoint(properties)
    fields.pop("동기화 시각", None)
    if not fields:
        return False
    expected = {name: properties[name] for name in fields}
    return _restore_projection_matches(page, {
        "expected_fields": fields,
        "expected_fingerprint": _restore_projection_checkpoint(expected)[1],
    })


def _date_format_shape(value):
    if not isinstance(value, str):
        return None, "missing" if value is None else "non_string", None
    fraction = re.search(r"\.(\d+)(?=(?:Z|[+-]\d{2}:\d{2})$)", value)
    fractional_digits = len(fraction.group(1)) if fraction else None
    if value.endswith("Z"):
        offset_shape = "z_suffix"
    else:
        offset = re.search(r"([+-])(\d{2}):(\d{2})$", value)
        if offset:
            offset_shape = ("zero_offset" if offset.group(2) == "00" and
                            offset.group(3) == "00" else "nonzero_offset")
        else:
            offset_shape = "no_offset"
    try:
        parsed = timestamp(value)
    except (SyncError, gp.SyncError):
        parsed = None
    if parsed is not None and fractional_digits is None:
        fractional_digits = 0
    return parsed, offset_shape, fractional_digits


def _date_shape_summary(value):
    parsed, offset_shape, fractional_digits = _date_format_shape(value)
    return {"parseable": parsed is not None,
            "fractional_digits": fractional_digits,
            "offset_shape": offset_shape}


def _compare_redacted_dates(expected, actual):
    expected_time, expected_offset, expected_fraction = _date_format_shape(expected)
    actual_time, actual_offset, actual_fraction = _date_format_shape(actual)
    expected_ok, actual_ok = expected_time is not None, actual_time is not None
    if expected_ok and actual_ok:
        same_instant = expected_time == actual_time
        same_minute = (expected_time.replace(second=0, microsecond=0) ==
                       actual_time.replace(second=0, microsecond=0))
        parseable = "both"
    else:
        same_instant = None
        same_minute = None
        parseable = ("expected_only" if expected_ok else
                     "actual_only" if actual_ok else "neither")
    return {
        "literal_equal": (isinstance(expected, str) and isinstance(actual, str) and
                          expected == actual),
        "parseable": parseable,
        "same_instant": same_instant,
        "same_minute": same_minute,
        "expected_fractional_digits": expected_fraction,
        "actual_fractional_digits": actual_fraction,
        "expected_offset_shape": expected_offset,
        "actual_offset_shape": actual_offset,
        "actual_seconds_zero": None if actual_time is None else actual_time.second == 0,
        "actual_microseconds_zero": None if actual_time is None else actual_time.microsecond == 0,
    }


def _diagnose_date_readback(source, index, now):
    issue_number = 1
    matches = [(key, row_data) for key, (kind, _, row_data) in source.items()
               if kind == "Issue" and row_data.get("number") == issue_number]
    diagnostic = {"issue_number": issue_number, "property": "GitHub 수정"}
    if not matches:
        return {**diagnostic, "row_match": "source_missing"}
    if len(matches) != 1:
        return {**diagnostic, "row_match": "source_ambiguous"}
    key, issue = matches[0]
    row = index.get(key)
    if row is None:
        return {**diagnostic, "row_match": "notion_missing"}
    expected_metadata = _make_metadata("Issue", issue, key)
    metadata_matches = []
    for name in DATE_DIAGNOSTIC_METADATA_WHITELIST:
        value = expected_metadata[name]
        kind, body = next(iter(value.items()))
        if kind in {"title", "rich_text"}:
            wanted = "".join(part["text"]["content"] for part in body)
        elif kind == "select":
            wanted = body.get("name") if body else None
        elif kind == "date":
            wanted = body.get("start") if body else None
        else:
            wanted = body
        if name in DISPLAY_MINUTE_DATE_PROPERTIES:
            matches_property = (_display_date_instant(row, name) ==
                                _expected_display_date_instant(name, body))
        else:
            matches_property = _notion_value(row, name, kind) == wanted
        metadata_matches.append({
            "property": name,
            "matches": matches_property,
        })
    first_metadata_mismatch = next(
        (entry["property"] for entry in metadata_matches if not entry["matches"]), None)
    return {**diagnostic, "row_match": "matched",
            **_compare_redacted_dates(issue.get("updatedAt"),
                                      read_date(row, "GitHub 수정")),
            "metadata_matches": metadata_matches,
            "first_metadata_mismatch": first_metadata_mismatch,
            "clock_shape": {
                "property": "동기화 시각",
                "current_run_would_write": _date_shape_summary(_minute_iso(now)),
                "stored_value": _date_shape_summary(read_date(row, "동기화 시각")),
            }}


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
        "GitHub 수정": {"date": {"start": _minute_iso(updated)}},
        "동기화 키": text_property(key),
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


def _status_facts_fingerprint(issue, linked_refs):
    """Bind a status baseline to semantic Issue/PR facts, excluding cosmetic Issue edits."""
    duplicate = issue.get("duplicateOf") or {}
    return status_sync.fingerprint({
        "issue_id": issue.get("databaseId"), "issue_node_id": issue.get("id"),
        "state": issue.get("state"), "stateReason": issue.get("stateReason"),
        "closedAt": issue.get("closedAt"), "duplicate_id": duplicate.get("id"),
        "reopens": [{"id": event["id"], "createdAt": event["createdAt"]}
                    for _, event in _issue_reopens(issue)],
        "linked_prs": linked_refs,
    })


def _pending_semantic_fingerprint(issue, linked_refs, item, kind):
    facts = _status_facts_fingerprint(issue, linked_refs)
    binding = None
    if kind == "status":
        binding = {"item_id": item.get("id") if item else None,
                   "option_id": item.get("status_option_id") if item else None}
    return status_sync.fingerprint({"facts": facts, "kind": kind, "item": binding})


def _status_operation_fingerprint(issue, linked_refs, item):
    return status_sync.fingerprint({
        "semantic": _pending_semantic_fingerprint(issue, linked_refs, item, "status"),
        "status_value_id": item.get("status_value_id") if item else None,
        "status_updated_at": item.get("status_updated_at") if item else None,
    })


def _pending_fingerprint(issue, linked_refs, item, pending):
    """Use semantic facts for new v2 checkpoints; preserve exact v1 hash semantics."""
    if pending.get("fact_contract") == "semantic_v2":
        return _pending_semantic_fingerprint(issue, linked_refs, item, pending["kind"])
    return _source_fingerprint(issue, linked_refs, item)


def _status_stamp(item, config):
    return {"field_id": config["status_field_id"],
            "option_id": item.get("status_option_id") if item else None,
            "value_id": item.get("status_value_id") if item else None,
            "updated_at": item.get("status_updated_at") if item else None}


def _new_status_baseline(notion_status, item, issue, linked_refs, config, observed_at):
    return status_sync.make_baseline(
        notion_status, _status_stamp(item, config), item["id"],
        _status_facts_fingerprint(issue, linked_refs), observed_at)


def _resume_checkpoint_matches(resume, issue, facts, item):
    """Only a checkpointed, exact current Project/source snapshot can recover display."""
    if (not isinstance(resume, dict) or set(resume) not in
            (CHECKPOINT_RESUME_KEYS, RESUME_KEYS) or
            not resume.get("display_pending") or resume.get("expected_fingerprint") is None or
            item is None or item.get("status_option_id") != resume.get("expected_option_id")):
        return False
    linked, _ = _linked_pr_facts(issue, facts)
    if set(resume) == RESUME_KEYS:
        return (_status_facts_fingerprint(issue, linked) == resume["facts_fingerprint"] and
        _status_operation_fingerprint(issue, linked, item) ==
                resume["expected_fingerprint"])
    return _source_fingerprint(issue, linked, item) == resume["expected_fingerprint"]


def _resume_approval_matches(resume, issue, facts, item):
    """An uncheckpointed saved approval may continue only from its original exact snapshot."""
    if (not isinstance(resume, dict) or set(resume) not in
            (CHECKPOINT_RESUME_KEYS, RESUME_KEYS) or
            not resume.get("display_pending") or resume.get("expected_fingerprint") is not None or
            item is None or item.get("status_option_id") != resume.get("approved_option_id")):
        return False
    linked, _ = _linked_pr_facts(issue, facts)
    if set(resume) == RESUME_KEYS:
        return (_status_facts_fingerprint(issue, linked) == resume["facts_fingerprint"] and
        _status_operation_fingerprint(issue, linked, item) ==
                resume["fingerprint"])
    return _source_fingerprint(issue, linked, item) == resume["fingerprint"]


def _resume_matches(resume, issue, facts, item, *, request_id=None, notion_status=None):
    if isinstance(resume, dict):
        if set(resume) == RESUME_KEYS:
            if (resume.get("request_id") != request_id or
                    resume.get("approved_notion_status") != notion_status):
                return False
        elif request_id is not None:
            # Pre-v2 approvals did not bind to a status request and cannot authorize one.
            return False
    return (_resume_checkpoint_matches(resume, issue, facts, item) or
            _resume_approval_matches(resume, issue, facts, item))


def _projection_fingerprint(projection, issue, linked_refs, item):
    if projection.get("fact_contract") == "semantic_v2":
        return _status_operation_fingerprint(issue, linked_refs, item)
    return _source_fingerprint(issue, linked_refs, item)


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
    if (pending["kind"] == "add" and (pre_pending_state.get("hold") or {}).get("code") in
            {"PROJECT_ADD_UNCERTAIN", "ADD_READBACK_EXHAUSTED",
             "ADD_READBACK_CONTRADICTION"}):
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
        if current is None:
            return {"hold": _new_hold(
                "MIGRATION_UNSET",
                "기존 이슈의 Project 상태가 미지정입니다. PM이 Project 상태를 정한 뒤 새 수동 실행으로 재개해야 합니다.",
                fingerprint), "fingerprint": fingerprint}
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
    if not (pending["migration_fingerprint"] is not None and hold and
            hold.get("code") in RESUMABLE_HOLD_CODES and resume and
            resume.get("display_pending") and
            resume.get("approved_option_id") == pending["before_option_id"]):
        return False
    if set(resume) == RESUME_KEYS:
        stamp = pending.get("before_status_stamp")
        expected_operation = (status_sync.fingerprint({
            "semantic": pending.get("semantic_fingerprint"),
            "status_value_id": stamp.get("value_id"),
            "status_updated_at": stamp.get("updated_at"),
        }) if isinstance(stamp, dict) and pending.get("fact_contract") == "semantic_v2"
            else pending.get("semantic_fingerprint"))
        return (resume.get("fact_contract") == "semantic_v2" and
                resume.get("facts_fingerprint") and
                resume.get("approved_notion_status") == pending.get("notion_status") and
                resume.get("request_id") == pending.get("request_id") and
                resume.get("fingerprint") == expected_operation)
    return resume.get("fingerprint") == pending["facts_fingerprint"]


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
    properties = {**metadata, "동기화 시각": {"date": {"start": _minute_iso(now)}},
                  "Pending create": text_property("")}
    if kind == "Issue":
        properties["작업 상태"] = {"select": None}
        properties["요청 처리"] = {"select": None}
        properties["요청 상태"] = {"select": None}
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
    require(identifier(confirmed.get("id")) == page_id,
            "Notion 생성행 ID readback이 POST 응답과 달라 생성 fence 유지")
    bound_page(confirmed, source_id)
    require(not is_archived(confirmed) and read_text(confirmed, "동기화 키") == key and
            read_select(confirmed, "종류") == kind, "Notion 생성행 식별 readback 오류; fence 유지")
    _verify_properties(confirmed, properties)
    update_control_pending(notion, source_id, control, "")
    index[key] = confirmed
    return confirmed, True


def update_control_pending(notion, source_id, control, value):
    _patch_page(notion, source_id, control, {"Pending create": text_property(value)})


def _project_snapshot(client, config, facts, *, add_readback_item_id=None, add_target_issue=None):
    project = gp.fetch_project(client, project_id=config["project_id"], owner_id=config["owner_id"],
                               status_field_id=config["status_field_id"],
                               status_options=config["status_options"],
                               repository_node_id=facts["repository_node_id"],
                               add_readback_item_id=add_readback_item_id,
                               add_target_issue_id=(add_target_issue["databaseId"]
                                                    if add_target_issue is not None else None),
                               add_target_issue_node_id=(add_target_issue["id"]
                                                         if add_target_issue is not None else None))
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


def _confirmed_add_item_matches(pending, item):
    if not pending["confirmed"]:
        return True
    if item is None:
        return False
    checkpoint = pending["checkpoint"]
    return (item.get("id") == pending.get("project_item_id") and
            item.get("id") == checkpoint.get("item_id"))


def _added_project_item_from_snapshot(project, direct_item, returned_item_id, issue, facts):
    observations = project.get("add_readback_items")
    require(isinstance(observations, list), "Project add readback 관측 오류")
    returned = [row for row in observations if row.get("item_id") == returned_item_id]
    target = [row for row in observations
              if (row.get("content_id") == issue["id"] or
                  (row.get("content_type") == "Issue" and
                   row.get("content_database_id") == issue["databaseId"]))]
    if not returned and not target and direct_item is None:
        return None
    if returned or target:
        require(len(returned) == 1 and len(target) == 1 and
                returned[0]["item_id"] == target[0]["item_id"] == returned_item_id,
                "Project 추가 결과 returned ID와 대상 Issue가 불일치; pending 유지")
        observed = returned[0]
        require(observed.get("is_archived") is False and
                observed.get("content_type") == "Issue" and
                observed.get("content_id") == issue["id"] and
                observed.get("content_database_id") == issue["databaseId"] and
                observed.get("repository_id") == facts["repository_node_id"] and
                observed.get("repository_database_id") == REPOSITORY_ID,
                "Project 추가 결과 ID/Issue/repository/archive 관계 불일치; pending 유지")

    item = project["items"].get(issue["databaseId"])
    archived = project.get("archived_items", {}).get(issue["databaseId"], [])
    if item is not None:
        require(item["id"] == returned_item_id and item["content_id"] == issue["id"] and
                not archived, "Project 추가 결과 active/archive 항목 충돌; pending 유지")
    elif archived:
        raise gp.SyncError("Project add 반환 ID가 보관된 항목과 충돌합니다")
    if direct_item is not None:
        require(direct_item.get("id") == returned_item_id and
                direct_item.get("content_id") == issue["id"] and
                direct_item.get("is_archived") is False,
                "Project add 직접 node ID/Issue/archive 관계 불일치")
    if item is None or direct_item is None:
        return None
    require(item["id"] == direct_item["id"] and
            item["status_option_id"] == direct_item["status_option_id"] and
            item["status_value_id"] == direct_item["status_value_id"] and
            item["status_updated_at"] == direct_item["status_updated_at"],
            "Project add 직접 node와 전체 목록의 Status 값 불일치")
    return item


def _project_readback_network_error(exc):
    message = str(exc)
    return ("연결/응답 실패" in message or
            any(f"HTTP {status}" in message for status in (429, 500, 502, 503, 504, 529)))


def _read_added_project_item_once(notion, source_id, row, state, project_client,
                                  config, facts, issue, run_identity):
    readback = state.get("readback")
    require(isinstance(readback, dict) and readback.get("returned_item_id"),
            "Project add 응답 item ID가 없어 자동 readback할 수 없습니다")
    require(not readback["validated"], "이미 검증된 Project add 결과를 다시 예약할 수 없습니다")
    if (readback["attempts"] == PROJECT_ADD_READBACK_MAX_ATTEMPTS and
            readback.get("last_result") is None and readback.get("reservation")):
        # The final reservation was durably recorded, but its query result was not.
        # Treat the consumed attempt conservatively and hand it to PM without querying again.
        hold = _new_hold("ADD_READBACK_EXHAUSTED",
                         "Project 추가 결과를 3회 확인하지 못해 PM 확인이 필요합니다.",
                         _digest(readback["reservation"]))
        state["hold"] = hold
        _verify_issue_state_write(notion, source_id, row, state)
        return None, None, hold
    if (readback["attempts"] == PROJECT_ADD_READBACK_MAX_ATTEMPTS and
            not readback["validated"] and
            readback.get("last_result") in {"network_error", "not_visible"} and
            state.get("hold") is None):
        # The final result was persisted, but the process stopped before persisting
        # the exhaustion hold. Never spend a fourth query to repair that split write.
        hold = _new_hold("ADD_READBACK_EXHAUSTED",
                         "Project 추가 결과를 3회 확인하지 못해 PM 확인이 필요합니다.",
                         _digest(readback.get("reservation")))
        state["hold"] = hold
        _verify_issue_state_write(notion, source_id, row, state)
        return None, None, hold
    require(readback["attempts"] < PROJECT_ADD_READBACK_MAX_ATTEMPTS,
            "Project add readback 횟수 초과")
    reservation = readback.get("reservation")
    if (reservation and reservation.get("run_id") == run_identity["run_id"] and
            reservation.get("run_attempt") == run_identity["run_attempt"]):
        return None, None, None
    readback["attempts"] += 1
    readback["reservation"] = {"token": str(uuid4()), "run_id": run_identity["run_id"],
                               "run_attempt": run_identity["run_attempt"],
                               "attempt": readback["attempts"]}
    readback["last_result"] = None
    _verify_issue_state_write(notion, source_id, row, state)
    returned_item_id = readback["returned_item_id"]
    try:
        direct_item = gp.fetch_project_item(
            project_client, config["project_id"], returned_item_id,
            config["status_field_id"], allow_archived=True, allow_missing=True)
        project = _project_snapshot(project_client, config, facts,
                                    add_readback_item_id=returned_item_id,
                                    add_target_issue=issue)
    except gp.SyncError as exc:
        if not _project_readback_network_error(exc):
            readback["last_result"] = "contradiction"
            hold = _new_hold("ADD_READBACK_CONTRADICTION",
                             "Project 추가 결과의 대상/저장소 관계를 검증하지 못했습니다.",
                             _digest(readback["reservation"]))
            state["hold"] = hold
            _verify_issue_state_write(notion, source_id, row, state)
            return None, None, hold
        readback["last_result"] = "network_error"
        _verify_issue_state_write(notion, source_id, row, state)
        if readback["attempts"] >= PROJECT_ADD_READBACK_MAX_ATTEMPTS:
            hold = _new_hold("ADD_READBACK_EXHAUSTED",
                             "Project 추가 결과를 3회 확인하지 못해 PM 확인이 필요합니다.",
                             _digest(readback["reservation"]))
            state["hold"] = hold
            _verify_issue_state_write(notion, source_id, row, state)
            return None, None, hold
        return None, None, None
    try:
        item = _added_project_item_from_snapshot(
            project, direct_item, returned_item_id, issue, facts)
    except (SyncError, gp.SyncError):
        readback["last_result"] = "contradiction"
        hold = _new_hold("ADD_READBACK_CONTRADICTION",
                         "Project add 응답 ID가 대상 Issue/저장소의 활성 항목과 일치하지 않습니다.",
                         _digest(readback["reservation"]))
        state["hold"] = hold
        _verify_issue_state_write(notion, source_id, row, state)
        return project, None, hold
    if item is None:
        readback["last_result"] = "not_visible"
        _verify_issue_state_write(notion, source_id, row, state)
        if readback["attempts"] >= PROJECT_ADD_READBACK_MAX_ATTEMPTS:
            hold = _new_hold("ADD_READBACK_EXHAUSTED",
                             "Project 추가 결과를 3회 확인하지 못해 PM 확인이 필요합니다.",
                             _digest(readback["reservation"]))
            state["hold"] = hold
            _verify_issue_state_write(notion, source_id, row, state)
            return project, None, hold
        return project, None, None
    readback["validated"] = True
    readback["last_result"] = "confirmed"
    _verify_issue_state_write(notion, source_id, row, state)
    return project, item, None


def _read_added_project_item_immediate(notion, source_id, row, state, project_client,
                                       config, facts, issue):
    readback = state["readback"]
    returned_item_id = readback["returned_item_id"]
    try:
        direct_item = gp.fetch_project_item(
            project_client, config["project_id"], returned_item_id,
            config["status_field_id"], allow_archived=True, allow_missing=True)
        project = _project_snapshot(project_client, config, facts,
                                    add_readback_item_id=returned_item_id,
                                    add_target_issue=issue)
    except gp.SyncError as exc:
        readback["last_result"] = "network_error" if _project_readback_network_error(exc) else "contradiction"
        if readback["last_result"] == "contradiction":
            hold = _new_hold("ADD_READBACK_CONTRADICTION",
                             "Project 추가 결과의 대상/저장소 관계를 검증하지 못했습니다.",
                             _digest({"item_id": returned_item_id, "result": "contradiction"}))
            state["hold"] = hold
        _verify_issue_state_write(notion, source_id, row, state)
        return None, None, state.get("hold")
    try:
        item = _added_project_item_from_snapshot(project, direct_item, returned_item_id, issue, facts)
    except (SyncError, gp.SyncError):
        readback["last_result"] = "contradiction"
        hold = _new_hold("ADD_READBACK_CONTRADICTION",
                         "Project add 응답 ID와 직접 node/전체 목록의 관계가 모순됩니다.",
                         _digest({"item_id": returned_item_id, "result": "contradiction"}))
        state["hold"] = hold
        _verify_issue_state_write(notion, source_id, row, state)
        return project, None, hold
    if item is None:
        readback["last_result"] = "not_visible"
        _verify_issue_state_write(notion, source_id, row, state)
        return project, None, None
    readback["validated"] = True
    readback["last_result"] = "confirmed"
    _verify_issue_state_write(notion, source_id, row, state)
    return project, item, None


def _display_add_readback_waiting(notion, source_id, row, state, now):
    readback = state["readback"]
    confirmation = (f"Project 추가 결과를 자동 확인 중입니다 "
                    f"({readback['attempts']}/{PROJECT_ADD_READBACK_MAX_ATTEMPTS}).")
    _patch_page(notion, source_id, row, {
        "동기화 시각": {"date": {"start": _minute_iso(now)}},
        "요청 처리": {"select": None}, "요청 상태": {"select": None},
        "확인 필요": text_property(confirmation),
        "동기화 내부 상태": text_property(canonical_json(state)),
    })


def _status_option(project, name):
    return project["status_options"][name] if name else None


def _verify_issue_state_write(notion, source_id, row, state):
    _save_issue_state(notion, source_id, row, state)


def _issue_state_binding_matches(row, state):
    saved = decode_internal(read_text(row, "동기화 내부 상태"), kind="issue",
                            project_id=state["project_id"], object_id=state["issue_id"])
    if saved is None:
        return False
    return (saved.get("projection") == state.get("projection") and
            (saved.get("request") or {}).get("id") ==
            (state.get("request") or {}).get("id") and
            (saved.get("request") or {}).get("phase") ==
            (state.get("request") or {}).get("phase") and
            saved.get("notion_write") == state.get("notion_write") and
            saved.get("baseline") == state.get("baseline"))


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


def _validate_manual_resume(github, raw, env, *, dry_run=False):
    numbers = _resume_numbers(raw)
    event_name = env.get("GITHUB_EVENT_NAME")
    ref = env.get("GITHUB_REF")
    if event_name == "workflow_dispatch":
        # Feature refs are useful for read-only previews, but all live writes
        # and every PM resolution must come from the trusted main checkout.
        feature_refs = {"refs/heads/fix/17-project-add-readback",
                        "refs/heads/feat/32-bidirectional-sync"}
        require(ref == "refs/heads/main" or
                ref in feature_refs and dry_run and not numbers,
                "수동 실행은 main에서만 쓰기/보류 재개할 수 있습니다")
        dispatch_ref = env.get("SYNC_DISPATCH_REF")
        dispatch_dry_run = env.get("SYNC_DISPATCH_DRY_RUN")
        dispatch_resolve = env.get("SYNC_DISPATCH_RESOLVE_ISSUES")
        require(dispatch_ref in (None, ref) and
                dispatch_dry_run in (None, "true" if dry_run else "false") and
                dispatch_resolve in (None, raw or ""),
                "수동 실행 입력과 검증된 dispatch 문맥이 일치하지 않습니다")
    elif env.get("GITHUB_ACTIONS") == "true":
        require(ref == "refs/heads/main",
                "자동 동기화 쓰기는 trusted main에서만 허용됩니다")
    if not numbers:
        return set(), None
    require(event_name == "workflow_dispatch",
            "수동 보류 재개는 workflow_dispatch에서만 허용됩니다")
    require(ref == "refs/heads/main",
            "PM 보류 재개는 refs/heads/main에서만 허용됩니다")
    sha = env.get("GITHUB_SHA", "")
    approved_sha = env.get("SYNC_APPROVED_SHA", "")
    require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) is not None and
            approved_sha == sha,
            "PM 재개 승인 SHA가 현재 workflow SHA와 일치하지 않습니다")
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
    properties["동기화 시각"] = {"date": {"start": _minute_iso(now)}}
    if kind == "Issue":
        request = _visible_status_request(state)
        if request and request.get("reason"):
            confirmation = request["reason"]
        properties.update({
            "작업 상태": {"select": {"name": target} if target else None},
            "종료 사유": {"select": {"name": end_reason} if end_reason else None},
            "대표 이슈": {"url": representative},
            "확인 필요": text_property(confirmation),
            "동기화 내부 상태": text_property(canonical_json(state)),
            "요청 처리": {"select": {"name": _request_ui_status(state, request)}
                         if request else None},
            "요청 상태": {"select": {"name": request["target"]}
                         if request and request["target"] else None},
        })
    return properties


def _visible_status_request(state):
    return state.get("deferred_request") or state.get("request")


def _request_ui_status(state, request=None):
    request = request or _visible_status_request(state)
    if not request:
        return None
    phase = request["phase"]
    return {
        "accepted": "반영 대기", "waiting": "자동 확인 중",
        "prepared": "반영 대기", "sent": "자동 확인 중",
        "uncertain": "자동 확인 중", "confirmed": "자동 확인 중",
        "completed": "반영 완료", "rejected": "요청 거절", "held": "PM 확인 필요",
    }[phase]


def _status_request_properties(state):
    request = _visible_status_request(state)
    return {"요청 처리": {"select": {"name": _request_ui_status(state, request)} if request else None},
            "요청 상태": {"select": {"name": request["target"]}
                          if request and request["target"] else None},
            "확인 필요": text_property(request["reason"] if request else ""),
            "동기화 내부 상태": text_property(canonical_json(state))}


def _save_status_request(notion, source_id, row, state):
    _patch_page(notion, source_id, row, _status_request_properties(state))


def _verify_project_status_page_before_mutation(notion, source_id, row):
    page_id = identifier(row.get("id"))
    latest = notion.request("GET", f"/pages/{page_id}")
    bound_page(latest, source_id)
    require(identifier(latest.get("id")) == page_id and not is_archived(latest),
            "Project 상태 변경 직전 Notion 페이지 대상이 달라졌습니다")
    expected = row.get("properties") or {}
    actual = latest.get("properties") or {}
    for name in ("작업 상태", "요청 처리", "요청 상태", "확인 필요",
                 "동기화 시각", "동기화 내부 상태"):
        require(actual.get(name) == expected.get(name),
                f"Project 상태 변경 직전 Notion {name} readback이 달라졌습니다")
    row.clear()
    row.update(latest)


def _confirmed_restore_echo(state, notion_status):
    marker = state.get("notion_write") or {}
    request = state.get("request") or {}
    return (marker.get("kind") == "restore" and marker.get("phase") == "confirmed" and
            marker.get("request_id") == request.get("id") and
            marker.get("target") == notion_status)


def _card_move_needs_preservation(state, row):
    if _confirmed_restore_echo(state, read_select(row, "작업 상태")):
        return True
    baseline = state.get("baseline")
    if baseline is None:
        return False
    if read_select(row, "작업 상태") != baseline["notion_status"]:
        return True
    request = state.get("request") or {}
    unresolved_request = request.get("phase") in {
        "accepted", "waiting", "prepared", "sent", "uncertain", "confirmed", "held"}
    unresolved_write = ((state.get("notion_write") or {}).get("phase") in
                        {"prepared", "sent", "uncertain"})
    return bool(state.get("pending") or state.get("projection") or
                state.get("deferred_request") or unresolved_request or unresolved_write)


def _record_held_card_request(notion, source_id, row, state, item, issue, facts,
                              config, observed_at):
    """Persist the latest card move without replacing an older uncertain request."""
    target = read_select(row, "작업 상태")
    if _confirmed_restore_echo(state, target):
        return False
    baseline = state.get("baseline")
    if baseline is None:
        return False
    active = state.get("request") or {}
    deferred = state.get("deferred_request")
    if (active.get("phase") == "held" and active.get("target") == target and
            deferred is None):
        # The same held card is still the same request, with or without PM approval.
        # Once a deferred move was observed, a return to this value is a new move.
        return False
    if (deferred and deferred.get("phase") == "held" and
            (state.get("hold") or {}).get("code") == "DEFERRED_REQUEST_PENDING" and
            active.get("phase") in {"completed", "rejected"} and
            target in {baseline.get("notion_status"),
                       active.get("target") if active.get("phase") == "completed" else None}):
        # After A is settled, either the prior baseline or A's completed result
        # can reappear as a display echo. Keep the separately observed B request.
        return False
    restore_marker = state.get("notion_write") or {}
    confirmed_restore_context = (
        restore_marker.get("kind") == "restore" and
        restore_marker.get("phase") == "confirmed" and
        restore_marker.get("request_id") == active.get("id"))
    older_result_unresolved = bool(state.get("pending") or state.get("projection") or
        active.get("phase") in {"accepted", "waiting", "prepared", "sent", "uncertain",
                                 "confirmed", "held"} or confirmed_restore_context)
    if target == baseline["notion_status"] and not (deferred or older_result_unresolved):
        return False
    use_deferred = (deferred is not None or older_result_unresolved or
                    active.get("phase") in {"prepared", "sent", "uncertain", "confirmed", "held"} or
                    confirmed_restore_context)
    current = deferred if use_deferred else active
    linked, _ = _linked_pr_facts(issue, facts, allow_snapshot_drift=True)
    project_option_id = item.get("status_option_id") if isinstance(item, dict) else None
    reason = (state.get("hold") or {}).get(
        "message", "기존 상태 결과 확인이 끝날 때까지 새 Notion 상태 요청을 보류합니다.")
    if not reason.startswith("보류:"):
        reason = "보류: " + reason
    if current and current.get("target") == target and current.get("phase") == "held":
        changed = current.get("project_option_id") != project_option_id
        if not changed:
            return False
        current["project_option_id"] = project_option_id
        if use_deferred:
            state["deferred_request"] = current
        else:
            state["request"] = current
        _save_status_request(notion, source_id, row, state)
        return True
    new_request = status_sync.request_record(
        str(uuid4()), target, observed_from=baseline["observed_at"],
        observed_to=observed_at, prior_notion_status=baseline["notion_status"],
        project_option_id=project_option_id, phase="held", reason=reason)
    if use_deferred:
        state["deferred_request"] = new_request
    else:
        state["request"] = new_request
        state["notion_write"] = None
    _save_status_request(notion, source_id, row, state)
    return True


def _deferred_card_matches(state, projection, notion_status):
    request = state.get("request") or {}
    deferred = state.get("deferred_request") or {}
    return bool(
        isinstance(projection, dict) and
        request.get("id") == projection.get("request_id") and
        deferred.get("id") and deferred.get("id") != request.get("id") and
        deferred.get("phase") == "held" and
        deferred.get("target") == notion_status and
        notion_status != projection.get("result_notion_status"))


def _classify_notion_status_change(state, row, item, issue, facts, config, observed_at):
    baseline = state.get("baseline")
    if baseline is None:
        return {"action": "initialize", "target": None}, None
    require(baseline["project_item_id"] == item["id"],
            "Status baseline Project item ID changed")
    linked, eligible = _linked_pr_facts(issue, facts, allow_snapshot_drift=True)
    semantic_match = (_status_facts_fingerprint(issue, linked) ==
                      baseline["facts_fingerprint"])
    status_by_option = {value: name for name, value in config["status_options"].items()}
    project_status = status_by_option.get(item.get("status_option_id"))
    decision = status_sync.decide(
        baseline, read_select(row, "작업 상태"), _status_stamp(item, config), project_status,
        facts_override=(not semantic_match or issue.get("state") != "OPEN" or
                        issue.get("duplicateOf") is not None or bool(eligible)),
        facts_verified=semantic_match)
    if not decision["notion_changed"]:
        return decision, None
    if decision["action"] == "converged":
        # A verified common state is a safe baseline refresh, not a held user request.
        return decision, None
    reason_by_action = {
        "conflict": "GitHub Project와 Notion 상태가 모두 바뀌었고 순서를 입증할 수 없어 PM 확인이 필요합니다.",
        "hold_unknown": "Project 상태 변경 근거가 불완전해 Notion 요청을 자동 반영할 수 없습니다.",
        "facts_override": "GitHub Issue/PR 사실 변경이 Notion 상태 요청보다 우선되어 PM 확인이 필요합니다.",
        "hold_invalid_request": (
            f"Notion 상태 요청 {read_select(row, '작업 상태')!r}은 허용되지 않습니다. "
            "백로그·준비 중·진행 중 요청만 자동 반영할 수 있습니다."),
    }
    phase = "accepted" if decision["action"] == "notion_request" else (
        "rejected" if decision["action"] == "hold_invalid_request" else "held")
    request = status_sync.request_record(
        str(uuid4()), read_select(row, "작업 상태"),
        observed_from=baseline["observed_at"], observed_to=observed_at,
        prior_notion_status=baseline["notion_status"],
        project_option_id=item.get("status_option_id"), phase=phase,
        reason=reason_by_action.get(decision["action"], ""))
    return decision, request


def _display_hold(notion, source_id, row, metadata, state, hold, now, *, preserve_task_status=False):
    state["hold"] = hold
    state["resume"] = None
    _verify_issue_state_write(notion, source_id, row, state)
    visible = "보류: " + hold["message"]
    active_request = state.get("request") or {}
    marker = state.get("notion_write") or {}
    if (active_request.get("phase") == "rejected" and
            marker.get("kind") == "restore" and marker.get("phase") == "confirmed" and
            read_select(row, "작업 상태") in status_sync.ALL_STATES):
        # Do not invalidate the exact terminal display fingerprint while the
        # feature is paused or a newer card move is awaiting separate review.
        return
    preserve_task_status = preserve_task_status or (
        active_request.get("phase") in {"prepared", "sent", "uncertain", "confirmed", "held",
                                         "rejected"} and
        read_select(row, "작업 상태") in status_sync.ALL_STATES)
    if (preserve_task_status or state.get("deferred_request") is not None or
            _confirmed_restore_echo(state, read_select(row, "작업 상태"))):
        props = {**metadata, "동기화 시각": {"date": {"start": _minute_iso(now)}},
                 "확인 필요": text_property(visible),
                 "동기화 내부 상태": text_property(canonical_json(state))}
    else:
        props = _projection_properties(metadata, target=None, end_reason=None, representative=None,
                                       confirmation=visible, state=state, now=now, kind="Issue")
    _patch_page(notion, source_id, row, props)


def _persist_restore_pm_approval(notion, source_id, row, state, issue, item,
                                 linked_refs, actor, notion_status, config):
    request_id = (state.get("request") or {}).get("id")
    state["resume"] = {
        "actor_id": actor["actor_id"], "run_id": actor["run_id"],
        "approved_option_id": item.get("status_option_id"),
        "fingerprint": _status_operation_fingerprint(issue, linked_refs, item),
        "display_pending": True, "expected_option_id": None,
        "expected_fingerprint": None, "request_id": request_id,
        "facts_fingerprint": _status_facts_fingerprint(issue, linked_refs),
        "fact_contract": "semantic_v2", "approved_notion_status": notion_status}
    state["notion_write"] = None
    if state.get("hold") is None:
        state["hold"] = _new_hold(
            "PENDING_RESULT_UNCLEAR",
            "PM이 현재 GitHub/Notion snapshot으로 복원 결과를 다시 판정하도록 승인했습니다.",
            _digest({"request_id": request_id,
                     "facts": state["resume"]["facts_fingerprint"],
                     "option_id": item.get("status_option_id")}))
    _verify_issue_state_write(notion, source_id, row, state)


def _recover_restore_write(notion, github, project_client, source_id, row, metadata,
                           state, facts, issue, item, config, cutoff, now,
                           *, manual_resume=False, resume_actor=None):
    """Reconcile a durable Notion status restore without replaying an uncertain PATCH."""
    marker = state.get("notion_write") or {}
    if (marker.get("kind") != "restore" or "project_stamp" not in marker or
            marker.get("phase") == "confirmed"):
        return False

    latest_issue = gp.fetch_issue_detail(github, issue["id"])
    latest_refs, _ = _linked_pr_facts(latest_issue, facts, allow_snapshot_drift=True)
    latest_item = gp.fetch_project_item(
        project_client, config["project_id"], marker["project_item_id"],
        config["status_field_id"], allow_archived=True)
    latest_page = notion.request("GET", f"/pages/{identifier(row.get('id'))}")
    bound_page(latest_page, source_id)
    latest_state = decode_internal(
        read_text(latest_page, "동기화 내부 상태"), kind="issue",
        project_id=config["project_id"], object_id=issue["databaseId"])
    saved_marker = (latest_state or {}).get("notion_write") or {}
    current_task = read_select(latest_page, "작업 상태")
    request_id = marker.get("request_id")
    request_matches = ((latest_state or {}).get("request") or {}).get("id") == request_id
    fingerprint = _status_facts_fingerprint(latest_issue, latest_refs)
    identity_ok = (latest_item.get("id") == marker["project_item_id"] and
                   latest_item.get("content_id") == latest_issue.get("id") and
                   latest_item.get("status_field_id") == config["status_field_id"] and
                   not latest_item.get("is_archived"))
    facts_ok = fingerprint == marker["facts_fingerprint"]
    project_ok = _status_stamp(latest_item, config) == marker["project_stamp"]
    marker_ok = (saved_marker.get("kind") == "restore" and
                 saved_marker.get("request_id") == request_id and
                 saved_marker.get("target") == marker.get("target"))

    if not (identity_ok and facts_ok and project_ok and request_matches and marker_ok):
        if (manual_resume and resume_actor and request_matches and
                current_task == marker.get("expected_before")):
            _persist_restore_pm_approval(notion, source_id, latest_page, state,
                                         latest_issue, latest_item, latest_refs,
                                         resume_actor, current_task, config)
            return False
        if (current_task not in {marker.get("target"), marker.get("expected_before")} and
                state.get("baseline")):
            _record_held_card_request(notion, source_id, latest_page, state,
                                      latest_item, latest_issue, facts, config, now)
        state["hold"] = _new_hold(
            "PENDING_RESULT_UNCLEAR",
            "Notion 상태 복원 중 Project 연결·상태 또는 GitHub 사실이 달라져 자동 복구를 멈췄습니다.",
            _digest({"request_id": request_id, "facts": fingerprint,
                     "item": latest_item.get("id"),
                     "project_stamp": _status_stamp(latest_item, config)}))
        if saved_marker.get("phase") in {"prepared", "sent", "uncertain"}:
            state["notion_write"] = dict(saved_marker)
            state["notion_write"]["phase"] = "uncertain"
        _verify_issue_state_write(notion, source_id, latest_page, state)
        _display_hold(notion, source_id, latest_page, metadata, state,
                      state["hold"], now, preserve_task_status=True)
        return True

    phase = saved_marker.get("phase")
    if phase == "confirmed":
        if current_task == marker["target"]:
            state["notion_write"] = dict(saved_marker)
            _verify_issue_state_write(notion, source_id, latest_page, state)
            return True
        phase = "uncertain"
    if phase in {"sent", "uncertain"}:
        if current_task == marker["target"]:
            if not _restore_projection_matches(latest_page, saved_marker):
                state["notion_write"] = dict(saved_marker)
                state["notion_write"]["phase"] = "uncertain"
                state["hold"] = _new_hold(
                    "PENDING_RESULT_UNCLEAR",
                    "Notion 복원 목표는 보이지만 함께 전송한 표시 속성을 모두 확인하지 못했습니다.",
                    _digest({"request_id": request_id,
                             "expected": saved_marker.get("expected_fingerprint")}))
                _verify_issue_state_write(notion, source_id, latest_page, state)
                return True
            state["notion_write"] = dict(saved_marker)
            state["notion_write"]["phase"] = "confirmed"
            _verify_issue_state_write(notion, source_id, latest_page, state)
            return True
        if (manual_resume and resume_actor and current_task == marker["expected_before"]):
            _persist_restore_pm_approval(notion, source_id, latest_page, state,
                                         latest_issue, latest_item, latest_refs,
                                         resume_actor, current_task, config)
            return False
        if (current_task != marker["expected_before"] and state.get("baseline")):
            _record_held_card_request(notion, source_id, latest_page, state,
                                      latest_item, latest_issue, facts, config, now)
        state["notion_write"] = dict(saved_marker)
        state["notion_write"]["phase"] = "uncertain"
        state["hold"] = _new_hold(
            "PENDING_RESULT_UNCLEAR",
            "Notion 복원 전송 결과를 확인할 수 없어 중복 쓰기를 멈췄습니다.",
            _digest({"request_id": request_id, "phase": phase,
                     "current_task": current_task}))
        _verify_issue_state_write(notion, source_id, latest_page, state)
        _display_hold(notion, source_id, latest_page, metadata, state,
                      state["hold"], now, preserve_task_status=True)
        return True

    if phase != "prepared" or current_task != marker["expected_before"]:
        state["hold"] = _new_hold(
            "REQUEST_RACE", "Notion 상태 복원 직전 새 이동이 확인되어 요청을 보존했습니다.",
            _digest({"request_id": request_id, "current_task": current_task}))
        if (current_task not in {marker.get("target"), marker.get("expected_before")} and
                state.get("baseline")):
            _record_held_card_request(
                notion, source_id, latest_page, state, latest_item, latest_issue,
                facts, config, now)
        _verify_issue_state_write(notion, source_id, latest_page, state)
        _display_hold(notion, source_id, latest_page, metadata, state,
                      state["hold"], now, preserve_task_status=True)
        return True

    plan = _plan_issue(latest_issue, latest_item, latest_page, state, facts, cutoff,
                       config["project_id"], config["status_options"])
    if plan.get("hold") or plan.get("target") != marker["target"]:
        state["hold"] = _new_hold(
            "GITHUB_FACTS_CHANGED", "현재 GitHub 사실로 복원 목표를 확인할 수 없어 보류했습니다.",
            _digest({"request_id": request_id, "target": plan.get("target")}))
        _verify_issue_state_write(notion, source_id, latest_page, state)
        _display_hold(notion, source_id, latest_page, metadata, state,
                      state["hold"], now, preserve_task_status=True)
        return True

    state["notion_write"] = dict(saved_marker)
    state["notion_write"]["phase"] = "sent"
    sent_properties = _projection_properties(
        metadata, target=plan.get("target"), end_reason=plan.get("end_reason"),
        representative=plan.get("representative"),
        confirmation=plan.get("confirmation", ""), state=state,
        now=now, kind="Issue")
    fields, fingerprint = _restore_projection_checkpoint(sent_properties)
    state["notion_write"]["expected_fields"] = fields
    state["notion_write"]["expected_fingerprint"] = fingerprint
    task_before_marker = current_task
    _verify_issue_state_write(notion, source_id, latest_page, state)
    if (read_select(latest_page, "작업 상태") != task_before_marker or
            not _issue_state_binding_matches(latest_page, state)):
        _persist_observed_card_race(
            notion, source_id, latest_page, metadata, state, item=latest_item,
            issue=latest_issue, facts=facts, config=config, now=now,
            message="상태 복원 checkpoint 확인 중 Notion에 새 이동이 있어 복원 쓰기를 멈추고 보류했습니다.")
        return True
    _update_row(notion, source_id, latest_page, metadata, plan, state, now, kind="Issue")
    state["notion_write"]["phase"] = "confirmed"
    _verify_issue_state_write(notion, source_id, latest_page, state)
    return True


def _resolve_pending_add(notion, github, source_id, row, state, project, facts, issue):
    item = project["items"].get(issue["databaseId"])
    pending = state["pending"]
    require(pending and pending["kind"] == "add", "Project add pending 복구 종류 오류")
    fingerprint = _source_fingerprint(issue, _linked_pr_facts(issue, facts)[0], None)
    if item is None:
        return None, _new_hold("PROJECT_ADD_UNCERTAIN", "Project 추가 결과를 확인할 수 없습니다.",
                               fingerprint)
    require(item["content_id"] == issue["id"], "pending Project item content ID 불일치")
    if not _confirmed_add_item_matches(pending, item):
        return None, _new_hold(
            "PROJECT_ADD_UNCERTAIN",
            "확정된 Project add checkpoint와 현재 항목 ID가 다릅니다. 재바인딩하지 않았습니다.",
            fingerprint)
    latest = gp.fetch_issue_detail(github, issue["id"])
    latest_refs, _ = _linked_pr_facts(latest, facts, allow_snapshot_drift=True)
    latest_fingerprint = _pending_fingerprint(latest, latest_refs, None, pending)
    if latest_fingerprint != pending.get("semantic_fingerprint", pending["facts_fingerprint"]):
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
    current_fingerprint = _pending_fingerprint(issue, linked, before_item, pending)

    # A difference between the initial full snapshot and this per-issue reread is a
    # current-run race. Keep it issue-local and never authorize it as a historical delta.
    if snapshot_issue is not None:
        snapshot_linked, _ = _linked_pr_facts(snapshot_issue, facts)
        snapshot_fingerprint = _pending_fingerprint(snapshot_issue, snapshot_linked,
                                                    before_item, pending)
        if current_fingerprint != snapshot_fingerprint:
            resolved_state["pending"] = None
            return {"mode": "hold", "state": resolved_state,
                    "hold": _new_hold(
                        "SOURCE_CHANGED_BEFORE_WRITE",
                        "전체 조회 뒤 GitHub 원본이 달라져 이 이슈의 복구를 보류했습니다.",
                        _source_fingerprint(issue, linked, item))}

    facts_match = current_fingerprint == pending.get("semantic_fingerprint",
                                                        pending["facts_fingerprint"])
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
        if set(state["resume"]) == RESUME_KEYS:
            state["resume"]["expected_fingerprint"] = _status_operation_fingerprint(
                latest, linked, item)
        else:
            state["resume"]["expected_fingerprint"] = expected_fingerprint
    state["pending"]["confirmed"] = True
    if state.get("request") and state["request"].get("phase") == "sent":
        state["request"]["phase"] = "confirmed"
        if state.get("notion_write"):
            state["notion_write"]["phase"] = "confirmed"
    _verify_issue_state_write(notion, source_id, row, state)
    _apply_status_checkpoint(state, checkpoint)
    active_request = state.get("request") or {}
    expected_notion_status = active_request.get("target")
    if (active_request.get("phase") == "confirmed" and
            expected_notion_status in OPTIONS["작업 상태"]):
        state["projection"] = {
            "expected_option_id": item["status_option_id"],
            "expected_fingerprint": _status_operation_fingerprint(latest, linked, item),
            "checkpoint": checkpoint,
            "fact_contract": "semantic_v2",
            "request_id": (state.get("request") or {}).get("id"),
            "expected_notion_status": pending.get("notion_status"),
            "result_notion_status": expected_notion_status,
        }
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
               "checkpoint": {"content_id": issue["id"]},
               "fact_contract": "semantic_v2",
               "semantic_fingerprint": _pending_semantic_fingerprint(
                   issue, _linked_pr_facts(issue, facts)[0], None, "add")}
    state["pending"] = pending
    _verify_issue_state_write(notion, source_id, row, state)

    latest = gp.fetch_issue_detail(github, issue["id"])
    latest_refs, _ = _linked_pr_facts(latest, facts, allow_snapshot_drift=True)
    latest_fp = _pending_fingerprint(latest, latest_refs, None, state["pending"])
    if latest_fp != state["pending"].get("semantic_fingerprint", fingerprint):
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
        returned_item_id = gp.add_project_issue(project_client, config["project_id"], issue["id"])
    except gp.SyncError:
        # No returned ID means automatic readback cannot be safely targeted.
        raise SyncError("Project 추가 결과 불명; pending을 유지하고 수동 확인 필요") from None
    state["readback"] = {"returned_item_id": returned_item_id, "validated": False,
                         "attempts": 0, "reservation": None, "last_result": None}
    _verify_issue_state_write(notion, source_id, row, state)
    prior_project = project
    readback_project, item, readback_hold = _read_added_project_item_immediate(
        notion, source_id, row, state, project_client, config, facts, issue)
    project = readback_project if readback_project is not None else prior_project
    if readback_hold:
        return project, item, readback_hold
    if item is None:
        return project, None, None
    item, pending_hold = _resolve_pending_add(
        notion, github, source_id, row, state, project, facts, issue)
    return project, item, pending_hold


def _apply_project_status(notion, github, project_client, source_id, row, state, project,
                          facts, issue, item, plan, config, observed_at):
    target_id = _status_option(project, plan["target"])
    before = item["status_option_id"]
    notion_status = read_select(row, "작업 상태")
    semantic_fingerprint = _pending_semantic_fingerprint(
        issue, _linked_pr_facts(issue, facts)[0], item, "status")
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
                              "review_pr_hash": plan["state"]["review_pr_hash"]},
               "fact_contract": "semantic_v2",
               "semantic_fingerprint": semantic_fingerprint,
               "before_status_stamp": _status_stamp(item, config),
               "request_id": ((state.get("request") or {}).get("id")
                              if (state.get("request") or {}).get("phase") in
                              {"prepared", "sent", "uncertain", "confirmed"} else None)}
    state["pending"] = pending
    _verify_issue_state_write(notion, source_id, row, state)

    latest = gp.fetch_issue_detail(github, issue["id"])
    latest_item = gp.fetch_project_item(project_client, config["project_id"], item["id"],
                                        config["status_field_id"], allow_archived=True)
    latest_refs, _ = _linked_pr_facts(latest, facts, allow_snapshot_drift=True)
    relation_ok = (latest_item.get("id") == item.get("id") and
                   latest_item.get("content_id") == issue.get("id") and
                   latest.get("id") == issue.get("id") and
                   latest.get("databaseId") == issue.get("databaseId") and
                   latest_item.get("status_field_id") == config["status_field_id"])
    if not relation_ok:
        state["pending"] = None
        hold = _new_hold(
            "PROJECT_ITEM_RACE",
            "Project 쓰기 직전 항목의 Issue 연결 또는 Status 대상이 달라 자동 변경을 멈췄습니다.",
            _digest({"expected_issue": issue.get("id"),
                     "observed_issue": latest_item.get("content_id"),
                     "expected_item": item.get("id"),
                     "observed_item": latest_item.get("id")}))
        updated_project = dict(project)
        updated_items = dict(project["items"])
        updated_items[issue["databaseId"]] = latest_item
        updated_project["items"] = updated_items
        return updated_project, latest_item, hold
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
    latest_fingerprint = _pending_fingerprint(latest, latest_refs, latest_item, pending)
    if latest_fingerprint != pending["semantic_fingerprint"]:
        state["pending"] = None
        hold = _new_hold("SOURCE_CHANGED_BEFORE_WRITE",
                         "Project 변경 직전 GitHub 또는 Project 값이 달라져 자동 변경을 멈췄습니다.",
                         latest_fingerprint)
        updated_project = dict(project)
        updated_items = dict(project["items"])
        updated_items[issue["databaseId"]] = latest_item
        updated_project["items"] = updated_items
        return updated_project, latest_item, hold
    if (_status_stamp(latest_item, config) != pending["before_status_stamp"]):
        state["pending"] = None
        hold = _new_hold(
            "SOURCE_CHANGED_BEFORE_WRITE",
            "쓰기 직전 Project Status 값 객체가 바뀌어 순서를 확인할 수 없습니다.",
            _projection_fingerprint({}, latest, latest_refs, latest_item))
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

    approval = state.get("resume")
    if approval and set(approval) == RESUME_KEYS and approval.get("display_pending"):
        latest_page = notion.request("GET", f"/pages/{identifier(row.get('id'))}")
        bound_page(latest_page, source_id)
        latest_state = decode_internal(
            read_text(latest_page, "동기화 내부 상태"), kind="issue",
            project_id=config["project_id"], object_id=issue["databaseId"])
        saved_approval = latest_state.get("resume") if latest_state else None
        current_refs, _ = _linked_pr_facts(latest, facts, allow_snapshot_drift=True)
        if (approval.get("request_id") != (state.get("request") or {}).get("id") or
                read_select(latest_page, "작업 상태") != approval["approved_notion_status"] or
                latest_item.get("status_option_id") != approval["approved_option_id"] or
                _status_facts_fingerprint(latest, current_refs) !=
                approval["facts_fingerprint"] or
                not isinstance(saved_approval, dict) or set(saved_approval) != RESUME_KEYS or
                any(saved_approval.get(key) != approval.get(key) for key in
                    ("run_id", "request_id", "approved_option_id",
                     "approved_notion_status", "facts_fingerprint"))):
            state["pending"] = None
            state["hold"] = _new_hold(
                "REQUEST_RACE",
                "PM 승인 도중 Notion 요청, Project 상태 또는 GitHub 사실이 달라져 승인을 보존했습니다.",
                _source_fingerprint(latest, current_refs, latest_item))
            _verify_issue_state_write(notion, source_id, latest_page, state)
            return project, latest_item, state["hold"]

    if state.get("request") and state["request"].get("phase") == "prepared":
        latest_page = notion.request("GET", f"/pages/{identifier(row.get('id'))}")
        bound_page(latest_page, source_id)
        latest_state = decode_internal(read_text(latest_page, "동기화 내부 상태"),
                                       kind="issue", project_id=config["project_id"],
                                       object_id=issue["databaseId"])
        latest_task = read_select(latest_page, "작업 상태")
        approval = state.get("resume") or {}
        approved_card_echo = (
            approval.get("request_id") == state["request"].get("id") and
            approval.get("approved_notion_status") == latest_task and
            latest_task == (state.get("notion_write") or {}).get("expected_before") and
            approval.get("approved_option_id") == latest_item.get("status_option_id"))
        if ((latest_task != state["request"]["target"] and not approved_card_echo) or
                latest_state is None or
                (latest_state.get("request") or {}).get("id") != state["request"]["id"]):
            state["pending"] = None
            state["request"]["phase"] = "held"
            state["request"]["reason"] = (
                "GitHub 쓰기 직전 Notion 요청 상태가 달라져 새 요청을 다시 확인해야 합니다.")
            hold = _new_hold("REQUEST_RACE", state["request"]["reason"], plan["fingerprint"])
            state["hold"] = hold
            _record_held_card_request(
                notion, source_id, latest_page, state, latest_item, latest, facts,
                config, observed_at)
            _verify_issue_state_write(notion, source_id, row, state)
            _save_status_request(notion, source_id, row, state)
            return project, latest_item, hold
        state["request"]["phase"] = "sent"
        state["notion_write"] = {"request_id": state["request"]["id"],
                                 "expected_before": notion_status, "target": plan["target"],
                                 "kind": "request", "phase": "sent"}
        sent_properties = _status_request_properties(state)
        if observed_at is not None:
            sent_properties["동기화 시각"] = {
                "date": {"start": _minute_iso(observed_at)}}
        _patch_page(notion, source_id, row, sent_properties)

    if target_id != before:
        _verify_project_status_page_before_mutation(notion, source_id, row)
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
    if state.get("request") and state["request"].get("phase") == "sent":
        state["request"]["phase"] = "confirmed"
        state["notion_write"]["phase"] = "confirmed"
    resume_checkpoint = None
    if state["resume"] and state["resume"]["display_pending"]:
        resume_checkpoint = {
            "expected_option_id": verified["status_option_id"],
            "expected_fingerprint": _status_operation_fingerprint(latest, latest_refs, verified),
        }
        state["resume"].update(resume_checkpoint)
    _verify_issue_state_write(notion, source_id, row, state)
    saved_request = state.get("request")
    saved_notion_write = state.get("notion_write")
    saved_baseline = state.get("baseline")
    state.update(plan["state"])
    state["request"] = saved_request
    state["notion_write"] = saved_notion_write
    state["baseline"] = saved_baseline
    _apply_status_checkpoint(state, pending["checkpoint"])
    state["pending"] = None
    state["project_item_id"] = verified["id"]
    projection_fingerprint = _status_operation_fingerprint(latest, latest_refs, verified)
    state["projection"] = {
        "expected_option_id": target_id,
        "expected_fingerprint": projection_fingerprint,
        "checkpoint": pending["checkpoint"],
        "fact_contract": "semantic_v2",
        "request_id": (state.get("request") or {}).get("id"),
        "expected_notion_status": pending.get("notion_status"),
        "result_notion_status": plan.get("target"),
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


def _persist_observed_card_race(notion, source_id, row, metadata, state, *,
                                item, issue, facts, config, now, message):
    """Keep an already-observed Notion move separate from the operation in flight."""
    hold = _new_hold(
        "REQUEST_RACE", message,
        _projection_fingerprint(state.get("projection") or {}, issue,
                                _linked_pr_facts(issue, facts, allow_snapshot_drift=True)[0],
                                item))
    state["hold"] = hold
    _record_held_card_request(notion, source_id, row, state, item, issue,
                              facts, config, now)
    _verify_issue_state_write(notion, source_id, row, state)
    _save_status_request(notion, source_id, row, state)
    _display_hold(notion, source_id, row, metadata, state, hold, now,
                  preserve_task_status=True)
    return False


def _write_checkpointed_projection(notion, source_id, row, metadata, plan, state, now,
                                   *, item=None, issue=None, facts=None, config=None,
                                   preserve_task_status=None):
    projection = state.get("projection")
    require(isinstance(projection, dict), "Status display projection checkpoint 누락")
    properties = _projection_properties(
        metadata, target=plan.get("target"), end_reason=plan.get("end_reason"),
        representative=plan.get("representative"),
        confirmation=plan.get("confirmation", ""), state=state, now=now, kind="Issue")
    if preserve_task_status is not None:
        properties["작업 상태"] = {"select": {"name": preserve_task_status}}
    if projection.get("fact_contract") != "semantic_v2":
        # The original v1 projection has only its raw source fingerprint and
        # checkpoint. Do not manufacture semantic fields or re-sign that record.
        # If the exact display already landed before a crash, finish the durable
        # exchange without replaying its user-facing PATCH.
        if _projection_display_matches_except_sync_clock(row, properties):
            return True
        task = read_select(row, "작업 상태")
        baseline = state.get("baseline")
        if (baseline is not None and
                task not in {baseline.get("notion_status"), plan.get("target")}):
            require(item is not None and issue is not None and facts is not None and
                    config is not None,
                    "새 Notion 이동 보존 문맥 누락")
            return _persist_observed_card_race(
                notion, source_id, row, metadata, state, item=item, issue=issue,
                facts=facts, config=config, now=now,
                message="legacy 표시 복구 중 Notion에 새 상태 이동이 있어 이전 표시를 멈추고 보류했습니다.")
        _patch_page(notion, source_id, row, properties)
        return True
    properties = _projection_properties(
        metadata, target=plan.get("target"), end_reason=plan.get("end_reason"),
        representative=plan.get("representative"),
        confirmation=plan.get("confirmation", ""), state=state, now=now, kind="Issue")
    if preserve_task_status is not None:
        properties["작업 상태"] = {"select": {"name": preserve_task_status}}
    fields, fingerprint = _restore_projection_checkpoint(properties)
    display_checkpoint = projection.get("display_checkpoint") or {}
    if (display_checkpoint.get("phase") == "display_sent" and
            _projection_display_matches(row, projection) and
            _issue_state_binding_matches(row, state)):
        # The complete user-facing projection was already read back. Replaying
        # an identical PATCH creates a needless race window during restart.
        return True
    task_before_checkpoint = read_select(row, "작업 상태")
    projection["display_checkpoint"] = {
        "phase": "display_sent", "expected_fields": fields,
        "expected_fingerprint": fingerprint}
    _verify_issue_state_write(notion, source_id, row, state)
    # A fresh GET is an observation boundary: do not send the older projection
    # when the card has already moved, including a move back to the baseline.
    if (read_select(row, "작업 상태") != task_before_checkpoint or
            not _issue_state_binding_matches(row, state)):
        require(item is not None and issue is not None and facts is not None and
                config is not None, "새 Notion 이동 보존 문맥 누락")
        return _persist_observed_card_race(
            notion, source_id, row, metadata, state, item=item, issue=issue,
            facts=facts, config=config, now=now,
            message="표시 checkpoint 확인 중 Notion에 새 상태 이동이 있어 이전 표시를 멈추고 보류했습니다.")
    # The user-facing write must carry the already-durable marker too.  The
    # internal property is excluded from the display fingerprint, so rebuilding
    # this payload preserves the exact readback contract.
    properties = _projection_properties(
        metadata, target=plan.get("target"), end_reason=plan.get("end_reason"),
        representative=plan.get("representative"),
        confirmation=plan.get("confirmation", ""), state=state, now=now, kind="Issue")
    _patch_page(notion, source_id, row, properties)
    return True


def _completed_projection_state(state, *, issue, item, linked_refs, config,
                                notion_status, observed_at, approved_resume=False,
                                preserve_task_status=None):
    completed = json.loads(json.dumps(state))
    completed["project_item_id"] = item["id"]
    deferred = state.get("deferred_request")
    request = state.get("request") or {}
    unresolved_deferred = bool(
        deferred and deferred.get("phase") not in {"rejected", "completed"} and
        deferred.get("id") != request.get("id"))
    if (unresolved_deferred and preserve_task_status is not None and
            preserve_task_status != notion_status):
        # A settled older operation may finish while the page already shows a
        # separate deferred request. Its result is not a joint Project/Notion
        # observation, so keep the previous baseline until both sides converge.
        completed["baseline"] = json.loads(json.dumps(state.get("baseline")))
    else:
        completed["baseline"] = _new_status_baseline(
            notion_status, item, issue, linked_refs, config, observed_at)
    completed["projection"] = None
    completed["pending"] = None
    completed["hold"] = None
    completed["resume"] = None
    request = completed.get("request")
    if request and (request.get("phase") == "confirmed" or
                    approved_resume and request.get("phase") in {"held", "rejected"}):
        if approved_resume and request.get("target") != notion_status:
            request["phase"] = "rejected"
            request["reason"] = (
                "PM 재개에서 확인된 현재 GitHub 사실이 기존 요청과 달라 요청값은 반영하지 않았습니다.")
        else:
            request["phase"] = "completed"
            request["reason"] = "Project 상태와 Notion 표시를 확인했습니다."
        completed["notion_write"] = None
    marker = completed.get("notion_write") or {}
    if (request and request.get("phase") == "rejected" and
            marker.get("kind") == "restore" and marker.get("phase") == "confirmed" and
            marker.get("request_id") == request.get("id") and
            marker.get("target") == notion_status and
            marker.get("project_item_id") == item.get("id") and
            marker.get("project_stamp") == _status_stamp(item, config) and
            marker.get("facts_fingerprint") ==
            _status_facts_fingerprint(issue, linked_refs)):
        # A PM rejection is terminal only after the exact confirmed restore is
        # promoted against its current card, item, Project stamp and facts.
        completed["notion_write"] = None
    deferred = completed.get("deferred_request")
    if deferred and deferred.get("phase") not in {"rejected", "completed"}:
        deferred["phase"] = "held"
        deferred["reason"] = (
            "이전 요청 결과를 확정했습니다. 후속 요청은 PM이 현재 GitHub 상태를 확인한 뒤 처리해야 합니다.")
        completed["hold"] = _new_hold(
            "DEFERRED_REQUEST_PENDING", deferred["reason"],
            _digest({"request_id": deferred["id"], "target": deferred["target"],
                     "facts": _status_facts_fingerprint(issue, linked_refs)}))
    return completed


def _terminal_restore_binding_matches(state, *, page, item, config, notion_status):
    request = state.get("request") or {}
    marker = state.get("notion_write") or {}
    return bool(
        request.get("phase") == "rejected" and
        marker.get("kind") == "restore" and marker.get("phase") == "confirmed" and
        marker.get("request_id") == request.get("id") and
        marker.get("target") == notion_status and
        marker.get("project_item_id") == item.get("id") and
        marker.get("project_stamp") == _status_stamp(item, config) and
        _restore_projection_matches(page, marker))


def _terminal_restore_marker_matches(state, *, page, issue, item, linked_refs, config,
                                     notion_status):
    return bool(
        _terminal_restore_binding_matches(
            state, page=page, item=item, config=config, notion_status=notion_status) and
        state["notion_write"].get("facts_fingerprint") ==
        _status_facts_fingerprint(issue, linked_refs))


def _closed_facts_supersede_restore(state, *, page, issue, item, linked_refs, config):
    """Allow a new CLOSED operation, never promote a restore under changed facts."""
    if (not config.get("bidirectional_enabled") or item is None or
            not state.get("baseline") or state.get("pending") or state.get("projection") or
            state.get("deferred_request") is not None or state.get("resume") or
            (state.get("hold") or {}).get("code") not in {None, "STATUS_REQUEST_INVALID"} or
            issue.get("state") != "CLOSED" or issue.get("stateReason") != "COMPLETED"):
        return False
    notion_status = read_select(page, "작업 상태")
    project_status = {value: name for name, value in config["status_options"].items()}.get(
        item.get("status_option_id"))
    return bool(
        project_status == notion_status and
        _terminal_restore_binding_matches(
            state, page=page, item=item, config=config, notion_status=notion_status) and
        state["notion_write"].get("facts_fingerprint") !=
        _status_facts_fingerprint(issue, linked_refs))


def _promote_held_terminal_restore(notion, source_id, row, state, *, issue, item,
                                   linked_refs, config, observed_at, facts,
                                   metadata, now):
    """Resolve an exact rejected-request restore that an older hold was masking."""
    hold = state.get("hold") or {}
    if item is None:
        return False
    project_status = {value: name for name, value in config["status_options"].items()}.get(
        item.get("status_option_id"))
    notion_status = read_select(row, "작업 상태")
    if (not config.get("bidirectional_enabled") or hold.get("code") !=
            "STATUS_REQUEST_INVALID" or state.get("deferred_request") is not None or
            project_status != notion_status or
            not _terminal_restore_marker_matches(
                state, page=row, issue=issue, item=item, linked_refs=linked_refs,
                config=config, notion_status=notion_status)):
        return False
    marker = json.loads(json.dumps(state["notion_write"]))
    previous = json.loads(json.dumps(state))
    state["baseline"] = _new_status_baseline(
        notion_status, item, issue, linked_refs, config, observed_at)
    state["notion_write"] = None
    state["hold"] = None
    _verify_issue_state_write(notion, source_id, row, state)
    if (read_select(row, "작업 상태") != notion_status or
            not _restore_projection_matches(row, marker) or
            not _issue_state_binding_matches(row, state)):
        state.clear()
        state.update(previous)
        state["hold"] = _new_hold(
            "REQUEST_RACE",
            "확정된 복원 표시 정리 중 Notion 요청 또는 카드가 달라 결과 승격을 보류했습니다.",
            _digest({"request_id": (state.get("request") or {}).get("id"),
                     "observed_status": read_select(row, "작업 상태")}))
        _persist_observed_card_race(
            notion, source_id, row, metadata, state, item=item, issue=issue,
            facts=facts, config=config, now=now, message=state["hold"]["message"])
    return True


def _finalize_bidirectional_projection(notion, source_id, row, state, *,
                                       issue, item, linked_refs, config,
                                       notion_status, observed_at, approved_resume=False,
                                       facts=None, metadata=None, now=None,
                                       preserve_task_status=None):
    """Mark completion durable only after every terminal display property reads back."""
    completed = _completed_projection_state(
        state, issue=issue, item=item, linked_refs=linked_refs, config=config,
        notion_status=notion_status, observed_at=observed_at,
        approved_resume=approved_resume, preserve_task_status=preserve_task_status)

    # Keep the old baseline and confirmed request in the durable stage. The
    # terminal UI payload is prepared separately and completion is promoted
    # only after its full readback succeeds.
    require(isinstance(state.get("projection"), dict),
            "Status completion projection checkpoint 누락")
    terminal_display = _status_request_properties(completed)
    terminal_task_status = (preserve_task_status if preserve_task_status is not None
                            else notion_status)
    terminal_display["작업 상태"] = {
        "select": {"name": terminal_task_status}
        if terminal_task_status is not None else None}
    fields, fingerprint = _restore_projection_checkpoint(terminal_display)
    task_before_checkpoint = read_select(row, "작업 상태")
    state["projection"]["display_checkpoint"] = {
        "phase": "completion_pending", "expected_fields": fields,
        "expected_fingerprint": fingerprint}
    _verify_issue_state_write(notion, source_id, row, state)

    # The checkpoint write itself performs a fresh GET. A card move already
    # visible there belongs to a separate request and must stop completion.
    if (read_select(row, "작업 상태") != task_before_checkpoint or
            not _issue_state_binding_matches(row, state)):
        require(isinstance(facts, dict) and isinstance(metadata, dict) and now is not None,
                "완료 중 새 Notion 이동을 보존할 동기화 문맥이 없습니다")
        return _persist_observed_card_race(
            notion, source_id, row, metadata, state, item=item, issue=issue,
            facts=facts, config=config, now=now,
            message="완료 checkpoint 확인 중 Notion에 새 상태 이동이 있어 기존 결과와 분리해 보류합니다.")

    terminal_display["동기화 내부 상태"] = text_property(canonical_json(state))
    page_id = identifier(row.get("id"))
    notion.request("PATCH", f"/pages/{page_id}",
                   {"properties": terminal_display}, write=True)
    confirmed = notion.request("GET", f"/pages/{page_id}")
    bound_page(confirmed, source_id)
    require(identifier(confirmed.get("id")) == page_id and not is_archived(confirmed),
            "Notion 변경 대상 페이지 readback 오류")
    row.clear()
    row.update(confirmed)
    approved_card_status = (state.get("resume") or {}).get("approved_notion_status")
    approved_card_echo = (approved_resume and approved_card_status is not None and
                          read_select(row, "작업 상태") == approved_card_status)
    preserved_deferred_echo = (
        preserve_task_status is not None and
        read_select(row, "작업 상태") == preserve_task_status and
        _deferred_card_matches(state, state.get("projection"), preserve_task_status))
    if (read_select(row, "작업 상태") != notion_status and not approved_card_echo and
            not preserved_deferred_echo):
        require(isinstance(facts, dict) and isinstance(metadata, dict) and now is not None,
                "완료 readback 중 새 Notion 이동을 보존할 동기화 문맥이 없습니다")
        hold = _new_hold(
            "REQUEST_RACE",
            "완료 표시 readback 중 Notion에 새 상태 이동이 있어 기존 결과와 분리해 보류합니다.",
            _projection_fingerprint(state["projection"], issue, linked_refs, item))
        state["hold"] = hold
        _record_held_card_request(
            notion, source_id, row, state, item, issue, facts, config, observed_at)
        _verify_issue_state_write(notion, source_id, row, state)
        _save_status_request(notion, source_id, row, state)
        _display_hold(notion, source_id, row, metadata, state, hold, now,
                      preserve_task_status=True)
        return False
    _verify_properties(row, terminal_display)
    terminal_state = decode_internal(
        read_text(row, "동기화 내부 상태"), kind="issue",
        project_id=state["project_id"], object_id=state["issue_id"])
    require(terminal_state.get("projection") == state.get("projection") and
            (terminal_state.get("request") or {}).get("id") ==
            (state.get("request") or {}).get("id"),
            "완료 readback의 projection/request 결속 불일치")
    inflight_state = json.loads(json.dumps(state))
    task_before_promotion = read_select(row, "작업 상태")
    state.update(completed)
    _verify_issue_state_write(notion, source_id, row, state)
    promotion_mismatch = {
        "task_changed": read_select(row, "작업 상태") != task_before_promotion,
        "state_binding_changed": not _issue_state_binding_matches(row, state),
    }
    if any(promotion_mismatch.values()):
        # The completed marker may already have reached Notion. Roll it back
        # durably before returning so restart cannot mistake it for success.
        state.clear()
        state.update(inflight_state)
        return _persist_observed_card_race(
            notion, source_id, row, metadata, state, item=item, issue=issue,
            facts=facts, config=config, now=now,
            message="완료 승격 readback에서 카드 또는 요청 결속이 달라 완료를 되돌리고 보류했습니다.")
    return True


def _update_control_summary(notion, source_id, control, control_state, issue_rows,
                            *, now, run_id, counts):
    holds = []
    for row, state in issue_rows:
        awaiting_add_readback = bool(
            state and state.get("pending") and state["pending"].get("kind") == "add" and
            state.get("readback") and state["readback"].get("returned_item_id") and
            not state["readback"].get("validated"))
        awaiting_deferred_request = bool(
            state and state.get("deferred_request") and
            state["deferred_request"].get("phase") in {"waiting", "held"})
        if state and (state.get("hold") or state.get("projection") or
                      (state.get("resume") or {}).get("display_pending") or
                      awaiting_add_readback or awaiting_deferred_request):
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
        props["동기화 시각"] = {"date": {"start": _minute_iso(now)}}
    _patch_page(notion, source_id, control, props)
    if success:
        actual = _display_date_instant(control, "동기화 시각")
        expected = _expected_display_date_instant(
            "동기화 시각", {"start": _minute_iso(now)})
        require(actual == expected, "전체 성공 시각 readback 오류")
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
                readback = state.get("readback") or {}
                auto_readback = bool(
                    state["hold"] is None and readback.get("returned_item_id") and
                    (readback.get("validated") or
                     readback.get("attempts", 0) < PROJECT_ADD_READBACK_MAX_ATTEMPTS))
                if pending["confirmed"] and not _confirmed_add_item_matches(pending, item):
                    result["hold"] = {"code": "PROJECT_ADD_UNCERTAIN",
                                       "message": "저장된 Project add checkpoint와 현재 항목 ID가 다릅니다."}
                    plans.append(result)
                    continue
                linked, _ = _linked_pr_facts(issue, facts)
                current_source_fingerprint = _pending_fingerprint(issue, linked, None, pending)
                expected_pending_fingerprint = pending.get("semantic_fingerprint",
                                                          pending["facts_fingerprint"])
                if ((number not in resume_numbers and not auto_readback) or
                        current_source_fingerprint != expected_pending_fingerprint):
                    result["hold"] = {"code": "PROJECT_ADD_UNCERTAIN",
                                      "message": "PM 확인 전에는 이전 Project 추가 결과를 재사용할 수 없습니다."}
                    plans.append(result)
                    continue
                if auto_readback and item is None:
                    result["hold"] = {"code": "PROJECT_ADD_UNCERTAIN",
                                      "message": "Project 추가 반환 ID를 확인했지만 목록에는 아직 나타나지 않았습니다."}
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
                        if set(state["resume"]) == RESUME_KEYS:
                            state["resume"]["expected_fingerprint"] = _status_operation_fingerprint(
                                issue, linked, item)
                        else:
                            state["resume"]["expected_fingerprint"] = _source_fingerprint(
                                issue, linked, item)
                    state["pending"] = None
        resume_candidate = _resume_matches(
            state["resume"], issue, facts, item,
            request_id=(state.get("request") or {}).get("id"),
            notion_status=read_select(notion_row, "작업 상태"))
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
         diagnose_date_readback=False, resolve_issue_numbers="", env=None, now=None,
         notification_result=None):
    """Run one complete local/API sync; all global reads finish before the first write."""
    require(not diagnose_date_readback or dry_run,
            "--diagnose-date-readback requires --dry-run")
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
                pending_facts_match = (_pending_fingerprint(
                    issue, pending_refs, {"id": pending["item_id"],
                                          "status_option_id": pending["before_option_id"]},
                    pending) == pending.get("semantic_fingerprint",
                                             pending["facts_fingerprint"]))
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
                        if candidate.get("hold") or not isinstance(candidate.get("state"), dict):
                            return False
                        same_checkpoint = all(
                            candidate["state"][field] == checkpoint[field]
                            for field in STATUS_CHECKPOINT_KEYS)
                        canonical_target = (
                            config["status_options"].get(candidate.get("target")) ==
                            pending["target_option_id"])
                        request = state.get("request") or {}
                        notion_write = state.get("notion_write") or {}
                        linked_request_target = (
                            request.get("phase") in {"prepared", "sent", "uncertain", "confirmed"} and
                            ("request_id" not in pending or
                             request.get("id") == pending.get("request_id")) and
                            request.get("target") == pending.get("notion_status") and
                            config["status_options"].get(request.get("target")) ==
                            pending["target_option_id"] and
                            notion_write.get("request_id") == request.get("id") and
                            notion_write.get("target") == request.get("target") and
                            notion_write.get("kind") == "request" and
                            notion_write.get("phase") == request.get("phase"))
                        return (not candidate.get("hold") and
                                candidate.get("event_id") == pending_event and
                                same_checkpoint and (canonical_target or linked_request_target))
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
    resume_numbers, resume_actor = _validate_manual_resume(
        github, resolve_issue_numbers, env, dry_run=dry_run)
    if now is None:
        now = _iso(datetime.now(timezone.utc))
    else:
        now = _iso(timestamp(now))
    run_id = env.get("GITHUB_RUN_ID") or "local"
    require(isinstance(run_id, str) and len(run_id) <= 128, "workflow run ID 형식 오류")
    readback_identity = {"run_id": run_id if run_id != "local" else str(uuid4()),
                         "run_attempt": env.get("GITHUB_RUN_ATTEMPT", "1")}
    require(isinstance(readback_identity["run_attempt"], str) and
            re.fullmatch(r"[1-9][0-9]*", readback_identity["run_attempt"]) is not None,
            "Project add readback 실행 시도 형식 오류")
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

    observation_mark = None
    status_observed_at, status_observation_uncertainty_seconds = None, None
    if not dry_run:
        observation_mark = observation_clock.mark_snapshot()
        status_observed_at, status_observation_uncertainty_seconds = observation_clock.verify_snapshot(
            observation_mark, lambda: observation_clock.fetch_fresh_date(github))

    if dry_run:
        counts["issue_plans"] = _preview_issue_plans(notion, source_id, source, index, project, facts,
                                                       config, cutoff, resume_numbers)
        if diagnose_date_readback:
            counts["date_readback_diagnostic"] = _diagnose_date_readback(source, index, now)
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
                _patch_page(notion, source_id, row, {**metadata,
                    "동기화 시각": {"date": {"start": _minute_iso(now)}}})
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
            if (config.get("bidirectional_enabled") and
                    _card_move_needs_preservation(state, row)):
                _record_held_card_request(notion, source_id, row, state, item, row_data,
                                          facts, config, status_observed_at)
            _display_hold(notion, source_id, row, metadata, state, hold, now,
                          preserve_task_status=True)
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue

        if state["project_item_id"] is not None and (item is None or
                state["project_item_id"] != item["id"]):
            code = "PROJECT_ITEM_MISSING" if item is None else "PROJECT_ITEM_ID_CHANGED"
            fingerprint = _source_fingerprint(row_data, _linked_pr_facts(row_data, facts)[0], item)
            hold = _new_hold(code, "기존 Project item 고정 식별자를 확인할 수 없습니다.", fingerprint)
            if (config.get("bidirectional_enabled") and
                    _card_move_needs_preservation(state, row)):
                _record_held_card_request(notion, source_id, row, state, item, row_data,
                                          facts, config, status_observed_at)
            _display_hold(notion, source_id, row, metadata, state, hold, now,
                          preserve_task_status=True)
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue

        issue_number = row_data["number"]
        restore_marker = state.get("notion_write") or {}
        if restore_marker.get("kind") == "restore":
            if "project_stamp" not in restore_marker:
                if (issue_number in resume_numbers and resume_actor and
                        read_select(row, "작업 상태") == restore_marker.get("expected_before") and
                        (state.get("request") or {}).get("id") ==
                        restore_marker.get("request_id")):
                    linked_for_resume, _ = _linked_pr_facts(row_data, facts)
                    _persist_restore_pm_approval(
                        notion, source_id, row, state, row_data, item,
                        linked_for_resume, resume_actor,
                        read_select(row, "작업 상태"), config)
                else:
                    legacy_restore_hold = _new_hold(
                        "PENDING_RESULT_UNCLEAR",
                        "이전 복원 checkpoint에 재검증 정보가 없어 자동 재전송을 멈췄습니다.",
                        _digest(restore_marker))
                    state["hold"] = legacy_restore_hold
                    _verify_issue_state_write(notion, source_id, row, state)
                    _display_hold(notion, source_id, row, metadata, state,
                                  legacy_restore_hold, now, preserve_task_status=True)
                    counts["held"] += 1
                    issue_rows_for_summary.append((row, state))
                    continue
            else:
                if _recover_restore_write(
                        notion, github, project_client, source_id, row, metadata, state,
                        facts, row_data, item, config, cutoff, now,
                        manual_resume=issue_number in resume_numbers,
                        resume_actor=resume_actor):
                    counts["held"] += 1
                    issue_rows_for_summary.append((row, state))
                    continue
        terminal_refs, _ = _linked_pr_facts(row_data, facts, allow_snapshot_drift=True)
        if _promote_held_terminal_restore(
                notion, source_id, row, state, issue=row_data, item=item,
                linked_refs=terminal_refs, config=config,
                observed_at=status_observed_at, facts=facts, metadata=metadata, now=now):
            if state.get("hold"):
                counts["held"] += 1
            elif not created:
                counts["updated"] += 1
            issue_rows_for_summary.append((row, state))
            continue
        closed_supersedes_restore = _closed_facts_supersede_restore(
            state, page=row, issue=row_data, item=item, linked_refs=terminal_refs,
            config=config)
        if closed_supersedes_restore:
            # Keep A and its old baseline. The restore marker is retired only with
            # the new operation's durable pending checkpoint below.
            state["hold"] = None
        if (config.get("bidirectional_enabled") and state.get("hold") and
                state["hold"]["code"] != "BIDIRECTIONAL_DISABLED" and
                issue_number not in resume_numbers and state.get("baseline") and
                not state.get("pending") and not state.get("projection") and
                _card_move_needs_preservation(state, row)):
            # Preserve a later card move verbatim until the held request follows PM recovery.
            _record_held_card_request(notion, source_id, row, state, item, row_data,
                                      facts, config, status_observed_at)
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue
        request_phase = (state.get("request") or {}).get("phase")
        if (config.get("bidirectional_enabled") and state.get("hold") and
                state["hold"]["code"] == "BIDIRECTIONAL_DISABLED"):
            state["hold"] = None
            _verify_issue_state_write(notion, source_id, row, state)
        elif (not config.get("bidirectional_enabled") and
              request_phase in {"prepared", "sent", "uncertain", "confirmed"}):
            if state.get("hold") is None:
                state["hold"] = _new_hold(
                    "BIDIRECTIONAL_DISABLED",
                    "양방향 동기화가 꺼져 있어 진행 중인 상태 요청을 보존하고 멈췄습니다.",
                    _digest({"request_id": (state.get("request") or {}).get("id"),
                             "phase": request_phase}))
                _verify_issue_state_write(notion, source_id, row, state)
                _patch_page(notion, source_id, row, {
                    "확인 필요": text_property("보류: 양방향 동기화가 꺼져 있어 요청 checkpoint를 보존했습니다.")})
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue
        projection = state["projection"]
        approving_prior_request = bool(
            issue_number in resume_numbers and projection and state.get("deferred_request") and
            state.get("hold", {}).get("code") == "REQUEST_RACE" and
            (state.get("request") or {}).get("id") == projection.get("request_id"))
        if approving_prior_request:
            linked_for_resume, _ = _linked_pr_facts(row_data, facts)
            state["resume"] = {
                "actor_id": resume_actor["actor_id"], "run_id": resume_actor["run_id"],
                "approved_option_id": item["status_option_id"],
                "fingerprint": _status_operation_fingerprint(
                    row_data, linked_for_resume, item),
                "display_pending": True, "expected_option_id": None,
                "expected_fingerprint": None,
                "request_id": (state.get("request") or {}).get("id"),
                "facts_fingerprint": _status_facts_fingerprint(
                    row_data, linked_for_resume),
                "fact_contract": "semantic_v2",
                "approved_notion_status": read_select(row, "작업 상태")}
            _verify_issue_state_write(notion, source_id, row, state)
        saved_pm_display = bool(state["resume"] and state["resume"]["display_pending"])
        if (projection is not None and
                (state["hold"] is None or issue_number in resume_numbers or saved_pm_display or
                 state["hold"]["code"] == "REQUEST_RACE")):
            latest_issue = gp.fetch_issue_detail(github, row_data["id"])
            latest_refs, _ = _linked_pr_facts(latest_issue, facts, allow_snapshot_drift=True)
            latest_item = gp.fetch_project_item(project_client, config["project_id"], item["id"],
                                                config["status_field_id"], allow_archived=True)
            fresh_fingerprint = _projection_fingerprint(
                projection, latest_issue, latest_refs, latest_item)
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
                    "Project 상태 stamp 또는 원본 사실이 checkpoint 이후 달라졌습니다. PM 확인이 필요합니다.",
                    fresh_fingerprint)
            elif projection.get("fact_contract") == "semantic_v2":
                display_checkpoint = projection.get("display_checkpoint") or {}
                current_task = read_select(row, "작업 상태")
                own_display_readback = (
                    display_checkpoint.get("phase") == "display_sent" and
                    _projection_display_matches(row, projection))
                completion_task_present = (
                    display_checkpoint.get("phase") == "completion_pending" and
                    current_task == projection.get("result_notion_status"))
                if projection.get("request_id") != (state.get("request") or {}).get("id"):
                    projection_hold = _new_hold(
                        "REQUEST_RACE",
                        "Project 결과 확인 뒤 Notion 상태 요청이 달라져 이전 표시 복구를 멈췄습니다.",
                        fresh_fingerprint)
                elif (current_task != projection.get("expected_notion_status") and
                      not approving_prior_request and not own_display_readback and
                      not completion_task_present):
                    code = ("PROJECTION_CHECKPOINT_CHANGED"
                            if current_task == projection.get("result_notion_status") and
                            display_checkpoint else "REQUEST_RACE")
                    projection_hold = _new_hold(
                        code,
                        ("이전 Notion 표시와의 일치 근거가 달라 완료를 확정할 수 없습니다."
                         if code == "PROJECTION_CHECKPOINT_CHANGED" else
                         "Project 결과 확인 뒤 Notion에 새 상태 이동이 있어 이전 표시 복구를 멈췄습니다."),
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
                    latest_page = notion.request("GET", f"/pages/{identifier(row.get('id'))}")
                    bound_page(latest_page, source_id)
                    latest_state = decode_internal(
                        read_text(latest_page, "동기화 내부 상태"), kind="issue",
                        project_id=config["project_id"], object_id=issue_id)
                    latest_projection = ((latest_state or {}).get("projection") or {})
                    display_checkpoint = projection.get("display_checkpoint") or {}
                    latest_task = read_select(latest_page, "작업 상태")
                    preserve_deferred_status = (
                        latest_task if (approving_prior_request or saved_pm_display) and
                        latest_state is not None and
                        _deferred_card_matches(latest_state, projection, latest_task) else None)
                    checkpoint_still_bound = (
                        latest_projection == projection and
                        (latest_state.get("request") or {}).get("id") ==
                        projection.get("request_id"))
                    own_display_echo = (
                        checkpoint_still_bound and
                        display_checkpoint.get("phase") == "display_sent" and
                        _projection_display_matches(latest_page, projection))
                    completion_echo = (
                        checkpoint_still_bound and
                        display_checkpoint.get("phase") == "completion_pending" and
                        (latest_task == projection.get("result_notion_status") or
                         preserve_deferred_status is not None and
                         _projection_display_matches(latest_page, projection)))
                    approved_older_card_values = {
                        projection.get("expected_notion_status"),
                        projection.get("result_notion_status"),
                        ((latest_state or {}).get("deferred_request") or {}).get("target"),
                    }
                    if (approving_prior_request and latest_state is not None and
                            latest_task not in approved_older_card_values):
                        # Recheck the page after persisting the PM approval. A newly
                        # observed C is neither A's approval nor a reason to discard B.
                        state = latest_state
                        state["resume"] = None
                        _persist_observed_card_race(
                            notion, source_id, latest_page, metadata, state,
                            item=latest_item, issue=latest_issue, facts=facts,
                            config=config, now=now,
                            message="이전 결과를 PM이 재개하는 동안 새 Notion 상태 이동이 확인되어 최신 이동을 보류했습니다.")
                        counts["held"] += 1
                        issue_rows_for_summary.append((latest_page, state))
                        continue
                    if (projection.get("fact_contract") == "semantic_v2" and
                            (not checkpoint_still_bound or
                             (latest_task != projection.get("expected_notion_status") and
                              not own_display_echo and not completion_echo)) and
                            not (approving_prior_request and latest_state and
                                 (latest_state.get("request") or {}).get("id") ==
                                 projection.get("request_id"))):
                        projection_hold = _new_hold(
                            "REQUEST_RACE",
                            "표시 복구 직전 Notion 상태 요청이 달라져 최신 이동을 보존했습니다.",
                            fresh_fingerprint)
                        if (state.get("request") and
                                state["request"].get("id") == projection.get("request_id")):
                            state["request"]["phase"] = "held"
                            state["request"]["reason"] = projection_hold["message"]
                        state["hold"] = projection_hold
                        _verify_issue_state_write(notion, source_id, latest_page, state)
                        _save_status_request(notion, source_id, latest_page, state)
                        row = latest_page
                    elif (projection.get("fact_contract") == "semantic_v2" and
                          display_checkpoint.get("phase") == "completion_pending"):
                        approved_card_status = (state.get("resume") or {}).get(
                            "approved_notion_status")
                        approved_card_echo = (
                            issue_number in resume_numbers and
                            approved_card_status is not None and
                            latest_task == approved_card_status)
                        if (latest_task != projection.get("result_notion_status") and
                                not approved_card_echo and
                                preserve_deferred_status is None):
                            projection_hold = _new_hold(
                                "REQUEST_RACE",
                                "완료 복구 중 Notion에 새 상태 이동이 있어 최신 이동을 보존했습니다.",
                                fresh_fingerprint)
                            state["hold"] = projection_hold
                            _record_held_card_request(
                                notion, source_id, latest_page, state, latest_item,
                                latest_issue, facts, config, status_observed_at)
                            _verify_issue_state_write(notion, source_id, latest_page, state)
                            _save_status_request(notion, source_id, latest_page, state)
                            _display_hold(notion, source_id, latest_page, metadata, state,
                                          projection_hold, now, preserve_task_status=True)
                            counts["held"] += 1
                            issue_rows_for_summary.append((latest_page, state))
                            continue
                        if not _projection_display_matches(latest_page, projection):
                            completed = _completed_projection_state(
                                latest_state, issue=latest_issue, item=latest_item,
                                linked_refs=latest_refs, config=config,
                                notion_status=projection.get("result_notion_status"),
                                observed_at=status_observed_at,
                                approved_resume=issue_number in resume_numbers or
                                saved_pm_display,
                                preserve_task_status=preserve_deferred_status)
                            repair = _status_request_properties(completed)
                            repair_task_status = (preserve_deferred_status
                                                  if preserve_deferred_status is not None else
                                                  projection.get("result_notion_status"))
                            repair["작업 상태"] = {"select": {
                                "name": repair_task_status}
                                if repair_task_status is not None else None}
                            fields, fingerprint = _restore_projection_checkpoint(repair)
                            latest_state["projection"]["display_checkpoint"] = {
                                "phase": "completion_pending", "expected_fields": fields,
                                "expected_fingerprint": fingerprint}
                            _verify_issue_state_write(
                                notion, source_id, latest_page, latest_state)
                            if (read_select(latest_page, "작업 상태") != latest_task or
                                    not _issue_state_binding_matches(latest_page, latest_state)):
                                _persist_observed_card_race(
                                    notion, source_id, latest_page, metadata,
                                    latest_state, item=latest_item, issue=latest_issue,
                                    facts=facts, config=config, now=now,
                                    message="완료 표시 복구 checkpoint 확인 중 Notion에 새 이동이 있어 완료 표시를 멈추고 보류했습니다.")
                                state = latest_state
                                counts["held"] += 1
                                issue_rows_for_summary.append((latest_page, state))
                                continue
                            projection = latest_state["projection"]
                            state["projection"] = projection
                            repair["동기화 내부 상태"] = text_property(
                                canonical_json(latest_state))
                            _patch_page(notion, source_id, latest_page, repair)
                        else:
                            completed = _completed_projection_state(
                                latest_state, issue=latest_issue, item=latest_item,
                                linked_refs=latest_refs, config=config,
                                notion_status=projection.get("result_notion_status"),
                                observed_at=status_observed_at,
                                approved_resume=issue_number in resume_numbers or
                                saved_pm_display,
                                preserve_task_status=preserve_deferred_status)

                        # Completion is promoted only after a final fresh GET
                        # confirms both the terminal card value and its exact
                        # durable projection/request checkpoint.
                        latest_page = notion.request(
                            "GET", f"/pages/{identifier(latest_page.get('id'))}")
                        bound_page(latest_page, source_id)
                        latest_state = decode_internal(
                            read_text(latest_page, "동기화 내부 상태"), kind="issue",
                            project_id=config["project_id"], object_id=issue_id)
                        latest_projection = (latest_state or {}).get("projection") or {}
                        latest_request_id = ((latest_state or {}).get("request") or {}).get("id")
                        final_task_status = read_select(latest_page, "작업 상태")
                        final_preserved_deferred = _deferred_card_matches(
                            latest_state or {}, projection, final_task_status)
                        if (final_task_status != projection.get("result_notion_status") and
                                not final_preserved_deferred):
                            projection_hold = _new_hold(
                                "REQUEST_RACE",
                                "완료 readback 중 Notion에 새 상태 이동이 있어 최신 이동을 보존했습니다.",
                                fresh_fingerprint)
                            state["hold"] = projection_hold
                            _record_held_card_request(
                                notion, source_id, latest_page, state, latest_item,
                                latest_issue, facts, config, status_observed_at)
                            _verify_issue_state_write(notion, source_id, latest_page, state)
                            _save_status_request(notion, source_id, latest_page, state)
                            _display_hold(notion, source_id, latest_page, metadata, state,
                                          projection_hold, now, preserve_task_status=True)
                            counts["held"] += 1
                            issue_rows_for_summary.append((latest_page, state))
                            continue
                        if (latest_projection != projection or latest_request_id !=
                                projection.get("request_id")):
                            projection_hold = _new_hold(
                                "REQUEST_RACE",
                                "완료 readback에서 projection/request 결속이 달라져 완료를 확정하지 않았습니다.",
                                fresh_fingerprint)
                            state["hold"] = projection_hold
                            _verify_issue_state_write(notion, source_id, latest_page, state)
                            _display_hold(notion, source_id, latest_page, metadata, state,
                                          projection_hold, now,
                                          preserve_task_status=bool(state.get("deferred_request")))
                            counts["held"] += 1
                            issue_rows_for_summary.append((latest_page, state))
                            continue
                        require(_projection_display_matches(latest_page, projection),
                                "완료 표시 복구 최종 readback 불일치")
                        recovery_inflight_state = json.loads(json.dumps(latest_state))
                        task_before_promotion = read_select(latest_page, "작업 상태")
                        state.update(completed)
                        _verify_issue_state_write(notion, source_id, latest_page, state)
                        if (read_select(latest_page, "작업 상태") != task_before_promotion or
                                not _issue_state_binding_matches(latest_page, state)):
                            state.clear()
                            state.update(recovery_inflight_state)
                            _persist_observed_card_race(
                                notion, source_id, latest_page, metadata, state,
                                item=latest_item, issue=latest_issue, facts=facts,
                                config=config, now=now,
                                message="완료 복구 승격 readback에서 카드 또는 요청 결속이 달라 완료를 되돌리고 보류했습니다.")
                            counts["held"] += 1
                            issue_rows_for_summary.append((latest_page, state))
                            continue
                        row = latest_page
                        if not created:
                            counts["updated"] += 1
                        issue_rows_for_summary.append((row, state))
                        continue
                    else:
                        state.update(projection_plan["state"])
                        _apply_status_checkpoint(state, projection["checkpoint"])
                        state["project_item_id"] = latest_item["id"]
                        project_by_issue[issue_id] = latest_item
                        checkpoint_written = _write_checkpointed_projection(
                            notion, source_id, row, metadata, projection_plan, state, now,
                            item=latest_item, issue=latest_issue, facts=facts, config=config,
                            preserve_task_status=preserve_deferred_status)
                        if not checkpoint_written:
                            counts["held"] += 1
                            issue_rows_for_summary.append((row, state))
                            continue
                        if projection.get("fact_contract") == "semantic_v2":
                            finalized = _finalize_bidirectional_projection(
                                notion, source_id, row, state, issue=latest_issue,
                                item=latest_item, linked_refs=latest_refs, config=config,
                                notion_status=projection.get("result_notion_status"),
                                observed_at=status_observed_at,
                                approved_resume=issue_number in resume_numbers or saved_pm_display,
                                facts=facts, metadata=metadata, now=now,
                                preserve_task_status=preserve_deferred_status)
                            if not finalized:
                                counts["held"] += 1
                                issue_rows_for_summary.append((row, state))
                                continue
                        else:
                            state["projection"] = None
                            state["pending"] = None
                            state["hold"] = None
                            state["resume"] = None
                            _verify_issue_state_write(notion, source_id, row, state)
                        if not created:
                            counts["updated"] += 1
                        issue_rows_for_summary.append((row, state))
                        continue

            if (projection_hold is not None and saved_pm_display and
                    projection_hold["code"] == "PROJECTION_CHECKPOINT_CHANGED"):
                projection_hold["code"] = "RESUME_CHECKPOINT_CHANGED"
            if projection_hold["code"] != "REQUEST_RACE":
                state["projection"] = None
            if (projection_hold["code"] == "REQUEST_RACE" and state.get("request") and
                    state["request"].get("id") == projection.get("request_id")):
                state["request"]["reason"] = projection_hold["message"]
            _record_held_card_request(notion, source_id, row, state, latest_item,
                                      latest_issue, facts, config, status_observed_at)
            _display_hold(notion, source_id, row, metadata, state, projection_hold, now,
                          preserve_task_status=True)
            if latest_item.get("is_archived"):
                project_by_issue.pop(issue_id, None)
            else:
                project_by_issue[issue_id] = latest_item
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue

        terminal_refs, _ = _linked_pr_facts(row_data, facts, allow_snapshot_drift=True)
        if _promote_held_terminal_restore(
                notion, source_id, row, state, issue=row_data, item=item,
                linked_refs=terminal_refs, config=config,
                observed_at=status_observed_at, facts=facts, metadata=metadata, now=now):
            if state.get("hold"):
                counts["held"] += 1
            elif not created:
                counts["updated"] += 1
            issue_rows_for_summary.append((row, state))
            continue

        current_request_id = (state.get("request") or {}).get("id")
        resume_display_candidate = _resume_matches(
            state["resume"], row_data, facts, item, request_id=current_request_id,
            notion_status=read_select(row, "작업 상태"))
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
            if (config.get("bidirectional_enabled") and
                    _card_move_needs_preservation(state, row)):
                _record_held_card_request(notion, source_id, row, state, item, row_data,
                                          facts, config, status_observed_at)
            _display_hold(notion, source_id, row, metadata, state, state["hold"], now,
                          preserve_task_status=state["hold"]["code"] in
                          {"MIGRATION_INPUT_CHANGED", "RESUME_CHECKPOINT_CHANGED",
                           "PENDING_RESULT_UNCLEAR", "PROJECT_STATUS_UNSET", "MIGRATION_UNSET",
                           "STATUS_REQUEST_INVALID"})
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue

        # A returned add ID is read back automatically with a durable three-check budget.
        if state["pending"] and state["pending"]["kind"] == "add":
            readback = state.get("readback") or {}
            if (issue_number not in resume_numbers and state["hold"] is None and
                    readback.get("returned_item_id")):
                if readback["validated"]:
                    pending_hold = None
                else:
                    prior_project = project
                    readback_project, readback_item, pending_hold = _read_added_project_item_once(
                        notion, source_id, row, state, project_client, config, facts, row_data,
                        readback_identity)
                    project = readback_project if readback_project is not None else prior_project
                    if pending_hold:
                        _display_hold(notion, source_id, row, metadata, state, pending_hold, now,
                                      preserve_task_status=True)
                        counts["held"] += 1
                        issue_rows_for_summary.append((row, state))
                        continue
                    if readback_item is None:
                        _display_add_readback_waiting(notion, source_id, row, state, now)
                        issue_rows_for_summary.append((row, state))
                        continue
                item, pending_hold = _resolve_pending_add(
                    notion, github, source_id, row, state, project, facts, row_data)
                if pending_hold:
                    _display_hold(notion, source_id, row, metadata, state, pending_hold, now,
                                  preserve_task_status=True)
                    counts["held"] += 1
                    issue_rows_for_summary.append((row, state))
                    continue
                require(item is not None, "Project add 자동 readback checkpoint 누락")
                project_by_issue[issue_id] = item
            else:
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
                if ((state.get("readback") or {}).get("returned_item_id") and
                        not state["readback"].get("validated")):
                    checked_project, checked_item, check_hold = _read_added_project_item_immediate(
                        notion, source_id, row, state, project_client, config, facts, row_data)
                    if checked_project is not None:
                        project = checked_project
                    if check_hold or checked_item is None:
                        hold = check_hold or state.get("hold") or _new_hold(
                            "PROJECT_ADD_UNCERTAIN",
                            "Project 반환 ID의 직접 조회와 전체 목록 관계를 확인하지 못했습니다.",
                            _source_fingerprint(row_data, _linked_pr_facts(row_data, facts)[0], None))
                        _display_hold(notion, source_id, row, metadata, state, hold, now,
                                      preserve_task_status=True)
                        counts["held"] += 1
                        issue_rows_for_summary.append((row, state))
                        continue
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
            if item is None and state.get("readback") and not state["readback"]["validated"]:
                _display_add_readback_waiting(notion, source_id, row, state, now)
                issue_rows_for_summary.append((row, state))
                continue
            project_by_issue[issue_id] = item

        pending_hold = None
        if state["pending"] and state["pending"]["kind"] == "status":
            pending_hold = _recover_pending_status(notion, source_id, row, state, project,
                                                   github, facts, row_data, config, cutoff,
                                                   manual_resume=issue_number in resume_numbers)
            if pending_hold:
                state["hold"] = pending_hold
                if (config.get("bidirectional_enabled") and
                        _card_move_needs_preservation(state, row)):
                    _record_held_card_request(notion, source_id, row, state, item, row_data,
                                              facts, config, status_observed_at)
                _display_hold(notion, source_id, row, metadata, state, pending_hold, now,
                              preserve_task_status=(pending_hold["code"] in
                                  {"MIGRATION_INPUT_CHANGED", "PENDING_RESULT_UNCLEAR",
                                   "PROJECT_STATUS_UNSET", "STATUS_REQUEST_INVALID"} or
                                  state.get("deferred_request") is not None))
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue

        projection = state.get("projection") or {}
        if (config.get("bidirectional_enabled") and issue_number not in resume_numbers and
                projection and
                read_select(row, "작업 상태") != projection.get("expected_notion_status") and
                (state.get("request") or {}).get("id") == projection.get("request_id")):
            race_hold = _new_hold(
                "REQUEST_RACE",
                "이전 Project 결과 확인 중 새 Notion 상태 이동이 있어 두 요청을 분리해 보류합니다.",
                _projection_fingerprint(projection, row_data,
                                        _linked_pr_facts(row_data, facts)[0], item))
            state["hold"] = race_hold
            _record_held_card_request(notion, source_id, row, state, item, row_data,
                                      facts, config, status_observed_at)
            _verify_issue_state_write(notion, source_id, row, state)
            _save_status_request(notion, source_id, row, state)
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue
        if (config.get("bidirectional_enabled") and state.get("deferred_request") and
                issue_number not in resume_numbers and
                projection and read_select(row, "작업 상태") !=
                projection.get("expected_notion_status")):
            # Keep the older projection/request binding intact until its result is
            # handled separately from the persisted newer card request.
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue
        saved_resume_recovery = (issue_number not in resume_numbers and
                                 _resume_matches(state["resume"], row_data, facts, item,
                                                 request_id=current_request_id,
                                                 notion_status=read_select(row, "작업 상태")))
        approved_resume = False
        promoted_deferred_request = False
        if issue_number in resume_numbers:
            prior_hold = state["hold"]
            if (state.get("projection") is None and
                    prior_hold.get("code") == "DEFERRED_REQUEST_PENDING" and
                    state.get("deferred_request") is not None):
                # The prior request is terminal. Transfer the saved follow-up using
                # its own identity before binding this PM approval; never reuse A's ID.
                state["request"] = state["deferred_request"]
                state["deferred_request"] = None
                promoted_deferred_request = True
            # A pending add has its own immutable returned-ID and relationship fence.
            # Do not alter its captured migration input with an unrelated resume marker;
            # the authorized dispatch is revalidated by direct-node and full-list reads below.
            pending_add_resume = bool(state.get("pending") and
                                      state["pending"].get("kind") == "add")
            if not pending_add_resume:
                linked_for_resume, _ = _linked_pr_facts(row_data, facts)
                state["resume"] = {"actor_id": resume_actor["actor_id"], "run_id": resume_actor["run_id"],
                                   "approved_option_id": item["status_option_id"],
                                   "fingerprint": _status_operation_fingerprint(
                                       row_data, linked_for_resume, item),
                                   "display_pending": True,
                                   "expected_option_id": None, "expected_fingerprint": None,
                                   "request_id": (state.get("request") or {}).get("id"),
                                   "facts_fingerprint": _status_facts_fingerprint(
                                       row_data, linked_for_resume),
                                   "fact_contract": "semantic_v2",
                                   "approved_notion_status": read_select(row, "작업 상태")}
                _verify_issue_state_write(notion, source_id, row, state)
            approved_resume = prior_hold["code"] in RESUMABLE_HOLD_CODES
        elif saved_resume_recovery:
            approved_resume = bool(state["hold"] and
                                   state["hold"]["code"] in RESUMABLE_HOLD_CODES)

        plan_state = json.loads(json.dumps(state))
        plan = _plan_issue(row_data, item, row, plan_state, facts, cutoff, config["project_id"],
                           config["status_options"], approved_resume=approved_resume)
        prepared_request = state.get("request") or {}
        prepared_pm_resume = (
            isinstance(state.get("resume"), dict) and
            state["resume"].get("request_id") == prepared_request.get("id") and
            state["resume"].get("approved_notion_status") ==
            read_select(row, "작업 상태") and
            state["resume"].get("approved_option_id") == item.get("status_option_id"))
        prepared_recovery = (prepared_request.get("phase") == "prepared" and
                             not prepared_pm_resume and
                             state.get("pending") is None and
                             state.get("projection") is None and
                             (state.get("notion_write") or {}).get("kind") == "request")
        if prepared_recovery:
            linked_now, eligible_now = _linked_pr_facts(
                row_data, facts, allow_snapshot_drift=True)
            baseline_now = state.get("baseline") or {}
            recovery_matches = (
                prepared_request.get("target") in GENERAL_STATES and
                read_select(row, "작업 상태") == prepared_request.get("target") and
                item.get("id") == baseline_now.get("project_item_id") and
                _status_stamp(item, config) == baseline_now.get("project") and
                _status_facts_fingerprint(row_data, linked_now) ==
                baseline_now.get("facts_fingerprint") and
                row_data.get("state") == "OPEN" and
                row_data.get("duplicateOf") is None and not eligible_now and
                (state.get("notion_write") or {}).get("request_id") ==
                prepared_request.get("id") and
                (state.get("notion_write") or {}).get("target") ==
                prepared_request.get("target"))
            if not recovery_matches:
                prepared_request["phase"] = "held"
                prepared_request["reason"] = (
                    "준비된 요청을 재개하는 동안 카드, Project stamp 또는 GitHub 사실이 달라져 PM 확인이 필요합니다.")
                state["notion_write"] = None
                state["hold"] = _new_hold(
                    "RESUME_CHECKPOINT_CHANGED", prepared_request["reason"],
                    _status_operation_fingerprint(row_data, linked_now, item))
                if read_select(row, "작업 상태") != prepared_request.get("target"):
                    _record_held_card_request(
                        notion, source_id, row, state, item, row_data, facts,
                        config, status_observed_at)
                _save_status_request(notion, source_id, row, state)
                _display_hold(notion, source_id, row, metadata, state, state["hold"], now,
                              preserve_task_status=True)
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue
            # Reconstruct only the pending operation; preserve the saved request
            # identity and target instead of accepting the ordinary source plan.
            plan["target"] = prepared_request["target"]
            plan["confirmation"] = ""
        if promoted_deferred_request and approved_resume:
            followup = state["request"]
            if plan.get("hold"):
                state["hold"] = plan["hold"]
                followup["phase"] = "held"
                followup["reason"] = plan["hold"]["message"]
                _display_hold(notion, source_id, row, metadata, state, state["hold"], now,
                              preserve_task_status=True)
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue
            state["hold"] = None
            if plan.get("target") == followup.get("target"):
                # PM selected the current Project value that agrees with the saved
                # follow-up. Reuse B's ID and continue through the normal fenced write.
                followup["phase"] = "prepared"
                state["notion_write"] = {
                    "request_id": followup["id"],
                    "expected_before": read_select(row, "작업 상태"),
                    "target": followup["target"], "kind": "request",
                    "phase": "prepared"}
                plan["target"] = followup["target"]
                plan["confirmation"] = ""
            else:
                selected = plan.get("target")
                followup["phase"] = "rejected"
                followup["reason"] = (
                    f"PM이 현재 GitHub 상태 {selected or '미지정'}을 선택하여 "
                    f"후속 요청 {followup['target']}은 반영하지 않았습니다.")
                state["resume"] = None
                state["notion_write"] = {
                    "request_id": followup["id"],
                    "expected_before": read_select(row, "작업 상태"),
                    "target": selected, "kind": "restore", "phase": "prepared",
                    "project_item_id": item["id"],
                    "project_stamp": _status_stamp(item, config),
                    "facts_fingerprint": _status_facts_fingerprint(
                        row_data, _linked_pr_facts(row_data, facts)[0])}
                _verify_issue_state_write(notion, source_id, row, state)
                _save_status_request(notion, source_id, row, state)
                _recover_restore_write(
                    notion, github, project_client, source_id, row, metadata, state,
                    facts, row_data, item, config, cutoff, now)
                if state.get("hold"):
                    counts["held"] += 1
                else:
                    counts["updated"] += 1
                issue_rows_for_summary.append((row, state))
                continue
        if (state["hold"] is not None and issue_number not in resume_numbers and
                config.get("bidirectional_enabled") and
                _card_move_needs_preservation(state, row)):
            # Do not let either the durable hold renderer or a source plan erase a later card move.
            _record_held_card_request(notion, source_id, row, state, item, row_data,
                                      facts, config, status_observed_at)
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue
        if plan.get("hold"):
            if (config.get("bidirectional_enabled") and
                    _card_move_needs_preservation(state, row)):
                state["hold"] = plan["hold"]
                _record_held_card_request(notion, source_id, row, state, item, row_data,
                                          facts, config, status_observed_at)
            _display_hold(notion, source_id, row, metadata, state, plan["hold"], now,
                          preserve_task_status=plan["hold"]["code"] in
                          {"MIGRATION_INPUT_CHANGED", "PROJECT_STATUS_UNSET", "MIGRATION_UNSET"})
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue
        if state["hold"] is not None and issue_number not in resume_numbers and not saved_resume_recovery:
            # A durable hold cannot disappear merely because the source now looks consistent.
            if (config.get("bidirectional_enabled") and
                    _card_move_needs_preservation(state, row)):
                # Keep a newer card move visible while the original PM hold remains unresolved.
                _record_held_card_request(notion, source_id, row, state, item, row_data,
                                          facts, config, status_observed_at)
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue
            _display_hold(notion, source_id, row, metadata, state, state["hold"], now,
                          preserve_task_status=state["hold"]["code"] in
                          {"MIGRATION_INPUT_CHANGED", "RESUME_CHECKPOINT_CHANGED",
                           "PENDING_RESULT_UNCLEAR", "PROJECT_STATUS_UNSET", "MIGRATION_UNSET",
                           "STATUS_REQUEST_INVALID"})
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue

        if (config.get("bidirectional_enabled") and state.get("baseline") is None and
                state.get("pending") is None and state.get("projection") is None and
                state.get("hold") is None and state.get("resume") is None and
                issue_number not in resume_numbers):
            current_notion_status = read_select(row, "작업 상태")
            current_project_status = {value: name for name, value in
                                      config["status_options"].items()}.get(
                                          item.get("status_option_id"))
            issue_age_seconds = (timestamp(row_data["createdAt"]) - timestamp(cutoff)).total_seconds()
            new_row_bootstrap = (
                issue_age_seconds > BOUNDARY_SECONDS and
                plan.get("target") == "백로그" and not plan.get("hold") and
                current_notion_status in {None, "백로그"} and
                current_project_status in {None, "백로그"})
            refs, eligible_facts = _linked_pr_facts(row_data, facts)
            source_authoritative = (row_data.get("state") == "CLOSED" or
                                    row_data.get("duplicateOf") is not None or
                                    bool(eligible_facts))
            if (current_notion_status == current_project_status and
                    plan.get("target") == current_notion_status):
                latest_issue = gp.fetch_issue_detail(github, row_data["id"])
                latest_refs, _ = _linked_pr_facts(
                    latest_issue, facts, allow_snapshot_drift=True)
                latest_item = gp.fetch_project_item(
                    project_client, config["project_id"], item["id"],
                    config["status_field_id"], allow_archived=True)
                latest_page = notion.request("GET", f"/pages/{identifier(row.get('id'))}")
                bound_page(latest_page, source_id)
                latest_state = decode_internal(
                    read_text(latest_page, "동기화 내부 상태"), kind="issue",
                    project_id=config["project_id"], object_id=issue_id)
                latest_plan = _plan_issue(
                    latest_issue, latest_item, latest_page, state, facts, cutoff,
                    config["project_id"], config["status_options"])
                stable_facts = (_status_facts_fingerprint(latest_issue, latest_refs) ==
                                _status_facts_fingerprint(row_data, refs))
                stable = (not latest_item.get("is_archived") and
                          latest_item.get("id") == item.get("id") and
                          latest_item.get("content_id") == latest_issue.get("id") and
                          latest_item.get("status_field_id") == config["status_field_id"] and
                          _status_stamp(latest_item, config) == _status_stamp(item, config) and
                          latest_issue.get("id") == row_data.get("id") and stable_facts and
                          read_select(latest_page, "작업 상태") == current_notion_status and
                          latest_state is not None and latest_state.get("baseline") is None and
                          latest_state.get("pending") is None and
                          latest_state.get("projection") is None and
                          latest_state.get("hold") is None and
                          latest_state.get("resume") is None and
                          latest_state.get("project_item_id") in {None, latest_item.get("id")} and
                          ((latest_state.get("request") or {}).get("phase") in
                           {None, "completed", "rejected"}) and
                          latest_state.get("notion_write") is None and
                          (latest_state.get("readback") is None or
                           latest_state["readback"].get("validated")) and
                          latest_plan.get("target") == current_notion_status and
                          not latest_plan.get("hold"))
                if not stable:
                    hold = _new_hold(
                        "SOURCE_CHANGED_BEFORE_WRITE",
                        "초기 기준 상태를 저장하기 직전 Project, Notion 또는 GitHub 사실이 달라졌습니다.",
                        _digest({"issue": latest_issue.get("id"),
                                 "item": latest_item.get("id"),
                                 "facts": _status_facts_fingerprint(latest_issue, latest_refs)}))
                    state["hold"] = hold
                    _display_hold(notion, source_id, latest_page, metadata, state, hold, now,
                                  preserve_task_status=True)
                    counts["held"] += 1
                    issue_rows_for_summary.append((row, state))
                    continue
                state["baseline"] = _new_status_baseline(
                    current_notion_status, latest_item, latest_issue, latest_refs,
                    config, status_observed_at)
                state["project_item_id"] = latest_item["id"]
                _verify_issue_state_write(notion, source_id, row, state)
            elif not source_authoritative and not new_row_bootstrap:
                code = ("MIGRATION_CONFLICT" if current_notion_status in GENERAL_STATES and
                        current_project_status in GENERAL_STATES else "MIGRATION_UNSET")
                message = ("기준 상태가 없어 서로 다른 Notion/Project 값을 선택할 수 없습니다."
                           if code == "MIGRATION_CONFLICT" else
                           "기준 상태를 만들기 위해 Notion과 Project의 유효한 상태 확인이 필요합니다.")
                hold = _new_hold(code, message,
                                 _pending_semantic_fingerprint(row_data, refs, item, "status"))
                _display_hold(notion, source_id, row, metadata, state, hold, now,
                              preserve_task_status=True)
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue

        terminal_marker = state.get("notion_write") or {}
        terminal_request = state.get("request") or {}
        terminal_project_status = {value: name for name, value in
                                   config["status_options"].items()}.get(
                                       item.get("status_option_id"))
        if (config.get("bidirectional_enabled") and kind == "Issue" and
                not closed_supersedes_restore and
                terminal_request.get("phase") == "rejected" and
                terminal_marker.get("kind") == "restore" and
                terminal_marker.get("phase") == "confirmed" and
                not (terminal_project_status == read_select(row, "작업 상태") and
                     _terminal_restore_marker_matches(
                         state, page=row, issue=row_data, item=item,
                         linked_refs=_linked_pr_facts(
                             row_data, facts, allow_snapshot_drift=True)[0],
                         config=config, notion_status=read_select(row, "작업 상태")))):
            hold = _new_hold(
                "PENDING_RESULT_UNCLEAR",
                "확정된 요청 복원 표시의 카드·요청·Project·GitHub 확인이 달라 baseline 승격을 멈췄습니다.",
                _digest({"request_id": terminal_request.get("id"),
                         "marker_request_id": terminal_marker.get("request_id"),
                         "item": item.get("id"),
                         "project_stamp": _status_stamp(item, config)}))
            state["hold"] = hold
            _display_hold(notion, source_id, row, metadata, state, hold, now,
                          preserve_task_status=True)
            counts["held"] += 1
            issue_rows_for_summary.append((row, state))
            continue

        status_request_action = None
        status_decision = None
        if (config.get("bidirectional_enabled") and kind == "Issue" and
                issue_number not in resume_numbers and
                not closed_supersedes_restore and
                not promoted_deferred_request and
                (state.get("request") or {}).get("phase") not in
                {"prepared", "sent", "uncertain", "confirmed"}):
            status_decision, status_request = _classify_notion_status_change(
                state, row, item, row_data, facts, config, status_observed_at)
            status_request_action = status_decision["action"]
            if status_request is not None:
                state["request"] = status_request
                if status_decision["action"] == "notion_request":
                    state["request"]["phase"] = "prepared"
                    state["notion_write"] = {
                        "request_id": status_request["id"],
                        "expected_before": state["baseline"]["notion_status"],
                        "target": status_request["target"], "kind": "request",
                        "phase": "prepared"}
                    plan["target"] = status_request["target"]
                    plan["confirmation"] = ""
                else:
                    facts_invalid_request = (
                        status_decision["action"] == "facts_override" and
                        read_select(row, "작업 상태") not in status_sync.GENERAL_STATES)
                    state["request"]["phase"] = (
                        "rejected" if (status_decision["action"] == "hold_invalid_request" or
                                       facts_invalid_request) else "held")
                    status_hold_code = {
                        "hold_invalid_request": "STATUS_REQUEST_INVALID",
                        "conflict": "BIDIRECTIONAL_CONFLICT",
                        "hold_unknown": "STATUS_ORDER_UNKNOWN",
                        "facts_override": "GITHUB_FACTS_CHANGED",
                    }.get(status_decision["action"], "STATUS_ORDER_UNKNOWN")
                    if facts_invalid_request:
                        # Reject the invalid card value, but let the independently
                        # verified GitHub fact transition continue through the normal
                        # Project mutation and final dual-side readback path.
                        state["hold"] = None
                        state["request"]["reason"] = status_request["reason"]
                        state["notion_write"] = None
                        _save_status_request(notion, source_id, row, state)
                    else:
                        state["hold"] = _new_hold(status_hold_code, status_request["reason"],
                                                   _source_fingerprint(row_data,
                                                       _linked_pr_facts(row_data, facts)[0], item))
                        state["request"]["reason"] = state["hold"]["message"]
                        state["notion_write"] = {"request_id": status_request["id"],
                                                  "expected_before": status_request["target"],
                                                  "target": plan.get("target"), "kind": "restore",
                                                  "phase": "prepared",
                                                  "project_item_id": item["id"],
                                                  "project_stamp": _status_stamp(item, config),
                                                  "facts_fingerprint": _status_facts_fingerprint(
                                                      row_data, _linked_pr_facts(
                                                          row_data, facts)[0])}
                        _verify_issue_state_write(notion, source_id, row, state)
                        _save_status_request(notion, source_id, row, state)
                        _recover_restore_write(
                            notion, github, project_client, source_id, row, metadata, state,
                            facts, row_data, item, config, cutoff, now)
                        counts["held"] += 1
                        issue_rows_for_summary.append((row, state))
                        continue

        # Two independently changed sides that already show the same verified
        # value are converged. Refresh only the durable baseline after one last
        # source/page check; do not manufacture a task-status projection.
        if (status_decision and status_decision.get("action") == "converged" and
                status_decision.get("notion_changed") and
                status_decision.get("project_changed") and
                state.get("baseline") is not None and
                state.get("pending") is None and state.get("projection") is None and
                state.get("hold") is None and state.get("resume") is None and
                state.get("deferred_request") is None and state.get("notion_write") is None and
                plan.get("target") == read_select(row, "작업 상태") and
                (state.get("request") or {}).get("phase") in {None, "completed", "rejected"}):
            latest_issue = gp.fetch_issue_detail(github, row_data["id"])
            latest_refs, _ = _linked_pr_facts(latest_issue, facts, allow_snapshot_drift=True)
            latest_item = gp.fetch_project_item(
                project_client, config["project_id"], item["id"],
                config["status_field_id"], allow_archived=True)
            latest_page = notion.request("GET", f"/pages/{identifier(row.get('id'))}")
            bound_page(latest_page, source_id)
            latest_state = decode_internal(
                read_text(latest_page, "동기화 내부 상태"), kind="issue",
                project_id=config["project_id"], object_id=issue_id)
            same_value = {value: name for name, value in config["status_options"].items()}.get(
                latest_item.get("status_option_id"))
            stable = (
                latest_issue.get("id") == row_data.get("id") and
                latest_item.get("id") == item.get("id") and
                latest_item.get("content_id") == latest_issue.get("id") and
                latest_item.get("status_field_id") == config["status_field_id"] and
                not latest_item.get("is_archived") and
                _status_stamp(latest_item, config) == _status_stamp(item, config) and
                _status_facts_fingerprint(latest_issue, latest_refs) ==
                _status_facts_fingerprint(row_data,
                    _linked_pr_facts(row_data, facts, allow_snapshot_drift=True)[0]) and
                same_value == read_select(latest_page, "작업 상태") == plan.get("target") and
                latest_state == state)
            if not stable:
                hold = _new_hold(
                    "SOURCE_CHANGED_BEFORE_WRITE",
                    "동일 상태 수렴 확인 중 Project, Notion 또는 GitHub 값이 달라 baseline 저장을 보류했습니다.",
                    _projection_fingerprint({}, latest_issue, latest_refs, latest_item))
                state["hold"] = hold
                _display_hold(notion, source_id, latest_page, metadata, state, hold, now,
                              preserve_task_status=True)
                counts["held"] += 1
                issue_rows_for_summary.append((latest_page, state))
                continue
            state["baseline"] = _new_status_baseline(
                same_value, latest_item, latest_issue, latest_refs, config, status_observed_at)
            state["project_item_id"] = latest_item["id"]
            _verify_issue_state_write(notion, source_id, latest_page, state)
            row = latest_page
            item = latest_item
            project_by_issue[issue_id] = latest_item

        # A genuinely unchanged, fully baselined row needs metadata and clock
        # refreshes only. Recheck the semantic Project stamp and GitHub facts
        # first so same-option resets and source changes still take the guarded
        # reconciliation path below.
        if (config.get("bidirectional_enabled") and kind == "Issue" and
                state.get("pending") is None and state.get("projection") is None and
                state.get("hold") is None and
                _terminal_restore_marker_matches(
                    state, page=row, issue=row_data, item=item,
                    linked_refs=_linked_pr_facts(row_data, facts,
                                                 allow_snapshot_drift=True)[0],
                    config=config, notion_status=read_select(row, "작업 상태"))):
            terminal_marker = state["notion_write"]
            prior_baseline = state.get("baseline")
            observed_status = read_select(row, "작업 상태")
            state["baseline"] = _new_status_baseline(
                observed_status, item, row_data,
                _linked_pr_facts(row_data, facts, allow_snapshot_drift=True)[0],
                config, status_observed_at)
            state["notion_write"] = None
            _verify_issue_state_write(notion, source_id, row, state)
            if (read_select(row, "작업 상태") != observed_status or
                    not _restore_projection_matches(row, terminal_marker)):
                # The state PATCH readback also fetches the live card. If the
                # terminal display changed in that interval, undo promotion and
                # preserve the newly observed move as a separate held request.
                state["baseline"] = prior_baseline
                state["notion_write"] = terminal_marker
                hold = _new_hold(
                    "REQUEST_RACE",
                    "확정된 복원 표시 확인 중 Notion 카드 변경이 확인되어 baseline 확정을 보류했습니다.",
                    _digest({"request_id": (state.get("request") or {}).get("id"),
                             "observed_status": read_select(row, "작업 상태")}))
                _persist_observed_card_race(
                    notion, source_id, row, metadata, state, item=item,
                    issue=row_data, facts=facts, config=config,
                    now=now,
                    message=hold["message"])
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue

        baseline = state.get("baseline")
        request_phase = (state.get("request") or {}).get("phase")
        if (config.get("bidirectional_enabled") and kind == "Issue" and
                status_decision and status_decision.get("action") in {"unchanged", "converged"} and
                baseline and baseline.get("notion_status") == read_select(row, "작업 상태") and
                baseline.get("project_item_id") == item.get("id") and
                baseline.get("project") == _status_stamp(item, config) and
                baseline.get("facts_fingerprint") == _status_facts_fingerprint(
                    row_data, _linked_pr_facts(row_data, facts)[0]) and
                plan.get("target") == read_select(row, "작업 상태") and
                state.get("migration_complete") and state.get("pending") is None and
                state.get("projection") is None and state.get("hold") is None and
                state.get("resume") is None and state.get("deferred_request") is None and
                state.get("notion_write") is None and
                (state.get("readback") is None or state["readback"].get("validated")) and
                request_phase in {None, "completed", "rejected"}):
            stable_issue = gp.fetch_issue_detail(github, row_data["id"])
            stable_refs, _ = _linked_pr_facts(stable_issue, facts, allow_snapshot_drift=True)
            stable_item = gp.fetch_project_item(
                project_client, config["project_id"], item["id"],
                config["status_field_id"], allow_archived=True)
            stable_page = notion.request("GET", f"/pages/{identifier(row.get('id'))}")
            bound_page(stable_page, source_id)
            stable_state = decode_internal(
                read_text(stable_page, "동기화 내부 상태"), kind="issue",
                project_id=config["project_id"], object_id=issue_id)
            stable = (
                not stable_item.get("is_archived") and
                stable_item.get("id") == baseline["project_item_id"] and
                stable_item.get("content_id") == stable_issue.get("id") and
                stable_item.get("status_field_id") == config["status_field_id"] and
                _status_stamp(stable_item, config) == baseline["project"] and
                _status_facts_fingerprint(stable_issue, stable_refs) ==
                baseline["facts_fingerprint"] and
                read_select(stable_page, "작업 상태") == baseline["notion_status"] and
                stable_state == state)
            if stable:
                stable_metadata = _make_metadata(kind, stable_issue, key)
                _patch_page(notion, source_id, stable_page, {
                    **stable_metadata,
                    "동기화 시각": {"date": {"start": _minute_iso(now)}},
                })
                project_by_issue[issue_id] = stable_item
                if not created:
                    counts["updated"] += 1
                issue_rows_for_summary.append((stable_page, state))
                continue

        active_prepared = state.get("request") or {}
        if (config.get("bidirectional_enabled") and kind == "Issue" and
                active_prepared.get("phase") == "prepared" and
                state.get("pending") is None and state.get("projection") is None):
            request_ui = _status_request_properties(state)
            if not _properties_match(row, request_ui):
                _save_status_request(notion, source_id, row, state)
            saved_ui_state = decode_internal(
                read_text(row, "동기화 내부 상태"), kind="issue",
                project_id=config["project_id"], object_id=issue_id)
            current_card = read_select(row, "작업 상태")
            approved_card_matches = (
                prepared_pm_resume and
                current_card == (state.get("notion_write") or {}).get("expected_before"))
            if ((current_card != active_prepared.get("target") and
                 not approved_card_matches) or
                    saved_ui_state is None or
                    (saved_ui_state.get("request") or {}).get("id") !=
                    active_prepared.get("id") or
                    (saved_ui_state.get("request") or {}).get("phase") != "prepared"):
                active_prepared["phase"] = "held"
                active_prepared["reason"] = (
                    "반영 대기 표시를 확인하는 동안 Notion 카드 또는 요청이 바뀌어 Project 반영을 멈췄습니다.")
                state["notion_write"] = None
                state["hold"] = _new_hold(
                    "REQUEST_RACE", active_prepared["reason"],
                    _status_operation_fingerprint(row_data,
                        _linked_pr_facts(row_data, facts, allow_snapshot_drift=True)[0], item))
                _record_held_card_request(notion, source_id, row, state, item, row_data,
                                          facts, config, status_observed_at)
                _display_hold(notion, source_id, row, metadata, state, state["hold"], now,
                              preserve_task_status=True)
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue

        old_option = item["status_option_id"]
        target_option = _status_option(project, plan["target"])
        if state.get("projection") is not None:
            expected = state["projection"]
            if (target_option != expected.get("expected_option_id") or
                    plan.get("target") != expected.get("result_notion_status")):
                migration_hold = _new_hold(
                    "PROJECTION_CHECKPOINT_CHANGED",
                    "복구된 Project 결과가 현재 원본 사실과 일치하지 않아 표시를 보류했습니다.",
                    _projection_fingerprint(expected, row_data,
                                            _linked_pr_facts(row_data, facts)[0], item))
            else:
                migration_hold = None
        elif promoted_deferred_request and target_option == old_option:
            # The PM selected the exact Project value requested by the follow-up.
            # Confirm that unchanged Project value against fresh Issue/item reads,
            # then checkpoint only the Notion projection; do not manufacture a
            # status mutation or bind B to A's previous pending write.
            latest_issue = gp.fetch_issue_detail(github, row_data["id"])
            latest_item = gp.fetch_project_item(
                project_client, config["project_id"], item["id"],
                config["status_field_id"], allow_archived=True)
            latest_refs, _ = _linked_pr_facts(latest_issue, facts, allow_snapshot_drift=True)
            stable = (not latest_item.get("is_archived") and
                      latest_issue.get("id") == row_data.get("id") and
                      latest_item.get("id") == item.get("id") and
                      latest_item.get("content_id") == latest_issue.get("id") and
                      latest_item.get("status_field_id") == config["status_field_id"] and
                      latest_item.get("status_option_id") == target_option and
                      _status_stamp(latest_item, config) == _status_stamp(item, config) and
                      _status_facts_fingerprint(latest_issue, latest_refs) ==
                      _status_facts_fingerprint(row_data,
                                                _linked_pr_facts(row_data, facts)[0]))
            if not stable:
                migration_hold = _new_hold(
                    "SOURCE_CHANGED_BEFORE_WRITE",
                    "PM이 후속 요청을 확인하는 동안 GitHub 사실 또는 Project 상태가 달라져 표시를 보류했습니다.",
                    _projection_fingerprint({}, latest_issue, latest_refs, latest_item))
            else:
                checkpoint = {
                    "migration_complete": bool(plan["state"].get("migration_complete")),
                    "review_cycle": plan["state"]["review_cycle"],
                    "review_return_cycle": plan["state"]["review_return_cycle"],
                    "reopen_last_id": plan["state"]["reopen_last_id"],
                    "review_pr_hash": plan["state"]["review_pr_hash"],
                }
                operation_fingerprint = _status_operation_fingerprint(
                    latest_issue, latest_refs, latest_item)
                state["request"]["phase"] = "confirmed"
                state["notion_write"]["phase"] = "confirmed"
                state["projection"] = {
                    "expected_option_id": target_option,
                    "expected_fingerprint": operation_fingerprint,
                    "checkpoint": checkpoint,
                    "fact_contract": "semantic_v2",
                    "request_id": state["request"]["id"],
                    "expected_notion_status": read_select(row, "작업 상태"),
                    "result_notion_status": plan["target"],
                }
                if state.get("resume"):
                    state["resume"]["expected_option_id"] = target_option
                    state["resume"]["expected_fingerprint"] = operation_fingerprint
                _verify_issue_state_write(notion, source_id, row, state)
                item = latest_item
                project_by_issue[issue_id] = latest_item
                migration_hold = None
        else:
            if closed_supersedes_restore:
                state["notion_write"] = None
            project, item, migration_hold = _apply_project_status(
                notion, github, project_client, source_id, row, state, project, facts,
                row_data, item, plan, config, status_observed_at)
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
        current_projection_fingerprint = _projection_fingerprint(
            expected_projection or {}, latest_issue, latest_refs, latest_item)
        if latest_item.get("is_archived"):
            projection_hold = _new_hold("PROJECT_ITEM_ARCHIVED",
                                        "Notion 표시 전에 Project 항목이 보관되었습니다.",
                                        current_projection_fingerprint)
        elif (latest_item.get("content_id") != latest_issue.get("id") or
              latest_item["status_option_id"] != target_option or
              expected_projection is None or
              expected_projection["expected_option_id"] != target_option or
              (expected_projection.get("fact_contract") == "semantic_v2" and
               (expected_projection.get("request_id") !=
                (state.get("request") or {}).get("id") or
               expected_projection.get("result_notion_status") != plan.get("target"))) or
              current_projection_fingerprint != expected_projection["expected_fingerprint"]):
            projection_hold = _new_hold(
                "PROJECTION_CHECKPOINT_CHANGED",
                "Project 또는 원본 사실이 checkpoint 이후 달라져 표시 복구를 보류했습니다. PM 확인이 필요합니다.",
                current_projection_fingerprint)
        else:
            projection_hold = None
        if projection_hold:
            if projection_hold.get("code") == "REQUEST_RACE":
                state["hold"] = projection_hold
                _record_held_card_request(notion, source_id, row, state, latest_item,
                                          latest_issue, facts, config, status_observed_at)
                _verify_issue_state_write(notion, source_id, row, state)
                _save_status_request(notion, source_id, row, state)
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue
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
        if config.get("bidirectional_enabled") and kind == "Issue":
            latest_page = notion.request("GET", f"/pages/{identifier(row.get('id'))}")
            bound_page(latest_page, source_id)
            latest_state = decode_internal(
                read_text(latest_page, "동기화 내부 상태"), kind="issue",
                project_id=config["project_id"], object_id=issue_id)
            approving_older_result = bool(
                issue_number in resume_numbers and state.get("deferred_request") and
                state.get("request") and
                state["request"].get("id") == expected_projection.get("request_id"))
            latest_task = read_select(latest_page, "작업 상태")
            latest_deferred = (latest_state or {}).get("deferred_request") or {}
            preserve_deferred_status = (
                latest_task if approving_older_result and latest_state is not None and
                _deferred_card_matches(latest_state, expected_projection, latest_task)
                else None)
            older_result_card_values = {expected_projection.get("expected_notion_status")}
            if approving_older_result:
                older_result_card_values.update({
                    expected_projection.get("result_notion_status"),
                    latest_deferred.get("target"),
                })
            if (approving_older_result and latest_state is not None and
                    latest_task not in older_result_card_values):
                # PM approval of A cannot consume a card move C that is already
                # visible. Replace only the deferred snapshot (B -> latest C), keep
                # A's operation checkpoint, and require a fresh PM decision later.
                state = latest_state
                state["resume"] = None
                race = _new_hold(
                    "REQUEST_RACE",
                    "이전 결과를 PM이 재개하는 동안 새 Notion 상태 이동이 확인되어 최신 이동을 보류했습니다.",
                    current_projection_fingerprint)
                _persist_observed_card_race(
                    notion, source_id, latest_page, metadata, state,
                    item=latest_item, issue=latest_issue, facts=facts,
                    config=config, now=now, message=race["message"])
                counts["held"] += 1
                issue_rows_for_summary.append((latest_page, state))
                continue
            card_matches_older_operation = latest_task in older_result_card_values
            if (not card_matches_older_operation or latest_state is None or
                    (latest_state.get("request") or {}).get("id") !=
                    expected_projection.get("request_id")):
                display_race = _new_hold(
                    "REQUEST_RACE",
                    "Project 결과 확인 뒤 Notion에 새 상태 이동이 있어 이전 표시 복구를 멈췄습니다.",
                    current_projection_fingerprint)
                state["hold"] = display_race
                if (state.get("request") and
                        state["request"].get("id") == expected_projection.get("request_id")):
                    state["request"]["phase"] = "held"
                    state["request"]["reason"] = display_race["message"]
                _verify_issue_state_write(notion, source_id, latest_page, state)
                _display_hold(notion, source_id, latest_page, metadata, state,
                              display_race, now, preserve_task_status=True)
                counts["held"] += 1
                issue_rows_for_summary.append((latest_page, state))
                continue
            row = latest_page
            # First verify the visible projection while the durable checkpoint remains.
            checkpoint_written = _write_checkpointed_projection(
                notion, source_id, row, metadata, plan, state, now,
                item=latest_item, issue=latest_issue, facts=facts, config=config,
                preserve_task_status=preserve_deferred_status)
            if not checkpoint_written:
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue
            finalized = _finalize_bidirectional_projection(
                notion, source_id, row, state, issue=latest_issue, item=latest_item,
                linked_refs=latest_refs, config=config, notion_status=plan.get("target"),
                observed_at=status_observed_at, approved_resume=approved_resume,
                facts=facts, metadata=metadata, now=now,
                preserve_task_status=preserve_deferred_status)
            if not finalized:
                counts["held"] += 1
                issue_rows_for_summary.append((row, state))
                continue
        else:
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
    if notification_result is not None:
        report_mark = observation_clock.mark_snapshot()
        report_observed_at, report_uncertainty = observation_clock.verify_snapshot(
            report_mark, lambda: observation_clock.fetch_fresh_date(github))
        awaiting_add_readback = any(
            bool(state and state.get("pending") and
                 state["pending"].get("kind") == "add" and
                 state.get("readback") and
                 state["readback"].get("returned_item_id") and
                 not state["readback"].get("validated"))
            for _, state in issue_rows_for_summary)
        notification_result.update(scan_complete=True,
                                   holds=notification_report.collect_holds(issue_rows_for_summary),
                                   observed_at=report_observed_at,
                                   observation_uncertainty_seconds=report_uncertainty)
        if holds or counts.get("failed", 0) or awaiting_add_readback:
            notification_result["partial"] = True
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
    bidirectional = env.get("NOTION_BIDIRECTIONAL_ENABLED", "false")
    require(bidirectional in {"true", "false"},
            "NOTION_BIDIRECTIONAL_ENABLED는 true 또는 false여야 합니다")
    return {"project_id": env["PROJECT_ID"], "owner_id": env["PROJECT_OWNER_ID"],
            "status_field_id": env["PROJECT_STATUS_FIELD_ID"],
            "status_options": gp.parse_status_options(env["PROJECT_STATUS_OPTIONS"]),
            "notion_source_id": identifier(env["NOTION_DATA_SOURCE_ID"]),
            "notion_control_id": identifier(env["NOTION_CONTROL_PAGE_ID"]),
            "bidirectional_enabled": bidirectional == "true"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="전체 조회·검증만 하고 어떤 API에도 쓰지 않음")
    parser.add_argument("--diagnose-date-readback", action="store_true",
                        help="dry-run에서 Issue #1 metadata readback을 redacted 진단")
    parser.add_argument("--resolve-issue-numbers", default="",
                        help="PM 수동 재개 대상 Issue 번호를 comma-separated로 지정")
    parser.add_argument("--notification-report", help="비밀 없는 Discord 알림 결과 파일")
    args = parser.parse_args(argv)
    if args.diagnose_date_readback and not args.dry_run:
        print("--diagnose-date-readback requires --dry-run", file=sys.stderr)
        return 2
    env = os.environ
    event = env.get("GITHUB_EVENT_NAME")
    enabled = env.get("NOTION_SYNC_ENABLED") == "true"
    explicit_dispatch_dry_run = event == "workflow_dispatch" and args.dry_run
    if not enabled and not explicit_dispatch_dry_run:
        if args.notification_report:
            notification_report.write(args.notification_report, env, kind="skipped",
                                      dry_run=args.dry_run)
        print("동기화 비활성: NOTION_SYNC_ENABLED=true 설정 후 실행하세요.")
        return 0
    try:
        notification_result = {}
        config = _load_config(env)
        gh = gp.GraphQLClient(env["GITHUB_TOKEN"])
        rest = gp.RESTClient(env["GITHUB_TOKEN"])
        project_client = gp.GraphQLClient(env["PROJECT_TOKEN"])
        notion = NotionClient(env["NOTION_TOKEN"])
        counts = sync(gh, rest, project_client, notion, config, dry_run=args.dry_run,
                      diagnose_date_readback=args.diagnose_date_readback,
                      resolve_issue_numbers=args.resolve_issue_numbers, env=env,
                      notification_result=notification_result)
        if args.notification_report:
            full = not args.dry_run and notification_result.get("scan_complete") is True
            holds = notification_result.get("holds", []) if full else []
            notification_report.write(args.notification_report, env,
                                      kind=("partial" if notification_result.get("partial") or
                                            notification_result.get("holds") or
                                            counts.get("failed", 0) else "complete")
                                      if full else "skipped",
                                      dry_run=args.dry_run, scan_complete=full, holds=holds,
                                      observed_at=notification_result.get("observed_at") if full else None,
                                      observation_uncertainty_seconds=(
                                          notification_result.get("observation_uncertainty_seconds")
                                          if full else None))
        print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
        return 0
    except (SyncError, gp.SyncError) as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        # Never emit request bodies, remote payloads or tracebacks that can contain secrets.
        print("동기화 실패: 원격 응답 또는 스키마를 확인하세요. 기존 데이터는 보존됩니다.", file=sys.stderr)
    if args.notification_report:
        try:
            notification_report.write(args.notification_report, env, kind="failed",
                                      dry_run=args.dry_run)
        except Exception:
            print("알림 결과 파일 기록 실패", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
