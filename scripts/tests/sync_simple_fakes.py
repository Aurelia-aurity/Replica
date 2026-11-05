"""In-memory wire-shaped APIs; production GitHub parsers remain in the path."""
import copy
import json
from datetime import datetime, timezone

import github_project as gp
import sync_engine as legacy

SOURCE = "00000000-0000-4000-8000-000000000001"
PAGE = "00000000-0000-4000-8000-000000000002"
REPO = "R_replica"
FIELD = "PVTSSF_fixture"
OPTIONS = {**gp.EXPECTED_STATUS_OPTIONS, "취소": "fixture-cancel"}
CONFIG = {"project_id": gp.EXPECTED_PROJECT_ID, "owner_id": gp.EXPECTED_PROJECT_OWNER_ID,
          "status_field_id": FIELD, "status_options": OPTIONS, "notion_source_id": SOURCE}
NOW = "2026-11-05T00:00:00Z"


def conn(nodes, total=False):
    result = {"nodes": copy.deepcopy(nodes),
              "pageInfo": {"hasNextPage": False, "endCursor": None}}
    if total:
        result["totalCount"] = len(nodes)
    return result


def issue():
    return {"id": "I_80", "databaseId": 800, "number": 80, "title": "Synthetic",
            "url": f"https://github.com/{gp.REPOSITORY}/issues/80",
            "createdAt": NOW, "updatedAt": NOW, "closedAt": None, "state": "OPEN",
            "stateReason": None, "duplicateOf": None, "author": {"login": "fixture"},
            "assignees": conn([]), "labels": conn([]), "timelineItems": conn([]),
            "closedByPullRequestsReferences": conn([]), "linked_prs": [], "reopens": []}


def text(value):
    return {"rich_text": [{"type": "text", "text": {"content": value}}]}


def checkpoint(github, last="백로그"):
    """Build a synthetic checkpoint with the existing engine's own hash producer."""
    row = github.issues[0]
    snapshot = {"repository_node_id": REPO,
                "pulls": {pr["legacy_issue_id"]: pr for pr in github.pulls}}
    refs, eligible = legacy._linked_pr_facts(row, snapshot)
    saved = legacy.new_issue_state(row["databaseId"], CONFIG["project_id"])
    saved["project_item_id"] = "PVTI_80"
    saved["review_pr_hash"] = legacy._digest([pr["id"] for pr in eligible])
    saved["baseline"] = legacy.status_sync.make_baseline(
        last, {"field_id": FIELD, "option_id": OPTIONS[last],
               "value_id": "PVTSV_fixture", "updated_at": NOW},
        "PVTI_80", legacy._status_facts_fingerprint(row, refs), NOW)
    saved["hold"] = {"code": "STATUS_CONFLICT", "message": "not for artifact"}
    return json.dumps(saved)


def page(status="백로그", internal=""):
    return {"object": "page", "id": PAGE, "archived": False, "in_trash": False,
            "parent": {"type": "data_source_id", "data_source_id": SOURCE},
            "properties": {"동기화 키": text(f"gh:{gp.REPOSITORY_ID}:issue:800"),
                           "종류": {"select": {"name": "Issue"}}, "번호": {"number": 80},
                           "작업 상태": {"select": {"name": status} if status else None},
                           "동기화 내부 상태": text(internal), "확인 필요": text("")}}


def item(status="백로그"):
    return {"id": "PVTI_80", "isArchived": False,
            "content": {"__typename": "Issue", "id": "I_80", "databaseId": 800,
                        "number": 80, "repository": repository()},
            "fieldValues": conn([] if status is None else [status_value(status)])}


def repository():
    return {"id": REPO, "databaseId": gp.REPOSITORY_ID, "nameWithOwner": gp.REPOSITORY,
            "owner": {"id": "O_fixture", "login": "Aurelia-aurity"}}


def status_value(name):
    return {"__typename": "ProjectV2ItemFieldSingleSelectValue", "id": "PVTSV_fixture",
            "updatedAt": NOW, "field": {"id": FIELD}, "optionId": OPTIONS[name], "name": name}


class GitHub:
    def __init__(self):
        self.issues = [issue()]
        self.pulls = []
        self.calls = []

    def request(self, query, variables=None, *, mutation=False):
        self.calls.append((query, variables, mutation))
        if mutation:
            raise AssertionError("Repository mutation forbidden")
        if "viewer" in query:
            return {"viewer": {"id": "U_fixture"}}, datetime(2026, 11, 5, tzinfo=timezone.utc)
        repo = repository()
        if query == gp.ISSUES_QUERY:
            repo["issues"] = conn(self.issues, total=True)
        elif query == gp.PULLS_QUERY:
            repo["pullRequests"] = conn(self.pulls, total=True)
        else:
            raise AssertionError("Unexpected GitHub query")
        return {"repository": repo}, None


class REST:
    def __init__(self, github):
        self.github = github

    def request(self, path):
        if path == f"/repos/{gp.REPOSITORY}":
            return {"id": gp.REPOSITORY_ID, "full_name": gp.REPOSITORY}
        if "/issues?" in path:
            return [{"id": row.get("legacy_issue_id", row["databaseId"]), "node_id": row["id"],
                     "number": row["number"], "state": "open" if row["state"] == "OPEN" else "closed",
                     **({"pull_request": {}} if row in self.github.pulls else {})}
                    for row in self.github.issues + self.github.pulls]
        if "/pulls/" in path:
            row = next(row for row in self.github.pulls if path.endswith("/" + str(row["number"])))
            return {"id": row["databaseId"], "node_id": row["id"], "number": row["number"],
                    "state": "open" if row["state"] == "OPEN" else "closed",
                    "draft": row["isDraft"], "merged": row["state"] == "MERGED",
                    "base": {"ref": row["baseRefName"], "repo": {
                        "id": gp.REPOSITORY_ID, "node_id": REPO}}}
        raise AssertionError("Unexpected REST request")


class Project:
    def __init__(self, status="백로그"):
        self.items = [item(status)]
        self.calls = []

    def request(self, query, variables=None, *, mutation=False):
        self.calls.append((query, copy.deepcopy(variables), mutation))
        if mutation:
            raise AssertionError("Project mutation forbidden")
        node = {"id": gp.EXPECTED_PROJECT_ID, "number": 4,
                "owner": {"id": gp.EXPECTED_PROJECT_OWNER_ID, "login": gp.PROJECT_OWNER_LOGIN}}
        if query == gp.PROJECT_FIELDS_QUERY:
            node["fields"] = conn([{"__typename": "ProjectV2SingleSelectField", "id": FIELD,
                                    "name": "Status", "options": [
                                        {"name": name, "id": option} for name, option in OPTIONS.items()]}])
        elif query == gp.PROJECT_ITEMS_QUERY:
            node["items"] = conn(self.items, total=True)
        else:
            raise AssertionError("Unexpected Project query")
        return {"node": node}, None


class Notion:
    def __init__(self, status="백로그", internal=""):
        self.pages = [page(status, internal)]
        self.calls = []
        self.query_transform = lambda response: response

    def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "GET" and path == f"/data_sources/{SOURCE}":
            props = {name: {"type": kind} for name, kind in (
                ("동기화 키", "rich_text"), ("번호", "number"), ("종류", "select"),
                ("확인 필요", "rich_text"), ("동기화 내부 상태", "rich_text"))}
            props["작업 상태"] = {"type": "select", "select": {
                "options": [{"name": name} for name in OPTIONS]}}
            return {"id": SOURCE, "properties": props}
        if method == "POST" and path == f"/data_sources/{SOURCE}/query":
            rows = [page for page in self.pages if page["archived"] == payload["is_archived"]]
            return self.query_transform({"object": "list", "type": "page_or_data_source",
                                        "page_or_data_source": {}, "results": copy.deepcopy(rows),
                                        "has_more": False, "next_cursor": None})
        raise AssertionError("Unexpected Notion request")
