#!/usr/bin/env python3
"""Mirror one repository's issue/PR metadata; never edit team notes or page bodies."""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from uuid import UUID

REPOSITORY = "Aurelia-aurity/Replica"
REPOSITORY_ID = 1392442366
CONTROL_KEY = "replica-sync-control"
SCHEMA = {
    "제목": "title", "종류": "select", "번호": "number", "GitHub URL": "url",
    "GitHub 상태": "select", "작성자": "rich_text", "담당자": "rich_text",
    "라벨": "rich_text", "GitHub 수정": "date", "동기화 키": "rich_text",
    "동기화 시각": "date", "Pending create": "rich_text",
    "작업 상태": "select", "일정": "date", "메모": "rich_text",
}
OPTIONS = {"종류": {"Issue", "PR", "Sync"},
           "GitHub 상태": {"Open", "Closed", "Draft", "Merged"},
           "작업 상태": {"백로그", "진행 중", "검토 중", "완료"}}
KEY_RE = re.compile(r"gh:([1-9][0-9]*):issue:([1-9][0-9]*)\Z")


class SyncError(Exception):
    """Only fixed, non-sensitive diagnostic messages may leave this module."""


def require(condition, message="원격 데이터 검증 실패"):
    if not condition:
        raise SyncError(message)


def identifier(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise SyncError("Notion ID 형식 오류") from None


def positive(value):
    require(type(value) is int and value > 0, "GitHub ID/번호 형식 오류")
    return value


def key_for(repo_id, issue_id):
    return f"gh:{positive(repo_id)}:issue:{positive(issue_id)}"


def valid_key(value):
    match = KEY_RE.fullmatch(value)
    return bool(match and int(match[1]) == REPOSITORY_ID)


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(result.tzinfo is not None, "시각에 timezone이 없습니다")
        return result
    except (ValueError, AttributeError, TypeError):
        raise SyncError("시각 형식 오류") from None


def text_property(value, kind="rich_text"):
    require(isinstance(value, str) and len(value) <= 20000, "텍스트 크기/형식 오류")
    return {kind: [{"type": "text", "text": {"content": value[i:i + 2000]}}
                   for i in range(0, len(value), 2000)]}


def read_text(page, name):
    prop = page["properties"][name]
    return "".join(part.get("plain_text", part.get("text", {}).get("content", ""))
                   for part in prop["rich_text"])


def array_text(values):
    require(all(isinstance(v, str) for v in values))
    return json.dumps(sorted(set(values)), ensure_ascii=False)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, service, token, *, opener=None, sleep=time.sleep):
        require(service in {"github", "notion"})
        self.base = {"github": "https://api.github.com", "notion": "https://api.notion.com/v1"}[service]
        self.service, self.sleep = service, sleep
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.headers.update({"Notion-Version": "2026-03-11"} if service == "notion" else
                            {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"})

    def request(self, method, path, payload=None, *, create=False):
        require(path.startswith("/") and not path.startswith("//"), "API 경로 오류")
        url = self.base + path
        require(urllib.parse.urlsplit(url).hostname == urllib.parse.urlsplit(self.base).hostname)
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        attempts = 1 if create else 4
        for attempt in range(attempts):
            self.sleep(0.4)
            request = urllib.request.Request(url, data=data, headers=self.headers, method=method)
            try:
                with self.opener.open(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                # Inspect classification only; never print error bodies, request or credentials.
                blocked = False
                if exc.code == 429:
                    try:
                        body = json.loads(exc.read(16384))
                        blocked = body.get("additional_data", {}).get("rate_limit_reason") == "public_api_request_blocked"
                    except (ValueError, AttributeError):
                        pass
                exc.close()
                retryable = exc.code in {429, 529, 500, 502, 503, 504} and not blocked
                if create or not retryable or attempt == attempts - 1:
                    raise SyncError(f"{self.service} HTTP {exc.code}; 실행 중단") from None
                header = exc.headers.get("Retry-After")
                try:
                    delay = int(header) if header is not None else 2 ** attempt + random.random()
                except ValueError:
                    raise SyncError("Retry-After 형식 오류") from None
                require(0 <= delay <= 60, "긴 Retry-After; 다음 실행에서 재시도")
                self.sleep(delay)
            except (urllib.error.URLError, TimeoutError, OSError):
                if create or attempt == attempts - 1:
                    raise SyncError(f"{self.service} 연결/응답 실패; 실행 중단") from None
                self.sleep(2 ** attempt + random.random())
            except (ValueError, UnicodeError):
                raise SyncError(f"{self.service} 응답 형식 오류") from None


def github_snapshot(api):
    prefix = f"/repos/{REPOSITORY}"
    repo = api.request("GET", prefix)
    require(repo["id"] == REPOSITORY_ID and repo["full_name"].casefold() == REPOSITORY.casefold(),
            "동기화 저장소 식별자 불일치")
    result, page = {}, 1
    while True:
        rows = api.request("GET", f"{prefix}/issues?state=all&sort=created&direction=asc&per_page=100&page={page}")
        require(isinstance(rows, list))
        for issue in rows:
            key = key_for(repo["id"], issue["id"])
            require(key not in result, "GitHub snapshot 중복; 다시 실행")
            number = positive(issue["number"])
            kind, source = "Issue", issue
            if "pull_request" in issue:
                kind = "PR"
                source = api.request("GET", f"{prefix}/pulls/{number}")
                require(source["number"] == number)
                require(source["base"]["repo"]["id"] == REPOSITORY_ID)
            require(source["state"] in {"open", "closed"})
            state = "Open" if source["state"] == "open" else "Closed"
            if kind == "PR":
                state = "Merged" if source["merged"] else (
                    "Draft" if source["state"] == "open" and source["draft"] else state)
            updated = source["updated_at"]
            timestamp(updated)
            props = {
                "제목": text_property(source["title"], "title"), "종류": {"select": {"name": kind}},
                "번호": {"number": number},
                "GitHub URL": {"url": f"https://github.com/{REPOSITORY}/{'pull' if kind == 'PR' else 'issues'}/{number}"},
                "GitHub 상태": {"select": {"name": state}},
                "작성자": text_property((source.get("user") or {}).get("login", "")),
                "담당자": text_property(array_text([a["login"] for a in source["assignees"]])),
                "라벨": text_property(array_text([a["name"] for a in source["labels"]])),
                "GitHub 수정": {"date": {"start": updated}}, "동기화 키": text_property(key),
            }
            result[key] = props
        if len(rows) < 100:
            return result
        page += 1


def query_all(api, source_id, archived=False):
    rows, cursor, seen = [], None, set()
    while True:
        payload = {"page_size": 100, "is_archived": archived}
        if cursor:
            payload["start_cursor"] = cursor
        response = api.request("POST", f"/data_sources/{source_id}/query", payload)
        require(response.get("request_status", {}).get("type") != "incomplete", "Notion 조회가 불완전합니다")
        rows.extend(response["results"])
        if not response["has_more"]:
            return rows
        cursor = response["next_cursor"]
        require(isinstance(cursor, str) and cursor and cursor not in seen, "Notion cursor 오류")
        seen.add(cursor)


def bound_page(page, source_id):
    require(identifier(page["parent"].get("data_source_id")) == source_id, "Notion 부모 데이터 소스 불일치")
    identifier(page["id"])


def is_archived(page):
    return page.get("is_archived", False) or page.get("archived", False) or page.get("in_trash", False)


def check_control(page, source_id, control_id):
    bound_page(page, source_id)
    require(identifier(page["id"]) == control_id and not is_archived(page), "동기화 관리 행 ID/활성 상태 불일치")
    require(read_text(page, "동기화 키") == CONTROL_KEY and
            page["properties"]["종류"]["select"]["name"] == "Sync", "동기화 관리 행 키/종류 불일치")
    for name, kind in {"번호": "number", "GitHub URL": "url", "GitHub 상태": "select", "GitHub 수정": "date"}.items():
        require(page["properties"][name][kind] is None, "동기화 관리 행 고정 필드는 비어 있어야 합니다")


def preflight(github, notion, source_id, control_id):
    snapshot = github_snapshot(github)
    schema = notion.request("GET", f"/data_sources/{source_id}")
    require(identifier(schema["id"]) == source_id)
    require(set(schema["properties"]) == set(SCHEMA), "Notion 스키마 속성 집합 불일치")
    for name, kind in SCHEMA.items():
        require(schema["properties"].get(name, {}).get("type") == kind, "Notion 스키마 타입 불일치")
    for name, options in OPTIONS.items():
        actual = {o["name"] for o in schema["properties"][name]["select"]["options"]}
        require(options == actual, "Notion select 옵션 집합 불일치")
    control = notion.request("GET", f"/pages/{control_id}")
    check_control(control, source_id, control_id)
    index = {}
    for archived in (False, True):
        for row in query_all(notion, source_id, archived):
            bound_page(row, source_id)
            key = read_text(row, "동기화 키")
            if not key:
                continue  # Unlinked team-created pages are left untouched.
            require(key == CONTROL_KEY or valid_key(key), "동기화 키 형식/저장소 불일치")
            require(key not in index, "동기화 키 중복; 수동 확인 필요")
            require(not archived and not is_archived(row), "동기화 행 보관/휴지통 상태; 복구 필요")
            if key != CONTROL_KEY:
                require(row["properties"]["종류"]["select"]["name"] in {"Issue", "PR"})
                date = row["properties"]["GitHub 수정"]["date"]
                if date:
                    timestamp(date["start"])
            index[key] = row
    require(CONTROL_KEY in index, "동기화 관리 행이 조회되지 않습니다")
    check_control(index[CONTROL_KEY], source_id, control_id)
    pending = read_text(control, "Pending create")
    require(read_text(index[CONTROL_KEY], "Pending create") == pending, "관리 행 snapshot 불일치")
    if pending:
        require(valid_key(pending) and pending in index, "생성 결과 불명; Pending create를 유지하고 수동 조사 필요")
    return snapshot, index, pending


def update_pending(api, source_id, control_id, value):
    api.request("PATCH", f"/pages/{control_id}", {"properties": {"Pending create": text_property(value)}})
    control = api.request("GET", f"/pages/{control_id}")
    check_control(control, source_id, control_id)
    require(read_text(control, "Pending create") == value, "생성 fence readback 불일치; 실행 중단")


def sync(github, notion, source_id, control_id, *, dry_run=False):
    source_id, control_id = identifier(source_id), identifier(control_id)
    snapshot, index, pending = preflight(github, notion, source_id, control_id)
    counts = {"source_items": len(snapshot), "created": 0, "updated": 0, "newer_skipped": 0}
    if dry_run:
        return counts
    if pending:
        update_pending(notion, source_id, control_id, "")
    now = datetime.now(timezone.utc).isoformat()
    for key, properties in snapshot.items():
        if key in index:
            row = index[key]
            date = row["properties"]["GitHub 수정"]["date"]
            if date and timestamp(date["start"]) > timestamp(properties["GitHub 수정"]["date"]["start"]):
                counts["newer_skipped"] += 1
                continue
            notion.request("PATCH", f"/pages/{identifier(row['id'])}",
                           {"properties": {**properties, "동기화 시각": {"date": {"start": now}}}})
            counts["updated"] += 1
        else:
            update_pending(notion, source_id, control_id, key)
            # No retries on create, including definite errors. Persistent fence survives failure.
            created = notion.request("POST", "/pages", {"parent": {"data_source_id": source_id},
                                     "properties": {**properties, "동기화 시각": {"date": {"start": now}}}}, create=True)
            row = notion.request("GET", f"/pages/{identifier(created['id'])}")
            bound_page(row, source_id)
            require(not is_archived(row) and read_text(row, "동기화 키") == key, "생성행 readback 불일치")
            update_pending(notion, source_id, control_id, "")
            index[key] = row
            counts["created"] += 1
    # This timestamp means the complete scan/apply succeeded, not merely a single-row update.
    notion.request("PATCH", f"/pages/{control_id}", {"properties": {"동기화 시각": {"date": {"start": now}}}})
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="조회·검증만 하고 Notion에 쓰지 않음")
    args = parser.parse_args(argv)
    if os.environ.get("NOTION_SYNC_ENABLED") != "true":
        print("동기화 비활성: NOTION_SYNC_ENABLED=true 설정 후 실행하세요.")
        return 0
    names = ("GITHUB_TOKEN", "NOTION_TOKEN", "NOTION_DATA_SOURCE_ID", "NOTION_CONTROL_PAGE_ID")
    if not all(os.environ.get(name) for name in names):
        print("동기화 설정 누락: Actions secret/variables를 확인하세요.", file=sys.stderr)
        return 1
    try:
        counts = sync(API("github", os.environ["GITHUB_TOKEN"]), API("notion", os.environ["NOTION_TOKEN"]),
                      os.environ["NOTION_DATA_SOURCE_ID"], os.environ["NOTION_CONTROL_PAGE_ID"], dry_run=args.dry_run)
        print(json.dumps(counts, ensure_ascii=False))
        return 0
    except SyncError as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        # No traceback/remote payload even when schema changes unexpectedly.
        print("동기화 실패: 원격 응답/스키마를 확인하세요. 기존 데이터는 보존됩니다.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    from sync_engine import main as core_main
    sys.exit(core_main())
