"""Minimal fixed-host Notion transport; reads are the default capability."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from uuid import UUID

from github_project import NoRedirect, SyncError, require


def identifier(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise SyncError("Notion ID format invalid") from None


def text(value):
    require(isinstance(value, str) and len(value) <= 20000, "Notion text invalid")
    return {"rich_text": [{"type": "text", "text": {"content": value[i:i + 2000]}}
                          for i in range(0, len(value), 2000)]}


def read_text(page, name):
    prop = page["properties"][name]
    require(isinstance(prop.get("rich_text"), list), "Notion text property invalid")
    return "".join(part.get("plain_text", part.get("text", {}).get("content", ""))
                   for part in prop["rich_text"])


def read_select(page, name):
    value = page["properties"][name]["select"]
    return value.get("name") if value else None


def bound_page(page, source_id):
    require(isinstance(page, dict) and page.get("object") == "page",
            "Notion page invalid")
    identifier(page.get("id"))
    require(identifier(page.get("parent", {}).get("data_source_id")) == source_id,
            "Notion data source mismatch")


class NotionClient:
    def __init__(self, token, *, allow_apply=False, opener=None, sleep=time.sleep):
        require(isinstance(token, str) and token, "NOTION_TOKEN missing")
        self.allow_apply = allow_apply
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.sleep = sleep
        self.headers = {"Authorization": f"Bearer {token}",
                        "Content-Type": "application/json", "Notion-Version": "2026-03-11"}

    def request(self, method, path, payload=None):
        query = method == "POST" and re.fullmatch(r"/data_sources/[0-9a-f-]+/query", path)
        read = method == "GET" and re.fullmatch(r"/(?:pages|data_sources)/[0-9a-f-]+", path)
        write = method == "PATCH" and re.fullmatch(r"/pages/[0-9a-f-]+", path)
        require(bool(query or read or (write and self.allow_apply)),
                "Notion operation not allowed")
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        self.sleep(0.4)
        request = urllib.request.Request("https://api.notion.com/v1" + path,
                                         data=body, headers=self.headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                result = json.load(response)
            require(isinstance(result, dict), "Notion response invalid")
            return result
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise SyncError(f"Notion HTTP {status}; stopped") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeError):
            raise SyncError("Notion connection or response failed") from None


def query_all(notion, source_id, archived):
    rows, cursor, seen = [], None, set()
    while True:
        payload = {"page_size": 100, "is_archived": archived}
        if cursor is not None:
            payload["start_cursor"] = cursor
        response = notion.request("POST", f"/data_sources/{source_id}/query", payload)
        require(response.get("object") == "list" and response.get("type") == "page_or_data_source"
                and isinstance(response.get("page_or_data_source"), dict),
                "Notion query envelope invalid")
        batch = response.get("results")
        require(isinstance(batch, list) and type(response.get("has_more")) is bool
                and "next_cursor" in response, "Notion query page invalid")
        status = response.get("request_status", {"type": "complete"})
        require(isinstance(status, dict) and status.get("type") == "complete"
                and ("incomplete_reason" not in status or
                     status["incomplete_reason"] == "query_result_limit_reached"),
                "Notion query incomplete")
        rows.extend(batch)
        require(len(rows) < 10000, "Notion query limit reached")
        cursor = response["next_cursor"]
        if not response["has_more"]:
            require(cursor is None, "Notion terminal cursor invalid")
            return rows
        require(isinstance(cursor, str) and cursor and cursor not in seen,
                "Notion query cursor invalid")
        seen.add(cursor)


def check_schema(notion, source_id):
    source = notion.request("GET", f"/data_sources/{source_id}")
    require(identifier(source.get("id")) == source_id, "Notion schema ID mismatch")
    props = source.get("properties", {})
    for name, kind in (("동기화 키", "rich_text"), ("번호", "number"), ("종류", "select"),
                       ("작업 상태", "select"), ("확인 필요", "rich_text"),
                       ("동기화 내부 상태", "rich_text")):
        require(props.get(name, {}).get("type") == kind, "Notion schema property mismatch")
    options = props["작업 상태"]["select"]["options"]
    from sync_rules import STATUSES
    require({option["name"] for option in options} == set(STATUSES),
            "Notion task status options mismatch")
