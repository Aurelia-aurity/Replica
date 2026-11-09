"""In-memory wire-shaped APIs; production GitHub parsers remain in the path."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import github_project as gp
from sync_simple_metadata import metadata
from sync_simple_state import CHECKPOINT

SOURCE = "00000000-0000-4000-8000-000000000001"
PAGE = "00000000-0000-4000-8000-000000000002"
REPO = "R_replica"
FIELD = "PVTSSF_fixture"
OPTIONS = {**gp.EXPECTED_STATUS_OPTIONS, "취소": "fixture-cancel"}
CONFIG = {"project_id": gp.EXPECTED_PROJECT_ID, "owner_id": gp.EXPECTED_PROJECT_OWNER_ID,
          "status_field_id": FIELD, "status_options": OPTIONS, "notion_source_id": SOURCE}
NOW = "2026-11-05T00:00:00Z"
LEGACY = json.loads((Path(__file__).parent / "fixtures" / "sync_legacy.json").read_text())


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
    """Previously generated legacy oracle, independent of the new hash producer."""
    saved = copy.deepcopy(LEGACY["complex" if github.issues[0]["reopens"] else "basic"])
    saved["baseline"]["notion_status"] = last
    saved["baseline"]["project"]["option_id"] = OPTIONS[last]
    return json.dumps(saved)


def page(status="백로그", internal=""):
    return {"object": "page", "id": PAGE, "archived": False, "in_trash": False,
            "parent": {"type": "data_source_id", "data_source_id": SOURCE},
            "properties": {**metadata("Issue", issue(), f"gh:{gp.REPOSITORY_ID}:issue:800"),
                           "동기화 시각": {"date": {"start": NOW}},
                           "종료 사유": {"select": None}, "대표 이슈": {"url": None},
                           "메모": text("User-owned synthetic memo"),
                           "일정": {"date": {"start": "2026-12-01"}},
                           CHECKPOINT: text(""),
                           "동기화 키": text(f"gh:{gp.REPOSITORY_ID}:issue:800"),
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
            start = (int(path.rsplit("page=", 1)[1]) - 1) * 100
            return [{"id": row.get("legacy_issue_id", row["databaseId"]), "node_id": row["id"],
                     "number": row["number"], "state": "open" if row["state"] == "OPEN" else "closed",
                     **({"pull_request": {}} if row in self.github.pulls else {})}
                    for row in (self.github.issues + self.github.pulls)[start:start + 100]]
        if "/pulls?" in path:
            start = (int(path.rsplit("page=", 1)[1]) - 1) * 100
            return [{"id": row["databaseId"], "node_id": row["id"], "number": row["number"],
                    "state": "open" if row["state"] == "OPEN" else "closed",
                    "draft": row["isDraft"], "merged_at": row["mergedAt"],
                    "base": {"ref": row["baseRefName"], "repo": {
                        "id": gp.REPOSITORY_ID, "node_id": REPO}}}
                    for row in self.github.pulls[start:start + 100]]
        raise AssertionError("Unexpected REST request")


class Project:
    def __init__(self, status="백로그"):
        self.items = [item(status)]
        self.calls = []
        self.source_issues = {"I_80": issue()}
        self.before_request = lambda query, variables: None

    def request(self, query, variables=None, *, mutation=False):
        self.calls.append((query, copy.deepcopy(variables), mutation))
        self.before_request(query, variables)
        if mutation:
            if query == gp.SET_STATUS_MUTATION:
                row = next(row for row in self.items if row["id"] == variables["item"])
                assert variables["project"] == gp.EXPECTED_PROJECT_ID and variables["field"] == FIELD
                name = next(name for name, option in OPTIONS.items() if option == variables["option"])
                row["fieldValues"] = conn([status_value(name)])
                return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": row["id"]}}}, None
            if query == gp.ADD_ISSUE_MUTATION:
                source = self.source_issues[variables["content"]]
                assert not any(row["content"]["id"] == source["id"] for row in self.items)
                row = item(None)
                row["id"] = "PVTI_" + str(source["number"])
                row["content"].update(id=source["id"], databaseId=source["databaseId"],
                                      number=source["number"])
                self.items.append(row)
                return {"addProjectV2ItemById": {"item": {"id": row["id"]}}}, None
            raise AssertionError("Unexpected Project mutation")
        node = {"id": gp.EXPECTED_PROJECT_ID, "number": 4,
                "owner": {"id": gp.EXPECTED_PROJECT_OWNER_ID, "login": gp.PROJECT_OWNER_LOGIN}}
        if query == gp.PROJECT_FIELDS_QUERY:
            node["fields"] = conn([{"__typename": "ProjectV2SingleSelectField", "id": FIELD,
                                    "name": "Status", "options": [
                                        {"name": name, "id": option} for name, option in OPTIONS.items()]}])
        elif query == gp.PROJECT_ITEMS_QUERY:
            node["items"] = conn(self.items, total=True)
        elif query == gp.PROJECT_ITEM_QUERY:
            row = next((row for row in self.items if row["id"] == variables["id"]), None)
            return {"node": None if row is None else {
                **copy.deepcopy(row), "project": {
                    "id": gp.EXPECTED_PROJECT_ID, "number": 4,
                    "owner": {"id": gp.EXPECTED_PROJECT_OWNER_ID}}}}, None
        else:
            raise AssertionError("Unexpected Project query")
        return {"node": node}, None


class Notion:
    def __init__(self, status="백로그", internal=""):
        self.pages = [page(status, internal)]
        self.calls = []
        self.query_transform = lambda response: response
        self.before_request = lambda method, path, payload: None

    def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        self.before_request(method, path, payload)
        if method == "GET" and path == f"/data_sources/{SOURCE}":
            props = {name: {"type": next(iter(value))}
                     for name, value in page()["properties"].items()}
            props["작업 상태"] = {"type": "select", "select": {
                "options": [{"name": name} for name in OPTIONS]}}
            for name, names in (("종류", ("Issue", "PR", "Sync")),
                                ("GitHub 상태", ("Open", "Closed", "Draft", "Merged")),
                                ("종료 사유", ("완료", "미계획", "중복", "확인 필요"))):
                props[name] = {"type": "select", "select": {
                    "options": [{"name": value} for value in names]}}
            return {"id": SOURCE, "properties": props}
        if method == "POST" and path == f"/data_sources/{SOURCE}/query":
            rows = [page for page in self.pages if page["archived"] == payload["is_archived"]]
            return self.query_transform({"object": "list", "type": "page_or_data_source",
                                        "page_or_data_source": {}, "results": copy.deepcopy(rows),
                                        "has_more": False, "next_cursor": None})
        if method == "POST" and path == "/pages":
            assert payload["parent"] == {"data_source_id": SOURCE}
            row = page()
            row["id"] = f"00000000-0000-4000-8000-{100 + len(self.pages):012d}"
            row["properties"].update(copy.deepcopy(payload["properties"]))
            self.pages.append(row)
            return copy.deepcopy(row)
        if path.startswith("/pages/"):
            row = next(row for row in self.pages if row["id"] == path[len("/pages/"):])
            if method == "PATCH":
                row["properties"].update(copy.deepcopy(payload["properties"]))
            elif method != "GET":
                raise AssertionError("Unexpected page operation")
            return copy.deepcopy(row)
        raise AssertionError("Unexpected Notion request")
