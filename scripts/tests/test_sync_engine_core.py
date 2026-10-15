import copy
import contextlib
import io
import json
import sys
import unittest
import urllib.error
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_project as gp
import notification_report as nr
import observation_clock
import sync_engine as se


SOURCE_ID = "00000000-0000-4000-8000-000000000001"
CONTROL_ID = "00000000-0000-4000-8000-000000000002"
PR_PAGE_ID = "00000000-0000-4000-8000-000000000003"
ISSUE_PAGE_ID = "00000000-0000-4000-8000-000000000004"
SECOND_ISSUE_PAGE_ID = "00000000-0000-4000-8000-000000000005"
REPO_NODE = "R_kgDOGitHubRepo"
ISSUE_NODE = "I_kwDOIssue18"
PR_NODE = "PR_kwDOUv77_s8AAAABHQtAUA"
PR_REST_ID = 5756422334
PR_DATABASE_ID = 4782243920
ISSUE_ID = 555
ISSUE_NUMBER = 18
STATUS_FIELD = "PVTSSF_status_test"
CUTOFF = "2026-10-01T00:00:00Z"
NOW = "2026-10-08T00:00:00Z"


def conn(nodes=(), *, more=False, cursor=None, total=None):
    result = {"nodes": copy.deepcopy(list(nodes)),
              "pageInfo": {"hasNextPage": more, "endCursor": cursor}}
    if total is not None:
        result["totalCount"] = total
    return result


def rich_text_value(value):
    return "".join(part.get("plain_text", part.get("text", {}).get("content", ""))
                   for part in value.get("rich_text", []))


def title_text(page, name="제목"):
    return "".join(part.get("plain_text", part.get("text", {}).get("content", ""))
                   for part in page["properties"][name]["title"])


def notion_patch_properties(writes, page_id):
    return [payload.get("properties", {}) for method, path, payload, _ in writes
            if method == "PATCH" and path == f"/pages/{page_id}"]


def task_status_patch_payloads(writes, page_id):
    return [properties["작업 상태"] for properties in notion_patch_properties(writes, page_id)
            if "작업 상태" in properties]


def request_ui_patch_count(writes, page_id):
    return sum(any(key in properties for key in ("요청 처리", "요청 상태"))
               for properties in notion_patch_properties(writes, page_id))


def status_value(name):
    return {"__typename": "ProjectV2ItemFieldSingleSelectValue",
            "id": f"PVTSV_fixture_{gp.EXPECTED_STATUS_OPTIONS[name]}",
            "updatedAt": "2026-10-01T00:00:00Z",
            "field": {"id": STATUS_FIELD},
            "optionId": gp.EXPECTED_STATUS_OPTIONS[name], "name": name}


def make_issue(*, created_at="2026-09-01T00:00:00Z", state="OPEN", reason=None,
               duplicate=None, reopens=None, linked=None, closed_at=None):
    return {"id": ISSUE_NODE, "databaseId": ISSUE_ID, "number": ISSUE_NUMBER,
            "title": "Original issue", "url": f"https://github.com/{gp.REPOSITORY}/issues/{ISSUE_NUMBER}",
            "repository": {"id": REPO_NODE, "databaseId": gp.REPOSITORY_ID,
                           "nameWithOwner": gp.REPOSITORY},
            "createdAt": created_at, "updatedAt": "2026-10-02T00:00:00Z", "closedAt": closed_at,
            "state": state, "stateReason": reason, "author": {"login": "author"},
            "assignees": conn(), "labels": conn(), "duplicateOf": duplicate,
            "closedByPullRequestsReferences": conn(linked or []),
            "timelineItems": conn(reopens or []), "linked_prs": linked or [],
            "reopens": reopens or []}


def make_pr():
    return {"id": PR_NODE, "databaseId": PR_DATABASE_ID, "number": 19,
            "title": "merged feature", "url": f"https://github.com/{gp.REPOSITORY}/pull/19",
            "createdAt": "2026-09-01T00:00:00Z", "updatedAt": "2026-09-03T00:00:00Z",
            "state": "MERGED", "isDraft": False, "mergedAt": "2026-09-02T00:00:00Z",
            "baseRefName": "main", "baseRepository": {"id": REPO_NODE,
                "databaseId": gp.REPOSITORY_ID, "nameWithOwner": gp.REPOSITORY},
            "author": {"login": "contributor"}, "assignees": conn(), "labels": conn(),
            "closingIssuesReferences": conn()}


class RepoGraph:
    def __init__(self, issues=(), pulls=()):
        self.issues = list(issues)
        self.pulls = list(pulls)
        self.queries = []
        self.mutations = 0
        self.server_time = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.issue_detail_transform = None

    @staticmethod
    def _repo():
        return {"id": REPO_NODE, "databaseId": gp.REPOSITORY_ID,
                "nameWithOwner": gp.REPOSITORY,
                "owner": {"id": "O_owner", "login": "Aurelia-aurity"}}

    def request(self, query, variables=None, *, mutation=False):
        self.queries.append((query, variables or {}, mutation))
        if mutation:
            self.mutations += 1
            raise AssertionError("Repository facts client must remain read-only")
        if "viewer" in query:
            return {"viewer": {"id": "U_viewer", "login": "test"}}, self.server_time
        if "user(login:" in query:
            login = (variables or {}).get("login")
            user_id = gp.EXPECTED_PM_USER_ID if login == "Just-Simple0" else 12345678
            return {"user": {"databaseId": user_id, "login": login}}, None
        repo = self._repo()
        after = (variables or {}).get("after")
        if "pullRequests(first:" in query:
            repo["pullRequests"] = conn(self.pulls if after is None else [],
                                        total=len(self.pulls))
            return {"repository": repo}, None
        if "issues(first:" in query:
            repo["issues"] = conn(self.issues if after is None else [], total=len(self.issues))
            return {"repository": repo}, None
        if "node(id:$id)" in query or "node(id: $id)" in query:
            node_id = (variables or {}).get("id")
            issue = next((row for row in self.issues if row["id"] == node_id), None)
            if issue:
                value = copy.deepcopy(issue)
                if self.issue_detail_transform:
                    value = self.issue_detail_transform(value)
                return {"node": value}, None
        raise AssertionError("Unexpected repository GraphQL query")


class LegacyREST:
    def __init__(self, issues=(), pull=None):
        self.issues = list(issues)
        self.pull = pull
        self.calls = []

    def request(self, path):
        self.calls.append(path)
        if path == f"/repos/{gp.REPOSITORY}":
            return {"id": gp.REPOSITORY_ID, "full_name": gp.REPOSITORY}
        if "/issues?" in path:
            return list(self.issues)
        if (self.pull is not None and
                path.endswith(f"/pulls/{self.pull.get('number')}")):
            return copy.deepcopy(self.pull)
        raise AssertionError("Unexpected legacy REST path")


class ProjectAPI:
    def __init__(self, items=()):
        self.items = list(items)
        self.source_issues = {}
        self.writes = []
        self.status_values = {}
        self.later_field_values = {}
        self.add_calls = 0
        self.add_response_loss_count = 1
        self.add_response_loss = False
        self.add_visibility_delay_snapshots = 0
        self.post_add_snapshot_calls = 0
        self.direct_item_reads = 0
        self.direct_item_visibility_delay_reads = 0
        self.readback_visibility_events = []
        self.fail_post_add_snapshot_once = False
        self.fail_direct_readback_once = False
        self.added_item_ids = []
        self.add_item_transform = None
        self.after_add = None
        self.after_status_write = None
        self.before_status_mutation = None
        self.fail_status_write_once = False
        self.status_mutation_attempts = 0
        self.status_mutation_entries = []
        self.add_result_item_id_override = None
        self.add_response_override = None
        self.next_project_items_override = None
        self.direct_item_transform = None
        self.direct_project_transform = None
        self.sleep_calls = []
        self.sleep = self.sleep_calls.append
        self.item_page_calls = 0
        self.fail_late_item_page = False
        self.item_pages = None

    def request(self, query, variables=None, *, mutation=False):
        variables = variables or {}
        if mutation:
            if query in {gp.SET_STATUS_MUTATION, gp.CLEAR_STATUS_MUTATION}:
                self.status_mutation_attempts += 1
                entry = (query, copy.deepcopy(variables))
                self.status_mutation_entries.append(entry)
                if self.before_status_mutation:
                    self.before_status_mutation(self, query, copy.deepcopy(variables))
            if query == gp.ADD_ISSUE_MUTATION:
                if variables.get("project") != gp.EXPECTED_PROJECT_ID:
                    raise gp.SyncError("synthetic wrong Project target")
                source = self.source_issues.get(variables.get("content"))
                if source is None:
                    raise gp.SyncError("synthetic unknown Issue content target")
                self.add_calls += 1
                self.writes.append((query, copy.deepcopy(variables)))
                if self.add_response_loss:
                    for index in range(self.add_response_loss_count):
                        item = make_project_item(
                            item_id=f"PVTI_added_{self.add_calls}_{index}", option=None,
                            issue_id=source["databaseId"], issue_node=source["id"],
                            number=source["number"])
                        self.items.append(item)
                        self.added_item_ids.append(item["id"])
                    raise gp.SyncError("synthetic lost add response")
                item_id = f"PVTI_added_{self.add_calls}_0"
                item = make_project_item(item_id=item_id, option=None,
                    issue_id=source["databaseId"], issue_node=source["id"],
                    number=source["number"])
                if self.add_item_transform:
                    item = self.add_item_transform(item)
                self.items.append(item)
                self.added_item_ids.append(item["id"])
                if self.after_add:
                    self.after_add(self, item, variables)
                if self.add_response_override is not None:
                    return copy.deepcopy(self.add_response_override), None
                returned_id = self.add_result_item_id_override or item["id"]
                return {"addProjectV2ItemById": {"item": {"id": returned_id}}}, None
            if variables.get("project") != gp.EXPECTED_PROJECT_ID:
                raise gp.SyncError("synthetic wrong Project target")
            item_id = variables["item"]
            item = next((row for row in self.items if row["id"] == item_id), None)
            if item is None or item.get("isArchived"):
                raise gp.SyncError("synthetic wrong Project item target")
            if variables.get("field") != STATUS_FIELD:
                raise gp.SyncError("synthetic wrong Project status field target")
            if query == gp.SET_STATUS_MUTATION:
                option = variables["option"]
                if option not in gp.EXPECTED_STATUS_OPTIONS.values():
                    raise gp.SyncError("synthetic wrong Project status option")
                if self.fail_status_write_once:
                    self.fail_status_write_once = False
                    raise gp.SyncError("synthetic Project SET failed before apply")
                self.writes.append((query, copy.deepcopy(variables)))
                name = next(name for name, value in gp.EXPECTED_STATUS_OPTIONS.items()
                            if value == option)
                value = [{"__typename": "ProjectV2ItemFieldSingleSelectValue",
                          "id": f"PVTSV_{item_id}_{option}",
                          "updatedAt": "2026-10-01T00:00:00Z",
                          "field": {"id": variables["field"]}, "optionId": option, "name": name}]
                self.status_values[item_id] = value
                item = next(row for row in self.items if row["id"] == item_id)
                item["fieldValues"]["nodes"] = value
                if self.after_status_write:
                    self.after_status_write(self, item, variables)
                return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": item_id}}}, None
            if query == gp.CLEAR_STATUS_MUTATION:
                self.writes.append((query, copy.deepcopy(variables)))
                self.status_values[item_id] = []
                item["fieldValues"]["nodes"] = []
                return {"clearProjectV2ItemFieldValue": {"projectV2Item": {"id": item_id}}}, None
            raise AssertionError("Unexpected Project mutation")
        if query == gp.PROJECT_FIELDS_QUERY:
            fields = [
                {"__typename": "ProjectV2SingleSelectField", "id": STATUS_FIELD, "name": "Status",
                 "options": [{"id": value, "name": name}
                             for name, value in gp.EXPECTED_STATUS_OPTIONS.items()]},
                {"__typename": "ProjectV2IterationField", "id": "PVTIF_iteration", "name": "Iteration"},
            ]
            return {"node": self._project(fields=conn(fields))}, None
        if query == gp.PROJECT_ITEMS_QUERY:
            self.item_page_calls += 1
            if self.fail_late_item_page:
                if self.item_page_calls == 1:
                    return {"node": self._project(items=conn([], more=True,
                        cursor="item-page-one", total=2))}, None
                return {"node": self._project(items={"nodes": [],
                    "pageInfo": {"hasNextPage": True}})}, None
            pages = self.item_pages or [self.items]
            hidden_ids = set()
            if variables.get("after") is None and self.add_calls:
                self.post_add_snapshot_calls += 1
                if self.fail_post_add_snapshot_once:
                    self.fail_post_add_snapshot_once = False
                    raise gp.SyncError("synthetic HTTP 529 Project readback failure")
                if self.post_add_snapshot_calls <= self.add_visibility_delay_snapshots:
                    hidden_ids = set(self.added_item_ids)
                if self.next_project_items_override is not None:
                    response_items = self.next_project_items_override
                    self.next_project_items_override = None
                    return {"node": self._project(items=response_items)}, None
            if hidden_ids:
                pages = [[row for row in page if row["id"] not in hidden_ids]
                         for page in pages]
            if self.added_item_ids and variables.get("after") is None:
                returned_id = self.added_item_ids[-1]
                visible = any(row["id"] == returned_id for page in pages for row in page)
                self.readback_visibility_events.append(("list", returned_id, visible))
            after = variables.get("after")
            page_index = 0 if after is None else int(after.rsplit("-", 1)[1])
            rows = pages[page_index]
            more = page_index + 1 < len(pages)
            cursor = f"project-page-{page_index + 1}" if more else None
            total = sum(len(page) for page in pages)
            return {"node": self._project(items=conn(rows, more=more, cursor=cursor,
                                                       total=total))}, None
        if query == gp.ITEM_FIELD_VALUES_QUERY:
            return {"node": {"id": variables["id"], "fieldValues": conn(
                self.later_field_values.get(variables["id"], []))}}, None
        if query == gp.PROJECT_ITEM_QUERY:
            self.direct_item_reads += 1
            item_id = variables["id"]
            if self.direct_item_visibility_delay_reads > 0:
                self.direct_item_visibility_delay_reads -= 1
                self.readback_visibility_events.append(("direct", item_id, False))
                return {"node": None}, None
            if self.fail_direct_readback_once:
                self.fail_direct_readback_once = False
                raise gp.SyncError("synthetic HTTP 529 direct item readback failure")
            item = next((row for row in self.items if row["id"] == item_id), None)
            if item is None:
                self.readback_visibility_events.append(("direct", item_id, False))
                return {"node": None}, None
            if self.direct_item_transform:
                item = self.direct_item_transform(copy.deepcopy(item))
            value = self.status_values.get(item_id, item["fieldValues"]["nodes"])
            self.readback_visibility_events.append(("direct", item_id, True))
            project = {"id": gp.EXPECTED_PROJECT_ID, "number": 4,
                       "owner": {"id": gp.EXPECTED_PROJECT_OWNER_ID}}
            if self.direct_project_transform:
                project = self.direct_project_transform(copy.deepcopy(project))
            return {"node": {"id": item_id, "isArchived": bool(item.get("isArchived")),
                "project": project,
                "content": copy.deepcopy(item["content"]),
                "fieldValues": conn(value)}}, None
        raise AssertionError("Unexpected Project GraphQL query")

    @staticmethod
    def _project(*, fields=None, items=None):
        return {"id": gp.EXPECTED_PROJECT_ID, "number": 4,
                "owner": {"id": gp.EXPECTED_PROJECT_OWNER_ID, "login": "Just-Simple0"},
                "fields": fields or conn(), "items": items or conn([], total=0)}


def make_project_item(*, item_id="PVTI_issue18", option="백로그", archived=False,
                      issue_id=ISSUE_ID, issue_node=ISSUE_NODE, number=ISSUE_NUMBER):
    value = [] if option is None else [{"__typename": "ProjectV2ItemFieldSingleSelectValue",
        "id": f"PVTSV_{item_id}_{option}", "updatedAt": "2026-10-01T00:00:00Z",
        "field": {"id": STATUS_FIELD}, "optionId": gp.EXPECTED_STATUS_OPTIONS[option], "name": option}]
    return {"id": item_id, "isArchived": archived,
            "content": {"__typename": "Issue", "id": issue_node, "databaseId": issue_id,
                        "number": number,
                        "repository": {"id": REPO_NODE, "databaseId": gp.REPOSITORY_ID,
                                       "nameWithOwner": gp.REPOSITORY}},
            "fieldValues": {"nodes": value,
                            "pageInfo": {"hasNextPage": False, "endCursor": None}}}


def notion_page(page_id, *, key="", kind=None, number=None, task=None, internal="", archived=False):
    props = {}
    for name, value_type in se.SCHEMA.items():
        if value_type in {"title", "rich_text"}:
            props[name] = {value_type: []}
        else:
            props[name] = {value_type: None}
    if kind:
        props["종류"] = {"select": {"name": kind}}
    if number is not None:
        props["번호"] = {"number": number}
    if task is not None:
        props["작업 상태"] = {"select": {"name": task}}
    props["동기화 키"] = se.text_property(key)
    props["동기화 내부 상태"] = se.text_property(internal)
    props["Pending create"] = se.text_property("")
    props["제목"] = se.text_property("", "title")
    return {"id": page_id, "parent": {"data_source_id": SOURCE_ID}, "is_archived": archived,
            "archived": archived, "in_trash": False, "properties": props,
            "body": "meeting body"}


class FakeNotion:
    def __init__(self, pages):
        self.pages = {page["id"]: page for page in pages}
        self.calls = []
        self.writes = []
        self.read_snapshots = []
        self.creates = 0
        self.page_size = 100
        self.fail_patch = None
        self.fail_readback_after_patch = set()
        self.fail_get_once = set()
        self.after_patch = None
        self.get_response_transform = None
        self.request_status = {"type": "complete"}
        self.omit_request_status = False
        self.query_response_transform = None
        self.normalize_minute_dates = False

    def _stored_properties(self, properties):
        result = copy.deepcopy(properties)
        if not self.normalize_minute_dates:
            return result
        for name in se.DISPLAY_MINUTE_DATE_PROPERTIES:
            value = result.get(name)
            date_value = value.get("date") if isinstance(value, dict) else None
            if isinstance(date_value, dict) and isinstance(date_value.get("start"), str):
                parsed = se.timestamp(date_value["start"])
                result[name] = {"date": {
                    "start": parsed.strftime("%Y-%m-%dT%H:%M:00.000+00:00"),
                    "end": None, "time_zone": None}}
        return result

    @staticmethod
    def schema():
        properties = {name: {"type": value_type} for name, value_type in se.SCHEMA.items()}
        for name, options in se.OPTIONS.items():
            properties[name]["select"] = {"options": [{"name": value} for value in sorted(options)]}
        return {"id": SOURCE_ID, "properties": properties}

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(payload), kwargs))
        if method == "GET" and path == f"/data_sources/{SOURCE_ID}":
            return self.schema()
        if path.endswith("/query"):
            archived = payload["is_archived"]
            rows = [page for page in self.pages.values() if se.is_archived(page) == archived]
            offset = int(payload.get("start_cursor", "0"))
            end = offset + self.page_size
            batch = copy.deepcopy(rows[offset:end])
            has_more = end < len(rows)
            response = {"object": "list", "type": "page_or_data_source",
                        "page_or_data_source": {}, "results": batch,
                        "has_more": has_more,
                        "next_cursor": str(end) if has_more else None}
            if not self.omit_request_status:
                response["request_status"] = copy.deepcopy(self.request_status)
            if self.query_response_transform:
                response = self.query_response_transform(response, payload)
            return response
        if method == "GET":
            page_id = path.rsplit("/", 1)[1]
            if page_id in self.fail_get_once:
                self.fail_get_once.remove(page_id)
                raise se.SyncError("synthetic page readback failure")
            snapshot = copy.deepcopy(self.pages[page_id])
            self.read_snapshots.append((path, snapshot))
            if self.get_response_transform:
                snapshot = self.get_response_transform(path, snapshot)
            return snapshot
        if method in {"PATCH", "POST"}:
            properties = payload.get("properties") if isinstance(payload, dict) else None
            self._validate_page_properties(properties)
        self.writes.append((method, path, copy.deepcopy(payload), kwargs))
        if method == "POST" and path == "/pages":
            self.creates += 1
            new_id = str(UUID(int=1000 + self.creates))
            page = notion_page(new_id)
            page["properties"].update(self._stored_properties(payload["properties"]))
            self.pages[new_id] = page
            return {"id": new_id}
        if method == "PATCH":
            page_id = path.rsplit("/", 1)[1]
            if self.fail_patch and self.fail_patch(path, payload["properties"]):
                self.fail_patch = None
                raise se.SyncError("synthetic page write failure")
            self.pages[page_id]["properties"].update(
                self._stored_properties(payload["properties"]))
            if self.after_patch:
                self.after_patch(path, payload["properties"], self)
            if page_id in self.fail_readback_after_patch:
                self.fail_readback_after_patch.remove(page_id)
                self.fail_get_once.add(page_id)
            return copy.deepcopy(self.pages[page_id])
        raise AssertionError("Unexpected Notion request")

    @staticmethod
    def _validate_page_properties(properties):
        if not isinstance(properties, dict):
            raise se.SyncError("synthetic Notion API rejected missing properties")
        for property_name, value in properties.items():
            expected_type = se.SCHEMA.get(property_name)
            if expected_type is None or not isinstance(value, dict) or expected_type not in value:
                raise se.SyncError("synthetic Notion API rejected unknown property or type")
            if set(value) != {expected_type}:
                raise se.SyncError("synthetic Notion API rejected property type mismatch")
            if expected_type == "select" and value["select"] is not None:
                selection = value["select"]
                if (not isinstance(selection, dict) or set(selection) != {"name"} or
                        not isinstance(selection["name"], str) or
                        selection["name"] not in se.OPTIONS.get(property_name, set())):
                    raise se.SyncError("synthetic Notion API rejected invalid select option payload")


def control_page(internal=""):
    page = notion_page(CONTROL_ID, key=se.CONTROL_KEY, kind="Sync")
    page["properties"]["GitHub 상태"] = {"select": None}
    page["properties"]["GitHub URL"] = {"url": None}
    page["properties"]["GitHub 수정"] = {"date": None}
    page["properties"]["번호"] = {"number": None}
    page["properties"]["동기화 내부 상태"] = se.text_property(internal)
    return page


def active_issue_page(*, task="백로그", internal="", note="team memo", page_id=ISSUE_PAGE_ID,
                      issue_id=ISSUE_ID, number=ISSUE_NUMBER):
    page = notion_page(page_id, key=se.key_for(gp.REPOSITORY_ID, issue_id),
                       kind="Issue", number=number, task=task, internal=internal)
    page["properties"]["제목"] = se.text_property("#18 old title", "title")
    page["properties"]["일정"] = {"date": {"start": "2026-10-12", "end": "2026-10-13"}}
    page["properties"]["메모"] = se.text_property(note)
    return page


def control_internal():
    return se.canonical_json({"v": 1, "project_id": gp.EXPECTED_PROJECT_ID,
        "migration_cutoff": CUTOFF, "last_success_at": None, "run_id": "prior-run",
        "last_result": None})


def config():
    return {"project_id": gp.EXPECTED_PROJECT_ID, "owner_id": gp.EXPECTED_PROJECT_OWNER_ID,
            "status_field_id": STATUS_FIELD, "status_options": dict(gp.EXPECTED_STATUS_OPTIONS),
            "notion_source_id": SOURCE_ID, "notion_control_id": CONTROL_ID}


def pm_env(run_id, *, actor="Just-Simple0", triggering_actor="Just-Simple0", attempt="1"):
    sha = "a" * 40
    value = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_RUN_ID": str(run_id),
             "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID),
             "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": sha,
             "SYNC_APPROVED_SHA": sha}
    if attempt is not None:
        value["GITHUB_RUN_ATTEMPT"] = attempt
    if actor is not None:
        value["GITHUB_ACTOR"] = actor
    if triggering_actor is not None:
        value["GITHUB_TRIGGERING_ACTOR"] = triggering_actor
    return value


def pr_reference(pr):
    return {"id": pr["id"], "databaseId": pr["databaseId"], "number": pr["number"],
            "state": pr["state"], "isDraft": pr["isDraft"],
            "baseRefName": pr["baseRefName"],
            "baseRepository": {"id": pr["baseRepository"]["id"]},
            "repository": {"id": REPO_NODE}, "url": pr["url"]}


def make_pending_status_state(issue, item_id="PVTI_issue18", *, before="백로그",
                              target="완료", migration_complete=False, facts=None,
                              semantic_v2=False):
    state = se.new_issue_state(issue["databaseId"], gp.EXPECTED_PROJECT_ID)
    state["migration_complete"] = migration_complete
    if migration_complete:
        state["project_item_id"] = item_id
    item = {"id": item_id, "status_option_id": gp.EXPECTED_STATUS_OPTIONS[before]}
    facts = facts or {"repository_node_id": REPO_NODE, "pulls": {}}
    refs, _ = se._linked_pr_facts(issue, facts)
    state["pending"] = {
        "kind": "status", "issue_id": issue["databaseId"], "item_id": item_id,
        "event_id": None, "before_option_id": item["status_option_id"],
        "target_option_id": gp.EXPECTED_STATUS_OPTIONS[target],
        "facts_fingerprint": se._source_fingerprint(issue, refs, item),
        "migration_fingerprint": None, "notion_status": "백로그", "confirmed": False,
        "project_item_id": item_id,
        "checkpoint": {"migration_complete": True, "review_cycle": 0,
                        "review_return_cycle": 0, "reopen_last_id": None,
                        "review_pr_hash": se._digest([])},
    }
    if semantic_v2:
        state["pending"].update({
            "fact_contract": "semantic_v2",
            "semantic_fingerprint": se._pending_semantic_fingerprint(
                issue, refs, item, "status"),
        })
    return state


class SyncEngineCoreTests(unittest.TestCase):
    def setUp(self):
        self.repo_graph = RepoGraph()
        self.rest = LegacyREST()
        self.project = ProjectAPI()
        self.notion = FakeNotion([control_page(control_internal())])

    def run_sync(self, **kwargs):
        env = kwargs.pop("env", {})
        now = kwargs.pop("now", NOW)
        bidirectional_enabled = kwargs.pop("bidirectional_enabled", None)
        test_config = config()
        self.project.source_issues = {issue["id"]: copy.deepcopy(issue)
                                      for issue in self.repo_graph.issues}
        if bidirectional_enabled is not None:
            test_config["bidirectional_enabled"] = bidirectional_enabled
        return se.sync(self.repo_graph, self.rest, self.project, self.notion,
                       test_config, now=now, env=env, **kwargs)

    def _configure_bidirectional_status(self, *, notion_status="백로그",
                                        baseline_notion_status="백로그",
                                        project_status="백로그",
                                        baseline_project_status=None,
                                        baseline_issue=None, issue=None):
        issue = issue or make_issue()
        baseline_issue = baseline_issue or issue
        item = make_project_item(item_id="PVTI_bidirectional", option=project_status)
        self.repo_graph = RepoGraph(issues=[issue])
        self.rest = LegacyREST(issues=[{"id": issue["databaseId"], "node_id": issue["id"],
            "number": issue["number"], "state": issue["state"].lower()}])
        self.project = ProjectAPI([item])
        state = se.new_issue_state(issue["databaseId"], gp.EXPECTED_PROJECT_ID)
        state["migration_complete"] = True
        state["project_item_id"] = item["id"]
        facts = {"repository_node_id": REPO_NODE, "pulls": {}}
        refs, _ = se._linked_pr_facts(baseline_issue, facts)
        baseline_project_status = baseline_project_status or project_status
        baseline_value = next((value for value in item["fieldValues"]["nodes"]
                               if value["optionId"] ==
                               gp.EXPECTED_STATUS_OPTIONS[baseline_project_status]),
                              status_value(baseline_project_status))
        baseline_item = {"id": item["id"],
                         "status_option_id": gp.EXPECTED_STATUS_OPTIONS[baseline_project_status],
                         "status_value_id": baseline_value["id"],
                         "status_updated_at": baseline_value["updatedAt"]}
        state["baseline"] = se._new_status_baseline(
            baseline_notion_status, baseline_item, baseline_issue, refs, config(), NOW)
        self.notion = FakeNotion([control_page(control_internal()),
            active_issue_page(task=notion_status, internal=se.canonical_json(state))])

    def _read_bidirectional_state(self):
        return se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)

    def test_created_notion_page_id_mismatch_keeps_create_fence_and_does_not_index(self):
        self._configure_bidirectional_status()
        self.notion = FakeNotion([control_page(control_internal())])
        self.notion.get_response_transform = lambda path, page: (
            {**page, "id": str(UUID(int=9999))}
            if path.startswith("/pages/") and path != f"/pages/{CONTROL_ID}" else page)

        with self.assertRaisesRegex(se.SyncError, "ID readback"):
            self.run_sync(bidirectional_enabled=True)

        control = self.notion.pages[CONTROL_ID]
        self.assertEqual(self.notion.creates, 1)
        self.assertEqual(rich_text_value(control["properties"]["Pending create"]),
                         se.key_for(gp.REPOSITORY_ID, ISSUE_ID))
        self.assertFalse(any(method == "PATCH" and path == f"/pages/{CONTROL_ID}" and
                             payload["properties"].get("Pending create") ==
                             se.text_property("")
                             for method, path, payload, _ in self.notion.writes))

    def test_project_api_rejects_wrong_add_set_and_clear_targets(self):
        issue = make_issue()
        self.project.source_issues = {issue["id"]: issue}
        item = make_project_item()
        self.project.items = [item]
        invalid = [
            (gp.ADD_ISSUE_MUTATION, {"project": "P_wrong", "content": ISSUE_NODE}),
            (gp.ADD_ISSUE_MUTATION, {"project": gp.EXPECTED_PROJECT_ID,
                                     "content": "I_wrong"}),
            (gp.SET_STATUS_MUTATION, {"project": "P_wrong", "item": item["id"],
                                      "field": STATUS_FIELD,
                                      "option": gp.EXPECTED_STATUS_OPTIONS["준비 중"]}),
            (gp.SET_STATUS_MUTATION, {"project": gp.EXPECTED_PROJECT_ID,
                                      "item": "PVTI_wrong", "field": STATUS_FIELD,
                                      "option": gp.EXPECTED_STATUS_OPTIONS["준비 중"]}),
            (gp.SET_STATUS_MUTATION, {"project": gp.EXPECTED_PROJECT_ID,
                                      "item": item["id"], "field": "PVTSSF_wrong",
                                      "option": gp.EXPECTED_STATUS_OPTIONS["준비 중"]}),
            (gp.CLEAR_STATUS_MUTATION, {"project": "P_wrong", "item": item["id"],
                                        "field": STATUS_FIELD}),
            (gp.CLEAR_STATUS_MUTATION, {"project": gp.EXPECTED_PROJECT_ID,
                                        "item": "PVTI_wrong", "field": STATUS_FIELD}),
            (gp.CLEAR_STATUS_MUTATION, {"project": gp.EXPECTED_PROJECT_ID,
                                        "item": item["id"], "field": "PVTSSF_wrong"}),
        ]
        for query, variables in invalid:
            with self.subTest(query=query, variables=variables):
                writes_before = copy.deepcopy(self.project.writes)
                items_before = copy.deepcopy(self.project.items)
                with self.assertRaises(gp.SyncError):
                    self.project.request(query, variables, mutation=True)
                self.assertEqual(self.project.writes, writes_before)
                self.assertEqual(self.project.items, items_before)

    def test_nested_forbidden_task_patch_is_detected_by_guard_counter(self):
        writes = [("PATCH", f"/pages/{ISSUE_PAGE_ID}",
                   {"properties": {"작업 상태": {"select": {"name": "완료"}}}}, {})]
        with self.assertRaises(AssertionError):
            self.assertEqual(task_status_patch_payloads(writes, ISSUE_PAGE_ID), [])
        request_ui_write = [("PATCH", f"/pages/{ISSUE_PAGE_ID}",
                             {"properties": {"요청 처리": {"select": None}}}, {})]
        self.assertEqual(request_ui_patch_count([], ISSUE_PAGE_ID), 0)
        self.assertEqual(request_ui_patch_count(request_ui_write, ISSUE_PAGE_ID), 1)

    def test_notion_fake_rejects_invalid_create_and_update_property_schemas(self):
        invalid_payloads = [
            {"properties": {"없는 속성": {"rich_text": []}}},
            {"properties": {"메모": {"number": 7}}},
            {"properties": {"작업 상태": {"select": {"name": None}}}},
        ]
        for payload in invalid_payloads:
            with self.subTest(operation="create", payload=payload):
                notion = FakeNotion([control_page(control_internal())])
                with self.assertRaises(se.SyncError):
                    notion.request("POST", "/pages", payload)
                self.assertEqual(notion.creates, 0)
                self.assertEqual(notion.writes, [])

        bad_updates = [
            {"properties": {"없는 속성": {"rich_text": []}}},
            {"properties": {"메모": {"number": 7}}},
            {"properties": {"작업 상태": {"select": {"name": "없는 상태"}}}},
        ]
        for payload in bad_updates:
            with self.subTest(operation="update", payload=payload):
                notion = FakeNotion([control_page(control_internal())])
                with self.assertRaises(se.SyncError):
                    notion.request("PATCH", f"/pages/{CONTROL_ID}", payload)
                self.assertEqual(notion.writes, [])

    def test_new_closed_issue_create_accepts_null_task_select_schema(self):
        issue = make_issue(created_at="2026-10-02T00:00:00Z", state="CLOSED",
                           reason="COMPLETED", closed_at="2026-10-03T00:00:00Z")
        self.repo_graph = RepoGraph(issues=[issue])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "closed"}])
        self.project = ProjectAPI()
        self.notion = FakeNotion([control_page(control_internal())])

        result = self.run_sync(bidirectional_enabled=True)

        self.assertEqual(result["created"], 1)
        self.assertEqual(self.notion.creates, 1)
        post = next(payload for method, path, payload, _ in self.notion.writes
                    if method == "POST" and path == "/pages")
        self.assertEqual(post["properties"]["작업 상태"], {"select": None})
        created = next(page for page in self.notion.pages.values()
                       if se.read_text(page, "동기화 키") ==
                       se.key_for(gp.REPOSITORY_ID, ISSUE_ID))
        self.assertEqual(se.read_select(created, "작업 상태"), "완료")
        self.assertEqual(self.project.items[0]["content"]["id"], ISSUE_NODE)
        self.assertEqual(self.project.writes[0][1]["content"], ISSUE_NODE)

    def test_flag_enabled_notion_move_mutates_project_confirms_baseline_and_repeats_noop(self):
        self._configure_bidirectional_status(notion_status="진행 중")

        first = self.run_sync(bidirectional_enabled=True)
        state = self._read_bidirectional_state()
        self.assertEqual(first["project_changes"], 1)
        self.assertEqual(len(self.project.writes), 1)
        self.assertEqual(state["request"]["phase"], "completed")
        self.assertIsNone(state["notion_write"])
        self.assertEqual(state["baseline"]["notion_status"], "진행 중")
        self.assertEqual(state["baseline"]["project"]["option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["진행 중"])
        props = self.notion.pages[ISSUE_PAGE_ID]["properties"]
        self.assertEqual(props["요청 처리"]["select"]["name"], "반영 완료")
        self.assertEqual(props["요청 상태"]["select"]["name"], "진행 중")
        self.assertEqual(rich_text_value(props["확인 필요"]),
                         "Project 상태와 Notion 표시를 확인했습니다.")

        second = self.run_sync(bidirectional_enabled=True)
        state = self._read_bidirectional_state()
        self.assertEqual(second["project_changes"], 0)
        self.assertEqual(len(self.project.writes), 1)
        self.assertEqual(second["held"], 0)
        self.assertEqual(state["request"]["phase"], "completed")

    def test_all_six_general_status_moves_confirm_exactly_and_repeat_as_noop(self):
        states = ("백로그", "준비 중", "진행 중")
        for before in states:
            for target in states:
                if before == target:
                    continue
                with self.subTest(before=before, target=target):
                    self._configure_bidirectional_status(
                        notion_status=target, baseline_notion_status=before,
                        project_status=before, baseline_project_status=before)

                    first = self.run_sync(bidirectional_enabled=True)
                    state = self._read_bidirectional_state()
                    self.assertEqual(first["project_changes"], 1)
                    self.assertEqual(len(self.project.writes), 1)
                    query, variables = self.project.writes[0]
                    self.assertEqual(query, gp.SET_STATUS_MUTATION)
                    self.assertEqual(variables["project"], gp.EXPECTED_PROJECT_ID)
                    self.assertEqual(variables["item"], state["project_item_id"])
                    self.assertEqual(variables["field"], STATUS_FIELD)
                    self.assertEqual(variables["option"],
                                     gp.EXPECTED_STATUS_OPTIONS[target])
                    self.assertEqual(self.project.items[0]["fieldValues"]["nodes"][0]["optionId"],
                                     gp.EXPECTED_STATUS_OPTIONS[target])
                    self.assertEqual(state["request"]["phase"], "completed")
                    self.assertEqual(state["request"]["target"], target)
                    self.assertEqual(state["baseline"]["notion_status"], target)
                    self.assertEqual(state["baseline"]["project"]["option_id"],
                                     gp.EXPECTED_STATUS_OPTIONS[target])
                    properties = self.notion.pages[ISSUE_PAGE_ID]["properties"]
                    self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID],
                                                    "작업 상태"), target)
                    self.assertEqual(properties["요청 처리"]["select"]["name"], "반영 완료")
                    self.assertEqual(properties["요청 상태"]["select"]["name"], target)

                    second = self.run_sync(bidirectional_enabled=True)
                    self.assertEqual(second["project_changes"], 0)
                    self.assertEqual(len(self.project.writes), 1)
                    self.assertEqual(second["held"], 0)
                    self.assertEqual(self._read_bidirectional_state()["baseline"]["notion_status"],
                                     target)

    def test_both_changed_statuses_never_pick_a_timestamp_winner_at_or_outside_window(self):
        observed_from = "2026-10-08T00:00:00Z"
        observed_to = "2026-10-08T00:00:10Z"
        cases = {
            "inside": "2026-10-08T00:00:05Z",
            "at_start": observed_from,
            "at_end": observed_to,
            "outside_before": "2026-10-07T23:59:59Z",
            "outside_after": "2026-10-08T00:00:11Z",
        }
        for label, project_updated_at in cases.items():
            with self.subTest(window=label):
                self._configure_bidirectional_status(
                    notion_status="진행 중", baseline_notion_status="백로그",
                    project_status="준비 중", baseline_project_status="백로그")
                state = self._read_bidirectional_state()
                original_baseline = copy.deepcopy(state["baseline"])
                state["baseline"]["observed_at"] = observed_from
                self.notion.pages[ISSUE_PAGE_ID]["properties"]["동기화 내부 상태"] = \
                    se.text_property(se.canonical_json(state))
                current = status_value("준비 중")
                current.update(id=f"PVTSV_window_{label}", updatedAt=project_updated_at)
                item = self.project.items[0]
                item["fieldValues"]["nodes"] = [current]
                self.project.status_values[item["id"]] = [current]

                with patch.object(observation_clock, "verify_snapshot",
                                  return_value=(observed_to, 3)):
                    result = self.run_sync(bidirectional_enabled=True)

                held = self._read_bidirectional_state()
                self.assertEqual(result["held"], 1)
                self.assertEqual(held["hold"]["code"], "BIDIRECTIONAL_CONFLICT")
                self.assertEqual(held["hold"]["message"],
                    "GitHub Project와 Notion 상태가 모두 바뀌었고 순서를 입증할 수 없어 PM 확인이 필요합니다.")
                self.assertEqual(held["baseline"], original_baseline | {"observed_at": observed_from})
                self.assertEqual(held["request"]["phase"], "held")
                self.assertEqual(held["request"]["target"], "진행 중")
                self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]
                                 ["요청 처리"]["select"]["name"], "PM 확인 필요")
                self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]
                                 ["요청 상태"]["select"]["name"], "진행 중")
                self.assertEqual(self.project.writes, [])

    def test_unchanged_notion_same_option_stamp_resets_rebase_only_matching_current_stamp(self):
        variants = {
            "value_id_only": ("PVTSV_same_option_reset_no_notion_move", "2026-10-01T00:00:00Z"),
            "updated_at_only": ("PVTSV_PVTI_bidirectional_백로그", "2026-10-08T00:00:05Z"),
        }
        for label, (value_id, updated_at) in variants.items():
            with self.subTest(reset=label):
                self._configure_bidirectional_status(notion_status="백로그",
                                                     baseline_notion_status="백로그")
                item = self.project.items[0]
                current = status_value("백로그")
                current.update(id=value_id, updatedAt=updated_at)
                item["fieldValues"]["nodes"] = [current]
                self.project.status_values[item["id"]] = [current]

                result = self.run_sync(bidirectional_enabled=True)

                final = self._read_bidirectional_state()
                self.assertEqual(result["held"], 0)
                self.assertIsNone(final["hold"])
                self.assertIsNone(final["request"])
                self.assertEqual(final["baseline"]["notion_status"], "백로그")
                self.assertEqual(final["baseline"]["project"]["option_id"],
                                 gp.EXPECTED_STATUS_OPTIONS["백로그"])
                self.assertEqual(final["baseline"]["project"]["value_id"], value_id)
                self.assertEqual(final["baseline"]["project"]["updated_at"], updated_at)
                self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID],
                                                "작업 상태"), "백로그")
                self.assertEqual(self.project.writes, [])
    def test_flag_enabled_project_status_same_option_stamp_reset_blocks_late_write(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        changed = False

        def replace_status_value_after_reservation(path, properties, notion):
            nonlocal changed
            if changed or path != f"/pages/{ISSUE_PAGE_ID}":
                return
            raw = properties.get("동기화 내부 상태")
            if not raw:
                return
            saved = json.loads(rich_text_value(raw))
            pending = saved.get("pending") or {}
            if pending.get("before_status_stamp"):
                changed = True
                old = self.project.items[0]["fieldValues"]["nodes"][0]
                reset = dict(old, id="PVTSV_recreated_same_option",
                             updatedAt="2026-10-09T12:00:00Z")
                self.project.items[0]["fieldValues"]["nodes"] = [reset]
                self.project.status_values[self.project.items[0]["id"]] = [reset]

        self.notion.after_patch = replace_status_value_after_reservation

        result = self.run_sync(bidirectional_enabled=True)

        state = self._read_bidirectional_state()
        self.assertTrue(changed)
        self.assertEqual(result["held"], 1)
        self.assertEqual(state["hold"]["code"], "SOURCE_CHANGED_BEFORE_WRITE")
        self.assertIsNone(state["pending"])
        self.assertEqual(self.project.items[0]["fieldValues"]["nodes"][0]["optionId"],
                         gp.EXPECTED_STATUS_OPTIONS["백로그"])
        self.assertEqual(self.project.writes, [])

    def test_flag_enabled_project_value_id_only_reset_before_mutation_holds_and_restarts_safely(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        initial = self._read_bidirectional_state()
        baseline = copy.deepcopy(initial["baseline"])
        changed = False

        def replace_value_id_after_reservation(path, properties, _notion):
            nonlocal changed
            raw = properties.get("동기화 내부 상태")
            if changed or path != f"/pages/{ISSUE_PAGE_ID}" or not raw:
                return
            pending = json.loads(rich_text_value(raw)).get("pending") or {}
            if pending.get("before_status_stamp"):
                changed = True
                old = self.project.items[0]["fieldValues"]["nodes"][0]
                replacement = dict(old, id="PVTSV_value_id_only_race")
                self.project.items[0]["fieldValues"]["nodes"] = [replacement]
                self.project.status_values[self.project.items[0]["id"]] = [replacement]

        self.notion.after_patch = replace_value_id_after_reservation
        first = self.run_sync(bidirectional_enabled=True)
        held = self._read_bidirectional_state()
        request_id = held["request"]["id"]
        self.assertTrue(changed)
        self.assertEqual(first["held"], 1)
        self.assertEqual(held["hold"]["code"], "SOURCE_CHANGED_BEFORE_WRITE")
        self.assertEqual(held["baseline"], baseline)
        self.assertEqual(held["request"]["phase"], "prepared")
        self.assertEqual(self.project.writes, [])

        self.notion.after_patch = None
        second = self.run_sync(bidirectional_enabled=True)
        repeated = self._read_bidirectional_state()
        self.assertEqual(second["held"], 1)
        self.assertEqual(repeated["baseline"], baseline)
        self.assertEqual(repeated["request"]["id"], request_id)
        self.assertEqual(self.project.writes, [])

    def test_flag_enabled_same_option_project_reset_before_poll_holds_both_sides(self):
        for reset_kind in ("value_id_and_time", "value_id_only", "time_only", "cleared"):
            with self.subTest(reset_kind=reset_kind):
                self._configure_bidirectional_status(notion_status="진행 중")
                initial = self._read_bidirectional_state()
                baseline = copy.deepcopy(initial["baseline"])
                item_id = self.project.items[0]["id"]
                prior_value = copy.deepcopy(self.project.items[0]["fieldValues"]["nodes"][0])
                if reset_kind == "value_id_and_time":
                    reset = dict(prior_value, id="PVTSV_poll_reset_v2",
                                 updatedAt="2026-10-07T12:00:00Z")
                    self.project.items[0]["fieldValues"]["nodes"] = [reset]
                    self.project.status_values[item_id] = [reset]
                elif reset_kind == "value_id_only":
                    reset = dict(prior_value, id="PVTSV_poll_value_id_only")
                    self.project.items[0]["fieldValues"]["nodes"] = [reset]
                    self.project.status_values[item_id] = [reset]
                elif reset_kind == "time_only":
                    reset = dict(prior_value, updatedAt="2026-10-07T12:00:00Z")
                    self.project.items[0]["fieldValues"]["nodes"] = [reset]
                    self.project.status_values[item_id] = [reset]
                else:
                    self.project.items[0]["fieldValues"]["nodes"] = []
                    self.project.status_values[item_id] = []

                first = self.run_sync(bidirectional_enabled=True)

                held = self._read_bidirectional_state()
                props = self.notion.pages[ISSUE_PAGE_ID]["properties"]
                self.assertEqual(first["held"], 1)
                self.assertIn(held["hold"]["code"],
                              {"BIDIRECTIONAL_CONFLICT", "STATUS_ORDER_UNKNOWN",
                               "PROJECT_STATUS_UNSET"})
                self.assertEqual(held["baseline"], baseline)
                self.assertEqual(held["project_item_id"], item_id)
                request_id = (held.get("request") or {}).get("id")
                self.assertEqual(held["request"]["phase"], "held")
                self.assertEqual(held["request"]["target"], "진행 중")
                expected_task = "진행 중" if reset_kind == "cleared" else "백로그"
                self.assertEqual(props["요청 처리"]["select"]["name"], "PM 확인 필요")
                self.assertEqual(props["요청 상태"]["select"]["name"], "진행 중")
                self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                                 expected_task)
                self.assertEqual(len(self.project.writes), 0)

                second = self.run_sync(bidirectional_enabled=True)
                repeated = self._read_bidirectional_state()
                self.assertEqual(second["held"], 1)
                self.assertEqual(repeated["baseline"], baseline)
                self.assertEqual(repeated["project_item_id"], item_id)
                self.assertEqual((repeated.get("request") or {}).get("id"), request_id)
                self.assertEqual(repeated["request"]["target"], "진행 중")
                self.assertEqual(repeated["request"]["phase"], "held")
                self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                                 expected_task)
                self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]["요청 처리"]
                                 ["select"]["name"], "PM 확인 필요")
                self.assertEqual(len(self.project.writes), 0)

    def test_graphql_connection_snapshot_does_not_alias_mutable_server_nodes(self):
        original = [{"id": "node-1", "nested": {"value": "before"}}]
        snapshot = conn(original)
        original[0]["nested"]["value"] = "after"
        self.assertEqual(snapshot["nodes"][0]["nested"]["value"], "before")

    def test_memo_only_notion_edit_does_not_create_bidirectional_status_request(self):
        self._configure_bidirectional_status()
        state_before = copy.deepcopy(self._read_bidirectional_state())
        self.notion.pages[ISSUE_PAGE_ID]["last_edited_time"] = "2026-10-11T02:20:00Z"

        result = self.run_sync(bidirectional_enabled=True)

        after = self._read_bidirectional_state()
        self.assertEqual(result["held"], 0)
        for key in ("notion_status", "project", "project_item_id", "facts_fingerprint"):
            self.assertEqual(after["baseline"][key], state_before["baseline"][key])
        self.assertEqual(after["request"], state_before["request"])
        self.assertIsNone(after["hold"])
        self.assertEqual(self.project.writes, [])

    def test_flag_enabled_unchanged_status_rows_skip_status_checkpoints_for_three_polls(self):
        self._configure_bidirectional_status()
        before = self._read_bidirectional_state()
        for poll in range(3):
            if poll == 1:
                self.repo_graph.issues[0]["title"] = "Updated metadata only"
                self.repo_graph.issues[0]["updatedAt"] = "2026-10-09T00:00:00Z"
                self.rest.issues[0]["title"] = "Updated metadata only"
                self.rest.issues[0]["updatedAt"] = "2026-10-09T00:00:00Z"
            page_calls_before = len(self.notion.calls)
            result = self.run_sync(
                bidirectional_enabled=True,
                now=f"2026-10-11T00:0{poll}:00Z")
            new_page_patches = [
                payload.get("properties", {})
                for method, path, payload, _ in self.notion.calls[page_calls_before:]
                if method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}"]
            self.assertFalse(any("작업 상태" in patch or "동기화 내부 상태" in patch
                                 for patch in new_page_patches))
            self.assertEqual(result["held"], 0)
            self.assertEqual(self.project.writes, [])
        after = self._read_bidirectional_state()
        self.assertEqual(after["baseline"], before["baseline"])
        self.assertIsNone(after["pending"])
        self.assertIsNone(after["projection"])
        self.assertEqual(title_text(self.notion.pages[ISSUE_PAGE_ID]),
                         "#18 Updated metadata only")
        self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]["동기화 시각"]
                         ["date"]["start"], "2026-10-11T00:02:00Z")

    def test_display_checkpoint_readback_preserves_new_card_move_including_baseline(self):
        for moved_status in ("준비 중", "백로그"):
            with self.subTest(moved_status=moved_status):
                self._configure_bidirectional_status(notion_status="진행 중")
                changed = False

                def move_after_display_marker(path, properties, notion):
                    nonlocal changed
                    if changed or path != f"/pages/{ISSUE_PAGE_ID}":
                        return
                    raw = properties.get("동기화 내부 상태")
                    saved = json.loads(rich_text_value(raw)) if raw else {}
                    checkpoint = ((saved.get("projection") or {}).get(
                        "display_checkpoint") or {})
                    if checkpoint.get("phase") == "display_sent":
                        changed = True
                        notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                            "select": {"name": moved_status}}

                self.notion.after_patch = move_after_display_marker
                result = self.run_sync(bidirectional_enabled=True)
                state = self._read_bidirectional_state()
                self.assertTrue(changed)
                self.assertEqual(result["held"], 1)
                self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID],
                                                "작업 상태"), moved_status)
                self.assertIsNotNone(state["projection"])
                request = state.get("deferred_request") or state.get("request")
                self.assertEqual(request["phase"], "held")
                self.assertEqual(request["target"], moved_status)
                self.assertEqual(self.project.writes[0][1]["option"],
                                 gp.EXPECTED_STATUS_OPTIONS["진행 중"])

    def test_final_completion_marker_mismatch_is_compensated_and_survives_restart(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        moved = False

        def move_after_completed_marker(path, properties, notion):
            nonlocal moved
            if moved or path != f"/pages/{ISSUE_PAGE_ID}":
                return
            raw = properties.get("동기화 내부 상태")
            saved = json.loads(rich_text_value(raw)) if raw else {}
            if saved.get("projection") is None and (saved.get("request") or {}).get(
                    "phase") == "completed":
                moved = True
                notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                    "select": {"name": "준비 중"}}

        self.notion.after_patch = move_after_completed_marker
        result = self.run_sync(bidirectional_enabled=True)
        state = self._read_bidirectional_state()
        control = se.decode_internal(
            se.read_text(self.notion.pages[CONTROL_ID], "동기화 내부 상태"),
            kind="control", project_id=gp.EXPECTED_PROJECT_ID)
        self.assertTrue(moved)
        self.assertEqual(result["held"], 1)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "준비 중")
        self.assertIsNotNone(state["projection"])
        self.assertEqual(state["baseline"]["notion_status"], "백로그")
        self.assertEqual(state["request"]["phase"], "confirmed")
        self.assertEqual(control["last_result"]["kind"], "partial")
        self.assertEqual(control["last_success_at"], None)
        writes = copy.deepcopy(self.project.writes)

        self.notion.after_patch = None
        repeated = self.run_sync(bidirectional_enabled=True)
        recovered = self._read_bidirectional_state()
        self.assertGreaterEqual(repeated["held"], 1)
        self.assertEqual(recovered["baseline"]["notion_status"], "백로그")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "준비 중")
        self.assertEqual(self.project.writes, writes)

    def test_recovery_completion_repair_marker_readback_preserves_new_move(self):
        self._configure_bidirectional_status()
        changed_status = status_value("준비 중")
        self.project.items[0]["fieldValues"]["nodes"] = [changed_status]
        self.project.status_values[self.project.items[0]["id"]] = [changed_status]
        stopped = False

        def stop_before_terminal_display(path, properties, notion):
            nonlocal stopped
            raw = properties.get("동기화 내부 상태")
            if stopped or path != f"/pages/{ISSUE_PAGE_ID}" or not raw:
                return
            saved = json.loads(rich_text_value(raw))
            marker = (saved.get("projection") or {}).get("display_checkpoint") or {}
            if marker.get("phase") == "completion_pending" and "작업 상태" not in properties:
                stopped = True
                raise se.SyncError("synthetic stop after completion marker")

        self.notion.after_patch = stop_before_terminal_display
        with self.assertRaisesRegex(se.SyncError, "synthetic stop after completion marker"):
            self.run_sync(bidirectional_enabled=True)
        self.assertTrue(stopped)
        self.notion.after_patch = None
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["확인 필요"] = se.text_property(
            "stale terminal display")
        recovery_write_start = len(self.notion.writes)
        moved = False

        def move_after_repair_marker(path, properties, notion):
            nonlocal moved
            raw = properties.get("동기화 내부 상태")
            if moved or path != f"/pages/{ISSUE_PAGE_ID}" or not raw:
                return
            saved = json.loads(rich_text_value(raw))
            marker = (saved.get("projection") or {}).get("display_checkpoint") or {}
            if marker.get("phase") == "completion_pending":
                moved = True
                notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                    "select": {"name": "진행 중"}}

        self.notion.after_patch = move_after_repair_marker
        writes_before = copy.deepcopy(self.project.writes)
        result = self.run_sync(bidirectional_enabled=True)
        state = self._read_bidirectional_state()
        self.assertTrue(moved)
        self.assertEqual(result["held"], 1)
        self.assertEqual(state["hold"]["code"], "REQUEST_RACE")
        self.assertEqual(state["baseline"]["notion_status"], "백로그")
        self.assertIsNotNone(state["projection"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "진행 중")
        self.assertEqual(self.project.writes, writes_before)
        task_writes = task_status_patch_payloads(self.notion.writes[recovery_write_start:],
                                                 ISSUE_PAGE_ID)
        self.assertEqual(task_writes, [])

    def test_recovery_final_completion_readback_mismatch_is_compensated(self):
        self._configure_bidirectional_status()
        changed_status = status_value("준비 중")
        self.project.items[0]["fieldValues"]["nodes"] = [changed_status]
        self.project.status_values[self.project.items[0]["id"]] = [changed_status]
        lost = False

        def lose_terminal_display_readback(path, properties, notion):
            nonlocal lost
            if (lost or path != f"/pages/{ISSUE_PAGE_ID}" or
                    "작업 상태" not in properties or not properties.get("동기화 내부 상태")):
                return
            saved = json.loads(rich_text_value(properties["동기화 내부 상태"]))
            marker = (saved.get("projection") or {}).get("display_checkpoint") or {}
            if marker.get("phase") == "completion_pending":
                lost = True
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = lose_terminal_display_readback
        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync(bidirectional_enabled=True)
        self.assertTrue(lost)
        self.notion.after_patch = None
        moved = False

        def move_after_recovery_completion_marker(path, properties, notion):
            nonlocal moved
            raw = properties.get("동기화 내부 상태")
            if moved or path != f"/pages/{ISSUE_PAGE_ID}" or not raw:
                return
            saved = json.loads(rich_text_value(raw))
            if saved.get("projection") is None:
                moved = True
                notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                    "select": {"name": "진행 중"}}

        self.notion.after_patch = move_after_recovery_completion_marker
        writes_before = copy.deepcopy(self.project.writes)
        result = self.run_sync(bidirectional_enabled=True)
        state = self._read_bidirectional_state()
        control = se.decode_internal(
            se.read_text(self.notion.pages[CONTROL_ID], "동기화 내부 상태"),
            kind="control", project_id=gp.EXPECTED_PROJECT_ID)
        self.assertTrue(moved)
        self.assertEqual(result["held"], 1)
        self.assertEqual(state["baseline"]["notion_status"], "백로그")
        self.assertIsNotNone(state["projection"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "진행 중")
        self.assertEqual(control["last_result"]["kind"], "partial")
        self.assertIsNone(control["last_success_at"])
        self.assertEqual(self.project.writes, writes_before)

    def test_fake_notion_rejects_null_select_name_but_accepts_select_clear(self):
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task="백로그")])
        with self.assertRaisesRegex(se.SyncError, "invalid select option payload"):
            self.notion.request("PATCH", f"/pages/{ISSUE_PAGE_ID}",
                {"properties": {"작업 상태": {"select": {"name": None}}}}, write=True)
        self.notion.request("PATCH", f"/pages/{ISSUE_PAGE_ID}",
            {"properties": {"작업 상태": {"select": None}}}, write=True)
        self.assertIsNone(self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"]["select"])

    def test_project_only_completion_pending_request_none_recovers_after_restart(self):
        self._configure_bidirectional_status()
        changed_status = status_value("준비 중")
        self.project.items[0]["fieldValues"]["nodes"] = [changed_status]
        self.project.status_values[self.project.items[0]["id"]] = [changed_status]
        interrupted = False

        def stop_after_completion_checkpoint(path, properties, notion):
            nonlocal interrupted
            if interrupted or path != f"/pages/{ISSUE_PAGE_ID}":
                return
            internal = properties.get("동기화 내부 상태")
            if not internal:
                return
            saved = json.loads(rich_text_value(internal))
            checkpoint = (saved.get("projection") or {}).get("display_checkpoint") or {}
            if checkpoint.get("phase") == "completion_pending":
                interrupted = True
                raise se.SyncError("synthetic interruption after completion_pending")

        self.notion.after_patch = stop_after_completion_checkpoint
        with self.assertRaisesRegex(se.SyncError, "after completion_pending"):
            self.run_sync(bidirectional_enabled=True)

        persisted = self._read_bidirectional_state()
        self.assertTrue(interrupted)
        self.assertIsNone(persisted["request"])
        self.assertIsNone(persisted["projection"]["request_id"])
        self.assertEqual(persisted["projection"]["display_checkpoint"]["phase"],
                         "completion_pending")
        self.assertEqual(persisted["baseline"]["notion_status"], "백로그")
        writes_before_recovery = copy.deepcopy(self.project.writes)

        self.notion.after_patch = None
        recovered = self.run_sync(bidirectional_enabled=True)

        settled = self._read_bidirectional_state()
        self.assertEqual(recovered["held"], 0)
        self.assertIsNone(settled["request"])
        self.assertIsNone(settled["projection"])
        self.assertEqual(settled["baseline"]["notion_status"], "준비 중")
        self.assertEqual(self.project.writes, writes_before_recovery)

        repeated = self.run_sync(bidirectional_enabled=True)
        self.assertEqual(repeated["held"], 0)
        self.assertEqual(self._read_bidirectional_state()["baseline"]["notion_status"],
                         "준비 중")
        self.assertEqual(self.project.writes, writes_before_recovery)

    def test_not_planned_and_duplicate_completion_clear_nullable_notion_select(self):
        for reason, recovery in (("NOT_PLANNED", False), ("DUPLICATE", True)):
            with self.subTest(reason=reason, recovery=recovery):
                duplicate = (None if reason != "DUPLICATE" else {
                    "id": "I_kwDORepresentative",
                    "url": f"https://github.com/{gp.REPOSITORY}/issues/7"})
                issue = make_issue(state="CLOSED", reason=reason, duplicate=duplicate,
                                   closed_at="2026-10-03T00:00:00Z")
                self._configure_bidirectional_status(issue=issue)
                armed = False

                def lose_terminal_readback(path, properties, notion):
                    nonlocal armed
                    if (armed or not recovery or path != f"/pages/{ISSUE_PAGE_ID}" or
                            "작업 상태" not in properties or
                            properties["작업 상태"] != {"select": None}):
                        return
                    internal = properties.get("동기화 내부 상태")
                    saved = json.loads(rich_text_value(internal)) if internal else {}
                    checkpoint = (saved.get("projection") or {}).get(
                        "display_checkpoint") or {}
                    if checkpoint.get("phase") == "completion_pending":
                        armed = True
                        notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

                if recovery:
                    self.notion.after_patch = lose_terminal_readback
                    with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
                        self.run_sync(bidirectional_enabled=True)
                    staged = self._read_bidirectional_state()
                    self.assertTrue(armed)
                    self.assertEqual(staged["projection"]["display_checkpoint"]["phase"],
                                     "completion_pending")
                    self.assertEqual(staged["baseline"]["notion_status"], "백로그")
                    self.notion.pages[ISSUE_PAGE_ID]["properties"]["확인 필요"] = (
                        se.text_property("stale terminal text"))
                    self.notion.after_patch = None

                result = self.run_sync(bidirectional_enabled=True)
                state = self._read_bidirectional_state()
                page = self.notion.pages[ISSUE_PAGE_ID]
                self.assertEqual(result["held"], 0)
                self.assertIsNone(se.read_select(page, "작업 상태"))
                self.assertEqual(se.read_select(page, "종료 사유"),
                                 "미계획" if reason == "NOT_PLANNED" else "중복")
                self.assertEqual(state["baseline"]["notion_status"], None)
                self.assertIsNone(state["projection"])
                terminal_patches = []
                terminal_markers = []
                for method, path, payload, _ in self.notion.calls:
                    if method != "PATCH" or path != f"/pages/{ISSUE_PAGE_ID}":
                        continue
                    properties = payload.get("properties", {})
                    if "작업 상태" not in properties:
                        continue
                    raw = properties.get("동기화 내부 상태")
                    saved = json.loads(rich_text_value(raw)) if raw else {}
                    marker = ((saved.get("projection") or {}).get("display_checkpoint") or {})
                    if marker.get("phase") == "completion_pending":
                        terminal_patches.append(properties)
                        terminal_markers.append(marker)
                self.assertTrue(terminal_patches)
                if recovery:
                    self.assertGreaterEqual(len(terminal_patches), 2)
                self.assertTrue(all(payload["작업 상태"] == {"select": None}
                                    for payload in terminal_patches))
                self.assertTrue(all(se._restore_projection_matches(page, marker)
                                    for marker in terminal_markers))
                self.assertIn(gp.CLEAR_STATUS_MUTATION,
                              [query for query, _ in self.project.writes])
                writes_after_completion = copy.deepcopy(self.project.writes)

                repeated = self.run_sync(bidirectional_enabled=True)
                self.assertEqual(repeated["held"], 0)
                self.assertEqual(self.project.writes, writes_after_completion)

    def test_project_only_projection_readback_crash_recovers_without_phantom_request(self):
        self._configure_bidirectional_status()
        changed_status = status_value("준비 중")
        self.project.items[0]["fieldValues"]["nodes"] = [changed_status]
        self.project.status_values[self.project.items[0]["id"]] = [changed_status]
        original_finalize = se._finalize_bidirectional_projection

        def stop_after_display_readback(*args, **kwargs):
            raise se.SyncError("synthetic crash after projection readback")

        se._finalize_bidirectional_projection = stop_after_display_readback
        try:
            with self.assertRaisesRegex(se.SyncError, "after projection readback"):
                self.run_sync(bidirectional_enabled=True)
        finally:
            se._finalize_bidirectional_projection = original_finalize

        interrupted = self._read_bidirectional_state()
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "준비 중")
        self.assertIsNone(interrupted["request"])
        self.assertEqual(interrupted["baseline"]["notion_status"], "백로그")
        self.assertEqual(interrupted["projection"]["request_id"], None)
        self.assertEqual(interrupted["projection"]["display_checkpoint"]["phase"],
                         "display_sent")

        recovered = self.run_sync(bidirectional_enabled=True)
        state = self._read_bidirectional_state()
        self.assertEqual(recovered["held"], 0)
        self.assertIsNone(state["projection"])
        self.assertIsNone(state["request"])
        self.assertEqual(state["baseline"]["notion_status"], "준비 중")
        self.assertEqual(state["baseline"]["project"]["option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["준비 중"])
        self.assertEqual(self.project.writes, [])

        repeated = self.run_sync(bidirectional_enabled=True)
        final_state = self._read_bidirectional_state()
        self.assertEqual(repeated["held"], 0)
        self.assertIsNone(final_state["request"])
        self.assertIsNone(final_state["projection"])
        self.assertEqual(self.project.writes, [])

    def _seed_prepared_request_without_pending(self, *, project_stamp_changed=False):
        self._configure_bidirectional_status(notion_status="진행 중")
        row = self.notion.pages[ISSUE_PAGE_ID]
        row["properties"]["작업 상태"] = {"select": {"name": "진행 중"}}
        state = self._read_bidirectional_state()
        request = se.status_sync.request_record(
            str(UUID(int=9914)), "진행 중", observed_from=state["baseline"]["observed_at"],
            observed_to=NOW, prior_notion_status="백로그",
            project_option_id=gp.EXPECTED_STATUS_OPTIONS["백로그"],
            phase="prepared", reason="")
        state["request"] = request
        state["notion_write"] = {
            "request_id": request["id"], "expected_before": "백로그",
            "target": request["target"], "kind": "request", "phase": "prepared"}
        if project_stamp_changed:
            changed = status_value("백로그")
            changed["id"] = "PVTSV_replaced_same_option"
            changed["updatedAt"] = "2026-10-11T00:10:00Z"
            self.project.items[0]["fieldValues"]["nodes"] = [changed]
            self.project.status_values[self.project.items[0]["id"]] = [changed]
        row["properties"]["동기화 내부 상태"] = se.text_property(se.canonical_json(state))
        return copy.deepcopy(request)

    def test_prepared_request_without_pending_restarts_with_original_target_and_id(self):
        request = self._seed_prepared_request_without_pending()
        observed_before_project_write = []

        def inspect_request_display(project, item, variables):
            waiting_writes = [
                payload["properties"] for method, path, payload, _ in self.notion.writes
                if method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}" and
                payload["properties"].get("요청 처리", {}).get("select", {}).get("name") ==
                "반영 대기"]
            self.assertTrue(waiting_writes)
            self.assertEqual(waiting_writes[-1]["요청 상태"]["select"]["name"], "진행 중")
            self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]
                             ["요청 처리"]["select"]["name"], "자동 확인 중")
            observed_before_project_write.append(True)

        self.project.after_status_write = inspect_request_display
        result = self.run_sync(bidirectional_enabled=True)
        state = self._read_bidirectional_state()
        self.assertEqual(result["held"], 0)
        self.assertEqual(state["request"]["id"], request["id"])
        self.assertEqual(state["request"]["target"], "진행 중")
        self.assertEqual(state["request"]["phase"], "completed")
        self.assertIsNone(state["pending"])
        self.assertIsNone(state["projection"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "진행 중")
        self.assertEqual([query for query, _ in self.project.writes],
                         [gp.SET_STATUS_MUTATION])
        self.assertEqual(observed_before_project_write, [True])
        self.assertEqual(self.project.items[0]["fieldValues"]["nodes"][0]["optionId"],
                         gp.EXPECTED_STATUS_OPTIONS["진행 중"])

    def test_actual_prepared_checkpoint_interruption_restarts_or_holds_by_current_facts(self):
        for variant in ("stable", "project_stamp", "facts", "card"):
            with self.subTest(variant=variant):
                self._configure_bidirectional_status(notion_status="진행 중")
                original_save = se._save_status_request
                interrupted = False

                def save_then_interrupt(notion, source_id, row, state):
                    nonlocal interrupted
                    result = original_save(notion, source_id, row, state)
                    if ((state.get("request") or {}).get("phase") == "prepared" and
                            not interrupted):
                        interrupted = True
                        raise se.SyncError("synthetic interruption after durable prepared UI")
                    return result

                se._save_status_request = save_then_interrupt
                try:
                    with self.assertRaisesRegex(se.SyncError, "durable prepared UI"):
                        self.run_sync(bidirectional_enabled=True)
                finally:
                    se._save_status_request = original_save

                persisted = self._read_bidirectional_state()
                request_id = persisted["request"]["id"]
                self.assertTrue(interrupted)
                self.assertEqual(persisted["request"]["phase"], "prepared")
                self.assertIsNone(persisted["pending"])
                self.assertIsNone(persisted["projection"])
                self.assertEqual(persisted["notion_write"]["phase"], "prepared")
                saved_baseline = copy.deepcopy(persisted["baseline"])

                if variant == "project_stamp":
                    self.project.items[0]["fieldValues"]["nodes"][0]["id"] = \
                        "PVTSV_changed_after_durable_prepare"
                elif variant == "facts":
                    self.repo_graph.issues[0]["state"] = "CLOSED"
                    self.repo_graph.issues[0]["stateReason"] = "COMPLETED"
                    self.repo_graph.issues[0]["closedAt"] = "2026-10-10T00:00:00Z"
                    self.rest.issues[0]["state"] = "closed"
                elif variant == "card":
                    self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                        "select": {"name": "준비 중"}}

                result = self.run_sync(bidirectional_enabled=True)
                resumed = self._read_bidirectional_state()
                self.assertEqual(resumed["request"]["id"], request_id)
                if variant == "stable":
                    self.assertEqual(result["held"], 0)
                    self.assertEqual(resumed["request"]["phase"], "completed")
                    self.assertEqual(self.project.status_mutation_attempts, 1)
                    self.assertEqual(len(self.project.writes), 1)
                    self.assertIsNone(resumed["pending"])
                    self.assertIsNone(resumed["projection"])
                else:
                    self.assertEqual(result["held"], 1)
                    self.assertNotEqual(resumed["request"]["phase"], "completed")
                    self.assertEqual(resumed["baseline"], saved_baseline)
                    self.assertEqual(self.project.status_mutation_attempts, 0)
                    self.assertEqual(self.project.writes, [])
                    if variant == "card":
                        self.assertEqual(resumed["deferred_request"]["target"], "준비 중")
                        self.assertEqual(se.read_select(
                            self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")

    def test_project_status_attempt_counter_tracks_failed_entries_and_accepts_valid_b(self):
        issue_a = make_issue()
        issue_b = copy.deepcopy(issue_a)
        issue_b.update(id="I_kwDOIssue19", databaseId=556, number=19)
        item_a = make_project_item(item_id="PVTI_issue_a", option="백로그",
                                   issue_id=issue_a["databaseId"],
                                   issue_node=issue_a["id"], number=issue_a["number"])
        item_b = make_project_item(item_id="PVTI_issue_b", option="백로그",
                                   issue_id=issue_b["databaseId"],
                                   issue_node=issue_b["id"], number=issue_b["number"])
        self.project = ProjectAPI([item_a, item_b])
        bad_targets = (
            (gp.SET_STATUS_MUTATION, {"project": "PVT_wrong", "item": item_b["id"],
                "field": STATUS_FIELD, "option": gp.EXPECTED_STATUS_OPTIONS["진행 중"]}),
            (gp.CLEAR_STATUS_MUTATION, {"project": gp.EXPECTED_PROJECT_ID,
                "item": "PVTI_missing", "field": STATUS_FIELD}),
        )
        for query, variables in bad_targets:
            with self.subTest(query=query), self.assertRaises(gp.SyncError):
                self.project.request(query, variables, mutation=True)
        self.assertEqual(self.project.status_mutation_attempts, 2)
        self.assertEqual([query for query, _ in self.project.status_mutation_entries],
                         [gp.SET_STATUS_MUTATION, gp.CLEAR_STATUS_MUTATION])
        self.assertEqual(self.project.writes, [])

        self.project.request(gp.SET_STATUS_MUTATION, {
            "project": gp.EXPECTED_PROJECT_ID, "item": item_b["id"],
            "field": STATUS_FIELD,
            "option": gp.EXPECTED_STATUS_OPTIONS["진행 중"]}, mutation=True)
        self.assertEqual(self.project.status_mutation_attempts, 3)
        self.assertEqual(self.project.writes[-1][1]["item"], item_b["id"])
        self.assertEqual(item_b["fieldValues"]["nodes"][0]["optionId"],
                         gp.EXPECTED_STATUS_OPTIONS["진행 중"])

    def test_prepared_request_restart_holds_when_project_stamp_changed(self):
        request = self._seed_prepared_request_without_pending(project_stamp_changed=True)
        result = self.run_sync(bidirectional_enabled=True)
        state = self._read_bidirectional_state()
        self.assertEqual(result["held"], 1)
        self.assertEqual(state["request"]["id"], request["id"])
        self.assertEqual(state["request"]["target"], "진행 중")
        self.assertEqual(state["request"]["phase"], "held")
        self.assertEqual(state["hold"]["code"], "RESUME_CHECKPOINT_CHANGED")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "진행 중")
        self.assertEqual(self.project.writes, [])

    def test_prepared_request_restart_holds_changed_facts_and_preserves_new_card(self):
        for change in ("facts", "card"):
            with self.subTest(change=change):
                request = self._seed_prepared_request_without_pending()
                if change == "facts":
                    self.repo_graph.issues[0]["state"] = "CLOSED"
                    self.repo_graph.issues[0]["stateReason"] = "COMPLETED"
                    self.repo_graph.issues[0]["closedAt"] = "2026-10-10T00:00:00Z"
                    self.rest.issues[0]["state"] = "closed"
                else:
                    self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                        "select": {"name": "준비 중"}}

                result = self.run_sync(bidirectional_enabled=True)
                state = self._read_bidirectional_state()
                self.assertEqual(result["held"], 1)
                self.assertEqual(state["request"]["id"], request["id"])
                self.assertEqual(state["request"]["phase"], "held")
                self.assertEqual(state["hold"]["code"], "RESUME_CHECKPOINT_CHANGED")
                self.assertEqual(self.project.writes, [])
                if change == "card":
                    self.assertEqual(se.read_select(
                        self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
                    self.assertEqual(state["deferred_request"]["target"], "준비 중")
                    self.assertNotEqual(state["deferred_request"]["id"], request["id"])

    def test_manual_resume_requires_trusted_main_and_matching_full_approved_sha(self):
        sha = "c" * 40
        base = {**pm_env(990001), "GITHUB_SHA": sha, "SYNC_APPROVED_SHA": sha,
                "SYNC_DISPATCH_REF": "refs/heads/main",
                "SYNC_DISPATCH_DRY_RUN": "false",
                "SYNC_DISPATCH_RESOLVE_ISSUES": "18"}
        with patch.object(se.gp, "resolve_actor", return_value=gp.EXPECTED_PM_USER_ID):
            self.assertEqual(se._validate_manual_resume(
                self.repo_graph, "18", base, dry_run=False)[0], {18})
            invalid = [
                {"GITHUB_REF": "refs/heads/feat/32-bidirectional-sync"},
                {"GITHUB_EVENT_NAME": "schedule"},
                {"GITHUB_SHA": "c" * 39},
                {"SYNC_APPROVED_SHA": "d" * 40},
                {"SYNC_DISPATCH_RESOLVE_ISSUES": "19"},
            ]
            for change in invalid:
                with self.subTest(change=change), self.assertRaises(se.SyncError):
                    se._validate_manual_resume(
                        self.repo_graph, "18", {**base, **change}, dry_run=False)
            feature_preview = {**base, "GITHUB_REF": "refs/heads/feat/32-bidirectional-sync",
                               "SYNC_DISPATCH_REF": "refs/heads/feat/32-bidirectional-sync",
                               "SYNC_DISPATCH_DRY_RUN": "true",
                               "SYNC_DISPATCH_RESOLVE_ISSUES": ""}
            self.assertEqual(se._validate_manual_resume(
                self.repo_graph, "", feature_preview, dry_run=True), (set(), None))
            with self.assertRaises(se.SyncError):
                se._validate_manual_resume(
                    self.repo_graph, "", feature_preview, dry_run=False)
            with self.assertRaises(se.SyncError):
                se._validate_manual_resume(
                    self.repo_graph, "18", feature_preview, dry_run=True)

    def test_prepared_request_ui_is_read_back_before_project_mutation_and_race_holds(self):
        for scenario in ("readback_failure", "new_card_move"):
            with self.subTest(scenario=scenario):
                self._configure_bidirectional_status(notion_status="진행 중")
                injected = False

                def interrupt_or_move(path, properties, notion):
                    nonlocal injected
                    raw = properties.get("동기화 내부 상태")
                    if (injected or path != f"/pages/{ISSUE_PAGE_ID}" or
                            "요청 처리" not in properties or not raw):
                        return
                    saved = json.loads(rich_text_value(raw))
                    if ((saved.get("request") or {}).get("phase") != "prepared" or
                            properties["요청 처리"].get("select", {}).get("name") !=
                            "반영 대기"):
                        return
                    injected = True
                    if scenario == "readback_failure":
                        notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)
                    else:
                        notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                            "select": {"name": "준비 중"}}

                self.notion.after_patch = interrupt_or_move
                if scenario == "readback_failure":
                    with self.assertRaisesRegex(se.SyncError, "readback failure"):
                        self.run_sync(bidirectional_enabled=True)
                else:
                    result = self.run_sync(bidirectional_enabled=True)
                    state = self._read_bidirectional_state()
                    self.assertEqual(result["held"], 1)
                    self.assertEqual(state["request"]["phase"], "held")
                    original_request_id = state["request"]["id"]
                    first_deferred_id = state["deferred_request"]["id"]
                    self.assertIsNotNone(state["deferred_request"])
                    self.assertEqual(state["deferred_request"]["target"], "준비 중")
                    self.assertEqual(state["hold"]["code"], "REQUEST_RACE")
                    self.assertEqual(se.read_select(
                        self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
                    self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                        "select": {"name": "완료"}}
                    later = self.run_sync(bidirectional_enabled=True)
                    later_state = self._read_bidirectional_state()
                    self.assertEqual(later["held"], 1)
                    self.assertEqual(later_state["request"]["id"], original_request_id)
                    self.assertEqual(later_state["deferred_request"]["target"], "완료")
                    self.assertNotEqual(later_state["deferred_request"]["id"],
                                        first_deferred_id)
                    self.assertEqual(se.read_select(
                        self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "완료")
                self.assertTrue(injected)
                self.assertEqual(self.project.writes, [])
                self.assertEqual(self.project.status_mutation_attempts, 0)
                self.assertEqual(self.project.status_mutation_entries, [])
                page = self.notion.pages[ISSUE_PAGE_ID]
                self.assertEqual(page["properties"]["요청 처리"]["select"]["name"],
                                 "반영 대기" if scenario == "readback_failure"
                                 else "PM 확인 필요")
                self.notion.after_patch = None

    def test_status_mutation_entry_has_independent_full_notion_get_snapshot(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        observed = []

        def inspect_mutation_entry(_project, query, variables):
            snapshots = [snapshot for path, snapshot in self.notion.read_snapshots
                         if path == f"/pages/{ISSUE_PAGE_ID}"]
            self.assertTrue(snapshots, "SET/CLEAR 진입 전에 실제 Notion GET이 없습니다")
            page = snapshots[-1]
            props = page["properties"]
            original_sends = [payload["properties"]
                for method, path, payload, _ in self.notion.writes
                if method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}" and
                "동기화 내부 상태" in payload.get("properties", {}) and
                json.loads(rich_text_value(
                    payload["properties"]["동기화 내부 상태"]
                )).get("notion_write", {}).get("phase") == "sent"]
            self.assertTrue(original_sends, "대기 UI 원본 전송을 찾지 못했습니다")
            original = original_sends[-1]
            for name, expected in original.items():
                self.assertEqual(props.get(name), expected, name)
            state = json.loads(rich_text_value(props["동기화 내부 상태"]))
            request = state["request"]
            pending = state["pending"]
            self.assertEqual(request["phase"], "sent")
            self.assertEqual(state["notion_write"]["phase"], "sent")
            self.assertEqual(state["notion_write"]["request_id"], request["id"])
            self.assertEqual(pending["request_id"], request["id"])
            self.assertEqual(pending["target_option_id"], variables.get("option"))
            self.assertEqual(request["target"], next(
                name for name, option in gp.EXPECTED_STATUS_OPTIONS.items()
                if option == variables.get("option")))
            observed.append((query, variables, copy.deepcopy(page), copy.deepcopy(original)))

        self.project.before_status_mutation = inspect_mutation_entry
        original_verify_snapshot = se.observation_clock.verify_snapshot
        se.observation_clock.verify_snapshot = lambda *_args, **_kwargs: (NOW, 3)
        try:
            self.run_sync(bidirectional_enabled=True)
        finally:
            se.observation_clock.verify_snapshot = original_verify_snapshot
            self.project.before_status_mutation = None
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], gp.SET_STATUS_MUTATION)
        self.assertEqual(self.project.status_mutation_attempts, 1)

    def test_sent_checkpoint_get_mismatch_and_loss_stop_set_before_attempt(self):
        def corrupt_baseline(props):
            saved = json.loads(rich_text_value(props["동기화 내부 상태"]))
            saved["baseline"]["observed_at"] = "2026-10-02T00:00:00Z"
            props["동기화 내부 상태"] = se.text_property(se.canonical_json(saved))

        corruptions = {
            "request handling": lambda props: props["요청 처리"].update(
                select={"name": "PM 확인 필요"}),
            "request target": lambda props: props["요청 상태"].update(
                select={"name": "백로그"}),
            "confirmation": lambda props: props["확인 필요"].update(
                rich_text=[{"type": "text", "text": {"content": "wrong"},
                            "plain_text": "wrong"}]),
            "sync time": lambda props: props["동기화 시각"].update(
                date={"start": "2026-10-01T00:00:00Z"}),
            "baseline binding": corrupt_baseline,
            "internal JSON": lambda props: props["동기화 내부 상태"].update(
                rich_text=[{"type": "text", "text": {"content": "{}"},
                            "plain_text": "{}"}]),
        }
        for label, corrupt in corruptions.items():
            with self.subTest(property=label):
                self._configure_bidirectional_status(notion_status="진행 중")
                armed = False
                sent_reads = 0

                def alter_sent_readback(path, snapshot):
                    nonlocal armed, sent_reads
                    if path != f"/pages/{ISSUE_PAGE_ID}":
                        return snapshot
                    state = se.decode_internal(
                        se.read_text(snapshot, "동기화 내부 상태"), kind="issue",
                        project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                    if ((state.get("notion_write") or {}).get("phase") != "sent"):
                        return snapshot
                    sent_reads += 1
                    if sent_reads == 1:
                        return snapshot
                    armed = True
                    corrupt(snapshot["properties"])
                    return snapshot

                self.notion.get_response_transform = alter_sent_readback
                original_verify_snapshot = se.observation_clock.verify_snapshot
                se.observation_clock.verify_snapshot = lambda *_args, **_kwargs: (NOW, 3)
                try:
                    with self.assertRaises(se.SyncError):
                        self.run_sync(bidirectional_enabled=True)
                finally:
                    se.observation_clock.verify_snapshot = original_verify_snapshot
                self.notion.get_response_transform = None
                state = self._read_bidirectional_state()
                self.assertTrue(armed)
                self.assertEqual(self.project.status_mutation_attempts, 0)
                self.assertEqual(self.project.status_mutation_entries, [])
                self.assertEqual(self.project.writes, [])
                self.assertEqual(state["request"]["id"],
                                 state["notion_write"]["request_id"])
                self.assertEqual(state["request"]["target"], "진행 중")
                self.assertNotEqual(state["request"]["phase"], "completed")
                self.assertEqual(state["baseline"]["notion_status"], "백로그")

        self._configure_bidirectional_status(notion_status="진행 중")
        sent_reads = 0

        def fail_after_sent_checkpoint(path, snapshot):
            nonlocal sent_reads
            if path != f"/pages/{ISSUE_PAGE_ID}":
                return snapshot
            saved = se.decode_internal(se.read_text(snapshot, "동기화 내부 상태"),
                                       kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
                                       object_id=ISSUE_ID)
            if (saved.get("notion_write") or {}).get("phase") == "sent":
                sent_reads += 1
                if sent_reads == 2:
                    raise se.SyncError("synthetic pre-mutation Notion GET failure")
            return snapshot

        self.notion.get_response_transform = fail_after_sent_checkpoint
        original_verify_snapshot = se.observation_clock.verify_snapshot
        se.observation_clock.verify_snapshot = lambda *_args, **_kwargs: (NOW, 3)
        try:
            with self.assertRaisesRegex(se.SyncError, "pre-mutation Notion GET failure"):
                self.run_sync(bidirectional_enabled=True)
        finally:
            se.observation_clock.verify_snapshot = original_verify_snapshot
            self.notion.get_response_transform = None
        self.assertEqual(sent_reads, 2)
        self.assertEqual(self.project.status_mutation_attempts, 0)
        self.assertEqual(self.project.status_mutation_entries, [])
        self.assertEqual(self.project.writes, [])
        failed_state = self._read_bidirectional_state()
        self.assertEqual(failed_state["request"]["target"], "진행 중")
        self.assertNotEqual(failed_state["request"]["phase"], "completed")
        self.assertEqual(failed_state["baseline"]["notion_status"], "백로그")

        self._configure_bidirectional_status(notion_status="진행 중")
        failed_after_send = False

        def fail_sent_checkpoint_readback(path, properties, notion):
            nonlocal failed_after_send
            raw = properties.get("동기화 내부 상태")
            if (not failed_after_send and path == f"/pages/{ISSUE_PAGE_ID}" and raw and
                    json.loads(rich_text_value(raw)).get("notion_write", {}).get(
                        "phase") == "sent"):
                failed_after_send = True
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = fail_sent_checkpoint_readback
        original_verify_snapshot = se.observation_clock.verify_snapshot
        se.observation_clock.verify_snapshot = lambda *_args, **_kwargs: (NOW, 3)
        try:
            with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
                self.run_sync(bidirectional_enabled=True)
        finally:
            se.observation_clock.verify_snapshot = original_verify_snapshot
        self.notion.after_patch = None
        self.assertTrue(failed_after_send)
        self.assertEqual(self.project.status_mutation_attempts, 0)
        self.assertEqual(self.project.status_mutation_entries, [])
        self.assertEqual(self.project.writes, [])

    def test_status_clear_entry_has_fresh_full_page_snapshot(self):
        baseline_issue = make_issue()
        closed_issue = make_issue(state="CLOSED", reason="NOT_PLANNED",
                                  closed_at="2026-10-03T00:00:00Z")
        self._configure_bidirectional_status(
            issue=closed_issue, baseline_issue=baseline_issue,
            notion_status="진행 중", baseline_notion_status="진행 중",
            project_status="진행 중", baseline_project_status="진행 중")
        entries = []

        def inspect_clear_entry(_project, query, variables):
            snapshots = [page for path, page in self.notion.read_snapshots
                         if path == f"/pages/{ISSUE_PAGE_ID}"]
            self.assertTrue(snapshots)
            self.assertEqual(query, gp.CLEAR_STATUS_MUTATION)
            state = self._read_bidirectional_state()
            self.assertIsNone(state["pending"]["target_option_id"])
            self.assertEqual(variables.get("item"), self.project.items[0]["id"])
            entries.append(copy.deepcopy(snapshots[-1]))

        self.project.before_status_mutation = inspect_clear_entry
        self.run_sync(bidirectional_enabled=True)
        self.project.before_status_mutation = None
        self.assertEqual(len(entries), 1)
        self.assertEqual([query for query, _ in self.project.status_mutation_entries],
                         [gp.CLEAR_STATUS_MUTATION])
        self.assertEqual(self.project.status_mutation_attempts, 1)

    def test_clear_pre_mutation_page_mismatch_or_get_failure_stops_clear(self):
        corruptions = {
            "request handling": lambda props: props["요청 처리"].update(
                select={"name": "PM 확인 필요"}),
            "request target": lambda props: props["요청 상태"].update(
                select={"name": "진행 중"}),
            "confirmation": lambda props: props["확인 필요"].update(
                rich_text=[{"type": "text", "text": {"content": "wrong"},
                            "plain_text": "wrong"}]),
            "sync time": lambda props: props["동기화 시각"].update(
                date={"start": "2026-10-01T00:00:00Z"}),
            "internal JSON": lambda props: props["동기화 내부 상태"].update(
                rich_text=[{"type": "text", "text": {"content": "{}"},
                            "plain_text": "{}"}]),
        }
        for label, corrupt in corruptions.items():
            with self.subTest(property=label):
                baseline_issue = make_issue()
                closed_issue = make_issue(state="CLOSED", reason="NOT_PLANNED",
                                          closed_at="2026-10-03T00:00:00Z")
                self._configure_bidirectional_status(
                    issue=closed_issue, baseline_issue=baseline_issue,
                    notion_status="진행 중", baseline_notion_status="진행 중",
                    project_status="진행 중", baseline_project_status="진행 중")
                armed = False
                pending_reads = 0

                def corrupt_after_pending(path, snapshot):
                    nonlocal armed, pending_reads
                    if path != f"/pages/{ISSUE_PAGE_ID}":
                        return snapshot
                    state = se.decode_internal(
                        se.read_text(snapshot, "동기화 내부 상태"), kind="issue",
                        project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                    pending = state.get("pending") or {}
                    if pending.get("kind") != "status" or pending.get("target_option_id") is not None:
                        return snapshot
                    pending_reads += 1
                    if pending_reads == 1:
                        return snapshot
                    armed = True
                    corrupt(snapshot["properties"])
                    return snapshot

                self.notion.get_response_transform = corrupt_after_pending
                original_verify_snapshot = se.observation_clock.verify_snapshot
                se.observation_clock.verify_snapshot = lambda *_args, **_kwargs: (NOW, 3)
                try:
                    with self.assertRaises(se.SyncError):
                        self.run_sync(bidirectional_enabled=True)
                finally:
                    se.observation_clock.verify_snapshot = original_verify_snapshot
                    self.notion.get_response_transform = None
                state = self._read_bidirectional_state()
                self.assertTrue(armed)
                self.assertEqual(pending_reads, 2)
                self.assertEqual(self.project.status_mutation_attempts, 0)
                self.assertEqual(self.project.status_mutation_entries, [])
                self.assertEqual(self.project.writes, [])
                self.assertIsNotNone(state["pending"])
                self.assertFalse(state["pending"]["confirmed"])
                self.assertEqual(state["baseline"]["notion_status"], "진행 중")

        baseline_issue = make_issue()
        closed_issue = make_issue(state="CLOSED", reason="NOT_PLANNED",
                                  closed_at="2026-10-03T00:00:00Z")
        self._configure_bidirectional_status(
            issue=closed_issue, baseline_issue=baseline_issue,
            notion_status="진행 중", baseline_notion_status="진행 중",
            project_status="진행 중", baseline_project_status="진행 중")
        pending_reads = 0

        def fail_clear_preflight(path, snapshot):
            nonlocal pending_reads
            if path != f"/pages/{ISSUE_PAGE_ID}":
                return snapshot
            state = se.decode_internal(se.read_text(snapshot, "동기화 내부 상태"),
                                       kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
                                       object_id=ISSUE_ID)
            pending = state.get("pending") or {}
            if pending.get("kind") == "status" and pending.get("target_option_id") is None:
                pending_reads += 1
                if pending_reads == 2:
                    raise se.SyncError("synthetic clear pre-mutation GET failure")
            return snapshot

        self.notion.get_response_transform = fail_clear_preflight
        original_verify_snapshot = se.observation_clock.verify_snapshot
        se.observation_clock.verify_snapshot = lambda *_args, **_kwargs: (NOW, 3)
        try:
            with self.assertRaisesRegex(se.SyncError, "clear pre-mutation GET failure"):
                self.run_sync(bidirectional_enabled=True)
        finally:
            se.observation_clock.verify_snapshot = original_verify_snapshot
            self.notion.get_response_transform = None
        self.assertEqual(pending_reads, 2)
        self.assertEqual(self.project.status_mutation_attempts, 0)
        self.assertEqual(self.project.status_mutation_entries, [])
        self.assertEqual(self.project.writes, [])

    def test_prepared_request_sent_property_readback_mismatch_stops_before_status_attempt(self):
        corruptions = {
            "request handling": lambda props: props["요청 처리"].update(
                select={"name": "PM 확인 필요"}),
            "request target": lambda props: props["요청 상태"].update(
                select={"name": "준비 중"}),
            "confirmation": lambda props: props["확인 필요"].update(
                rich_text=[{"type": "text", "text": {"content": "wrong"},
                            "plain_text": "wrong"}]),
        }
        for label, corrupt in corruptions.items():
            with self.subTest(property=label):
                self._configure_bidirectional_status(notion_status="진행 중")
                injected = False

                def alter_after_write(path, properties, notion):
                    nonlocal injected
                    if (injected or path != f"/pages/{ISSUE_PAGE_ID}" or
                            properties.get("요청 처리", {}).get("select", {}).get(
                                "name") != "반영 대기"):
                        return
                    injected = True
                    corrupt(notion.pages[ISSUE_PAGE_ID]["properties"])

                self.notion.after_patch = alter_after_write
                with self.assertRaises(se.SyncError):
                    self.run_sync(bidirectional_enabled=True)
                self.notion.after_patch = None
                self.assertTrue(injected)
                self.assertEqual(self.project.status_mutation_attempts, 0)
                self.assertEqual(self.project.status_mutation_entries, [])

    def test_project_set_failure_after_real_prepared_ui_is_not_automatically_replayed(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        self.project.fail_status_write_once = True
        prepared_ui_readbacks = []

        def observe_prepared_readback(path, properties, notion):
            raw = properties.get("동기화 내부 상태")
            if path != f"/pages/{ISSUE_PAGE_ID}" or not raw:
                return
            state = json.loads(rich_text_value(raw))
            if ((state.get("request") or {}).get("phase") == "prepared" and
                    properties.get("요청 처리", {}).get("select", {}).get("name") ==
                    "반영 대기"):
                prepared_ui_readbacks.append(copy.deepcopy(state))

        self.notion.after_patch = observe_prepared_readback
        with self.assertRaisesRegex(gp.SyncError, "failed before apply"):
            self.run_sync(bidirectional_enabled=True)
        self.notion.after_patch = None
        self.assertTrue(prepared_ui_readbacks)
        prepared = prepared_ui_readbacks[0]
        state = self._read_bidirectional_state()
        request_id = prepared["request"]["id"]
        self.assertEqual(state["request"]["id"], request_id)
        self.assertEqual(state["request"]["target"], "진행 중")
        self.assertEqual(state["request"]["phase"], "sent")
        self.assertEqual(self.project.status_mutation_attempts, 1)
        self.assertEqual(self.project.writes, [])
        self.assertEqual(state["baseline"]["project"]["option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["백로그"])

        # A different card move is held as a separate request while the original
        # sent operation remains unresolved; the next full sync must not resend SET.
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": "준비 중"}}
        resumed = self.run_sync(bidirectional_enabled=True)
        after = self._read_bidirectional_state()
        self.assertEqual(resumed["held"], 1)
        self.assertEqual(after["request"]["id"], request_id)
        self.assertEqual(after["request"]["target"], "진행 중")
        self.assertEqual(after["deferred_request"]["target"], "준비 중")
        self.assertNotEqual(after["deferred_request"]["id"], request_id)
        self.assertEqual(self.project.status_mutation_attempts, 1)
        self.assertEqual(self.project.writes, [])

    def test_semantic_display_sent_restart_skips_already_confirmed_user_patch(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        original_finalize = se._finalize_bidirectional_projection

        def stop_after_display_readback(*args, **kwargs):
            raise se.SyncError("synthetic crash after projection readback")

        se._finalize_bidirectional_projection = stop_after_display_readback
        try:
            with self.assertRaisesRegex(se.SyncError, "after projection readback"):
                self.run_sync(bidirectional_enabled=True)
        finally:
            se._finalize_bidirectional_projection = original_finalize
        state_before = self._read_bidirectional_state()
        self.assertEqual(state_before["projection"]["display_checkpoint"]["phase"],
                         "display_sent")
        original_display_payloads = []
        for properties in notion_patch_properties(self.notion.writes, ISSUE_PAGE_ID):
            raw = properties.get("동기화 내부 상태")
            if not raw or "작업 상태" not in properties:
                continue
            saved = json.loads(rich_text_value(raw))
            checkpoint = (saved.get("projection") or {}).get("display_checkpoint") or {}
            if checkpoint.get("phase") == "display_sent":
                original_display_payloads.append(copy.deepcopy(properties))
        self.assertEqual(len(original_display_payloads), 1)
        original_display = original_display_payloads[0]

        def page_matches(page, properties):
            return all(page["properties"].get(name) == expected
                       for name, expected in properties.items())

        self.assertTrue(any(path == f"/pages/{ISSUE_PAGE_ID}" and
                            page_matches(snapshot, original_display)
                            for path, snapshot in self.notion.read_snapshots),
                        "독립 GET snapshot이 실제 원본 display PATCH 전체와 일치하지 않습니다")
        self.assertTrue(se._projection_display_matches(
            self.notion.pages[ISSUE_PAGE_ID], state_before["projection"]))
        write_count_before_recovery = len(self.notion.writes)
        task_writes_before = len(task_status_patch_payloads(
            self.notion.writes, ISSUE_PAGE_ID))
        project_writes_before = copy.deepcopy(self.project.writes)

        recovered = self.run_sync(bidirectional_enabled=True)
        state_after = self._read_bidirectional_state()
        self.assertEqual(recovered["held"], 0)
        self.assertIsNone(state_after["projection"])
        self.assertEqual(state_after["request"]["phase"], "completed")
        self.assertEqual(len(task_status_patch_payloads(
            self.notion.writes, ISSUE_PAGE_ID)), task_writes_before + 1)
        recovery_ui = [
            payload["properties"] for method, path, payload, _ in self.notion.writes
            if method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}" and
            "요청 처리" in payload["properties"]
        ]
        self.assertEqual(
            [props["요청 처리"]["select"]["name"] for props in recovery_ui[-1:]],
            ["반영 완료"])
        self.assertEqual(sum(
            props["요청 처리"]["select"]["name"] == "반영 대기"
            for props in recovery_ui), 1)
        recovery_properties = notion_patch_properties(
            self.notion.writes[write_count_before_recovery:], ISSUE_PAGE_ID)
        self.assertEqual(sum(
            "작업 상태" in properties and
            (json.loads(rich_text_value(properties["동기화 내부 상태"])).get(
                "projection") or {}).get("display_checkpoint", {}).get("phase") ==
                "display_sent"
            for properties in recovery_properties
            if properties.get("동기화 내부 상태")), 0,
            "복구가 원본 display_sent PATCH를 중복 전송했습니다")
        terminal_payloads = [properties for properties in recovery_properties
            if properties.get("요청 처리", {}).get("select", {}).get("name") == "반영 완료"]
        self.assertTrue(terminal_payloads, "terminal completion PATCH를 찾지 못했습니다")
        terminal_payload = terminal_payloads[-1]
        self.assertEqual(json.loads(rich_text_value(
            terminal_payload["동기화 내부 상태"]))["projection"]["display_checkpoint"]["phase"],
            "completion_pending")
        self.assertTrue(any(path == f"/pages/{ISSUE_PAGE_ID}" and
                            page_matches(snapshot, terminal_payload)
                            for path, snapshot in self.notion.read_snapshots),
                        "terminal PATCH와 독립 GET snapshot이 일치하지 않습니다")
        self.assertEqual(self.project.writes, project_writes_before)

    def test_semantic_display_sent_restart_revalidates_every_transmitted_display_field(self):
        corruptions = {
            "request handling": lambda props: props["요청 처리"].update(
                select={"name": "요청 거절"}),
            "request target": lambda props: props["요청 상태"].update(
                select={"name": "준비 중"}),
            "confirmation": lambda props: props["확인 필요"].update(
                rich_text=[{"type": "text", "text": {"content": "변조"},
                            "plain_text": "변조"}]),
            "sync clock": lambda props: props["동기화 시각"].update(
                date={"start": "2020-01-01T00:00:00Z", "end": None,
                      "time_zone": None}),
        }
        for label, corrupt in corruptions.items():
            with self.subTest(property=label):
                self._configure_bidirectional_status(notion_status="진행 중")

                def crash_after_display(*_args, **_kwargs):
                    raise se.SyncError("synthetic crash after display sent")

                original_finalize = se._finalize_bidirectional_projection
                se._finalize_bidirectional_projection = crash_after_display
                try:
                    with self.assertRaisesRegex(se.SyncError, "after display sent"):
                        self.run_sync(bidirectional_enabled=True)
                finally:
                    se._finalize_bidirectional_projection = original_finalize
                before = self._read_bidirectional_state()
                self.assertEqual(before["projection"]["display_checkpoint"]["phase"],
                                 "display_sent")
                original_payload = next(properties for properties in
                    notion_patch_properties(self.notion.writes, ISSUE_PAGE_ID)
                    if "작업 상태" in properties and
                    properties.get("동기화 내부 상태") and
                    json.loads(rich_text_value(properties["동기화 내부 상태"]))
                    .get("projection", {}).get("display_checkpoint", {}).get("phase") ==
                    "display_sent")
                expected_user_display = {name: value for name, value in
                                         original_payload.items()
                                         if name != "동기화 내부 상태"}
                original_baseline = copy.deepcopy(before["baseline"])
                reads_before_restart = len(self.notion.read_snapshots)
                corrupt(self.notion.pages[ISSUE_PAGE_ID]["properties"])

                try:
                    self.run_sync(bidirectional_enabled=True)
                except se.SyncError:
                    pass  # A failed safe repair is acceptable only while the checkpoint remains.
                after = self._read_bidirectional_state()
                page = self.notion.pages[ISSUE_PAGE_ID]
                recovery_reads = [snapshot for path, snapshot in
                                  self.notion.read_snapshots[reads_before_restart:]
                                  if path == f"/pages/{ISSUE_PAGE_ID}"]
                verified_before_terminal = [snapshot for snapshot in recovery_reads
                    if all(snapshot["properties"].get(name) == value
                           for name, value in expected_user_display.items())
                    and (se.decode_internal(
                        se.read_text(snapshot, "동기화 내부 상태"), kind="issue",
                        project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                         or {}).get("request", {}).get("phase") != "completed"]
                self.assertTrue(verified_before_terminal,
                    "수정된 전송 속성의 완료 전 전체 readback을 확인하지 못했습니다")
                if after["request"]["phase"] == "completed":
                    self.assertEqual(se.read_select(page, "요청 처리"), "반영 완료")
                    self.assertEqual(after["baseline"]["notion_status"], "진행 중")
                else:
                    self.assertIsNotNone(after["projection"])
                    self.assertEqual(after["baseline"], original_baseline)
                    self.assertNotEqual(after["request"]["phase"], "completed")

    def test_legacy_v1_projection_recovers_after_display_write_without_resigning_checkpoint(self):
        for enabled in (False, True):
            with self.subTest(bidirectional_enabled=enabled):
                self._configure_bidirectional_status(project_status="준비 중")
                item = self.project.items[0]
                state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
                state["v"] = 1
                state["migration_complete"] = True
                state["review_pr_hash"] = se._digest([])
                state["project_item_id"] = item["id"]
                for key in ("baseline", "request", "readback", "notion_write",
                            "deferred_request"):
                    state.pop(key, None)
                current_facts = gp.fetch_repository_facts(self.repo_graph, self.rest)
                issue = gp.fetch_issue_detail(self.repo_graph, item["content"]["id"])
                refs, _ = se._linked_pr_facts(
                    issue, current_facts, allow_snapshot_drift=True)
                current_item = gp.fetch_project_item(
                    self.project, gp.EXPECTED_PROJECT_ID, item["id"], STATUS_FIELD)
                legacy_projection = {
                    "expected_option_id": gp.EXPECTED_STATUS_OPTIONS["준비 중"],
                    "expected_fingerprint": se._source_fingerprint(issue, refs, current_item),
                    "checkpoint": {key: state[key] for key in se.STATUS_CHECKPOINT_KEYS},
                }
                state["projection"] = copy.deepcopy(legacy_projection)
                self.notion.pages[ISSUE_PAGE_ID]["properties"]["동기화 내부 상태"] = \
                    se.text_property(se.canonical_json(state))
                current_issue = gp.fetch_issue_detail(self.repo_graph, issue["id"])
                current_refs, _ = se._linked_pr_facts(
                    current_issue, current_facts, allow_snapshot_drift=True)
                self.assertEqual(legacy_projection["expected_fingerprint"],
                                 se._projection_fingerprint(
                                     legacy_projection, current_issue, current_refs,
                                     current_item))
                self.assertEqual(legacy_projection["expected_option_id"],
                                 current_item["status_option_id"])
                failed = False

                def interrupt_before_projection_clear(path, properties):
                    nonlocal failed
                    raw = properties.get("동기화 내부 상태")
                    saved = json.loads(rich_text_value(raw)) if raw else {}
                    if (not failed and path == f"/pages/{ISSUE_PAGE_ID}" and
                            saved.get("projection") is None):
                        failed = True
                        return True
                    return False

                self.notion.fail_patch = interrupt_before_projection_clear
                with self.assertRaisesRegex(se.SyncError, "synthetic page write failure"):
                    self.run_sync(bidirectional_enabled=enabled)
                after_display = self._read_bidirectional_state()
                self.assertTrue(failed)
                self.assertEqual(after_display["projection"], legacy_projection)
                self.assertEqual(se.read_select(
                    self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
                self.assertEqual(self.project.writes, [])

                # The persisted v1-era fingerprint and IDs survive restart exactly;
                # the already-read-back display is not sent a second time.
                display_patch_count = sum(
                    1 for method, path, payload, _ in self.notion.writes
                    if method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}" and
                    "작업 상태" in payload["properties"])
                recovered = self.run_sync(
                    bidirectional_enabled=enabled, now="2026-10-11T00:05:00Z")
                final = self._read_bidirectional_state()
                self.assertEqual(recovered["held"], 0)
                self.assertIsNone(final["projection"])
                self.assertEqual(final["baseline"], after_display["baseline"])
                self.assertEqual(self.project.writes, [])
                self.assertEqual(sum(
                    1 for method, path, payload, _ in self.notion.writes
                    if method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}" and
                    "작업 상태" in payload["properties"]), display_patch_count)

                repeated = self.run_sync(bidirectional_enabled=enabled)
                final_after_poll = self._read_bidirectional_state()
                self.assertEqual(repeated["held"], 0)
                self.assertIsNone(final_after_poll["projection"])
                self.assertEqual(final_after_poll["baseline"] is not None, enabled)
                self.assertEqual(self.project.writes, [])

    def test_request_projection_preserves_baseline_valued_new_move_as_deferred(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        original_finalize = se._finalize_bidirectional_projection

        def stop_after_display_readback(*args, **kwargs):
            raise se.SyncError("synthetic crash after projection readback")

        se._finalize_bidirectional_projection = stop_after_display_readback
        try:
            with self.assertRaisesRegex(se.SyncError, "after projection readback"):
                self.run_sync(bidirectional_enabled=True)
        finally:
            se._finalize_bidirectional_projection = original_finalize
        older = self._read_bidirectional_state()
        self.assertIsNotNone(older["projection"])
        self.assertEqual(older["request"]["target"], "진행 중")
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": "백로그"}}

        held = self.run_sync(bidirectional_enabled=True)
        after = self._read_bidirectional_state()
        self.assertEqual(held["held"], 1)
        self.assertEqual(after["request"]["id"], older["request"]["id"])
        self.assertEqual(after["request"]["target"], "진행 중")
        self.assertNotEqual(after["deferred_request"]["id"], older["request"]["id"])
        self.assertEqual(after["deferred_request"]["target"], "백로그")
        self.assertEqual(after["deferred_request"]["phase"], "held")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "백로그")
        self.assertEqual(len(self.project.writes), 1)

    def test_completed_old_a_echo_does_not_replace_held_b_after_baseline_was_preserved(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        original_finalize = se._finalize_bidirectional_projection

        def stop_after_display_readback(*_args, **_kwargs):
            raise se.SyncError("synthetic interruption after old A display readback")

        se._finalize_bidirectional_projection = stop_after_display_readback
        try:
            with self.assertRaisesRegex(se.SyncError, "old A display readback"):
                self.run_sync(bidirectional_enabled=True)
        finally:
            se._finalize_bidirectional_projection = original_finalize

        old_a = self._read_bidirectional_state()
        old_a_id = old_a["request"]["id"]
        old_a_target = old_a["request"]["target"]
        previous_baseline = copy.deepcopy(old_a["baseline"])
        self.assertEqual(previous_baseline["notion_status"], "백로그")
        self.assertNotEqual(previous_baseline["notion_status"], old_a_target)

        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": "준비 중"}}
        held_b_result = self.run_sync(bidirectional_enabled=True)
        held_b = self._read_bidirectional_state()
        b = copy.deepcopy(held_b["deferred_request"])
        self.assertEqual(held_b_result["held"], 1)
        self.assertEqual(held_b["request"]["id"], old_a_id)
        self.assertEqual(b["target"], "준비 중")
        self.assertEqual(b["phase"], "held")

        settled_a = self.run_sync(
            bidirectional_enabled=True, resolve_issue_numbers="18",
            env=pm_env(944171))
        after_a = self._read_bidirectional_state()
        self.assertEqual(settled_a["held"], 1)
        self.assertEqual(after_a["request"]["id"], old_a_id)
        self.assertEqual(after_a["request"]["phase"], "completed")
        b_after_a = copy.deepcopy(after_a["deferred_request"])
        self.assertEqual(b_after_a["id"], b["id"])
        self.assertEqual(b_after_a["target"], b["target"])
        self.assertEqual(after_a["baseline"], previous_baseline)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         b_after_a["target"])
        project_writes = copy.deepcopy(self.project.writes)

        # The old A result reappears on the card after A is settled. It must not
        # become a replacement request for the independently held B move.
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": old_a_target}}
        for _ in range(2):
            poll = self.run_sync(bidirectional_enabled=True)
            after_poll = self._read_bidirectional_state()
            self.assertEqual(poll["held"], 1)
            self.assertEqual(after_poll["request"]["id"], old_a_id)
            self.assertEqual(after_poll["request"]["phase"], "completed")
            self.assertEqual(after_poll["deferred_request"], b_after_a)
            self.assertEqual(after_poll["baseline"], previous_baseline)
            self.assertEqual(after_poll["hold"]["code"], "DEFERRED_REQUEST_PENDING")
            self.assertEqual(se.read_select(
                self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), old_a_target)
            self.assertEqual(self.project.writes, project_writes)

    def test_pm_resume_of_old_projection_preserves_latest_c_after_b_across_restart(self):
        for b_status in ("백로그", "완료"):
            with self.subTest(b_status=b_status):
                self._configure_bidirectional_status(notion_status="진행 중")
                original_finalize = se._finalize_bidirectional_projection

                def stop_after_display_readback(*args, **kwargs):
                    raise se.SyncError("synthetic crash after projection readback")

                se._finalize_bidirectional_projection = stop_after_display_readback
                try:
                    with self.assertRaisesRegex(se.SyncError, "after projection readback"):
                        self.run_sync(bidirectional_enabled=True)
                finally:
                    se._finalize_bidirectional_projection = original_finalize
                a_state = self._read_bidirectional_state()
                a_id = a_state["request"]["id"]
                a_projection = copy.deepcopy(a_state["projection"])
                self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                    "select": {"name": b_status}}

                held_b = self.run_sync(bidirectional_enabled=True)
                after_b = self._read_bidirectional_state()
                b_id = after_b["deferred_request"]["id"]
                self.assertEqual(held_b["held"], 1)
                self.assertEqual(after_b["request"]["id"], a_id)
                self.assertEqual(after_b["projection"], a_projection)
                self.assertEqual(after_b["deferred_request"]["target"], b_status)
                self.assertEqual(se.read_select(
                    self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), b_status)
                self.assertEqual(len(self.project.writes), 1)

                # C is already on the page before PM reopens old A. The approval
                # must not be applied to C or permit A's display to overwrite it.
                c_status = "준비 중"
                self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                    "select": {"name": c_status}}
                task_patches_before = sum(
                    1 for method, path, payload, _ in self.notion.writes
                    if method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}" and
                    "작업 상태" in payload["properties"])
                pm_attempt = self.run_sync(
                    bidirectional_enabled=True, resolve_issue_numbers="18",
                    env=pm_env(918920))
                after_c = self._read_bidirectional_state()
                task_patches_after = sum(
                    1 for method, path, payload, _ in self.notion.writes
                    if method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}" and
                    "작업 상태" in payload["properties"])
                c_id = after_c["deferred_request"]["id"]
                self.assertEqual(pm_attempt["held"], 1)
                self.assertEqual(after_c["request"]["id"], a_id)
                self.assertEqual(after_c["projection"], a_projection)
                self.assertEqual(after_c["deferred_request"]["target"], c_status)
                self.assertNotEqual(c_id, b_id)
                self.assertEqual(after_c["hold"]["code"], "REQUEST_RACE")
                self.assertIsNone(after_c["resume"])
                prior_baseline = copy.deepcopy(after_c["baseline"])
                self.assertEqual(se.read_select(
                    self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), c_status)
                self.assertEqual(task_patches_after, task_patches_before)
                self.assertEqual(len(self.project.writes), 1)

                restarted = self.run_sync(bidirectional_enabled=True)
                after_restart = self._read_bidirectional_state()
                self.assertEqual(restarted["held"], 1)
                self.assertEqual(after_restart["request"]["id"], a_id)
                self.assertEqual(after_restart["projection"], a_projection)
                self.assertEqual(after_restart["deferred_request"]["id"], c_id)
                self.assertEqual(after_restart["deferred_request"]["target"], c_status)
                self.assertEqual(se.read_select(
                    self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), c_status)
                self.assertEqual(len(self.project.writes), 1)

                # A fresh PM decision may now confirm A while leaving C as a
                # separate held request; the following PM pass resolves C against
                # the current Project facts and must not replay a Project mutation.
                pm_a = self.run_sync(
                    bidirectional_enabled=True, resolve_issue_numbers="18",
                    env=pm_env(918921))
                after_a = self._read_bidirectional_state()
                self.assertEqual(pm_a["held"], 1)
                self.assertEqual(after_a["request"]["id"], a_id)
                self.assertEqual(after_a["request"]["phase"], "completed")
                self.assertEqual(after_a["deferred_request"]["id"], c_id)
                self.assertEqual(after_a["deferred_request"]["target"], c_status)
                self.assertEqual(after_a["hold"]["code"], "DEFERRED_REQUEST_PENDING")
                self.assertEqual(after_a["baseline"], prior_baseline)
                self.assertEqual(se.read_select(
                    self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), c_status)
                self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]
                                 ["요청 처리"]["select"]["name"], "PM 확인 필요")
                self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]
                                 ["요청 상태"]["select"]["name"], c_status)
                pm_a_task_payloads = task_status_patch_payloads(
                    self.notion.writes, ISSUE_PAGE_ID)
                self.assertTrue(pm_a_task_payloads)
                self.assertEqual(pm_a_task_payloads[-1]["select"]["name"], c_status)
                self.assertEqual(len(self.project.writes), 1)

                settled_id = after_a["deferred_request"]["id"]
                settled_reason = after_a["deferred_request"]["reason"]
                settled_task_writes = len(pm_a_task_payloads)
                for _ in range(2):
                    repeat_a = self.run_sync(bidirectional_enabled=True)
                    repeat_state = self._read_bidirectional_state()
                    self.assertEqual(repeat_a["held"], 1)
                    self.assertEqual(repeat_state["request"]["id"], a_id)
                    self.assertEqual(repeat_state["request"]["phase"], "completed")
                    self.assertEqual(repeat_state["deferred_request"]["id"], settled_id)
                    self.assertEqual(repeat_state["deferred_request"]["target"], c_status)
                    self.assertEqual(repeat_state["deferred_request"]["reason"], settled_reason)
                    self.assertEqual(se.read_select(
                        self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), c_status)
                    self.assertEqual(len(task_status_patch_payloads(
                        self.notion.writes, ISSUE_PAGE_ID)), settled_task_writes)
                    self.assertEqual(len(self.project.writes), 1)

                pm_c = self.run_sync(
                    bidirectional_enabled=True, resolve_issue_numbers="18",
                    env=pm_env(918922))
                terminal = self._read_bidirectional_state()
                self.assertEqual(pm_c["held"], 0)
                self.assertEqual(terminal["request"]["id"], c_id)
                self.assertEqual(terminal["request"]["phase"], "rejected")
                self.assertIsNone(terminal["deferred_request"])
                self.assertIsNone(terminal["hold"])
                self.assertEqual(terminal["baseline"], prior_baseline)
                self.assertEqual(se.read_select(
                    self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "진행 중")
                self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]
                                 ["요청 처리"]["select"]["name"], "요청 거절")
                self.assertEqual(len(self.project.writes), 1)
                settled_result = self.run_sync(bidirectional_enabled=True)
                terminal = self._read_bidirectional_state()
                self.assertEqual(settled_result["held"], 0)
                self.assertEqual(terminal["baseline"]["notion_status"], "진행 중")
                self.assertIsNone(
                    terminal["notion_write"],
                    f"request={terminal['request']!r} baseline={terminal['baseline']!r}")
                terminal_request = copy.deepcopy(terminal["request"])
                terminal_baseline = copy.deepcopy(terminal["baseline"])
                task_write_count = len(task_status_patch_payloads(
                    self.notion.writes, ISSUE_PAGE_ID))
                request_write_count = request_ui_patch_count(
                    self.notion.writes, ISSUE_PAGE_ID)
                for _ in range(2):
                    repeated = self.run_sync(bidirectional_enabled=True)
                    settled = self._read_bidirectional_state()
                    self.assertEqual(repeated["held"], 0)
                    self.assertEqual(settled["request"], terminal_request)
                    self.assertEqual(settled["baseline"], terminal_baseline)
                    self.assertIsNone(settled["notion_write"])
                    self.assertIsNone(settled["pending"])
                    self.assertIsNone(settled["projection"])
                    self.assertEqual(len(task_status_patch_payloads(
                        self.notion.writes, ISSUE_PAGE_ID)), task_write_count)
                    self.assertEqual(request_ui_patch_count(
                        self.notion.writes, ISSUE_PAGE_ID), request_write_count)
                    self.assertEqual(len(self.project.writes), 1)


    def test_deferred_b_pm_cannot_consume_stale_project_stamp_or_facts(self):
        freshness_variants = ("value_id_only", "updated_at_only", "closed_facts",
                              "baseline_mismatch")
        for variant in freshness_variants:
            for card_move in ("unchanged_b", "new_c"):
                with self.subTest(variant=variant, card_move=card_move):
                    self._configure_bidirectional_status(notion_status="진행 중")
                    original_finalize = se._finalize_bidirectional_projection

                    def stop_after_display(*_args, **_kwargs):
                        raise se.SyncError("synthetic crash after display readback")

                    se._finalize_bidirectional_projection = stop_after_display
                    try:
                        with self.assertRaisesRegex(se.SyncError, "after display readback"):
                            self.run_sync(bidirectional_enabled=True)
                    finally:
                        se._finalize_bidirectional_projection = original_finalize

                    self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                        "select": {"name": "준비 중"}}
                    held = self.run_sync(bidirectional_enabled=True)
                    held_state = self._read_bidirectional_state()
                    self.assertEqual(held["held"], 1)
                    b_id = held_state["deferred_request"]["id"]
                    self.assertEqual(held_state["deferred_request"]["target"], "준비 중")

                    # Settle old A first. B remains a distinct held request.
                    self.run_sync(bidirectional_enabled=True, resolve_issue_numbers="18",
                                  env=pm_env(938801))
                    settled = self._read_bidirectional_state()
                    self.assertEqual(settled["request"]["phase"], "completed")
                    self.assertEqual(settled["deferred_request"]["id"], b_id)
                    self.assertEqual(settled["deferred_request"]["target"], "준비 중")

                    # Persist and read back PM approval against the original stamp and
                    # facts first. Interrupt after the server has stored that approval.
                    original_verify = se._verify_issue_state_write
                    interrupted = False

                    def crash_after_resume_readback(notion, source_id, row, state):
                        nonlocal interrupted
                        result = original_verify(notion, source_id, row, state)
                        if (not interrupted and state.get("resume", {}).get("request_id") == b_id):
                            interrupted = True
                            raise se.SyncError("synthetic crash after PM approval readback")
                        return result

                    se._verify_issue_state_write = crash_after_resume_readback
                    try:
                        with self.assertRaisesRegex(se.SyncError, "approval readback"):
                            self.run_sync(bidirectional_enabled=True, resolve_issue_numbers="18",
                                          env=pm_env(938802))
                    finally:
                        se._verify_issue_state_write = original_verify
                    approved = self._read_bidirectional_state()
                    self.assertTrue(interrupted)
                    self.assertEqual(approved["request"]["id"], b_id)
                    self.assertEqual(approved["request"]["target"], "준비 중")
                    self.assertEqual(approved["resume"]["request_id"], b_id)

                    approved_baseline = copy.deepcopy(approved["baseline"])
                    writes_before = copy.deepcopy(self.project.writes)
                    mutation_count = self.project.status_mutation_attempts
                    item = self.project.items[0]
                    if variant == "value_id_only":
                        item["fieldValues"]["nodes"][0]["id"] = "PVTSV_recreated_same_option"
                    elif variant == "updated_at_only":
                        item["fieldValues"]["nodes"][0]["updatedAt"] = "2026-10-09T00:00:00Z"
                    elif variant == "closed_facts":
                        self.repo_graph.issues[0]["state"] = "CLOSED"
                        self.repo_graph.issues[0]["closedAt"] = "2026-10-09T00:00:00Z"
                        self.rest.issues[0]["state"] = "closed"
                    else:
                        latest = self._read_bidirectional_state()
                        latest["baseline"]["project"]["option_id"] = \
                            gp.EXPECTED_STATUS_OPTIONS["준비 중"]
                        page = self.notion.pages[ISSUE_PAGE_ID]
                        page["properties"]["동기화 내부 상태"] = se.text_property(
                            se.canonical_json(latest))
                    baseline_at_restart = copy.deepcopy(
                        self._read_bidirectional_state()["baseline"])
                    c_status = "준비 중" if card_move == "unchanged_b" else "백로그"
                    if card_move == "new_c":
                        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                            "select": {"name": c_status}}
                    self.run_sync(bidirectional_enabled=True)
                    current = self._read_bidirectional_state()
                    self.assertEqual(current["request"]["id"], b_id)
                    self.assertNotEqual(current["request"]["phase"], "completed")
                    if card_move == "unchanged_b":
                        self.assertIsNone(current["deferred_request"], repr(current))
                        self.assertEqual(current["request"]["target"], "준비 중")
                    else:
                        self.assertEqual(current["deferred_request"]["target"], c_status)
                        self.assertNotEqual(current["deferred_request"]["id"], b_id)
                    self.assertEqual(current["baseline"], baseline_at_restart)
                    if variant != "baseline_mismatch":
                        self.assertEqual(current["baseline"], approved_baseline)
                    self.assertIn(current["hold"]["code"],
                                  {"REQUEST_RACE", "DEFERRED_REQUEST_PENDING"})
                    self.assertEqual(current["resume"]["request_id"], b_id)
                    self.assertEqual(current["resume"]["approved_notion_status"], "준비 중")
                    self.assertEqual(se.read_select(
                        self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), c_status)
                    self.assertEqual(self.project.writes, writes_before)
                    self.assertEqual(self.project.status_mutation_attempts, mutation_count)

    def test_pending_project_request_preserves_baseline_valued_new_move(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        moved = False

        def move_back_and_lose_response(project, item, variables):
            nonlocal moved
            moved = True
            self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                "select": {"name": "백로그"}}
            raise gp.SyncError("synthetic committed status response loss after B move")

        self.project.after_status_write = move_back_and_lose_response
        with self.assertRaisesRegex(gp.SyncError, "after B move"):
            self.run_sync(bidirectional_enabled=True)
        first = self._read_bidirectional_state()
        self.assertTrue(moved)
        self.assertEqual(first["request"]["phase"], "sent")
        self.assertIsNotNone(first["pending"])
        self.assertEqual(len(self.project.writes), 1)

        self.project.after_status_write = None
        recovered = self.run_sync(bidirectional_enabled=True)
        after = self._read_bidirectional_state()
        self.assertEqual(recovered["held"], 1)
        self.assertEqual(after["request"]["id"], first["request"]["id"])
        self.assertEqual(after["request"]["target"], "진행 중")
        self.assertNotEqual(after["deferred_request"]["id"], first["request"]["id"])
        self.assertEqual(after["deferred_request"]["target"], "백로그")
        self.assertEqual(after["deferred_request"]["phase"], "held")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "백로그")
        self.assertEqual(len(self.project.writes), 1)

    def test_project_only_projection_keeps_new_request_separate_without_old_request_id(self):
        self._configure_bidirectional_status()
        changed_status = status_value("준비 중")
        self.project.items[0]["fieldValues"]["nodes"] = [changed_status]
        self.project.status_values[self.project.items[0]["id"]] = [changed_status]
        original_finalize = se._finalize_bidirectional_projection

        def stop_after_display_readback(*args, **kwargs):
            raise se.SyncError("synthetic crash after projection readback")

        se._finalize_bidirectional_projection = stop_after_display_readback
        try:
            with self.assertRaisesRegex(se.SyncError, "after projection readback"):
                self.run_sync(bidirectional_enabled=True)
        finally:
            se._finalize_bidirectional_projection = original_finalize
        older = self._read_bidirectional_state()
        self.assertIsNone(older["request"])
        self.assertIsNone(older["projection"]["request_id"])
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": "진행 중"}}

        held = self.run_sync(bidirectional_enabled=True)
        after = self._read_bidirectional_state()
        self.assertEqual(held["held"], 1)
        self.assertEqual(after["hold"]["code"], "REQUEST_RACE")
        self.assertIsNone(after["request"])
        self.assertEqual(after["projection"]["request_id"], None)
        self.assertEqual(after["deferred_request"]["target"], "진행 중")
        self.assertNotEqual(after["deferred_request"]["id"], None)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "진행 중")
        self.assertEqual(self.project.writes, [])

        repeat = self.run_sync(bidirectional_enabled=True)
        repeated = self._read_bidirectional_state()
        self.assertEqual(repeat["held"], 1)
        self.assertIsNone(repeated["request"])
        self.assertEqual(repeated["deferred_request"]["id"],
                         after["deferred_request"]["id"])
        prior_baseline = copy.deepcopy(repeated["baseline"])
        self.assertEqual(self.project.writes, [])

        old_result = self.run_sync(
            bidirectional_enabled=True, resolve_issue_numbers="18", env=pm_env(932018))
        confirmed = self._read_bidirectional_state()
        self.assertEqual(old_result["held"], 1)
        self.assertIsNone(confirmed["request"])
        self.assertEqual(confirmed["deferred_request"]["id"],
                         after["deferred_request"]["id"])
        self.assertEqual(confirmed["deferred_request"]["target"], "진행 중")
        self.assertEqual(confirmed["baseline"], prior_baseline)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "진행 중")
        self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]["요청 처리"]
                         ["select"]["name"], "PM 확인 필요")
        self.assertEqual(self.project.writes, [])

    def test_flag_enabled_project_relation_change_before_status_write_mutates_nothing(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        self.project.direct_item_transform = lambda item: {
            **item, "content": {**item["content"], "id": "I_kwDOOtherIssue"}}

        result = self.run_sync(bidirectional_enabled=True)

        state = self._read_bidirectional_state()
        self.assertEqual(result["held"], 1)
        self.assertEqual(state["hold"]["code"], "PROJECT_ITEM_RACE")
        self.assertEqual(self.project.writes, [])
        self.assertEqual(self.project.items[0]["fieldValues"]["nodes"][0]["optionId"],
                         gp.EXPECTED_STATUS_OPTIONS["백로그"])

    def test_valid_second_project_item_cannot_be_used_for_issue_a_status_write(self):
        for bad_relation in ("other_valid_issue_content", "wrong_project"):
            with self.subTest(bad_relation=bad_relation):
                self._configure_bidirectional_status(notion_status="진행 중")
                issue_a = copy.deepcopy(self.repo_graph.issues[0])
                issue_b = make_issue()
                issue_b.update(id="I_kwDOIssue19", databaseId=556, number=19,
                               url=f"https://github.com/{gp.REPOSITORY}/issues/19")
                item_a = self.project.items[0]
                item_b = make_project_item(item_id="PVTI_issue_b", option="백로그",
                    issue_id=issue_b["databaseId"], issue_node=issue_b["id"],
                    number=issue_b["number"])
                self.repo_graph = RepoGraph(issues=[issue_a, issue_b])
                self.rest = LegacyREST(issues=[
                    {"id": row["databaseId"], "node_id": row["id"],
                     "number": row["number"], "state": row["state"].lower()}
                    for row in (issue_a, issue_b)])
                self.project.items = [item_a, item_b]
                self.project.source_issues = {row["id"]: copy.deepcopy(row)
                                               for row in (issue_a, issue_b)}
                if bad_relation == "other_valid_issue_content":
                    self.project.direct_item_transform = lambda item: (
                        {**item, "content": copy.deepcopy(item_b["content"])}
                        if item["id"] == item_a["id"] else item)
                else:
                    self.project.direct_project_transform = lambda project: {
                        **project, "id": "PVT_wrong_valid_project"}

                if bad_relation == "wrong_project":
                    with self.assertRaisesRegex(gp.SyncError, "소속 Project 불일치"):
                        self.run_sync(bidirectional_enabled=True)
                else:
                    result = self.run_sync(bidirectional_enabled=True)
                    self.assertGreaterEqual(result["held"], 1)
                self.assertEqual(self.project.writes, [])
                self.assertEqual(self.project.status_mutation_attempts, 0)
                self.assertEqual(item_a["fieldValues"]["nodes"][0]["optionId"],
                                 gp.EXPECTED_STATUS_OPTIONS["백로그"])
                self.assertEqual(item_b["fieldValues"]["nodes"][0]["optionId"],
                                 gp.EXPECTED_STATUS_OPTIONS["백로그"])

    def test_archived_project_item_captures_new_notion_move_without_clearing_task(self):
        self._configure_bidirectional_status()
        self.project.items[0]["isArchived"] = True
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": "완료"}}

        result = self.run_sync(bidirectional_enabled=True)

        state = self._read_bidirectional_state()
        self.assertEqual(result["held"], 1)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "완료")
        self.assertIsNotNone(state["request"], repr(state))
        self.assertEqual(state["request"]["phase"], "held", repr(state))
        self.assertEqual(state["request"]["target"], "완료")
        self.assertEqual(state["hold"]["code"], "PROJECT_ITEM_ARCHIVED")
        self.assertEqual(self.project.writes, [])

    def test_initial_baseline_rechecks_latest_issue_facts_before_promotion(self):
        issue = make_issue()
        project_item = make_project_item(item_id="PVTI_uninitialized", option="백로그")
        self.repo_graph = RepoGraph(issues=[issue])
        self.repo_graph.issue_detail_transform = lambda latest: {
            **latest, "state": "CLOSED", "stateReason": "COMPLETED",
            "closedAt": "2026-10-03T00:00:00Z"}
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([project_item])
        initial = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        initial["migration_complete"] = True
        self.notion = FakeNotion([control_page(control_internal()),
            active_issue_page(task="백로그", internal=se.canonical_json(initial))])

        result = self.run_sync(bidirectional_enabled=True)

        state = self._read_bidirectional_state()
        self.assertEqual(result["held"], 1)
        self.assertIsNone(state["baseline"])
        self.assertEqual(state["hold"]["code"], "SOURCE_CHANGED_BEFORE_WRITE")
        self.assertEqual(self.project.writes, [])

    def test_conflict_restore_echo_keeps_original_request_until_real_new_move(self):
        self._configure_bidirectional_status(
            notion_status="진행 중", project_status="준비 중",
            baseline_project_status="백로그")
        self.run_sync(bidirectional_enabled=True)
        first = self._read_bidirectional_state()
        self.assertEqual(first["request"]["target"], "진행 중")
        original_id = first["request"]["id"]
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        request_patches = request_ui_patch_count(self.notion.writes, ISSUE_PAGE_ID)

        self.run_sync(bidirectional_enabled=True)
        self.run_sync(bidirectional_enabled=True)
        repeated = self._read_bidirectional_state()
        self.assertEqual(repeated["request"]["id"], original_id)
        self.assertEqual(repeated["request"]["target"], "진행 중")
        self.assertEqual(request_ui_patch_count(self.notion.writes, ISSUE_PAGE_ID),
                         request_patches)

        self.repo_graph.issues[0]["duplicateOf"] = {
            "id": "I_kwDOIssue17",
            "url": f"https://github.com/{gp.REPOSITORY}/issues/17"}
        self.run_sync(bidirectional_enabled=True)
        facts_changed = self._read_bidirectional_state()
        self.assertEqual(facts_changed["request"]["id"], original_id)
        self.assertEqual(facts_changed["request"]["target"], "진행 중")

        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": "완료"}}
        self.run_sync(bidirectional_enabled=True)
        moved = self._read_bidirectional_state()
        self.assertIsNotNone(moved["deferred_request"], repr(moved))
        self.assertNotEqual(moved["deferred_request"]["id"], original_id)
        self.assertEqual(moved["deferred_request"]["target"], "완료")

    def test_restore_response_loss_is_reconciled_without_replaying_notion_patch(self):
        self._configure_bidirectional_status(
            notion_status="진행 중", project_status="준비 중",
            baseline_project_status="백로그")
        injected = False

        def lose_restore_patch_readback(path, properties, notion):
            nonlocal injected
            raw = properties.get("동기화 내부 상태")
            if injected or path != f"/pages/{ISSUE_PAGE_ID}" or \
                    "작업 상태" not in properties or not raw:
                return
            marker = json.loads(rich_text_value(raw)).get("notion_write") or {}
            if marker.get("kind") == "restore" and marker.get("phase") == "sent":
                injected = True
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = lose_restore_patch_readback
        with self.assertRaises(se.SyncError):
            self.run_sync(bidirectional_enabled=True)

        sent = self._read_bidirectional_state()
        self.assertTrue(injected)
        self.assertEqual(sent["notion_write"]["phase"], "sent")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        restore_patch_count = sum(
            method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}" and
            "작업 상태" in payload["properties"] and
            payload["properties"]["작업 상태"]["select"]["name"] == "준비 중"
            for method, path, payload, _ in self.notion.writes)

        self.notion.after_patch = None
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": "완료"}}
        self.run_sync(bidirectional_enabled=True)

        recovered = self._read_bidirectional_state()
        self.assertEqual(recovered["notion_write"]["phase"], "uncertain")
        self.assertEqual(recovered["request"]["target"], "진행 중")
        self.assertEqual(recovered["deferred_request"]["target"], "완료")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "완료")
        self.assertEqual(sum(
            method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}" and
            "작업 상태" in payload["properties"] and
            payload["properties"]["작업 상태"]["select"]["name"] == "준비 중"
            for method, path, payload, _ in self.notion.writes), restore_patch_count)

    def test_restore_response_loss_with_applied_target_is_confirmed_without_replay(self):
        self._configure_bidirectional_status(
            notion_status="진행 중", project_status="준비 중",
            baseline_project_status="백로그")
        injected = False

        def lose_restore_patch_readback(path, properties, notion):
            nonlocal injected
            raw = properties.get("동기화 내부 상태")
            if injected or path != f"/pages/{ISSUE_PAGE_ID}" or \
                    "작업 상태" not in properties or not raw:
                return
            marker = json.loads(rich_text_value(raw)).get("notion_write") or {}
            if marker.get("kind") == "restore" and marker.get("phase") == "sent":
                injected = True
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = lose_restore_patch_readback
        with self.assertRaises(se.SyncError):
            self.run_sync(bidirectional_enabled=True)
        self.notion.after_patch = None
        first_count = sum(
            method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}" and
            "작업 상태" in payload["properties"] and
            payload["properties"]["작업 상태"]["select"]["name"] == "준비 중"
            for method, path, payload, _ in self.notion.writes)

        self.run_sync(bidirectional_enabled=True)

        state = self._read_bidirectional_state()
        self.assertEqual(state["notion_write"]["phase"], "confirmed")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(sum(
            method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}" and
            "작업 상태" in payload["properties"] and
            payload["properties"]["작업 상태"]["select"]["name"] == "준비 중"
            for method, path, payload, _ in self.notion.writes), first_count)

    def test_restore_recovery_requires_every_sent_projection_property(self):
        self._configure_bidirectional_status(
            notion_status="진행 중", project_status="준비 중",
            baseline_project_status="백로그")
        injected = False

        def lose_restore_patch_readback(path, properties, notion):
            nonlocal injected
            raw = properties.get("동기화 내부 상태")
            if injected or path != f"/pages/{ISSUE_PAGE_ID}" or not raw or \
                    "작업 상태" not in properties:
                return
            marker = json.loads(rich_text_value(raw)).get("notion_write") or {}
            if marker.get("kind") == "restore" and marker.get("phase") == "sent":
                injected = True
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = lose_restore_patch_readback
        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync(bidirectional_enabled=True)
        self.assertTrue(injected)
        sent = self._read_bidirectional_state()
        self.assertIn("expected_fingerprint", sent["notion_write"])
        self.assertIn("요청 처리", sent["notion_write"]["expected_fields"])
        restore_writes = sum(
            method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}" and
            "작업 상태" in payload.get("properties", {})
            for method, path, payload, _ in self.notion.writes)
        self.notion.after_patch = None
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["요청 처리"] = {
            "select": {"name": "반영 대기"}}

        self.run_sync(bidirectional_enabled=True)

        recovered = self._read_bidirectional_state()
        self.assertEqual(recovered["notion_write"]["phase"], "uncertain")
        self.assertEqual(recovered["hold"]["code"], "PENDING_RESULT_UNCLEAR")
        self.assertEqual(recovered["baseline"]["notion_status"], "백로그")
        self.assertEqual(sum(
            method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}" and
            "작업 상태" in payload.get("properties", {})
            for method, path, payload, _ in self.notion.writes), restore_writes)

    def test_prepared_restore_resumes_after_pre_patch_interruption(self):
        self._configure_bidirectional_status(
            notion_status="진행 중", project_status="준비 중",
            baseline_project_status="백로그")
        interrupted = False

        def stop_after_prepared_checkpoint(path, properties, notion):
            nonlocal interrupted
            raw = properties.get("동기화 내부 상태")
            if interrupted or path != f"/pages/{ISSUE_PAGE_ID}" or not raw:
                return
            marker = json.loads(rich_text_value(raw)).get("notion_write") or {}
            if marker.get("kind") == "restore" and marker.get("phase") == "prepared":
                interrupted = True
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = stop_after_prepared_checkpoint
        with self.assertRaises(se.SyncError):
            self.run_sync(bidirectional_enabled=True)
        prepared = self._read_bidirectional_state()
        self.assertTrue(interrupted)
        self.assertEqual(prepared["notion_write"]["phase"], "prepared")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "진행 중")
        self.notion.after_patch = None

        self.run_sync(bidirectional_enabled=True)

        recovered = self._read_bidirectional_state()
        self.assertEqual(recovered["notion_write"]["phase"], "confirmed")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(self.project.writes, [])

    def test_restore_prewrite_rechecks_latest_project_stamp(self):
        self._configure_bidirectional_status(
            notion_status="진행 중", project_status="준비 중",
            baseline_project_status="백로그")
        changed = False

        def reset_project_after_restore_prepared(path, properties, _notion):
            nonlocal changed
            raw = properties.get("동기화 내부 상태")
            if changed or path != f"/pages/{ISSUE_PAGE_ID}" or not raw:
                return
            marker = json.loads(rich_text_value(raw)).get("notion_write") or {}
            if marker.get("kind") == "restore" and marker.get("phase") == "prepared":
                changed = True
                old = self.project.items[0]["fieldValues"]["nodes"][0]
                reset = dict(old, id="PVTSV_restore_race", updatedAt="2026-10-09T12:00:00Z")
                self.project.items[0]["fieldValues"]["nodes"] = [reset]
                self.project.status_values[self.project.items[0]["id"]] = [reset]

        self.notion.after_patch = reset_project_after_restore_prepared

        result = self.run_sync(bidirectional_enabled=True)

        state = self._read_bidirectional_state()
        self.assertTrue(changed)
        self.assertEqual(result["held"], 1)
        self.assertEqual(state["hold"]["code"], "PENDING_RESULT_UNCLEAR")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "진행 중")
        self.assertEqual(self.project.writes, [])

    def test_prepared_restore_preserves_new_card_move_before_restore_write_and_restart(self):
        self._configure_bidirectional_status(
            notion_status="진행 중", project_status="준비 중",
            baseline_project_status="백로그")
        moved = False

        def move_card_after_restore_prepared(path, properties, notion):
            nonlocal moved
            raw = properties.get("동기화 내부 상태")
            if moved or path != f"/pages/{ISSUE_PAGE_ID}" or not raw:
                return
            marker = json.loads(rich_text_value(raw)).get("notion_write") or {}
            if marker.get("kind") == "restore" and marker.get("phase") == "prepared":
                moved = True
                notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                    "select": {"name": "백로그"}}

        self.notion.after_patch = move_card_after_restore_prepared
        result = self.run_sync(bidirectional_enabled=True)

        first = self._read_bidirectional_state()
        self.assertTrue(moved)
        self.assertEqual(result["held"], 1)
        self.assertEqual(first["hold"]["code"], "REQUEST_RACE")
        self.assertEqual(first["notion_write"]["kind"], "restore")
        self.assertEqual(first["notion_write"]["phase"], "prepared")
        self.assertEqual(first["request"]["phase"], "held")
        self.assertEqual(first["request"]["target"], "진행 중")
        self.assertIsNotNone(first["deferred_request"], repr(first))
        self.assertEqual(first["deferred_request"]["phase"], "held")
        self.assertEqual(first["deferred_request"]["target"], "백로그")
        self.assertNotEqual(first["deferred_request"]["id"], first["request"]["id"])
        deferred_id = first["deferred_request"]["id"]
        original_id = first["request"]["id"]
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "백로그")
        props = self.notion.pages[ISSUE_PAGE_ID]["properties"]
        self.assertEqual(props["요청 처리"]["select"]["name"], "PM 확인 필요")
        self.assertEqual(props["요청 상태"]["select"]["name"], "백로그")
        restore_writes = [
            payload["properties"]["작업 상태"]["select"]["name"]
            for method, path, payload, _ in self.notion.writes
            if method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}" and
            "작업 상태" in payload.get("properties", {})]
        self.assertNotIn("백로그", restore_writes)
        self.assertEqual(self.project.writes, [])

        self.notion.after_patch = None
        repeated = self.run_sync(bidirectional_enabled=True)
        after_restart = self._read_bidirectional_state()
        self.assertEqual(repeated["held"], 1)
        self.assertEqual(after_restart["hold"]["code"], "REQUEST_RACE")
        self.assertEqual(after_restart["notion_write"]["phase"], "prepared")
        self.assertEqual(after_restart["request"]["id"], original_id)
        self.assertEqual(after_restart["deferred_request"]["id"], deferred_id)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "백로그")
        props = self.notion.pages[ISSUE_PAGE_ID]["properties"]
        self.assertEqual(props["요청 처리"]["select"]["name"], "PM 확인 필요")
        self.assertEqual(props["요청 상태"]["select"]["name"], "백로그")
        restore_writes_after_restart = [
            payload["properties"]["작업 상태"]["select"]["name"]
            for method, path, payload, _ in self.notion.writes
            if method == "PATCH" and path == f"/pages/{ISSUE_PAGE_ID}" and
            "작업 상태" in payload.get("properties", {})]
        self.assertEqual(restore_writes_after_restart, restore_writes)
        self.assertEqual(self.project.writes, [])

    def test_restore_sent_marker_readback_preserves_baseline_move_before_task_write(self):
        self._configure_bidirectional_status(
            notion_status="진행 중", project_status="준비 중",
            baseline_project_status="백로그")
        moved = False

        def move_after_restore_sent_marker(path, properties, notion):
            nonlocal moved
            raw = properties.get("동기화 내부 상태")
            if moved or path != f"/pages/{ISSUE_PAGE_ID}" or not raw:
                return
            marker = json.loads(rich_text_value(raw)).get("notion_write") or {}
            if marker.get("kind") == "restore" and marker.get("phase") == "sent":
                moved = True
                notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                    "select": {"name": "백로그"}}

        self.notion.after_patch = move_after_restore_sent_marker
        restore_write_start = len(self.notion.writes)
        result = self.run_sync(bidirectional_enabled=True)

        state = self._read_bidirectional_state()
        self.assertTrue(moved)
        self.assertEqual(result["held"], 1)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "백로그")
        self.assertEqual(state["hold"]["code"], "REQUEST_RACE")
        self.assertEqual(state["request"]["phase"], "held")
        self.assertIsNotNone(state["deferred_request"])
        task_writes = task_status_patch_payloads(self.notion.writes[restore_write_start:],
                                                 ISSUE_PAGE_ID)
        self.assertEqual(task_writes, [])
        self.assertEqual(self.project.writes, [])

    def test_pm_resume_reconciles_unapplied_uncertain_restore_without_retry(self):
        self._configure_bidirectional_status(
            notion_status="진행 중", project_status="준비 중",
            baseline_project_status="백로그")
        failed = False

        def fail_before_restore_apply(path, properties):
            nonlocal failed
            raw = properties.get("동기화 내부 상태")
            if failed or path != f"/pages/{ISSUE_PAGE_ID}" or \
                    "작업 상태" not in properties or not raw:
                return False
            marker = json.loads(rich_text_value(raw)).get("notion_write") or {}
            if marker.get("kind") == "restore" and marker.get("phase") == "sent":
                failed = True
                return True
            return False

        self.notion.fail_patch = fail_before_restore_apply
        with self.assertRaises(se.SyncError):
            self.run_sync(bidirectional_enabled=True)
        uncertain = self._read_bidirectional_state()
        self.assertTrue(failed)
        self.assertEqual(uncertain["notion_write"]["phase"], "sent")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "진행 중")
        self.notion.fail_patch = None

        self.run_sync(bidirectional_enabled=True, resolve_issue_numbers="18",
                      env=pm_env(928322))

        resolved = self._read_bidirectional_state()
        self.assertIsNone(resolved["notion_write"])
        self.assertIn(resolved["request"]["phase"], {"rejected", "completed"})
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(self.project.writes, [])

    def test_verified_fresh_date_flows_from_sync_to_serialized_schema2_report(self):
        self._configure_bidirectional_status()
        report_result = {}
        observed = "2026-10-08T00:00:00Z"
        uncertainty = 3
        marks = []

        def checked(mark, fresh_date):
            marks.append(mark)
            self.assertEqual(fresh_date(), {
                "date": "Thu, 08 Oct 2026 00:00:00 GMT",
                "cache_control": "no-cache, no-store", "age": "0", "x_cache": None})
            return observed, uncertainty

        with patch.object(observation_clock, "verify_snapshot", side_effect=checked), \
                patch.object(observation_clock, "fetch_fresh_date", return_value={
                    "date": "Thu, 08 Oct 2026 00:00:00 GMT",
                    "cache_control": "no-cache, no-store", "age": "0", "x_cache": None}):
            self.run_sync(notification_result=report_result)
        self.assertEqual(report_result["observed_at"], observed)
        self.assertEqual(report_result["observation_uncertainty_seconds"], uncertainty)
        self.assertEqual(len(marks), 2)
        sha = "a" * 40
        env = {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": sha}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(nr.subprocess, "run",
                             return_value=type("Result", (), {"stdout": sha + "\n"})()):
            path = Path(directory) / nr.FILE_NAME
            nr.write(path, env, kind="complete", scan_complete=True,
                     observed_at=report_result["observed_at"],
                     observation_uncertainty_seconds=report_result[
                         "observation_uncertainty_seconds"])
            serialized = json.loads(path.read_text())
        self.assertEqual(serialized["schema"], 2)
        self.assertEqual(serialized["observed_at"], observed)
        self.assertEqual(serialized["observation_uncertainty_seconds"], uncertainty)

    def test_flag_off_to_on_recovers_sent_status_without_mutation_resend(self):
        self._configure_bidirectional_status(notion_status="진행 중")

        def lose_committed_response(project, item, variables):
            raise gp.SyncError("synthetic committed status response loss")

        self.project.after_status_write = lose_committed_response
        with self.assertRaisesRegex(gp.SyncError, "synthetic committed status response loss"):
            self.run_sync(bidirectional_enabled=True)
        sent = self._read_bidirectional_state()
        request_id = sent["request"]["id"]
        self.assertEqual(sent["request"]["phase"], "sent")
        self.assertEqual(len(self.project.writes), 1)

        self.project.after_status_write = None
        paused = self.run_sync(bidirectional_enabled=False)
        paused_state = self._read_bidirectional_state()
        self.assertEqual(paused["held"], 1)
        self.assertEqual(paused_state["request"]["id"], request_id)
        self.assertEqual(paused_state["request"]["phase"], "sent")
        self.assertEqual(len(self.project.writes), 1)

        resumed = self.run_sync(bidirectional_enabled=True)
        recovered = self._read_bidirectional_state()
        self.assertEqual(resumed["held"], 0)
        self.assertEqual(recovered["request"]["id"], request_id)
        self.assertEqual(recovered["request"]["phase"], "completed")
        self.assertEqual(len(self.project.writes), 1)

    def test_flag_enabled_uncertain_unapplied_status_is_held_without_resend(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        fail_pending_readback = True

        def stop_after_prepared_checkpoint(path, properties, notion):
            nonlocal fail_pending_readback
            raw = properties.get("동기화 내부 상태")
            if not fail_pending_readback or path != f"/pages/{ISSUE_PAGE_ID}" or not raw:
                return
            saved = json.loads(rich_text_value(raw))
            if saved.get("request", {}).get("phase") == "prepared" and saved.get("pending"):
                fail_pending_readback = False
                notion.after_patch = None
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = stop_after_prepared_checkpoint
        with self.assertRaises(se.SyncError):
            self.run_sync(bidirectional_enabled=True)
        state = self._read_bidirectional_state()
        state["request"]["phase"] = "uncertain"
        state["notion_write"]["phase"] = "uncertain"
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["동기화 내부 상태"] = \
            se.text_property(se.canonical_json(state))
        request_id = state["request"]["id"]
        self.assertEqual(self.project.items[0]["fieldValues"]["nodes"][0]["optionId"],
                         gp.EXPECTED_STATUS_OPTIONS["백로그"])

        result = self.run_sync(bidirectional_enabled=True)

        recovered = self._read_bidirectional_state()
        self.assertEqual(result["held"], 1)
        self.assertEqual(recovered["request"]["id"], request_id)
        self.assertEqual(recovered["request"]["phase"], "uncertain")
        self.assertIsNotNone(recovered["pending"])
        self.assertEqual(recovered["hold"]["code"], "PENDING_RESULT_UNCLEAR")
        self.assertEqual(len(self.project.writes), 0)

    def test_serialized_v1_pending_hold_resume_and_projection_migrate_without_checkpoint_loss(self):
        issue = make_issue()
        checkpoint = {"migration_complete": True, "review_cycle": 2,
                      "review_return_cycle": 1, "reopen_last_id": "R_PRE",
                      "review_pr_hash": se._digest(["PR_1"])}
        base = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        for key in ("baseline", "request", "readback", "notion_write", "deferred_request"):
            base.pop(key, None)
        base.update({"v": 1, "migration_complete": True,
                     "project_item_id": "PVTI_issue18", "review_cycle": 2,
                     "review_return_cycle": 1, "reopen_last_id": "R_PRE",
                     "review_pr_hash": checkpoint["review_pr_hash"],
                     "hold": se._new_hold("LEGACY_HOLD", "saved", se._digest("hold")),
                     "resume": {"actor_id": gp.EXPECTED_PM_USER_ID, "run_id": "918777",
                                "approved_option_id": gp.EXPECTED_STATUS_OPTIONS["백로그"],
                                "fingerprint": se._digest("resume"),
                                "display_pending": True}})
        pending_state = copy.deepcopy(base)
        pending_state["pending"] = make_pending_status_state(
            issue, migration_complete=True)["pending"]
        legacy_hash = pending_state["pending"]["facts_fingerprint"]
        decoded_pending = se.decode_internal(
            se.canonical_json(pending_state), kind="issue",
            project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(decoded_pending["v"], 2)
        self.assertEqual(decoded_pending["pending"]["facts_fingerprint"], legacy_hash)
        self.assertEqual(decoded_pending["pending"]["checkpoint"],
                         pending_state["pending"]["checkpoint"])
        self.assertEqual(decoded_pending["hold"], pending_state["hold"])
        self.assertEqual(decoded_pending["resume"], pending_state["resume"])

        live_issue = make_issue()
        live_pending_state = make_pending_status_state(live_issue, before="백로그",
                                                       target="백로그",
                                                       migration_complete=True)
        legacy_hash = live_pending_state["pending"]["facts_fingerprint"]
        for key in ("baseline", "request", "readback", "notion_write", "deferred_request"):
            live_pending_state.pop(key, None)
        live_pending_state["v"] = 1
        live_pending_state["hold"] = se._new_hold(
            "LEGACY_HOLD", "saved", se._digest("hold"))
        self.repo_graph = RepoGraph(issues=[live_issue])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="백로그")])
        self.notion = FakeNotion([control_page(control_internal()),
            active_issue_page(task="백로그", internal=se.canonical_json(live_pending_state))])
        self.run_sync()
        migrated = self._read_bidirectional_state()
        self.assertEqual(migrated["v"], 2)
        self.assertTrue(migrated["migration_complete"])
        self.assertIsNone(migrated["pending"])
        self.assertEqual(self.project.writes, [])
        self.assertEqual(legacy_hash, live_pending_state["pending"]["facts_fingerprint"])

        projection_state = copy.deepcopy(base)
        projection_state["projection"] = {
            "expected_option_id": gp.EXPECTED_STATUS_OPTIONS["진행 중"],
            "expected_fingerprint": se._digest("projection"), "checkpoint": checkpoint}
        decoded_projection = se.decode_internal(
            se.canonical_json(projection_state), kind="issue",
            project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(decoded_projection["projection"], projection_state["projection"])
        self.assertEqual(decoded_projection["hold"], projection_state["hold"])
        self.assertEqual(decoded_projection["resume"], projection_state["resume"])

    def test_completed_promotion_waits_for_each_request_display_property_readback(self):
        for property_name in ("작업 상태", "요청 처리", "요청 상태", "확인 필요",
                              "동기화 시각", "동기화 내부 상태"):
            with self.subTest(property=property_name):
                self._configure_bidirectional_status(notion_status="진행 중")
                original_baseline = copy.deepcopy(self._read_bidirectional_state()["baseline"])
                corrupted = False

                def mismatch_one_property(path, properties, notion):
                    nonlocal corrupted
                    if corrupted or path != f"/pages/{ISSUE_PAGE_ID}" or \
                            property_name not in properties:
                        return
                    internal = properties.get("동기화 내부 상태")
                    candidate = json.loads(rich_text_value(internal)) if internal else {}
                    final_display_write = (properties.get("요청 처리", {}).get(
                        "select", {}).get("name") == "반영 완료")
                    projection_write = (candidate.get("request", {}).get("phase") == "confirmed" and
                                        candidate.get("projection") is not None)
                    if (property_name in {"요청 처리", "요청 상태", "확인 필요",
                                          "동기화 내부 상태"} and not final_display_write) or \
                            (property_name in {"작업 상태", "동기화 시각"} and not projection_write):
                        return
                    corrupted = True
                    if property_name == "작업 상태":
                        notion.pages[ISSUE_PAGE_ID]["properties"][property_name] = {
                            "select": {"name": "준비 중"}}
                    elif property_name == "요청 처리":
                        notion.pages[ISSUE_PAGE_ID]["properties"][property_name] = {
                            "select": {"name": "자동 확인 중"}}
                    elif property_name == "요청 상태":
                        notion.pages[ISSUE_PAGE_ID]["properties"][property_name] = {
                            "select": {"name": "준비 중"}}
                    elif property_name == "확인 필요":
                        notion.pages[ISSUE_PAGE_ID]["properties"][property_name] = \
                            se.text_property("old hold display")
                    else:
                        if property_name == "동기화 시각":
                            notion.pages[ISSUE_PAGE_ID]["properties"][property_name] = {
                                "date": {"start": "2026-10-07T00:00:00Z"}}
                        else:
                            saved = json.loads(rich_text_value(
                                notion.pages[ISSUE_PAGE_ID]["properties"][property_name]))
                            saved["baseline"] = None
                            notion.pages[ISSUE_PAGE_ID]["properties"][property_name] = \
                                se.text_property(se.canonical_json(saved))

                self.notion.after_patch = mismatch_one_property
                with self.assertRaises(se.SyncError):
                    self.run_sync(bidirectional_enabled=True)
                self.assertTrue(corrupted)
                interrupted = self._read_bidirectional_state()
                self.assertNotEqual((interrupted.get("baseline") or {}).get("notion_status"),
                                    "진행 중")
                self.assertIsNotNone(interrupted["projection"])
                self.assertEqual(len(self.project.writes), 1)

                if property_name not in {"작업 상태", "동기화 내부 상태"}:
                    self.notion.after_patch = None
                    self.run_sync(bidirectional_enabled=True)
                    repaired = self._read_bidirectional_state()
                    self.assertEqual(repaired["request"]["phase"], "completed")
                    self.assertIsNone(repaired["projection"])
                    self.assertEqual(repaired["baseline"]["notion_status"], "진행 중")
                    self.assertEqual(len(self.project.writes), 1)

    def test_sent_project_response_loss_recovers_with_bound_projection_without_mutation_retry(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        lost = False

        def lose_status_response(project, item, variables):
            nonlocal lost
            if not lost:
                lost = True
                raise gp.SyncError("synthetic committed status response loss")

        self.project.after_status_write = lose_status_response
        with self.assertRaisesRegex(gp.SyncError, "synthetic committed status response loss"):
            self.run_sync(bidirectional_enabled=True)
        first = self._read_bidirectional_state()
        self.assertEqual(first["request"]["phase"], "sent")
        self.assertTrue(first["pending"])
        self.assertEqual(len(self.project.writes), 1)

        self.project.after_status_write = None
        result = self.run_sync(bidirectional_enabled=True)
        recovered = self._read_bidirectional_state()
        props = self.notion.pages[ISSUE_PAGE_ID]["properties"]
        self.assertEqual(result["held"], 0)
        self.assertEqual(recovered["request"]["phase"], "completed")
        self.assertIsNone(recovered["pending"])
        self.assertIsNone(recovered["projection"])
        self.assertEqual(props["요청 처리"]["select"]["name"], "반영 완료")
        self.assertEqual(len(self.project.writes), 1)
        self.assertEqual(self.run_sync(bidirectional_enabled=True)["project_changes"], 0)
        self.assertEqual(len(self.project.writes), 1)

    def test_closed_facts_do_not_seed_a_contradictory_initial_baseline(self):
        issue = make_issue(state="CLOSED", reason="COMPLETED",
                           closed_at="2026-10-03T00:00:00Z")
        self._configure_bidirectional_status(notion_status="백로그", project_status="백로그",
                                             issue=issue)
        state = self._read_bidirectional_state()
        state["baseline"] = None
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["동기화 내부 상태"] = se.text_property(
            se.canonical_json(state))
        lost = False

        def lose_projection_readback(path, properties, notion):
            nonlocal lost
            raw = properties.get("동기화 내부 상태")
            if lost or not path.startswith("/pages/") or not raw:
                return
            saved = json.loads(rich_text_value(raw))
            if saved.get("projection") and saved.get("baseline") is None:
                lost = True
                notion.fail_readback_after_patch.add(path.rsplit("/", 1)[1])

        self.notion.after_patch = lose_projection_readback
        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync(bidirectional_enabled=True)
        interrupted = self._read_bidirectional_state()
        self.assertTrue(lost)
        self.assertIsNone(interrupted["baseline"])
        self.assertIsNotNone(interrupted["projection"])
        self.assertEqual(len(self.project.writes), 1)

        self.notion.after_patch = None
        self.run_sync(bidirectional_enabled=True)
        settled = self._read_bidirectional_state()
        self.assertEqual(settled["baseline"]["notion_status"], "완료")
        self.assertEqual(settled["baseline"]["project"]["option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["완료"])
        self.assertEqual(len(self.project.writes), 1)

    def test_add_readback_third_reserved_query_loss_becomes_durable_pm_hold(self):
        self._configure_post_cutoff_project_add()
        self.project.add_response_loss = False
        self.project.add_visibility_delay_snapshots = 99
        self.run_sync()
        calls_before = self.project.post_add_snapshot_calls
        lost = False

        def lose_third_reservation_readback(path, properties, notion):
            nonlocal lost
            raw = properties.get("동기화 내부 상태")
            if lost or not path.startswith("/pages/") or not raw:
                return
            saved = json.loads(rich_text_value(raw))
            if (saved.get("readback") or {}).get("attempts") == 3 and \
                    saved["readback"].get("last_result") is None:
                lost = True
                notion.fail_readback_after_patch.add(path.rsplit("/", 1)[1])

        self.notion.after_patch = lose_third_reservation_readback
        self.run_sync()
        self.run_sync()
        before_third_reservation = self.project.post_add_snapshot_calls
        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync()
        stranded = self._stored_issue_state()[1]
        self.assertTrue(lost)
        self.assertEqual(stranded["readback"]["attempts"], 3)
        self.assertIsNone(stranded["readback"]["last_result"])
        self.assertIsNone(stranded["hold"])
        direct_reads_at_exhaustion = self.project.direct_item_reads

        self.notion.after_patch = None
        held = self.run_sync()
        recovered_hold = self._stored_issue_state()[1]
        self.assertGreaterEqual(held["held"], 1)
        self.assertEqual(recovered_hold["hold"]["code"], "ADD_READBACK_EXHAUSTED")
        self.assertEqual(self.project.direct_item_reads, direct_reads_at_exhaustion)
        self.assertEqual(self.project.add_calls, 1)

    def test_add_returned_id_is_durable_before_relation_queries_and_reentry_never_readds(self):
        self._configure_post_cutoff_project_add()
        self.project.add_response_loss = False
        failed = False

        def fail_returned_id_readback(path, properties, notion):
            nonlocal failed
            if failed or "동기화 내부 상태" not in properties:
                return
            saved = json.loads(rich_text_value(properties["동기화 내부 상태"]))
            returned_id = (saved.get("readback") or {}).get("returned_item_id")
            if returned_id:
                failed = True
                self.assertEqual(self.project.add_calls, 1)
                self.assertEqual(self.project.direct_item_reads, 0)
                self.assertEqual(self.project.post_add_snapshot_calls, 0)
                notion.fail_readback_after_patch.add(path.rsplit("/", 1)[1])

        self.notion.after_patch = fail_returned_id_readback
        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync()
        self.assertTrue(failed)
        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual(self.project.direct_item_reads, 0)

        self.notion.after_patch = None
        self.run_sync()
        _, state = self._stored_issue_state()
        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual(state["readback"]["returned_item_id"], "PVTI_added_1_0")

    def test_dry_run_add_readback_with_returned_id_but_missing_item_is_read_only(self):
        self._configure_post_cutoff_project_add()
        self.project.add_response_loss = False
        self.project.add_visibility_delay_snapshots = 99
        self.run_sync()
        _, waiting = self._stored_issue_state()
        self.assertEqual(waiting["readback"]["attempts"], 0)
        self.assertFalse(waiting["readback"]["validated"])
        self.assertEqual(waiting["readback"]["returned_item_id"], "PVTI_added_1_0")
        writes_before = copy.deepcopy(self.notion.writes)
        project_writes_before = copy.deepcopy(self.project.writes)

        preview = self.run_sync(dry_run=True)

        issue_plan = next(plan for plan in preview["issue_plans"]
                          if plan["issue_number"] == ISSUE_NUMBER)
        self.assertEqual(issue_plan["hold"]["code"], "PROJECT_ADD_UNCERTAIN")
        self.assertEqual(self.notion.writes, writes_before)
        self.assertEqual(self.project.writes, project_writes_before)
        self.assertEqual(self.project.add_calls, 1)

    def test_add_readback_reservation_readback_loss_is_durable_and_consumes_attempt(self):
        self._configure_post_cutoff_project_add()
        self.project.add_response_loss = False
        self.project.add_visibility_delay_snapshots = 99
        self.run_sync()
        run_one = {"GITHUB_RUN_ID": "777", "GITHUB_RUN_ATTEMPT": "1"}
        failed = False

        def fail_first_reservation_readback(path, properties, notion):
            nonlocal failed
            if failed or not path.startswith("/pages/"):
                return
            raw = properties.get("동기화 내부 상태")
            if not raw:
                return
            saved = json.loads(rich_text_value(raw))
            reservation = (saved.get("readback") or {}).get("reservation") or {}
            if reservation.get("attempt") == 1 and \
                    saved["readback"].get("last_result") is None:
                failed = True
                notion.fail_readback_after_patch.add(path.rsplit("/", 1)[1])

        self.notion.after_patch = fail_first_reservation_readback
        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync(env=run_one)
        self.assertTrue(failed)
        _, reserved = self._stored_issue_state()
        self.assertEqual(reserved["readback"]["attempts"], 1)
        token = reserved["readback"]["reservation"]["token"]
        self.assertIsNone(reserved["readback"]["last_result"])
        direct_reads_after_crash = self.project.direct_item_reads

        self.notion.after_patch = None
        self.run_sync(env=run_one)
        _, same_run = self._stored_issue_state()
        self.assertEqual(same_run["readback"]["attempts"], 1)
        self.assertEqual(same_run["readback"]["reservation"]["token"], token)
        self.assertEqual(self.project.direct_item_reads, direct_reads_after_crash)
        self.run_sync(env={"GITHUB_RUN_ID": "778", "GITHUB_RUN_ATTEMPT": "1"})
        _, retried = self._stored_issue_state()
        self.assertEqual(retried["readback"]["attempts"], 2)
        self.assertNotEqual(retried["readback"]["reservation"]["token"], token)
        self.assertGreater(self.project.direct_item_reads, direct_reads_after_crash)
        self.assertEqual(self.project.add_calls, 1)

    def test_add_third_saved_failure_result_finalizes_exhaustion_without_fourth_read(self):
        for result_kind in ("network_error", "not_visible"):
            with self.subTest(result_kind=result_kind):
                self._configure_post_cutoff_project_add()
                self.project.add_response_loss = False
                self.project.add_visibility_delay_snapshots = 99
                self.run_sync()
                self.run_sync(env={"GITHUB_RUN_ID": "881", "GITHUB_RUN_ATTEMPT": "1"})
                self.run_sync(env={"GITHUB_RUN_ID": "882", "GITHUB_RUN_ATTEMPT": "1"})
                self.assertEqual(self._stored_issue_state()[1]["readback"]["attempts"], 2)
                failed_hold_readback = False
                armed_network_failure = False

                def crash_after_final_result(path, properties, notion):
                    nonlocal failed_hold_readback, armed_network_failure
                    raw = properties.get("동기화 내부 상태")
                    if failed_hold_readback or not raw:
                        return
                    saved = json.loads(rich_text_value(raw))
                    saved_readback = saved.get("readback") or {}
                    if (result_kind == "network_error" and
                            saved_readback.get("attempts") == 3 and
                            saved_readback.get("last_result") is None and
                            not armed_network_failure):
                        armed_network_failure = True
                        self.project.fail_post_add_snapshot_once = True
                        return
                    if (saved_readback.get("attempts") == 3 and
                            saved_readback.get("last_result") == result_kind):
                        failed_hold_readback = True
                        notion.fail_readback_after_patch.add(path.rsplit("/", 1)[1])

                self.notion.after_patch = crash_after_final_result
                with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
                    self.run_sync(env={"GITHUB_RUN_ID": "883", "GITHUB_RUN_ATTEMPT": "1"})
                stranded = self._stored_issue_state()[1]
                self.assertTrue(failed_hold_readback)
                self.assertEqual(stranded["readback"]["attempts"], 3)
                self.assertEqual(stranded["readback"]["last_result"], result_kind)
                self.assertIsNone(stranded["hold"])
                calls = (self.project.direct_item_reads,
                         self.project.post_add_snapshot_calls, self.project.add_calls)

                self.notion.after_patch = None
                result = self.run_sync(env={"GITHUB_RUN_ID": "884", "GITHUB_RUN_ATTEMPT": "1"})

                exhausted = self._stored_issue_state()[1]
                self.assertEqual(result["held"], 1)
                self.assertEqual(exhausted["hold"]["code"], "ADD_READBACK_EXHAUSTED")
                self.assertEqual(self.project.direct_item_reads, calls[0])
                self.assertEqual(self.project.post_add_snapshot_calls, calls[1] + 1)
                self.assertEqual(self.project.add_calls, calls[2])

    def test_add_readback_reservation_write_failure_does_not_consume_attempt(self):
        self._configure_post_cutoff_project_add()
        self.project.add_response_loss = False
        self.project.add_visibility_delay_snapshots = 99
        self.run_sync()
        armed = False

        def fail_reservation_patch(path, properties):
            nonlocal armed
            raw = properties.get("동기화 내부 상태")
            if not raw:
                return False
            saved = json.loads(rich_text_value(raw))
            if (saved.get("readback") or {}).get("attempts") == 1 and \
                    saved["readback"].get("reservation"):
                armed = True
                return True
            return False

        self.notion.fail_patch = fail_reservation_patch
        with self.assertRaisesRegex(se.SyncError, "synthetic page write failure"):
            self.run_sync()
        self.assertTrue(armed)
        _, unchanged = self._stored_issue_state()
        self.assertEqual(unchanged["readback"]["attempts"], 0)
        self.assertIsNone(unchanged["readback"]["reservation"])
        direct_reads = self.project.direct_item_reads

        self.notion.fail_patch = None
        self.run_sync()
        _, reserved = self._stored_issue_state()
        self.assertEqual(reserved["readback"]["attempts"], 1)
        self.assertGreater(self.project.direct_item_reads, direct_reads)
        self.assertEqual(self.project.add_calls, 1)

    def test_flag_enabled_same_changed_statuses_converge_without_hold(self):
        self._configure_bidirectional_status(
            notion_status="진행 중", baseline_notion_status="백로그",
            project_status="진행 중", baseline_project_status="백로그")

        result = self.run_sync(bidirectional_enabled=True)

        state = self._read_bidirectional_state()
        self.assertEqual(result["held"], 0)
        self.assertIsNone(state["hold"])
        self.assertIsNone(state["request"])
        self.assertEqual(state["baseline"]["notion_status"], "진행 중")
        self.assertEqual(state["baseline"]["project"]["option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["진행 중"])
        self.assertEqual(self.project.writes, [])
        self.assertEqual(task_status_patch_payloads(self.notion.writes, ISSUE_PAGE_ID), [])

    def test_verified_closed_facts_reject_invalid_card_and_complete_project(self):
        opened = make_issue()
        closed = make_issue(state="CLOSED", reason="COMPLETED",
                            closed_at="2026-10-03T00:00:00Z")
        self._configure_bidirectional_status(
            notion_status="검토 중", baseline_notion_status="진행 중",
            project_status="진행 중", baseline_project_status="진행 중",
            baseline_issue=opened, issue=closed)

        result = self.run_sync(bidirectional_enabled=True)

        state = self._read_bidirectional_state()
        self.assertEqual(result["held"], 0)
        self.assertEqual(result["project_changes"], 1)
        self.assertEqual(len(self.project.writes), 1)
        self.assertEqual(self.project.items[0]["fieldValues"]["nodes"][0]["name"], "완료")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "완료")
        self.assertEqual(state["request"]["phase"], "rejected")
        self.assertEqual(state["request"]["target"], "검토 중")
        self.assertEqual(state["baseline"]["notion_status"], "완료")
        self.assertEqual(state["baseline"]["project"]["option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["완료"])
        props = self.notion.pages[ISSUE_PAGE_ID]["properties"]
        self.assertEqual(props["요청 처리"]["select"]["name"], "요청 거절")
        self.assertTrue(rich_text_value(props["확인 필요"]))

        # Native19 R2: CLOSED arrives after the invalid request's confirmed restore.
        self._configure_bidirectional_status(
            notion_status="검토 중", baseline_notion_status="진행 중",
            project_status="진행 중", baseline_project_status="진행 중",
            issue=opened)
        first = self.run_sync(bidirectional_enabled=True)
        restored = self._read_bidirectional_state()
        old_request = copy.deepcopy(restored["request"])
        old_baseline = copy.deepcopy(restored["baseline"])
        self.assertEqual(first["held"], 1)
        self.assertEqual(restored["hold"]["code"], "STATUS_REQUEST_INVALID")
        self.assertEqual(restored["notion_write"]["phase"], "confirmed")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "진행 중")
        self.assertEqual(self.project.writes, [])
        self.repo_graph.issues[0] = closed
        self.rest.issues[0]["state"] = "closed"
        apply_status = se._apply_project_status

        def apply_fresh_closed(*args, **kwargs):
            operation_state = args[5]
            self.assertEqual(operation_state["baseline"], old_baseline)
            self.assertEqual(operation_state["request"], old_request)
            return apply_status(*args, **kwargs)

        with patch.object(se, "_apply_project_status", side_effect=apply_fresh_closed) as apply:
            result = self.run_sync(bidirectional_enabled=True)
        self.assertEqual(apply.call_count, 1)
        state = self._read_bidirectional_state()
        self.assertEqual(result["held"], 0)
        self.assertEqual(result["project_changes"], 1)
        self.assertEqual(len(self.project.writes), 1)
        self.assertEqual(state["request"], old_request)
        self.assertIsNone(state["notion_write"])
        self.assertIsNone(state["hold"])
        self.assertEqual(state["baseline"]["notion_status"], "완료")
        self.assertEqual(state["baseline"]["project"]["option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["완료"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "완료")
        self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]["요청 처리"]
                         ["select"]["name"], "요청 거절")
        self.assertEqual(rich_text_value(self.notion.pages[ISSUE_PAGE_ID]
                                        ["properties"]["확인 필요"]), old_request["reason"])

        # The new operation retains the existing source/readback fences.
        for boundary in ("fresh_source", "display_readback"):
            with self.subTest(restored_then_closed_boundary=boundary):
                self._configure_bidirectional_status(
                    notion_status="검토 중", baseline_notion_status="진행 중",
                    project_status="진행 중", baseline_project_status="진행 중",
                    issue=opened)
                self.run_sync(bidirectional_enabled=True)
                restored = self._read_bidirectional_state()
                self.repo_graph.issues[0] = copy.deepcopy(closed)
                self.rest.issues[0]["state"] = "closed"
                triggered = False

                def interrupt_new_operation(path, properties, notion):
                    nonlocal triggered
                    if triggered or path != f"/pages/{ISSUE_PAGE_ID}":
                        return
                    raw = properties.get("동기화 내부 상태")
                    saved = json.loads(rich_text_value(raw)) if raw else {}
                    if boundary == "fresh_source" and saved.get("pending"):
                        triggered = True
                        self.repo_graph.issues[0] = copy.deepcopy(opened)
                        self.rest.issues[0]["state"] = "open"
                    elif (boundary == "display_readback" and saved.get("projection") and
                          properties.get("작업 상태", {}).get("select") == {"name": "완료"}):
                        triggered = True
                        notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

                self.notion.after_patch = interrupt_new_operation
                if boundary == "fresh_source":
                    result = self.run_sync(bidirectional_enabled=True)
                    self.assertEqual(result["held"], 1)
                    self.assertEqual(self.project.writes, [])
                else:
                    with self.assertRaisesRegex(se.SyncError, "readback failure"):
                        self.run_sync(bidirectional_enabled=True)
                    self.assertEqual(len(self.project.writes), 1)
                self.assertTrue(triggered)
                interrupted = self._read_bidirectional_state()
                self.assertEqual(interrupted["baseline"], restored["baseline"])
                self.assertEqual(interrupted["request"], restored["request"])
                self.notion.after_patch = None
                if boundary == "display_readback":
                    self.run_sync(bidirectional_enabled=True)
                    self.assertEqual(len(self.project.writes), 1)
                    self.assertEqual(self._read_bidirectional_state()["baseline"]
                                     ["notion_status"], "완료")

    def test_flag_enabled_github_only_reverse_and_forbidden_status_requests(self):
        with self.subTest(direction="github_only"):
            self._configure_bidirectional_status(
                notion_status="백로그", baseline_notion_status="백로그",
                project_status="진행 중", baseline_project_status="백로그")
            result = self.run_sync(bidirectional_enabled=True)
            state = self._read_bidirectional_state()
            self.assertEqual(result["held"], 0)
            self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                             "진행 중")
            self.assertEqual(state["baseline"]["notion_status"], "진행 중")
            self.assertEqual(self.project.writes, [])

        with self.subTest(direction="notion_only_reverse"):
            self._configure_bidirectional_status(
                notion_status="백로그", baseline_notion_status="진행 중",
                project_status="진행 중", baseline_project_status="진행 중")
            result = self.run_sync(bidirectional_enabled=True)
            state = self._read_bidirectional_state()
            self.assertEqual(result["project_changes"], 1)
            self.assertEqual(len(self.project.writes), 1)
            self.assertEqual(state["baseline"]["notion_status"], "백로그")

        with self.subTest(direction="forbidden_done"):
            self._configure_bidirectional_status(
                notion_status="완료", baseline_notion_status="백로그",
                project_status="백로그", baseline_project_status="백로그")
            result = self.run_sync(bidirectional_enabled=True)
            state = self._read_bidirectional_state()
            self.assertEqual(result["held"], 1)
            self.assertEqual(state["request"]["target"], "완료")
            self.assertIsNotNone(state["hold"])
            self.assertEqual(self.project.writes, [])

        with self.subTest(direction="forbidden_review"):
            self._configure_bidirectional_status(
                notion_status="검토 중", baseline_notion_status="백로그",
                project_status="백로그", baseline_project_status="백로그")
            result = self.run_sync(bidirectional_enabled=True)
            state = self._read_bidirectional_state()
            self.assertEqual(result["held"], 1)
            self.assertEqual(state["request"]["target"], "검토 중")
            self.assertEqual(state["request"]["phase"], "rejected")
            self.assertEqual(state["hold"]["code"], "STATUS_REQUEST_INVALID")
            self.assertEqual(self.project.writes, [])

    def test_flag_enabled_missing_baseline_initializes_only_stable_matching_values(self):
        self._configure_bidirectional_status(
            notion_status="진행 중", baseline_notion_status="진행 중",
            project_status="진행 중", baseline_project_status="진행 중")
        state = self._read_bidirectional_state()
        state["baseline"] = None
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["동기화 내부 상태"] = se.text_property(
            se.canonical_json(state))

        stable = self.run_sync(bidirectional_enabled=True)

        self.assertEqual(stable["held"], 0)
        self.assertEqual(self._read_bidirectional_state()["baseline"]["notion_status"], "진행 중")

        self._configure_bidirectional_status(notion_status="진행 중")
        state = self._read_bidirectional_state()
        state["baseline"] = None
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["동기화 내부 상태"] = se.text_property(
            se.canonical_json(state))

        mismatched = self.run_sync(bidirectional_enabled=True)

        held = self._read_bidirectional_state()
        self.assertEqual(mismatched["held"], 1)
        self.assertIsNone(held["baseline"])
        self.assertEqual(held["hold"]["code"], "MIGRATION_CONFLICT")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "진행 중")
        self.assertEqual(self.project.writes, [])

    def test_flag_off_preserves_existing_v2_status_baseline(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        before = copy.deepcopy(self._read_bidirectional_state()["baseline"])

        self.run_sync(bidirectional_enabled=False)

        state = self._read_bidirectional_state()
        self.assertEqual(state["baseline"], before)
        self.assertIsNone(state["request"])
        self.assertEqual(self.project.writes, [])

    def test_flag_enabled_conflict_and_facts_override_hold_request_and_restore_display(self):
        self._configure_bidirectional_status(
            notion_status="진행 중", project_status="준비 중",
            baseline_project_status="백로그")
        result = self.run_sync(bidirectional_enabled=True)
        conflict = self._read_bidirectional_state()
        self.assertEqual(result["held"], 1)
        self.assertEqual(conflict["hold"]["code"], "BIDIRECTIONAL_CONFLICT")
        self.assertEqual(conflict["request"]["phase"], "held")
        self.assertEqual(conflict["request"]["target"], "진행 중")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(self.project.writes, [])

        closed = make_issue(state="CLOSED", reason="COMPLETED",
                            closed_at="2026-10-03T00:00:00Z")
        self._configure_bidirectional_status(
            notion_status="진행 중", baseline_notion_status="백로그",
            project_status="백로그", baseline_project_status="백로그",
            baseline_issue=make_issue(), issue=closed)
        closed_baseline = copy.deepcopy(self._read_bidirectional_state()["baseline"])
        facts_result = self.run_sync(bidirectional_enabled=True)
        facts_state = self._read_bidirectional_state()
        self.assertEqual(facts_result["held"], 1)
        self.assertEqual(facts_state["hold"]["code"], "GITHUB_FACTS_CHANGED")
        self.assertEqual(facts_state["request"]["phase"], "held")
        self.assertEqual(facts_state["request"]["target"], "진행 중")
        self.assertEqual(facts_state["baseline"], closed_baseline)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "완료")
        self.assertEqual(self.project.writes, [])

        resumed = self.run_sync(bidirectional_enabled=True, resolve_issue_numbers="18",
                                env=pm_env(918811))
        canonical = self._read_bidirectional_state()
        self.assertEqual(resumed["held"], 0)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "완료")
        self.assertEqual(self.project.items[0]["fieldValues"]["nodes"][0]["name"], "완료")
        self.assertEqual(canonical["baseline"]["notion_status"], "완료")
        self.assertEqual(canonical["request"]["phase"], "rejected")
        self.assertEqual(canonical["request"]["target"], "진행 중")
        props = self.notion.pages[ISSUE_PAGE_ID]["properties"]
        self.assertEqual(props["요청 처리"]["select"]["name"], "요청 거절")
        self.assertEqual(props["요청 상태"]["select"]["name"], "진행 중")

    def test_flag_enabled_request_prewrite_detects_new_notion_move(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        mutated = False

        def change_card_before_project_write(path, properties, notion):
            nonlocal mutated
            if mutated or path != f"/pages/{ISSUE_PAGE_ID}":
                return
            raw = properties.get("동기화 내부 상태")
            if not raw:
                return
            saved = json.loads(rich_text_value(raw))
            if (saved.get("request") or {}).get("phase") == "prepared":
                mutated = True
                notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                    "select": {"name": "준비 중"}}

        self.notion.after_patch = change_card_before_project_write
        result = self.run_sync(bidirectional_enabled=True)

        state = self._read_bidirectional_state()
        self.assertTrue(mutated)
        self.assertEqual(result["held"], 1)
        self.assertEqual(state["hold"]["code"], "REQUEST_RACE")
        self.assertEqual(state["request"]["phase"], "held")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(self.project.writes, [])

    def test_flag_enabled_project_result_checks_card_before_old_projection(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        moved = False

        def move_card_during_project_write(project, item, variables):
            nonlocal moved
            moved = True
            self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                "select": {"name": "준비 중"}}

        self.project.after_status_write = move_card_during_project_write

        first = self.run_sync(bidirectional_enabled=True)

        checkpointed = self._read_bidirectional_state()
        original_request_id = checkpointed["request"]["id"]
        self.assertTrue(moved)
        self.assertEqual(first["held"], 1, repr(checkpointed))
        self.assertEqual(checkpointed["hold"]["code"], "REQUEST_RACE")
        self.assertIsNotNone(checkpointed["projection"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(len(self.project.writes), 1)

        self.project.after_status_write = None
        second = self.run_sync(bidirectional_enabled=True)

        recovered = self._read_bidirectional_state()
        props = self.notion.pages[ISSUE_PAGE_ID]["properties"]
        self.assertEqual(second["held"], 1)
        self.assertIsNotNone(recovered["projection"])
        self.assertEqual(recovered["request"]["id"], original_request_id)
        self.assertEqual(recovered["deferred_request"]["phase"], "held")
        self.assertEqual(recovered["deferred_request"]["target"], "준비 중")
        self.assertEqual(props["요청 상태"]["select"]["name"], "준비 중")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(len(self.project.writes), 1)

    def test_flag_enabled_projection_readback_loss_keeps_checkpoint_until_confirmed(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        failed = False

        def lose_final_projection_readback(path, properties, notion):
            nonlocal failed
            if (failed or path != f"/pages/{ISSUE_PAGE_ID}" or
                    "작업 상태" not in properties or
                    not properties.get("동기화 내부 상태")):
                return
            saved = json.loads(rich_text_value(properties["동기화 내부 상태"]))
            if saved.get("projection") and saved.get("request", {}).get("phase") == "confirmed":
                failed = True
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = lose_final_projection_readback
        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync(bidirectional_enabled=True)
        checkpointed = self._read_bidirectional_state()
        self.assertTrue(failed)
        self.assertIsNotNone(checkpointed["projection"])
        self.assertEqual(checkpointed["request"]["phase"], "confirmed")
        self.assertEqual(checkpointed["baseline"]["notion_status"], "백로그")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "진행 중")
        malformed = copy.deepcopy(checkpointed)
        malformed["projection"]["request_id"] = "different-request"
        with self.assertRaises(se.SyncError):
            se.decode_internal(se.canonical_json(malformed), kind="issue",
                               project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        malformed = copy.deepcopy(checkpointed)
        malformed["projection"]["result_notion_status"] = "준비 중"
        with self.assertRaises(se.SyncError):
            se.decode_internal(se.canonical_json(malformed), kind="issue",
                               project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(len(self.project.writes), 1)

        paused = self.run_sync(bidirectional_enabled=False)
        off_state = self._read_bidirectional_state()
        self.assertEqual(paused["held"], 1)
        self.assertEqual(off_state["hold"]["code"], "BIDIRECTIONAL_DISABLED")
        self.assertEqual(off_state["projection"], checkpointed["projection"])
        self.assertEqual(off_state["request"]["phase"], "confirmed")
        self.assertEqual(off_state["baseline"]["notion_status"], "백로그")
        self.assertEqual(len(self.project.writes), 1)

        self.notion.after_patch = None
        self.repo_graph.issues[0]["updatedAt"] = "2026-10-05T00:00:00Z"
        result = self.run_sync(bidirectional_enabled=True)
        state = self._read_bidirectional_state()
        self.assertEqual(result["held"], 0)
        self.assertIsNone(state["projection"])
        self.assertEqual(state["request"]["phase"], "completed")
        self.assertEqual(state["baseline"]["notion_status"], "진행 중")
        self.assertEqual(len(self.project.writes), 1)

    def test_completed_marker_readback_loss_repairs_request_ui_on_next_run(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        lost = False

        def lose_completed_marker_readback(path, properties, notion):
            nonlocal lost
            if (lost or path != f"/pages/{ISSUE_PAGE_ID}" or
                    "요청 처리" not in properties):
                return
            if properties["요청 처리"].get("select", {}).get("name") == "반영 완료":
                lost = True
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = lose_completed_marker_readback
        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync(bidirectional_enabled=True)
        persisted = self._read_bidirectional_state()
        self.assertTrue(lost)
        self.assertEqual(persisted["request"]["phase"], "confirmed")
        self.assertEqual(persisted["projection"]["display_checkpoint"]["phase"],
                         "completion_pending")
        self.assertEqual(persisted["baseline"]["notion_status"], "백로그")
        self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]["요청 처리"]
                         ["select"]["name"], "반영 완료")
        self.assertEqual(len(self.project.writes), 1)

        self.notion.after_patch = None
        result = self.run_sync(bidirectional_enabled=True)
        ui = self.notion.pages[ISSUE_PAGE_ID]["properties"]
        self.assertEqual(result["held"], 0)
        self.assertEqual(ui["요청 처리"]["select"]["name"], "반영 완료")
        self.assertEqual(ui["요청 상태"]["select"]["name"], "진행 중")
        self.assertEqual(rich_text_value(ui["확인 필요"]),
                         "Project 상태와 Notion 표시를 확인했습니다.")
        self.assertEqual(len(self.project.writes), 1)

    def test_completion_gates_preserve_new_card_move_at_both_readbacks_and_pm_lifecycle(self):
        for injection in ("completion_checkpoint", "terminal_readback"):
            with self.subTest(injection=injection):
                self._configure_bidirectional_status(notion_status="진행 중")
                moved = False

                def move_during_completion(path, properties, notion):
                    nonlocal moved
                    if moved or path != f"/pages/{ISSUE_PAGE_ID}":
                        return
                    internal = properties.get("동기화 내부 상태")
                    saved = (json.loads(rich_text_value(internal))
                             if internal else {})
                    inject_now = (
                        injection == "completion_checkpoint" and
                        "작업 상태" not in properties and
                        (saved.get("projection") or {}).get("display_checkpoint", {}).get(
                            "phase") == "completion_pending") or (
                        injection == "terminal_readback" and
                        properties.get("요청 처리", {}).get("select", {}).get("name") ==
                        "반영 완료" and "작업 상태" in properties)
                    if inject_now:
                        moved = True
                        notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                            "select": {"name": "준비 중"}}

                self.notion.after_patch = move_during_completion
                first = self.run_sync(bidirectional_enabled=True)
                state = self._read_bidirectional_state()
                props = self.notion.pages[ISSUE_PAGE_ID]["properties"]
                self.assertTrue(moved)
                self.assertEqual(first["held"], 1)
                self.assertEqual(state["request"]["phase"], "confirmed")
                self.assertEqual(state["request"]["target"], "진행 중")
                self.assertEqual(state["baseline"]["notion_status"], "백로그")
                self.assertEqual(state["projection"]["display_checkpoint"]["phase"],
                                 "completion_pending")
                self.assertEqual(state["deferred_request"]["target"], "준비 중")
                self.assertEqual(state["deferred_request"]["phase"], "held")
                self.assertNotEqual(state["deferred_request"]["id"], state["request"]["id"])
                self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                                 "준비 중")
                self.assertEqual(props["요청 처리"]["select"]["name"], "PM 확인 필요")
                self.assertEqual(props["요청 상태"]["select"]["name"], "준비 중")
                self.assertEqual(len(self.project.writes), 1)

                self.notion.after_patch = None
                repeated = self.run_sync(bidirectional_enabled=True)
                after_repeat = self._read_bidirectional_state()
                self.assertEqual(repeated["held"], 1)
                self.assertEqual(after_repeat["request"]["id"], state["request"]["id"])
                self.assertEqual(after_repeat["deferred_request"]["id"],
                                 state["deferred_request"]["id"])
                self.assertEqual(after_repeat["baseline"]["notion_status"], "백로그")
                self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                                 "준비 중")
                self.assertEqual(len(self.project.writes), 1)

                old_result = self.run_sync(
                    bidirectional_enabled=True, resolve_issue_numbers="18",
                    env=pm_env(941861))
                after_old_result = self._read_bidirectional_state()
                self.assertEqual(old_result["held"], 1)
                self.assertEqual(after_old_result["request"]["id"], state["request"]["id"])
                self.assertEqual(after_old_result["request"]["phase"], "completed",
                                 repr(after_old_result))
                self.assertEqual(after_old_result["deferred_request"]["id"],
                                 state["deferred_request"]["id"])
                self.assertEqual(after_old_result["baseline"], state["baseline"])
                self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                                 "준비 중")
                self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]["요청 처리"]
                                 ["select"]["name"], "PM 확인 필요")
                self.assertEqual(len(self.project.writes), 1)

                resolve_new_request = self.run_sync(
                    bidirectional_enabled=True, resolve_issue_numbers="18",
                    env=pm_env(941862))
                terminal = self._read_bidirectional_state()
                self.assertEqual(resolve_new_request["held"], 0)
                self.assertEqual(terminal["request"]["id"], state["deferred_request"]["id"])
                self.assertEqual(terminal["request"]["phase"], "rejected")
                self.assertIsNone(terminal["deferred_request"])
                self.assertEqual(terminal["baseline"], state["baseline"])
                self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]["요청 처리"]
                                 ["select"]["name"], "요청 거절")
                self.assertEqual(len(self.project.writes), 1)
                settled = self.run_sync(bidirectional_enabled=True)
                settled_state = self._read_bidirectional_state()
                self.assertEqual(settled["held"], 0)
                self.assertEqual(settled_state["baseline"]["notion_status"], "진행 중")

    def test_pending_source_changed_hold_keeps_deferred_notion_card_status_and_ui(self):
        self._configure_bidirectional_status(notion_status="진행 중")

        def stop_after_pending_checkpoint(path, properties, notion):
            if path != f"/pages/{ISSUE_PAGE_ID}":
                return
            internal = properties.get("동기화 내부 상태")
            if not internal:
                return
            saved = json.loads(rich_text_value(internal))
            if (saved.get("pending") or {}).get("kind") == "status":
                notion.after_patch = None
                raise se.SyncError("synthetic crash after pending checkpoint")

        self.notion.after_patch = stop_after_pending_checkpoint
        with self.assertRaisesRegex(se.SyncError, "after pending checkpoint"):
            self.run_sync(bidirectional_enabled=True)
        sent = self._read_bidirectional_state()
        self.assertEqual(sent["request"]["phase"], "prepared")
        self.assertEqual(sent["pending"]["kind"], "status")
        self.assertEqual(len(self.project.writes), 0)

        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": "준비 중"}}
        original_request = self.repo_graph.request

        def change_issue_after_full_snapshot(query, variables=None, *, mutation=False):
            data, server_time = original_request(query, variables, mutation=mutation)
            if ("node(id:$id)" in query or "node(id: $id)" in query) and \
                    (variables or {}).get("id") == ISSUE_NODE and data.get("node"):
                data["node"].update({"state": "CLOSED", "stateReason": "COMPLETED",
                                     "closedAt": "2026-10-07T00:00:00Z"})
            return data, server_time

        self.repo_graph.request = change_issue_after_full_snapshot
        resumed = self.run_sync(bidirectional_enabled=True)
        held = self._read_bidirectional_state()
        props = self.notion.pages[ISSUE_PAGE_ID]["properties"]
        self.assertEqual(resumed["held"], 1)
        self.assertEqual(held["hold"]["code"], "SOURCE_CHANGED_BEFORE_WRITE", repr(held))
        self.assertIsNone(held["pending"])
        self.assertEqual(held["request"]["id"], sent["request"]["id"])
        self.assertEqual(held["request"]["phase"], "prepared")
        self.assertEqual(held["deferred_request"]["target"], "준비 중")
        self.assertEqual(held["deferred_request"]["phase"], "held")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(props["요청 처리"]["select"]["name"], "PM 확인 필요")
        self.assertEqual(props["요청 상태"]["select"]["name"], "준비 중")
        self.assertTrue(rich_text_value(props["확인 필요"]).startswith("보류:"))
        self.assertEqual(len(self.project.writes), 0)

    def test_flag_off_preserves_and_pauses_unfinished_v2_request_without_mutation(self):
        self._configure_bidirectional_status(notion_status="진행 중")

        def lose_prepared_pending_readback(path, properties, notion):
            if path != f"/pages/{ISSUE_PAGE_ID}":
                return
            raw = properties.get("동기화 내부 상태")
            if not raw:
                return
            saved = json.loads(rich_text_value(raw))
            if (saved.get("request", {}).get("phase") == "prepared" and
                    (saved.get("pending") or {}).get("kind") == "status"):
                notion.after_patch = None
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = lose_prepared_pending_readback
        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync(bidirectional_enabled=True)
        self.notion.after_patch = None
        prepared = self._read_bidirectional_state()
        self.assertEqual(prepared["request"]["phase"], "prepared")
        self.assertEqual(prepared["notion_write"]["phase"], "prepared")
        self.assertEqual(prepared["pending"]["kind"], "status")
        malformed = copy.deepcopy(prepared)
        malformed["pending"]["request_id"] = "different-request"
        with self.assertRaises(se.SyncError):
            se.decode_internal(se.canonical_json(malformed), kind="issue",
                               project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        malformed = copy.deepcopy(prepared)
        malformed["pending"]["target_option_id"] = gp.EXPECTED_STATUS_OPTIONS["준비 중"]
        with self.assertRaises(se.SyncError):
            se.decode_internal(se.canonical_json(malformed), kind="issue",
                               project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        legacy_prepared = copy.deepcopy(prepared)
        legacy_prepared["pending"].pop("request_id")
        decoded_legacy = se.decode_internal(
            se.canonical_json(legacy_prepared), kind="issue",
            project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(decoded_legacy["request"]["id"], prepared["request"]["id"])
        self.assertEqual(self.project.writes, [])
        before_baseline = copy.deepcopy(prepared["baseline"])
        prepared_request_id = prepared["request"]["id"]

        result = self.run_sync(bidirectional_enabled=False)

        after = self._read_bidirectional_state()
        self.assertEqual(result["held"], 1)
        self.assertEqual(after["baseline"], before_baseline)
        self.assertEqual(after["request"]["id"], prepared_request_id)
        self.assertEqual(after["request"]["phase"], "prepared")
        self.assertEqual(after["pending"]["kind"], "status")
        self.assertEqual(after["notion_write"]["phase"], "prepared")
        self.assertEqual(after["hold"]["code"], "BIDIRECTIONAL_DISABLED")
        self.assertEqual(self.project.writes, [])

        reenabled = self.run_sync(bidirectional_enabled=True)

        resumed = self._read_bidirectional_state()
        self.assertEqual(self.project.writes, [])
        self.assertEqual(resumed["request"]["id"], prepared_request_id)
        self.assertEqual(resumed["request"]["phase"], "prepared")
        self.assertEqual(resumed["pending"]["kind"], "status")
        self.assertIsNotNone(resumed["hold"])
        self.assertNotEqual(resumed["hold"]["code"], "BIDIRECTIONAL_DISABLED")
        self.assertEqual(reenabled["held"], 1)

    def test_flag_off_does_not_replace_an_existing_hold(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        prepared = self._read_bidirectional_state()
        prepared["hold"] = se._new_hold(
            "BIDIRECTIONAL_CONFLICT", "original PM hold", se._digest("original-hold"))
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["동기화 내부 상태"] = se.text_property(
            se.canonical_json(prepared))

        result = self.run_sync(bidirectional_enabled=False)

        after = self._read_bidirectional_state()
        self.assertEqual(result["held"], 1)
        self.assertEqual(after["hold"], prepared["hold"])
        self.assertEqual(self.project.writes, [])

    def test_flag_off_preserves_legacy_sent_and_uncertain_v2_pending(self):
        for phase in ("sent", "uncertain"):
            with self.subTest(phase=phase):
                self._configure_bidirectional_status(notion_status="진행 중")

                def lose_prepared_pending_readback(path, properties, notion):
                    if path != f"/pages/{ISSUE_PAGE_ID}":
                        return
                    raw = properties.get("동기화 내부 상태")
                    if not raw:
                        return
                    saved = json.loads(rich_text_value(raw))
                    if (saved.get("request", {}).get("phase") == "prepared" and
                            (saved.get("pending") or {}).get("kind") == "status"):
                        notion.after_patch = None
                        notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

                self.notion.after_patch = lose_prepared_pending_readback
                with self.assertRaises(se.SyncError):
                    self.run_sync(bidirectional_enabled=True)
                self.notion.after_patch = None
                state = self._read_bidirectional_state()
                state["request"]["phase"] = phase
                state["notion_write"]["phase"] = phase
                state["pending"].pop("request_id")
                pending_before = copy.deepcopy(state["pending"])
                request_id = state["request"]["id"]
                self.notion.pages[ISSUE_PAGE_ID]["properties"]["동기화 내부 상태"] = se.text_property(
                    se.canonical_json(state))

                result = self.run_sync(bidirectional_enabled=False)

                paused = self._read_bidirectional_state()
                self.assertEqual(result["held"], 1)
                self.assertEqual(paused["request"]["id"], request_id)
                self.assertEqual(paused["request"]["phase"], phase)
                self.assertEqual(paused["notion_write"]["phase"], phase)
                self.assertEqual(paused["pending"], pending_before)
                self.assertEqual(paused["hold"]["code"], "BIDIRECTIONAL_DISABLED")
                self.assertEqual(self.project.writes, [])

    def test_pm_resume_binds_request_and_does_not_consume_approval_after_new_move(self):
        self._configure_bidirectional_status(notion_status="백로그")
        state = self._read_bidirectional_state()
        state["request"] = se.status_sync.request_record(
            "held-request-id", "진행 중", observed_from=NOW, observed_to=NOW,
            prior_notion_status="백로그",
            project_option_id=gp.EXPECTED_STATUS_OPTIONS["백로그"], phase="held",
            reason="PM review required")
        state["hold"] = se._new_hold("BIDIRECTIONAL_CONFLICT", "PM review required",
                                     se._digest("request-hold"))
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["동기화 내부 상태"] = se.text_property(
            se.canonical_json(state))
        changed = False

        def move_after_pm_approval(path, properties, notion):
            nonlocal changed
            if changed or path != f"/pages/{ISSUE_PAGE_ID}":
                return
            raw = properties.get("동기화 내부 상태")
            if not raw:
                return
            saved = json.loads(rich_text_value(raw))
            approval = saved.get("resume") or {}
            if approval.get("run_id") == "918777":
                changed = True
                notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                    "select": {"name": "준비 중"}}

        self.notion.after_patch = move_after_pm_approval
        result = self.run_sync(bidirectional_enabled=True, resolve_issue_numbers="18",
                               env=pm_env(918777))

        persisted = self._read_bidirectional_state()
        self.assertTrue(changed)
        self.assertEqual(result["held"], 1)
        self.assertEqual(persisted["request"]["id"], "held-request-id")
        self.assertEqual(persisted["request"]["phase"], "held")
        self.assertEqual(persisted["hold"]["code"], "REQUEST_RACE")
        self.assertIsNone(persisted["resume"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(self.project.writes, [])

    def test_existing_hold_preserves_a_new_notion_move_until_pm_flow(self):
        self._configure_bidirectional_status(notion_status="완료")
        state = self._read_bidirectional_state()
        state["request"] = se.status_sync.request_record(
            "older-held-request", "진행 중", observed_from=NOW, observed_to=NOW,
            prior_notion_status="백로그",
            project_option_id=gp.EXPECTED_STATUS_OPTIONS["백로그"], phase="held",
            reason="PM review required")
        state["hold"] = se._new_hold("BIDIRECTIONAL_CONFLICT", "PM review required",
                                     se._digest("existing-hold"))
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["동기화 내부 상태"] = se.text_property(
            se.canonical_json(state))

        result = self.run_sync(bidirectional_enabled=True)

        persisted = self._read_bidirectional_state()
        self.assertEqual(result["held"], 1)
        self.assertEqual(persisted["hold"]["code"], "BIDIRECTIONAL_CONFLICT")
        self.assertEqual(persisted["request"]["id"], "older-held-request")
        self.assertEqual(persisted["request"]["target"], "진행 중")
        self.assertNotEqual(persisted["deferred_request"]["id"], "older-held-request")
        self.assertEqual(persisted["deferred_request"]["phase"], "held")
        self.assertEqual(persisted["deferred_request"]["target"], "완료")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "완료")
        self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]["요청 상태"]
                         ["select"]["name"], "완료")
        self.assertEqual(self.project.writes, [])

    def test_projection_recovery_preserves_a_new_move_and_does_not_confirm_baseline(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        failed = False

        def lose_final_projection_readback(path, properties, notion):
            nonlocal failed
            if (failed or path != f"/pages/{ISSUE_PAGE_ID}" or
                    "작업 상태" not in properties or
                    not properties.get("동기화 내부 상태")):
                return
            saved = json.loads(rich_text_value(properties["동기화 내부 상태"]))
            if saved.get("projection") and saved.get("request", {}).get("phase") == "confirmed":
                failed = True
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = lose_final_projection_readback
        with self.assertRaises(se.SyncError):
            self.run_sync(bidirectional_enabled=True)
        checkpointed = self._read_bidirectional_state()
        self.assertIsNotNone(checkpointed["projection"])
        self.notion.after_patch = None
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": "준비 중"}}

        result = self.run_sync(bidirectional_enabled=True)

        state = self._read_bidirectional_state()
        self.assertEqual(result["held"], 1)
        self.assertEqual(state["hold"]["code"], "REQUEST_RACE")
        self.assertEqual(state["baseline"]["notion_status"], "백로그")
        self.assertEqual(state["request"]["id"], checkpointed["request"]["id"])
        self.assertEqual(state["request"]["phase"], "confirmed")
        self.assertIsNotNone(state["projection"])
        self.assertNotEqual(state["deferred_request"]["id"], checkpointed["request"]["id"])
        self.assertEqual(state["deferred_request"]["phase"], "held")
        self.assertEqual(state["deferred_request"]["target"], "준비 중")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(len(self.project.writes), 1)

        repeated = self.run_sync(bidirectional_enabled=True)
        after_repeat = self._read_bidirectional_state()
        self.assertEqual(repeated["held"], 1)
        self.assertEqual(after_repeat["deferred_request"], state["deferred_request"])
        self.assertEqual(len(self.project.writes), 1)

        old_request_id = state["request"]["id"]
        deferred_id = state["deferred_request"]["id"]
        old_target = state["request"]["target"]
        followup_target = state["deferred_request"]["target"]
        prior_baseline = copy.deepcopy(after_repeat["baseline"])
        confirmed_old = self.run_sync(
            bidirectional_enabled=True, resolve_issue_numbers="18",
            env=pm_env(918856))
        after_old_confirmation = self._read_bidirectional_state()
        self.assertEqual(confirmed_old["held"], 1)
        self.assertEqual(after_old_confirmation["request"]["id"], old_request_id)
        self.assertEqual(after_old_confirmation["request"]["target"], old_target)
        self.assertEqual(after_old_confirmation["request"]["phase"], "completed",
                         repr(after_old_confirmation))
        self.assertEqual(after_old_confirmation["deferred_request"]["id"], deferred_id)
        self.assertEqual(after_old_confirmation["deferred_request"]["target"], followup_target)
        self.assertEqual(after_old_confirmation["hold"]["code"], "DEFERRED_REQUEST_PENDING")
        self.assertEqual(after_old_confirmation["baseline"], prior_baseline)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         followup_target)
        request_ui = self.notion.pages[ISSUE_PAGE_ID]["properties"]
        self.assertEqual(request_ui["요청 처리"]["select"]["name"], "PM 확인 필요")
        self.assertEqual(request_ui["요청 상태"]["select"]["name"], followup_target)

        pm_resolved = self.run_sync(
            bidirectional_enabled=True, resolve_issue_numbers="18",
            env=pm_env(918857))
        terminal = self._read_bidirectional_state()
        self.assertEqual(pm_resolved["held"], 0,
                         f"state={self._read_bidirectional_state()!r}")
        self.assertEqual(terminal["request"]["id"], deferred_id)
        self.assertEqual(terminal["request"]["target"], followup_target)
        self.assertEqual(terminal["request"]["phase"], "rejected")
        self.assertIn("PM이 현재 GitHub 상태", terminal["request"]["reason"])
        self.assertIsNone(terminal["deferred_request"])
        self.assertIsNone(terminal["hold"])
        self.assertEqual(terminal["baseline"], prior_baseline)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), old_target)
        request_ui = self.notion.pages[ISSUE_PAGE_ID]["properties"]
        self.assertEqual(request_ui["요청 처리"]["select"]["name"], "요청 거절")
        self.assertEqual(request_ui["요청 상태"]["select"]["name"], followup_target)
        restore_marker = terminal["notion_write"]
        self.assertEqual(restore_marker["kind"], "restore")
        self.assertEqual(restore_marker["phase"], "confirmed")
        self.assertTrue(se._restore_projection_matches(
            self.notion.pages[ISSUE_PAGE_ID], restore_marker))
        project_mutations = len(self.project.writes)

        write_start = len(self.notion.writes)
        converged_poll = self.run_sync(bidirectional_enabled=True)
        after_noop = self._read_bidirectional_state()
        current_value = self.project.status_values[self.project.items[0]["id"]][0]
        self.assertEqual(converged_poll["held"], 0)
        self.assertEqual(after_noop["request"], terminal["request"])
        self.assertEqual(after_noop["baseline"]["notion_status"], old_target)
        self.assertEqual(after_noop["baseline"]["project"], {
            "field_id": STATUS_FIELD, "option_id": current_value["optionId"],
            "value_id": current_value["id"], "updated_at": current_value["updatedAt"]})
        self.assertEqual(after_noop["baseline"]["project_item_id"],
                         self.project.items[0]["id"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         old_target)
        self.assertIsNone(after_noop["deferred_request"])
        self.assertEqual(len(self.project.writes), project_mutations)
        self.assertIsNone(after_noop["pending"])
        self.assertIsNone(after_noop["projection"])
        self.assertIsNone(after_noop["notion_write"])
        first_recovery_writes = self.notion.writes[write_start:]
        self.assertEqual(task_status_patch_payloads(first_recovery_writes, ISSUE_PAGE_ID), [])
        for props in notion_patch_properties(first_recovery_writes, ISSUE_PAGE_ID):
            raw_state = props.get("동기화 내부 상태")
            if raw_state:
                saved = json.loads(rich_text_value(raw_state))
                self.assertIsNone(saved["pending"])
                self.assertIsNone(saved["projection"])
                self.assertIsNone(saved["notion_write"])

        for _ in range(2):
            poll_start = len(self.notion.writes)
            stable = self.run_sync(bidirectional_enabled=True)
            stable_state = self._read_bidirectional_state()
            self.assertEqual(stable["held"], 0)
            self.assertEqual(stable_state["baseline"], after_noop["baseline"])
            self.assertEqual(stable_state["request"], after_noop["request"])
            self.assertIsNone(stable_state["pending"])
            self.assertIsNone(stable_state["projection"])
            self.assertEqual(task_status_patch_payloads(
                self.notion.writes[poll_start:], ISSUE_PAGE_ID), [])
            self.assertEqual(len(self.project.writes), project_mutations)

    def _leave_rejected_restore_marker(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        failed = False

        def lose_final_projection_readback(path, properties, notion):
            nonlocal failed
            if (failed or path != f"/pages/{ISSUE_PAGE_ID}" or
                    "작업 상태" not in properties or
                    not properties.get("동기화 내부 상태")):
                return
            saved = json.loads(rich_text_value(properties["동기화 내부 상태"]))
            if saved.get("projection") and saved.get("request", {}).get("phase") == "confirmed":
                failed = True
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = lose_final_projection_readback
        with self.assertRaises(se.SyncError):
            self.run_sync(bidirectional_enabled=True)
        self.notion.after_patch = None
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": "준비 중"}}
        self.run_sync(bidirectional_enabled=True)
        self.run_sync(bidirectional_enabled=True, resolve_issue_numbers="18",
                      env=pm_env(941101))
        self.run_sync(bidirectional_enabled=True, resolve_issue_numbers="18",
                      env=pm_env(941102))
        terminal = self._read_bidirectional_state()
        self.assertEqual(terminal["request"]["phase"], "rejected")
        self.assertEqual(terminal["request"]["target"], "준비 중")
        self.assertEqual(terminal["notion_write"]["kind"], "restore")
        self.assertEqual(terminal["notion_write"]["phase"], "confirmed")
        self.assertEqual(terminal["notion_write"]["request_id"], terminal["request"]["id"])
        return copy.deepcopy(terminal), copy.deepcopy(terminal["baseline"]), len(self.project.writes)

    def test_confirmed_restore_terminal_marker_mismatch_does_not_promote_baseline(self):
        for variant in ("request_ui", "confirmation", "project_value_id",
                        "project_updated_at", "facts", "new_card", "request_binding"):
            with self.subTest(variant=variant):
                terminal, baseline_before, mutation_count = self._leave_rejected_restore_marker()
                page = self.notion.pages[ISSUE_PAGE_ID]
                if variant == "request_ui":
                    page["properties"]["요청 처리"] = {"select": {"name": "PM 확인 필요"}}
                elif variant == "confirmation":
                    page["properties"]["확인 필요"] = se.text_property("changed reason")
                elif variant == "project_value_id":
                    value = self.project.status_values[self.project.items[0]["id"]][0]
                    value["id"] = "PVTSV_changed_after_restore"
                    self.project.items[0]["fieldValues"]["nodes"] = [copy.deepcopy(value)]
                elif variant == "project_updated_at":
                    value = self.project.status_values[self.project.items[0]["id"]][0]
                    value["updatedAt"] = "2026-10-09T00:00:00Z"
                    self.project.items[0]["fieldValues"]["nodes"] = [copy.deepcopy(value)]
                elif variant == "facts":
                    self.repo_graph.issues[0]["state"] = "CLOSED"
                    self.repo_graph.issues[0]["stateReason"] = "COMPLETED"
                    self.repo_graph.issues[0]["closedAt"] = "2026-10-09T00:00:00Z"
                    self.rest.issues[0]["state"] = "closed"
                elif variant == "new_card":
                    page["properties"]["작업 상태"] = {"select": {"name": "백로그"}}
                else:
                    changed = self._read_bidirectional_state()
                    changed["request"]["id"] = "different-request-binding"
                    page["properties"]["동기화 내부 상태"] = se.text_property(
                        se.canonical_json(changed))

                if variant == "facts":
                    promote_restore = se._promote_held_terminal_restore
                    apply_status = se._apply_project_status

                    def refuse_old_restore(*args, **kwargs):
                        promoted = promote_restore(*args, **kwargs)
                        self.assertFalse(promoted)
                        self.assertEqual(args[3]["baseline"], baseline_before)
                        return promoted

                    def apply_new_closed(*args, **kwargs):
                        self.assertEqual(args[5]["baseline"], baseline_before)
                        self.assertEqual(args[5]["request"], terminal["request"])
                        return apply_status(*args, **kwargs)

                    with patch.object(se, "_promote_held_terminal_restore",
                                      side_effect=refuse_old_restore) as promote, \
                            patch.object(se, "_apply_project_status",
                                         side_effect=apply_new_closed) as apply:
                        result = self.run_sync(bidirectional_enabled=True)
                    self.assertGreater(promote.call_count, 0)
                    self.assertEqual(apply.call_count, 1)
                    after = self._read_bidirectional_state()
                    self.assertEqual(result["held"], 0)
                    self.assertEqual(len(self.project.writes), mutation_count + 1)
                    self.assertEqual(after["baseline"]["notion_status"], "완료")
                    self.assertEqual(after["request"], terminal["request"])
                    self.assertIsNone(after["notion_write"])
                    continue

                negative_page = copy.deepcopy(page)
                result = self.run_sync(bidirectional_enabled=True)
                after = self._read_bidirectional_state()
                self.assertEqual(after["baseline"], baseline_before)
                self.assertEqual(result["held"], 1)
                self.assertEqual(len(self.project.writes), mutation_count)
                self.assertEqual(after["request"]["target"], terminal["request"]["target"])
                if variant == "new_card":
                    self.assertEqual(se.read_select(page, "작업 상태"), "백로그")

                # The same existing binding/UI/card negatives must still hold
                # when CLOSED facts subsequently arrive; only a facts-only change
                # may start the independently verified source operation.
                self.notion.pages[ISSUE_PAGE_ID] = negative_page
                self.repo_graph.issues[0]["state"] = "CLOSED"
                self.repo_graph.issues[0]["stateReason"] = "COMPLETED"
                self.repo_graph.issues[0]["closedAt"] = "2026-10-09T00:00:00Z"
                self.rest.issues[0]["state"] = "closed"
                result = self.run_sync(bidirectional_enabled=True)
                after = self._read_bidirectional_state()
                self.assertEqual(result["held"], 1)
                self.assertEqual(after["baseline"], baseline_before)
                self.assertEqual(len(self.project.writes), mutation_count)

    def test_pm_resolving_deferred_request_completes_only_when_selected_project_matches(self):
        self._configure_bidirectional_status(notion_status="진행 중")
        failed = False

        def lose_final_projection_readback(path, properties, notion):
            nonlocal failed
            if (failed or path != f"/pages/{ISSUE_PAGE_ID}" or
                    "작업 상태" not in properties or
                    not properties.get("동기화 내부 상태")):
                return
            saved = json.loads(rich_text_value(properties["동기화 내부 상태"]))
            if saved.get("projection") and saved.get("request", {}).get("phase") == "confirmed":
                failed = True
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = lose_final_projection_readback
        with self.assertRaises(se.SyncError):
            self.run_sync(bidirectional_enabled=True)
        checkpointed = self._read_bidirectional_state()
        self.assertIsNotNone(checkpointed["projection"])
        self.notion.after_patch = None
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": "준비 중"}}
        self.run_sync(bidirectional_enabled=True)
        deferred_id = self._read_bidirectional_state()["deferred_request"]["id"]

        old_result = self.run_sync(
            bidirectional_enabled=True, resolve_issue_numbers="18", env=pm_env(918856))
        after_old_result = self._read_bidirectional_state()
        prior_baseline = copy.deepcopy(checkpointed["baseline"])
        self.assertEqual(old_result["held"], 1)
        self.assertEqual(after_old_result["request"]["phase"], "completed")
        self.assertEqual(after_old_result["request"]["id"], checkpointed["request"]["id"])
        self.assertEqual(after_old_result["deferred_request"]["id"], deferred_id)
        self.assertEqual(after_old_result["baseline"], prior_baseline)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "준비 중")
        self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]["요청 처리"]
                         ["select"]["name"], "PM 확인 필요")
        self.assertEqual(self.notion.pages[ISSUE_PAGE_ID]["properties"]["요청 상태"]
                         ["select"]["name"], "준비 중")

        # PM's current Project selection is now exactly B's requested target.
        selected_b = status_value("준비 중")
        self.project.items[0]["fieldValues"]["nodes"] = [selected_b]
        self.project.status_values[self.project.items[0]["id"]] = [selected_b]
        project_mutations = len(self.project.writes)
        pm_resolved = self.run_sync(
            bidirectional_enabled=True, resolve_issue_numbers="18", env=pm_env(918857))

        terminal = self._read_bidirectional_state()
        self.assertEqual(pm_resolved["held"], 0, repr(terminal))
        self.assertEqual(terminal["request"]["id"], deferred_id)
        self.assertEqual(terminal["request"]["target"], "준비 중")
        self.assertEqual(terminal["request"]["phase"], "completed")
        self.assertIsNone(terminal["deferred_request"])
        self.assertIsNone(terminal["hold"])
        self.assertEqual(terminal["baseline"]["notion_status"], "준비 중")
        self.assertEqual(terminal["baseline"]["project"]["option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["준비 중"])
        properties = self.notion.pages[ISSUE_PAGE_ID]["properties"]
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(properties["요청 처리"]["select"]["name"], "반영 완료")
        self.assertEqual(properties["요청 상태"]["select"]["name"], "준비 중")
        self.assertEqual(rich_text_value(properties["확인 필요"]),
                         "Project 상태와 Notion 표시를 확인했습니다.")
        self.assertEqual(len(self.project.writes), project_mutations)

        next_poll = self.run_sync(bidirectional_enabled=True)
        after_noop = self._read_bidirectional_state()
        self.assertEqual(next_poll["held"], 0)
        self.assertEqual(after_noop["request"], terminal["request"])
        self.assertEqual(after_noop["baseline"], terminal["baseline"])
        self.assertEqual(len(self.project.writes), project_mutations)

    def test_sent_old_request_and_new_move_keep_separate_ids_across_restart_and_pm(self):
        self._configure_bidirectional_status(notion_status="진행 중")

        def lose_applied_response(project, item, variables):
            raise gp.SyncError("synthetic committed status response loss")

        self.project.after_status_write = lose_applied_response
        with self.assertRaisesRegex(gp.SyncError, "synthetic committed status response loss"):
            self.run_sync(bidirectional_enabled=True)
        old = self._read_bidirectional_state()
        old_id = old["request"]["id"]
        old_target = old["request"]["target"]
        self.project.after_status_write = None
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": "준비 중"}}

        first_recovery = self.run_sync(bidirectional_enabled=True)
        separated = self._read_bidirectional_state()
        self.assertEqual(first_recovery["held"], 1)
        self.assertEqual(separated["request"]["id"], old_id)
        self.assertEqual(separated["request"]["target"], old_target)
        self.assertIsNotNone(separated["projection"])
        self.assertNotEqual(separated["deferred_request"]["id"], old_id)
        self.assertEqual(separated["deferred_request"]["target"], "준비 중")
        self.assertEqual(len(self.project.writes), 1)

        self.run_sync(bidirectional_enabled=True)
        restarted = self._read_bidirectional_state()
        self.assertEqual(restarted["request"]["id"], old_id)
        self.assertEqual(restarted["deferred_request"]["id"],
                         separated["deferred_request"]["id"])
        prior_baseline = copy.deepcopy(restarted["baseline"])
        self.assertEqual(len(self.project.writes), 1)

        pm_result = self.run_sync(bidirectional_enabled=True, resolve_issue_numbers="18",
                                  env=pm_env(918855))
        after_pm = self._read_bidirectional_state()
        self.assertEqual(pm_result["held"], 1)
        self.assertEqual(after_pm["request"]["id"], old_id)
        self.assertEqual(after_pm["request"]["phase"], "completed")
        self.assertEqual(after_pm["deferred_request"]["id"],
                         separated["deferred_request"]["id"])
        self.assertEqual(after_pm["hold"]["code"], "DEFERRED_REQUEST_PENDING")
        self.assertEqual(after_pm["baseline"], prior_baseline)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         separated["deferred_request"]["target"])
        props = self.notion.pages[ISSUE_PAGE_ID]["properties"]
        self.assertEqual(props["요청 처리"]["select"]["name"], "PM 확인 필요")
        self.assertEqual(props["요청 상태"]["select"]["name"], "준비 중")

        pm_followup = self.run_sync(bidirectional_enabled=True, resolve_issue_numbers="18",
                                    env=pm_env(918858))
        terminal = self._read_bidirectional_state()
        self.assertEqual(pm_followup["held"], 0)
        self.assertEqual(terminal["request"]["id"], separated["deferred_request"]["id"])
        self.assertEqual(terminal["request"]["phase"], "rejected")
        self.assertIn("PM이 현재 GitHub 상태 진행 중을 선택", terminal["request"]["reason"])
        self.assertIsNone(terminal["deferred_request"])
        self.assertIsNone(terminal["hold"])
        self.assertEqual(terminal["baseline"], prior_baseline)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), old_target)
        props = self.notion.pages[ISSUE_PAGE_ID]["properties"]
        self.assertEqual(props["요청 처리"]["select"]["name"], "요청 거절")
        self.assertEqual(props["요청 상태"]["select"]["name"], "준비 중")
        self.assertIn("PM이 현재 GitHub 상태 진행 중을 선택", rich_text_value(props["확인 필요"]))
        self.assertEqual(len(self.project.writes), 1)

        converged_poll = self.run_sync(bidirectional_enabled=True)
        after_noop = self._read_bidirectional_state()
        current_value = self.project.status_values[self.project.items[0]["id"]][0]
        self.assertEqual(converged_poll["held"], 0)
        self.assertEqual(after_noop["request"], terminal["request"])
        self.assertEqual(after_noop["baseline"]["notion_status"], old_target)
        self.assertEqual(after_noop["baseline"]["project"], {
            "field_id": STATUS_FIELD, "option_id": current_value["optionId"],
            "value_id": current_value["id"], "updated_at": current_value["updatedAt"]})
        self.assertEqual(after_noop["baseline"]["project_item_id"],
                         self.project.items[0]["id"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         old_target)
        self.assertEqual(len(self.project.writes), 1)

    def test_status_baseline_is_bound_to_top_level_item_and_null_request_roundtrips(self):
        state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        state["project_item_id"] = "PVTI_status_baseline"
        state["baseline"] = se.status_sync.make_baseline(
            "백로그", {"field_id": STATUS_FIELD,
                       "option_id": gp.EXPECTED_STATUS_OPTIONS["백로그"],
                       "value_id": "PVTSV_baseline", "updated_at": NOW},
            "PVTI_status_baseline", "a" * 64, NOW)
        state["request"] = se.status_sync.request_record(
            "request-null", None, observed_from=NOW, observed_to=NOW,
            prior_notion_status="백로그", project_option_id=gp.EXPECTED_STATUS_OPTIONS["백로그"],
            phase="rejected", reason="null 상태 요청")
        raw = se.canonical_json(state)
        decoded = se.decode_internal(raw, kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
                                     object_id=ISSUE_ID)
        self.assertEqual(decoded["baseline"]["project_item_id"], state["project_item_id"])
        self.assertIsNone(decoded["request"]["target"])
        self.assertEqual(decoded["request"]["phase"], "rejected")

        mismatched = copy.deepcopy(state)
        mismatched["project_item_id"] = "PVTI_different"
        with self.assertRaises(se.SyncError):
            se.decode_internal(se.canonical_json(mismatched), kind="issue",
                               project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)

    def _configure_date_readback_diagnostic(self, expected, actual, sync_time_actual=None,
                                            title_actual="#1 Original issue"):
        issue = make_issue()
        issue.update({"number": 1,
                      "url": f"https://github.com/{gp.REPOSITORY}/issues/1",
                      "updatedAt": expected})
        self.repo_graph = RepoGraph(issues=[issue])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": 1, "state": "open"}])
        row = active_issue_page(number=1)
        row["properties"]["제목"] = se.text_property(title_actual, "title")
        row["properties"]["종류"] = {"select": {"name": "Issue"}}
        row["properties"]["번호"] = {"number": 1}
        row["properties"]["GitHub URL"] = {"url": issue["url"]}
        row["properties"]["GitHub 상태"] = {"select": {"name": "Open"}}
        row["properties"]["작성자"] = se.text_property("author")
        row["properties"]["담당자"] = se.text_property("")
        row["properties"]["라벨"] = se.text_property("")
        row["properties"]["GitHub 수정"] = {"date": {"start": actual}}
        row["properties"]["동기화 키"] = se.text_property(
            se.key_for(gp.REPOSITORY_ID, ISSUE_ID))
        if sync_time_actual is not None:
            row["properties"]["동기화 시각"] = {"date": {"start": sync_time_actual}}
        self.project = ProjectAPI()
        self.notion = FakeNotion([control_page(control_internal()), row])

    def test_date_readback_diagnostic_is_redacted_and_dry_run_only(self):
        sync_time = "2026-10-09T09:59:00.123Z"
        current_run_time = "2026-10-09T10:00:31.972815Z"
        cases = [
            {"name": "fractional precision loss", "expected": "2026-10-02T00:00:37.123456Z",
             "actual": "2026-10-02T00:00:37.123Z", "same_instant": False,
             "expected_digits": 6, "actual_digits": 3, "actual_offset": "z_suffix",
             "actual_seconds_zero": False, "actual_microseconds_zero": False},
            {"name": "equivalent UTC spelling", "expected": "2026-10-02T00:00:00.123Z",
             "actual": "2026-10-02T00:00:00.123+00:00", "same_instant": True,
             "expected_digits": 3, "actual_digits": 3, "actual_offset": "zero_offset",
             "actual_seconds_zero": True, "actual_microseconds_zero": False},
            {"name": "minute truncation shape", "expected": "2026-10-02T00:00:37.000Z",
             "actual": "2026-10-02T00:00:00.000+00:00", "same_instant": False,
             "expected_digits": 3, "actual_digits": 3, "actual_offset": "zero_offset",
             "actual_seconds_zero": True, "actual_microseconds_zero": True},
            {"name": "seconds retained with zero fraction", "expected": "2026-10-02T00:00:37Z",
             "actual": "2026-10-02T00:00:37.000+00:00", "same_instant": True,
             "expected_digits": 0, "actual_digits": 3, "actual_offset": "zero_offset",
             "actual_seconds_zero": False, "actual_microseconds_zero": True},
        ]
        for case in cases:
            with self.subTest(case=case["name"]):
                self._configure_date_readback_diagnostic(
                    case["expected"], case["actual"], sync_time_actual=sync_time)
                result = self.run_sync(dry_run=True, diagnose_date_readback=True,
                                       now=current_run_time)
                diagnostic = result["date_readback_diagnostic"]
                self.assertEqual(diagnostic["issue_number"], 1)
                self.assertEqual(diagnostic["property"], "GitHub 수정")
                self.assertEqual(diagnostic["row_match"], "matched")
                self.assertFalse(diagnostic["literal_equal"])
                self.assertEqual(diagnostic["parseable"], "both")
                self.assertEqual(diagnostic["same_instant"], case["same_instant"])
                self.assertTrue(diagnostic["same_minute"])
                self.assertEqual(diagnostic["expected_fractional_digits"], case["expected_digits"])
                self.assertEqual(diagnostic["actual_fractional_digits"], case["actual_digits"])
                self.assertEqual(diagnostic["expected_offset_shape"], "z_suffix")
                self.assertEqual(diagnostic["actual_offset_shape"], case["actual_offset"])
                self.assertEqual(diagnostic["actual_seconds_zero"],
                                 case["actual_seconds_zero"])
                self.assertEqual(diagnostic["actual_microseconds_zero"],
                                 case["actual_microseconds_zero"])
                metadata_matches = diagnostic["metadata_matches"]
                self.assertEqual([entry["property"] for entry in metadata_matches],
                                 list(se.DATE_DIAGNOSTIC_METADATA_WHITELIST))
                self.assertTrue(all(set(entry) == {"property", "matches"}
                                    and type(entry["matches"]) is bool
                                    for entry in metadata_matches))
                expected_metadata_results = [
                    (entry["property"] != "GitHub 수정" or
                     case["name"] == "minute truncation shape")
                    for entry in metadata_matches]
                self.assertEqual([entry["matches"] for entry in metadata_matches],
                                 expected_metadata_results)
                expected_first_mismatch = (None if case["name"] == "minute truncation shape"
                                            else "GitHub 수정")
                self.assertEqual(diagnostic["first_metadata_mismatch"],
                                 expected_first_mismatch)
                clock_shape = diagnostic["clock_shape"]
                self.assertEqual(clock_shape["property"], "동기화 시각")
                self.assertEqual(clock_shape["current_run_would_write"]["fractional_digits"], 0)
                self.assertEqual(clock_shape["current_run_would_write"]["offset_shape"], "z_suffix")
                self.assertEqual(clock_shape["stored_value"]["fractional_digits"], 3)
                self.assertEqual(clock_shape["stored_value"]["offset_shape"], "z_suffix")
                rendered = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
                self.assertNotIn(case["expected"], rendered)
                self.assertNotIn(case["actual"], rendered)
                self.assertNotIn(sync_time, rendered)
                self.assertNotIn(current_run_time, rendered)
                self.assertNotIn("Original issue", rendered)
                self.assertEqual(self.notion.writes, [])
                self.assertEqual(self.project.writes, [])
                self.assertEqual(self.repo_graph.mutations, 0)

        self._configure_date_readback_diagnostic(
            "2026-10-02T00:00:37Z", "2026-10-02T00:00:37Z",
            title_actual="private title fixture")
        diagnostic = self.run_sync(dry_run=True, diagnose_date_readback=True)[
            "date_readback_diagnostic"]
        self.assertEqual(diagnostic["first_metadata_mismatch"], "제목")
        self.assertFalse(diagnostic["metadata_matches"][0]["matches"])
        self.assertNotIn("private title fixture", json.dumps(diagnostic, ensure_ascii=False))
        self.assertEqual(self.notion.writes, [])

    def test_date_readback_diagnostic_refuses_non_dry_run_before_any_api_call(self):
        self._configure_date_readback_diagnostic("2026-10-02T00:00:00Z",
                                                 "2026-10-02T00:00:00Z")
        with self.assertRaisesRegex(se.SyncError, "requires --dry-run"):
            self.run_sync(diagnose_date_readback=True)
        self.assertEqual(self.repo_graph.queries, [])
        self.assertEqual(self.rest.calls, [])
        self.assertEqual(self.notion.calls, [])
        self.assertEqual(self.project.writes, [])

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = se.main(["--diagnose-date-readback"])
        self.assertEqual(result, 2)
        self.assertIn("requires --dry-run", stderr.getvalue())

    def test_display_date_readback_requires_exact_projected_instant_and_single_date(self):
        for name in sorted(se.DISPLAY_MINUTE_DATE_PROPERTIES):
            expected = {name: {"date": {"start": "2026-10-02T00:00:00Z"}}}
            accepted = (
                {"start": "2026-10-02T00:00:00.000+00:00", "end": None,
                 "time_zone": None},
                {"start": "2026-10-02T05:30:00+05:30", "end": None,
                 "time_zone": None},
            )
            for actual in accepted:
                with self.subTest(name=name, actual=actual):
                    se._verify_properties({"properties": {name: {"date": actual}}}, expected)

            rejected = (
                {"start": "2026-10-02T00:00:01.000+00:00", "end": None,
                 "time_zone": None},
                {"start": "2026-10-02T00:01:00.000+00:00", "end": None,
                 "time_zone": None},
                None,
                {"start": "2026-10-02", "end": None, "time_zone": None},
                {"start": "2026-10-02T00:00:00", "end": None, "time_zone": None},
                {"start": "not-a-date", "end": None, "time_zone": None},
                {"start": "2026-10-02T00:00:00Z", "end": "2026-10-03T00:00:00Z",
                 "time_zone": None},
                {"start": "2026-10-02T00:00:00Z", "end": None,
                 "time_zone": "UTC"},
            )
            for actual in rejected:
                with self.subTest(name=name, rejected=actual):
                    with self.assertRaises((se.SyncError, gp.SyncError)):
                        se._verify_properties({"properties": {name: {"date": actual}}}, expected)
            with self.subTest(name=name, expected_nonminute=True):
                with self.assertRaises((se.SyncError, gp.SyncError)):
                    se._verify_properties(
                        {"properties": {name: {"date": {"start": "2026-10-02T00:00:00Z"}}}},
                        {name: {"date": {"start": "2026-10-02T00:00:12Z"}}})

        scheduled = {"properties": {"일정": {"date": {
            "start": "2026-10-12", "end": "2026-10-13"}}}}
        se._verify_properties(scheduled, {"일정": {"date": {"start": "2026-10-12"}}})
        with self.assertRaises(se.SyncError):
            se._verify_properties(
                {"properties": {"일정": {"date": {"start": "2026-10-13"}}}},
                {"일정": {"date": {"start": "2026-10-12"}}})

    def test_control_date_preflight_preserves_legacy_null_and_compares_instants(self):
        precise_success = "2026-10-09T09:59:41.123456Z"
        minute_visible = "2026-10-09T09:59:00.000+00:00"
        internal = json.loads(control_internal())
        internal["last_success_at"] = precise_success
        control = control_page(se.canonical_json(internal))
        control["properties"]["동기화 시각"] = {"date": {
            "start": minute_visible, "end": None, "time_zone": None}}
        self.notion = FakeNotion([control])

        def same_instant_different_offset(response, payload):
            if payload["is_archived"]:
                return response
            for row in response["results"]:
                if se.read_text(row, "동기화 키") == se.CONTROL_KEY:
                    row["properties"]["동기화 시각"]["date"]["start"] = (
                        "2026-10-09T19:29:00+09:30")
            return response

        self.notion.query_response_transform = same_instant_different_offset
        self.run_sync(dry_run=True)
        self.assertEqual(self.notion.writes, [])
        self.assertEqual(self.project.writes, [])
        self.assertEqual(json.loads(se.read_text(self.notion.pages[CONTROL_ID],
                                                "동기화 내부 상태"))["last_success_at"],
                         precise_success)

        legacy = control_page(control_internal())
        legacy_visible = "2026-09-20T12:34:56Z"
        legacy["properties"]["동기화 시각"] = {"date": {"start": legacy_visible}}
        self.notion = FakeNotion([legacy])
        self.run_sync(dry_run=True)
        self.assertEqual(se.read_date(self.notion.pages[CONTROL_ID], "동기화 시각"),
                         legacy_visible)
        self.assertEqual(self.notion.writes, [])

        initial = control_page(control_internal())
        self.notion = FakeNotion([initial])
        self.run_sync(dry_run=True)
        self.assertEqual(self.notion.writes, [])

        for visible in ("2026-10-09T09:59:22Z", None):
            with self.subTest(rejected_visible=visible):
                bad_state = json.loads(control_internal())
                bad_state["last_success_at"] = precise_success
                bad_control = control_page(se.canonical_json(bad_state))
                bad_control["properties"]["동기화 시각"] = {
                    "date": None if visible is None else {"start": visible}}
                self.notion = FakeNotion([bad_control])
                with self.assertRaises(se.SyncError):
                    self.run_sync(dry_run=True)
                self.assertEqual(self.notion.writes, [])
                self.assertEqual(self.project.writes, [])

        mismatch = control_page(control_internal())
        mismatch["properties"]["동기화 시각"] = {"date": {"start": minute_visible}}
        self.notion = FakeNotion([mismatch])

        def alter_listed_seconds(response, payload):
            if not payload["is_archived"]:
                for row in response["results"]:
                    if se.read_text(row, "동기화 키") == se.CONTROL_KEY:
                        row["properties"]["동기화 시각"]["date"]["start"] = (
                            "2026-10-09T09:59:01Z")
            return response
        self.notion.query_response_transform = alter_listed_seconds
        with self.assertRaisesRegex(se.SyncError, "snapshot 불일치"):
            self.run_sync(dry_run=True)
        self.assertEqual(self.notion.writes, [])
        self.assertEqual(self.project.writes, [])

    def _configure_post_cutoff_project_add(self):
        self.repo_graph = RepoGraph(issues=[make_issue(created_at="2026-10-03T00:00:00Z")])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI()
        self.notion = FakeNotion([control_page(control_internal())])

    def _leave_confirmed_add_marker_after_readback_loss(self):
        self._configure_post_cutoff_project_add()

        def lose_confirmed_marker_readback(path, properties, notion):
            internal = properties.get("동기화 내부 상태")
            if not path.startswith("/pages/") or not internal:
                return
            saved = json.loads(rich_text_value(internal))
            pending = saved.get("pending") or {}
            if pending.get("kind") == "add" and pending.get("confirmed"):
                notion.after_patch = None
                notion.fail_readback_after_patch.add(path.rsplit("/", 1)[1])

        self.notion.after_patch = lose_confirmed_marker_readback
        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync()

        _, state = self._stored_issue_state()
        pending = state["pending"]
        self.assertTrue(pending["confirmed"])
        self.assertEqual(pending["project_item_id"], pending["checkpoint"]["item_id"])
        self.assertIsNone(state["project_item_id"])
        self.assertEqual(self.project.add_calls, 1)
        return pending["project_item_id"]

    def _stored_issue_state(self):
        key = se.key_for(gp.REPOSITORY_ID, ISSUE_ID)
        page = next(page for page in self.notion.pages.values()
                    if se.read_text(page, "동기화 키") == key)
        state = se.decode_internal(se.read_text(page, "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        return page, state

    def _install_historical_pending_snapshot(self, issue, state, *,
                                             pending_option="완료", pending_task="완료"):
        healthy = make_issue()
        healthy.update({"id": "I_kwDOIssue19", "databaseId": 556, "number": 19,
                        "url": f"https://github.com/{gp.REPOSITORY}/issues/19"})
        self.repo_graph = RepoGraph(issues=[issue, healthy])
        self.rest = LegacyREST(issues=[
            {"id": ISSUE_ID, "node_id": ISSUE_NODE, "number": ISSUE_NUMBER,
             "state": issue["state"].lower()},
            {"id": 556, "node_id": healthy["id"], "number": 19, "state": "open"},
        ])
        self.project = ProjectAPI([
            make_project_item(option=pending_option),
            make_project_item(item_id="PVTI_issue19", issue_id=556,
                              issue_node=healthy["id"], number=19, option="백로그"),
        ])
        self.notion = FakeNotion([
            control_page(control_internal()),
            active_issue_page(task=pending_task, internal=se.canonical_json(state)),
            active_issue_page(page_id=SECOND_ISSUE_PAGE_ID, issue_id=556, number=19,
                              task="백로그"),
        ])

    def test_pr19_rest_issues_id_reuses_existing_row_and_preserves_manual_content(self):
        pr = make_pr()
        pr["updatedAt"] = "2026-09-03T14:27:48.987654Z"
        self.repo_graph = RepoGraph(pulls=[pr])
        self.rest = LegacyREST(issues=[{"id": PR_REST_ID, "node_id": PR_NODE, "number": 19,
            "state": "closed", "pull_request": {"url": "https://api.github.com/pulls/19"}}],
            pull={"id": PR_DATABASE_ID, "node_id": PR_NODE, "number": 19, "state": "closed",
                  "draft": False, "merged": True,
                  "base": {"ref": "main", "repo": {"id": gp.REPOSITORY_ID, "node_id": REPO_NODE}}})
        key = se.key_for(gp.REPOSITORY_ID, PR_REST_ID)
        old = notion_page(PR_PAGE_ID, key=key, kind="PR", number=19)
        old["properties"]["제목"] = se.text_property("old title", "title")
        old["properties"]["메모"] = se.text_property("human note")
        old["properties"]["일정"] = {"date": {"start": "2026-10-21"}}
        old["body"] = "human page body"
        self.notion = FakeNotion([control_page(control_internal()), old])
        self.notion.normalize_minute_dates = True

        facts = gp.fetch_repository_facts(self.repo_graph, self.rest)
        self.assertEqual(list(facts["pulls"]), [PR_REST_ID])
        self.assertEqual(facts["pulls"][PR_REST_ID]["databaseId"], PR_DATABASE_ID)
        counts = self.run_sync()

        self.assertEqual(counts["created"], 0)
        self.assertEqual(self.notion.creates, 0)
        self.assertEqual(len(self.notion.pages), 2)
        updated = self.notion.pages[PR_PAGE_ID]
        self.assertEqual(se.read_text(updated, "동기화 키"), key)
        self.assertEqual(se.read_select(updated, "GitHub 상태"), "Merged")
        self.assertEqual(se.read_date(updated, "GitHub 수정"),
                         "2026-09-03T14:27:00.000+00:00")
        self.assertEqual(se.read_date(updated, "동기화 시각"),
                         "2026-10-08T00:00:00.000+00:00")
        self.assertEqual(se.read_text(updated, "메모"), "human note")
        self.assertEqual(updated["properties"]["일정"]["date"]["start"], "2026-10-21")
        self.assertEqual(updated["body"], "human page body")
        self.assertEqual(self.project.writes, [])

    def test_minute_display_roundtrip_preserves_full_precision_and_repeat_is_idempotent(self):
        issue = make_issue(created_at="2026-10-03T00:00:00Z")
        issue["updatedAt"] = "2026-10-03T14:27:48.987654Z"
        self.repo_graph = RepoGraph(issues=[issue])
        self.repo_graph.server_time = datetime(2026, 10, 1, 0, 0, 12, 345678,
                                               tzinfo=timezone.utc)
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI()
        self.notion = FakeNotion([control_page("")])
        self.notion.normalize_minute_dates = True
        first_now = "2026-10-09T09:59:41.987654Z"

        first = self.run_sync(now=first_now)

        self.assertEqual(first["created"], 1)
        self.assertEqual(self.notion.creates, 1)
        self.assertEqual(self.project.add_calls, 1)
        issue_row = next(page for page in self.notion.pages.values()
                         if se.read_text(page, "동기화 키") ==
                         se.key_for(gp.REPOSITORY_ID, ISSUE_ID))
        self.assertEqual(se.read_date(issue_row, "GitHub 수정"),
                         "2026-10-03T14:27:00.000+00:00")
        self.assertEqual(se.read_date(issue_row, "동기화 시각"),
                         "2026-10-09T09:59:00.000+00:00")
        issue_post = next(payload for method, path, payload, _ in self.notion.writes
                          if method == "POST" and path == "/pages")
        self.assertEqual(issue_post["properties"]["GitHub 수정"]["date"]["start"],
                         "2026-10-03T14:27:00Z")
        self.assertEqual(issue_post["properties"]["동기화 시각"]["date"]["start"],
                         "2026-10-09T09:59:00Z")
        saved_control = se.decode_internal(
            se.read_text(self.notion.pages[CONTROL_ID], "동기화 내부 상태"),
            kind="control", project_id=gp.EXPECTED_PROJECT_ID)
        self.assertEqual(saved_control["migration_cutoff"],
                         "2026-10-01T00:00:12.345678Z")
        self.assertEqual(saved_control["last_success_at"], first_now)
        self.assertEqual(saved_control["last_result"]["at"], first_now)
        self.assertEqual(se.read_date(self.notion.pages[CONTROL_ID], "동기화 시각"),
                         "2026-10-09T09:59:00.000+00:00")

        project_writes = copy.deepcopy(self.project.writes)
        second_now = "2026-10-09T10:00:03.456789Z"
        second = self.run_sync(now=second_now)

        self.assertEqual(second["created"], 0)
        self.assertEqual(self.notion.creates, 1)
        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual(self.project.writes, project_writes)
        self.assertEqual(se.read_date(issue_row, "GitHub 수정"),
                         "2026-10-03T14:27:00.000+00:00")
        self.assertEqual(se.read_date(issue_row, "동기화 시각"),
                         "2026-10-09T10:00:00.000+00:00")
        repeated_control = se.decode_internal(
            se.read_text(self.notion.pages[CONTROL_ID], "동기화 내부 상태"),
            kind="control", project_id=gp.EXPECTED_PROJECT_ID)
        self.assertEqual(repeated_control["migration_cutoff"],
                         "2026-10-01T00:00:12.345678Z")
        self.assertEqual(repeated_control["last_success_at"], second_now)
        self.assertEqual(repeated_control["last_result"]["at"], second_now)

    def test_pr28_linked_issue13_still_enters_review_with_minute_dates(self):
        pr = make_pr()
        pr.update({"number": 28, "url": f"https://github.com/{gp.REPOSITORY}/pull/28",
                   "state": "OPEN", "isDraft": False, "mergedAt": None,
                   "updatedAt": "2026-10-08T10:22:39.123456Z"})
        reference = pr_reference(pr)
        issue = make_issue(linked=[reference])
        issue.update({"number": 13,
                      "url": f"https://github.com/{gp.REPOSITORY}/issues/13",
                      "updatedAt": "2026-10-08T10:21:57.654321Z"})
        self.repo_graph = RepoGraph(issues=[issue], pulls=[pr])
        self.rest = LegacyREST(issues=[
            {"id": ISSUE_ID, "node_id": ISSUE_NODE, "number": 13, "state": "open"},
            {"id": PR_REST_ID, "node_id": PR_NODE, "number": 28, "state": "open",
             "pull_request": {"url": "https://api.github.com/repos/Aurelia-aurity/Replica/pulls/28"}},
        ], pull={"id": PR_DATABASE_ID, "node_id": PR_NODE, "number": 28,
                 "state": "open", "draft": False, "merged": False,
                 "base": {"ref": "main", "repo": {"id": gp.REPOSITORY_ID,
                     "node_id": REPO_NODE}}})
        self.project = ProjectAPI([make_project_item(option="백로그", number=13)])
        issue_row = active_issue_page(task="백로그", number=13)
        issue_row["properties"]["메모"] = se.text_property("issue memo")
        issue_row["properties"]["일정"] = {"date": {
            "start": "2026-10-12", "end": "2026-10-13"}}
        issue_row["body"] = "issue body"
        self.notion = FakeNotion([control_page(control_internal()), issue_row])
        self.notion.normalize_minute_dates = True

        result = self.run_sync(now="2026-10-09T11:11:51.987654Z")

        self.assertEqual(result["held"], 0)
        self.assertEqual(self.project.add_calls, 0)
        self.assertEqual(self.project.items[0]["fieldValues"]["nodes"][0]["optionId"],
                         gp.EXPECTED_STATUS_OPTIONS["검토 중"])
        updated_issue = self.notion.pages[ISSUE_PAGE_ID]
        self.assertEqual(se.read_select(updated_issue, "작업 상태"), "검토 중")
        self.assertEqual(se.read_date(updated_issue, "GitHub 수정"),
                         "2026-10-08T10:21:00.000+00:00")
        self.assertEqual(se.read_date(updated_issue, "동기화 시각"),
                         "2026-10-09T11:11:00.000+00:00")
        self.assertEqual(se.read_text(updated_issue, "메모"), "issue memo")
        self.assertEqual(updated_issue["properties"]["일정"]["date"], {
            "start": "2026-10-12", "end": "2026-10-13"})
        self.assertEqual(updated_issue["body"], "issue body")
        created_pr = next(page for page in self.notion.pages.values()
                          if se.read_select(page, "종류") == "PR")
        self.assertEqual(se.read_date(created_pr, "GitHub 수정"),
                         "2026-10-08T10:22:00.000+00:00")
        control_state = se.decode_internal(
            se.read_text(self.notion.pages[CONTROL_ID], "동기화 내부 상태"),
            kind="control", project_id=gp.EXPECTED_PROJECT_ID)
        self.assertEqual(control_state["last_success_at"],
                         "2026-10-09T11:11:51.987654Z")

    def test_missing_rest_issue_mapping_fails_before_any_notion_write(self):
        self.repo_graph = RepoGraph(pulls=[make_pr()])
        self.rest = LegacyREST(issues=[])
        with self.assertRaises(gp.SyncError):
            self.run_sync()
        self.assertEqual(self.notion.writes, [])
        self.assertEqual(self.project.writes, [])

    def test_readonly_dry_run_outputs_migration_plan_and_writes_nothing(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="백로그")])
        self.notion = FakeNotion([control_page(control_internal()), active_issue_page(task="백로그")])
        result = self.run_sync(dry_run=True)
        plan = result["issue_plans"][0]
        self.assertEqual(plan["issue_number"], ISSUE_NUMBER)
        self.assertEqual(plan["target"], "백로그")
        self.assertIsNone(plan["hold"])
        self.assertFalse(plan["project_add_required"])
        self.assertFalse(plan["project_change"])
        self.assertEqual(self.notion.writes, [])
        self.assertEqual(self.project.writes, [])
        self.assertEqual(se.read_text(self.notion.pages[CONTROL_ID], "동기화 내부 상태"),
                         control_internal())

    def test_readonly_dry_run_previews_new_issue_project_add_without_marker(self):
        self.repo_graph = RepoGraph(issues=[make_issue(created_at="2026-10-03T00:00:00Z")])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.notion = FakeNotion([control_page(control_internal())])
        result = self.run_sync(dry_run=True)
        plan = result["issue_plans"][0]
        self.assertTrue(plan["project_add_required"])
        self.assertEqual(plan["target"], "백로그")
        self.assertEqual(self.notion.creates, 0)
        self.assertEqual(self.notion.writes, [])
        self.assertEqual(self.project.writes, [])

    def test_dispatch_dry_run_previews_pm_resolution_without_any_write(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="백로그")])
        state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        state["hold"] = se._new_hold("MIGRATION_CONFLICT", "migration conflict", se._digest("held"))
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task="진행 중", internal=se.canonical_json(state))])
        env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "Just-Simple0",
               "GITHUB_TRIGGERING_ACTOR": "Just-Simple0",
               "GITHUB_RUN_ID": "918273", "GITHUB_RUN_ATTEMPT": "1",
               "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID),
               "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "a" * 40,
               "SYNC_APPROVED_SHA": "a" * 40}
        result = self.run_sync(dry_run=True, resolve_issue_numbers="18,18", env=env)
        resolution = result["resolution_preview"][0]
        self.assertEqual(resolution["result"], "would_resume")
        self.assertEqual(resolution["project_before"], "백로그")
        self.assertEqual(resolution["project_after"], "백로그")
        self.assertFalse(resolution["project_change"])
        self.assertEqual(self.notion.writes, [])
        self.assertEqual(self.project.writes, [])
        self.assertEqual(self.repo_graph.mutations, 0)

    def test_missing_legacy_row_prevents_global_cutoff_write(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[])
        self.notion = FakeNotion([control_page()])
        with self.assertRaises(gp.SyncError):
            self.run_sync()
        self.assertEqual(self.notion.writes, [])

    def test_unknown_or_null_notion_request_status_fails_before_any_write(self):
        statuses = ({"type": "queued"}, None, {"type": "incomplete"}, {},
                    {"type": []}, {"type": "complete", "incomplete_reason": None},
                    {"type": "complete", "incomplete_reason": "unknown-reason"})
        for request_status in statuses:
            with self.subTest(request_status=request_status):
                self.notion = FakeNotion([control_page(control_internal())])
                self.notion.request_status = request_status
                with self.assertRaises(se.SyncError):
                    self.run_sync()
                self.assertEqual(self.notion.writes, [])
                self.assertEqual(self.project.writes, [])

    def test_missing_notion_request_status_is_accepted_after_all_sdk_shaped_pages(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="백로그")])
        self.notion = FakeNotion([control_page(control_internal()), active_issue_page(task="백로그")])
        self.notion.page_size = 1
        self.notion.omit_request_status = True

        diagnostics = io.StringIO()
        with contextlib.redirect_stderr(diagnostics):
            result = self.run_sync(dry_run=True)

        self.assertEqual(result["source_items"], 1)
        self.assertEqual(len(result["issue_plans"]), 1)
        self.assertIn("request_status=missing", diagnostics.getvalue())
        self.assertIn("type=page_or_data_source page_or_data_source=object", diagnostics.getvalue())
        self.assertIn("next_cursor_valid=true", diagnostics.getvalue())
        self.assertEqual(self.notion.writes, [])
        self.assertEqual(self.project.writes, [])

    def test_malformed_notion_query_envelopes_fail_before_any_write(self):
        def remove(name):
            def transform(response, _payload):
                response.pop(name, None)
                return response
            return transform

        def change(**values):
            def transform(response, _payload):
                response.update(values)
                return response
            return transform

        transforms = (
            lambda _response, _payload: None,
            remove("object"), change(object="page"),
            remove("type"), change(type="page"),
            remove("page_or_data_source"), change(page_or_data_source=[]),
            remove("results"), change(results={}),
            remove("has_more"), change(has_more=1),
            remove("next_cursor"), change(next_cursor="terminal-cursor"),
        )
        for transform in transforms:
            with self.subTest(transform=transform):
                self.notion = FakeNotion([control_page(control_internal())])
                self.notion.query_response_transform = transform
                with self.assertRaises(se.SyncError):
                    self.run_sync()
                self.assertEqual(self.notion.writes, [])
                self.assertEqual(self.project.writes, [])

    def test_control_row_accepts_only_legacy_empty_or_fixed_workflow_url(self):
        for value in (None, se.ACTIONS_WORKFLOW_URL):
            with self.subTest(url=value):
                self.notion = FakeNotion([control_page(control_internal())])
                self.notion.pages[CONTROL_ID]["properties"]["GitHub URL"] = {"url": value}
                self.run_sync(dry_run=True)
                self.assertEqual(self.notion.writes, [])
                self.assertEqual(self.project.writes, [])

        for value in ("https://github.com/Aurelia-aurity/Replica/actions/runs/12345",
                      "https://github.com/another/repo/actions/workflows/notion-sync.yml",
                      "https://example.invalid/workflow"):
            with self.subTest(rejected_url=value):
                self.notion = FakeNotion([control_page(control_internal())])
                self.notion.pages[CONTROL_ID]["properties"]["GitHub URL"] = {"url": value}
                with self.assertRaisesRegex(se.SyncError, "Actions URL"):
                    self.run_sync()
                self.assertEqual(self.notion.writes, [])
                self.assertEqual(self.project.writes, [])

    def test_control_summary_readback_mismatch_fails_closed(self):
        self.notion = FakeNotion([control_page(control_internal())])

        def corrupt_control_title(path, properties, notion):
            if path == f"/pages/{CONTROL_ID}" and "제목" in properties:
                notion.after_patch = None
                notion.pages[CONTROL_ID]["properties"]["제목"] = se.text_property(
                    "unexpected summary", "title")

        self.notion.after_patch = corrupt_control_title
        with self.assertRaisesRegex(se.SyncError, "readback"):
            self.run_sync()
        control_patches = [entry for entry in self.notion.writes
                           if entry[1] == f"/pages/{CONTROL_ID}"]
        self.assertEqual(len(control_patches), 1)
        self.assertEqual(self.project.writes, [])

    def test_late_notion_page_validation_failure_prevents_every_write(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="백로그")])
        self.notion = FakeNotion([control_page(control_internal()), active_issue_page(task="백로그")])
        self.notion.page_size = 1

        def break_second_page(response, payload):
            if "start_cursor" in payload:
                response["type"] = "unexpected-page-type"
            return response

        self.notion.query_response_transform = break_second_page
        with self.assertRaisesRegex(se.SyncError, "envelope"):
            self.run_sync()
        self.assertEqual(self.notion.writes, [])
        self.assertEqual(self.notion.creates, 0)
        self.assertEqual(self.project.writes, [])

    def test_notion_query_cursors_must_be_fresh_and_terminal_cursor_null(self):
        def response(*, has_more=False, next_cursor=None):
            return {"object": "list", "type": "page_or_data_source",
                    "page_or_data_source": {}, "results": [None],
                    "has_more": has_more, "next_cursor": next_cursor}

        class ResponseSequence:
            def __init__(self, responses):
                self.responses = list(responses)
                self.calls = 0

            def request(self, *_args, **_kwargs):
                value = self.responses[self.calls]
                self.calls += 1
                return copy.deepcopy(value)

        malformed = (
            {"object": "list", "type": "page_or_data_source", "page_or_data_source": {},
             "results": [], "has_more": True},
            response(has_more=True, next_cursor=""),
            response(has_more=True, next_cursor=7),
            response(next_cursor="terminal-cursor"),
        )
        for value in malformed:
            with self.subTest(value=value):
                with self.assertRaises(se.SyncError):
                    se.query_all(ResponseSequence([value]), SOURCE_ID, False)

        repeated = ResponseSequence([response(has_more=True, next_cursor="cursor-a"),
                                      response(has_more=True, next_cursor="cursor-a")])
        with self.assertRaisesRegex(se.SyncError, "반복"):
            se.query_all(repeated, SOURCE_ID, False)

    def test_notion_query_cap_refuses_10000_and_accepts_9999_per_query(self):
        class SizedNotion:
            def __init__(self, total):
                self.total = total
                self.archived_queries = []

            def request(self, _method, _path, payload):
                self.archived_queries.append(payload["is_archived"])
                start = int(payload.get("start_cursor", "0"))
                end = min(start + se.NOTION_QUERY_PAGE_SIZE, self.total)
                has_more = end < self.total
                return {"object": "list", "type": "page_or_data_source",
                        "page_or_data_source": {}, "results": [None] * (end - start),
                        "has_more": has_more,
                        "next_cursor": str(end) if has_more else None}

        for archived in (False, True):
            for total, rejected in ((9999, False), (10000, True)):
                with self.subTest(archived=archived, total=total):
                    notion = SizedNotion(total)
                    if rejected:
                        with self.assertRaisesRegex(se.SyncError, "10,000"):
                            se.query_all(notion, SOURCE_ID, archived)
                    else:
                        rows = se.query_all(notion, SOURCE_ID, archived)
                        self.assertEqual(len(rows), total)
                    self.assertEqual(set(notion.archived_queries), {archived})

    def test_query_shape_diagnostics_do_not_log_remote_values(self):
        class SingleResponse:
            def request(self, *_args, **_kwargs):
                return {"object": "list", "type": "page_or_data_source",
                        "page_or_data_source": {"remote_marker": "BODY_CANARY"},
                        "results": [{"id": "ROW_CANARY"}], "has_more": False,
                        "next_cursor": "CURSOR_CANARY",
                        "request_status": {"type": "STATUS_CANARY",
                                          "incomplete_reason": "REASON_CANARY"}}

        diagnostics = io.StringIO()
        with contextlib.redirect_stderr(diagnostics):
            with self.assertRaises(se.SyncError):
                se.query_all(SingleResponse(), SOURCE_ID, False, diagnostics=True)
        output = diagnostics.getvalue()
        for value in ("BODY_CANARY", "ROW_CANARY", "CURSOR_CANARY", "STATUS_CANARY", "REASON_CANARY"):
            self.assertNotIn(value, output)
        self.assertIn("request_status=other", output)
        self.assertIn("incomplete_reason=other", output)
        self.assertIn("results_count=1", output)
        self.assertIn("next_cursor_type=string", output)

    def test_transport_mutation_is_not_retried_and_read_can_retry(self):
        class Response:
            headers = {"Date": "Thu, 01 Oct 2026 00:00:00 GMT"}
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return b'{"data":{"viewer":{"id":"U","login":"test"}}}'

        class Opener:
            def __init__(self, fail_once=False): self.calls, self.fail_once = 0, fail_once
            def open(self, request, timeout):
                self.calls += 1
                if self.fail_once and self.calls == 1:
                    raise urllib.error.HTTPError(request.full_url, 503, "retry",
                        {"Retry-After": "0"}, None)
                return Response()

        opener = Opener(fail_once=True)
        client = gp.GraphQLClient("fixture-token", opener=opener, sleep=lambda _: None)
        data, _ = client.request("query { viewer { id login } }")
        self.assertIn("viewer", data)
        self.assertEqual(opener.calls, 2)
        opener = Opener(fail_once=True)
        client = gp.GraphQLClient("fixture-token", opener=opener, sleep=lambda _: None)
        with self.assertRaises(gp.SyncError):
            client.request("mutation { doThing }", mutation=True)
        self.assertEqual(opener.calls, 1)

    def _plan(self, issue, *, project_state="백로그", notion_state="백로그", state=None, facts=None):
        state = state or se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        item = {"id": "PVTI_issue18", "status_option_id":
                gp.EXPECTED_STATUS_OPTIONS[project_state] if project_state else None}
        row = {"properties": {"작업 상태": {"select": {"name": notion_state} if notion_state else None}}}
        facts = facts or {"repository_node_id": REPO_NODE, "pulls": {}}
        return se._plan_issue(issue, item, row, state, facts, CUTOFF,
                              gp.EXPECTED_PROJECT_ID, gp.EXPECTED_STATUS_OPTIONS)

    def test_closure_duplicate_unknown_reason_and_reopen_priority(self):
        completed = make_issue(state="CLOSED", reason="COMPLETED", closed_at="2026-10-03T00:00:00Z")
        self.assertEqual(self._plan(completed)["target"], "완료")
        duplicate = make_issue(state="CLOSED", reason="DUPLICATE", closed_at="2026-10-03T00:00:00Z")
        result = self._plan(duplicate)
        self.assertIsNone(result["target"])
        self.assertEqual(result["end_reason"], "중복")
        self.assertIn("대표 이슈", result["confirmation"])
        unknown = make_issue(state="CLOSED", reason=None, closed_at="2026-10-03T00:00:00Z")
        self.assertEqual(self._plan(unknown)["end_reason"], "확인 필요")

        reopened = make_issue(state="OPEN", reopens=[{"id": "REOPEN_EVENT_1",
            "createdAt": "2026-10-04T00:00:00Z", "stateReason": "REOPENED"}])
        plan = self._plan(reopened, project_state="완료")
        self.assertEqual(plan["target"], "백로그")
        self.assertEqual(plan["state"]["reopen_last_id"], "REOPEN_EVENT_1")
        blank_reopened = self._plan(reopened, project_state=None,
            state=se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID))
        self.assertEqual(blank_reopened["target"], "백로그")

    def test_explicit_main_non_draft_link_enters_review_and_returns_once(self):
        pr = {"id": PR_NODE, "databaseId": PR_DATABASE_ID, "number": 19, "state": "OPEN",
              "isDraft": False, "baseRefName": "main",
              "baseRepository": {"id": REPO_NODE, "databaseId": gp.REPOSITORY_ID}}
        ref = {"id": PR_NODE, "databaseId": PR_DATABASE_ID, "number": 19, "state": "OPEN",
               "isDraft": False, "baseRefName": "main", "baseRepository": {"id": REPO_NODE},
               "repository": {"id": REPO_NODE}, "url": "https://github.com/Aurelia-aurity/Replica/pull/19"}
        issue = make_issue(linked=[ref])
        facts = {"repository_node_id": REPO_NODE, "pulls": {PR_REST_ID: pr}}
        state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        plan = self._plan(issue, project_state="백로그", state=state, facts=facts)
        self.assertEqual(plan["target"], "검토 중")
        self.assertEqual(plan["state"]["review_cycle"], 1)
        blank_project = self._plan(issue, project_state=None,
            state=se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID), facts=facts)
        self.assertEqual(blank_project["target"], "검토 중")

        returned_state = plan["state"]
        returned = self._plan(make_issue(), project_state="검토 중", state=returned_state)
        self.assertEqual(returned["target"], "진행 중")
        self.assertEqual(returned["state"]["review_return_cycle"], 1)
        again = self._plan(make_issue(), project_state="진행 중", state=returned_state)
        self.assertEqual(again["target"], "진행 중")
        self.assertEqual(again["state"]["review_return_cycle"], 1)

    def test_reopen_event_replay_is_idempotent_and_same_second_distinct_ids_hold(self):
        event = {"id": "REOPEN_EVENT_A", "createdAt": "2026-10-04T00:00:00Z"}
        repeated = make_issue(reopens=[event, copy.deepcopy(event)])
        first = self._plan(repeated)
        self.assertEqual(first["target"], "백로그")
        self.assertEqual(first["state"]["reopen_last_id"], event["id"])
        second = self._plan(repeated, state=first["state"])
        self.assertNotIn("hold", second)
        self.assertEqual(second["state"]["reopen_last_id"], event["id"])

        ambiguous = make_issue(reopens=[event,
            {"id": "REOPEN_EVENT_B", "createdAt": event["createdAt"]}])
        held = self._plan(ambiguous)
        self.assertEqual(held["hold"]["code"], "REOPEN_ORDER_AMBIGUOUS")

    def test_multiple_post_cutoff_reopens_and_close_order_are_not_silently_skipped(self):
        events = [
            {"id": "REOPEN_EVENT_A", "createdAt": "2026-10-02T00:00:00Z"},
            {"id": "REOPEN_EVENT_B", "createdAt": "2026-10-03T00:00:00Z"},
        ]
        plan = self._plan(make_issue(reopens=events))
        self.assertEqual(plan["state"]["reopen_last_id"], "REOPEN_EVENT_B")
        closed_after = make_issue(state="CLOSED", reason="COMPLETED",
            closed_at="2026-10-03T00:00:00Z",
            reopens=[{"id": "REOPEN_EVENT_LATE", "createdAt": "2026-10-04T00:00:00Z"}])
        self.assertEqual(self._plan(closed_after)["hold"]["code"], "REOPEN_CLOSE_ORDER")

    def test_human_general_state_closes_old_review_cycle_before_manual_review(self):
        prior = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        prior.update({"migration_complete": True, "review_cycle": 1,
                      "review_return_cycle": 0})
        moved_out = self._plan(make_issue(), project_state="준비 중", state=prior)
        self.assertEqual(moved_out["target"], "준비 중")
        self.assertEqual(moved_out["state"]["review_return_cycle"], 1)

        manual_review = self._plan(make_issue(), project_state="검토 중",
                                   state=moved_out["state"])
        self.assertEqual(manual_review["hold"]["code"], "REVIEW_WITHOUT_CYCLE")

        pr = {"id": PR_NODE, "databaseId": PR_DATABASE_ID, "number": 19,
              "state": "OPEN", "isDraft": False, "baseRefName": "main",
              "baseRepository": {"id": REPO_NODE, "databaseId": gp.REPOSITORY_ID}}
        ref = {"id": PR_NODE, "number": 19, "state": "OPEN", "isDraft": False,
               "baseRefName": "main", "baseRepository": {"id": REPO_NODE},
               "repository": {"id": REPO_NODE}}
        next_cycle = self._plan(make_issue(linked=[ref]), project_state="준비 중",
            state=moved_out["state"], facts={"repository_node_id": REPO_NODE,
                                              "pulls": {PR_REST_ID: pr}})
        self.assertEqual(next_cycle["target"], "검토 중")
        self.assertEqual(next_cycle["state"]["review_cycle"], 2)

    def test_corrected_closure_duplicate_contradiction_requires_pm_resume(self):
        issue = make_issue(state="CLOSED", reason="COMPLETED", closed_at="2026-10-03T00:00:00Z",
                           duplicate={"id": "I_other", "url": "https://github.com/Aurelia-aurity/Replica/issues/7"})
        self.repo_graph = RepoGraph(issues=[issue])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "closed"}])
        self.project = ProjectAPI([make_project_item(option="백로그")])
        self.notion = FakeNotion([control_page(control_internal()), active_issue_page(task="백로그")])

        self.run_sync()
        held = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(held["hold"]["code"], "CLOSURE_DUPLICATE_CONFLICT")

        env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "Just-Simple0",
               "GITHUB_TRIGGERING_ACTOR": "Just-Simple0",
               "GITHUB_RUN_ID": "918299", "GITHUB_RUN_ATTEMPT": "1",
               "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID),
               "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "a" * 40,
               "SYNC_APPROVED_SHA": "a" * 40}
        self.run_sync(resolve_issue_numbers="18", env=env)
        still_held = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(still_held["hold"]["code"], "CLOSURE_DUPLICATE_CONFLICT")
        self.assertEqual(self.project.writes, [])

        self.repo_graph.issues[0]["duplicateOf"] = None
        env["GITHUB_RUN_ID"] = "918300"
        self.run_sync(resolve_issue_numbers="18", env=env)
        resumed = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertIsNone(resumed["hold"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "완료")
        self.assertEqual(len(self.project.writes), 1)

    def test_v2_pending_uses_semantic_facts_while_v1_pending_keeps_legacy_hash(self):
        issue = make_issue(state="CLOSED", reason="COMPLETED",
                           closed_at="2026-10-03T00:00:00Z")
        state = make_pending_status_state(issue, semantic_v2=True)
        legacy_hash = state["pending"]["facts_fingerprint"]
        issue["updatedAt"] = "2026-10-07T00:00:00Z"
        issue["title"] = "Cosmetic title edit"
        issue["body"] = "Cosmetic body edit"
        self.repo_graph = RepoGraph(issues=[issue])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "closed"}])
        self.project = ProjectAPI([make_project_item(option="완료")])
        self.notion = FakeNotion([control_page(control_internal()),
            active_issue_page(task="완료", internal=se.canonical_json(state))])

        result = self.run_sync()

        self.assertEqual(result["held"], 0)
        resolved = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertIsNone(resolved["pending"])
        self.assertIsNone(resolved["hold"])
        self.assertTrue(resolved["migration_complete"])
        self.assertNotEqual(legacy_hash, se._source_fingerprint(
            issue, [], {"id": "PVTI_issue18", "status_option_id":
                        gp.EXPECTED_STATUS_OPTIONS["백로그"]}))
        self.assertEqual(state["pending"]["semantic_fingerprint"],
                         se._pending_semantic_fingerprint(issue, [],
                             {"id": "PVTI_issue18", "status_option_id":
                              gp.EXPECTED_STATUS_OPTIONS["백로그"]}, "status"))

    def test_archived_project_hold_requires_unarchive_and_pm_resume(self):
        issue = make_issue()
        self.repo_graph = RepoGraph(issues=[issue])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="백로그", archived=True)])
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task="백로그")])

        counts = self.run_sync()
        self.assertEqual(counts["held"], 1)
        archived_state = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(archived_state["hold"]["code"], "PROJECT_ITEM_ARCHIVED")
        self.run_sync(resolve_issue_numbers="18", env=pm_env(918301))
        still_archived = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(still_archived["hold"]["code"], "PROJECT_ITEM_ARCHIVED")
        self.assertEqual(self.project.writes, [])

        self.project.items[0]["isArchived"] = False
        self.run_sync(resolve_issue_numbers="18", env=pm_env(918302))
        recovered = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertIsNone(recovered["hold"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "백로그")
        self.assertEqual(self.project.writes, [])

    def test_pending_status_unclear_or_changed_facts_needs_pm_cleanup_without_replay(self):
        for case in ("unclear", "facts_changed"):
            with self.subTest(case=case):
                issue = make_issue(state="CLOSED", reason="COMPLETED",
                                   closed_at="2026-10-03T00:00:00Z")
                state = make_pending_status_state(issue)
                actual_option = "준비 중" if case == "unclear" else "완료"
                if case == "facts_changed":
                    issue["updatedAt"] = "2026-10-05T00:00:00Z"
                self.repo_graph = RepoGraph(issues=[issue])
                self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
                    "number": ISSUE_NUMBER, "state": "closed"}])
                self.project = ProjectAPI([make_project_item(option=actual_option)])
                self.notion = FakeNotion([control_page(control_internal()),
                    active_issue_page(task="백로그", internal=se.canonical_json(state))])

                first = self.run_sync()
                self.assertEqual(first["held"], 1)
                held = se.decode_internal(
                    se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
                    kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                expected = "PENDING_RESULT_UNCLEAR" if case == "unclear" else "PENDING_FACTS_CHANGED"
                self.assertEqual(held["hold"]["code"], expected)
                if case == "unclear":
                    self.assertIsNotNone(held["pending"])
                    # The PM manually puts the active item at the saved target.
                    target = [status_value("완료")]
                    self.project.items[0]["fieldValues"]["nodes"] = target
                    self.project.status_values["PVTI_issue18"] = target

                writes_before = len(self.project.writes)
                self.run_sync(resolve_issue_numbers="18", env=pm_env(918303))
                recovered = se.decode_internal(
                    se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
                    kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                self.assertIsNone(recovered["pending"])
                self.assertIsNone(recovered["hold"])
                self.assertTrue(recovered["migration_complete"])
                self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "완료")
                self.assertEqual(len(self.project.writes), writes_before)

    def test_rerun_resume_requires_pm_as_original_and_triggering_actor(self):
        issue = make_issue()
        state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        state["migration_complete"] = True
        state["project_item_id"] = "PVTI_issue18"
        state["hold"] = se._new_hold("RESUME_CHECKPOINT_CHANGED", "resume checkpoint changed",
                                     se._digest("held"))
        self.repo_graph = RepoGraph(issues=[issue])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="준비 중")])
        self.notion = FakeNotion([control_page(control_internal()),
            active_issue_page(task=None, internal=se.canonical_json(state))])

        for env in (pm_env(918304, triggering_actor="teammate"),
                    pm_env(918305, triggering_actor=None),
                    pm_env(918307, actor="teammate"),
                    pm_env(918308, attempt=None),
                    pm_env(918309, attempt="2")):
            with self.subTest(trigger=env.get("GITHUB_TRIGGERING_ACTOR"),
                              actor=env.get("GITHUB_ACTOR"),
                              attempt=env.get("GITHUB_RUN_ATTEMPT")):
                with self.assertRaises((se.SyncError, gp.SyncError)):
                    self.run_sync(resolve_issue_numbers="18", env=env)
                self.assertEqual(self.notion.writes, [])
                self.assertEqual(self.project.writes, [])

        self.run_sync(resolve_issue_numbers="18", env=pm_env(918306))
        resumed = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertIsNone(resumed["hold"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(self.project.writes, [])

    def test_initial_migration_mismatch_holds_and_equal_state_is_checkpointable(self):
        mismatch = self._plan(make_issue(), project_state="백로그", notion_state="진행 중")
        self.assertEqual(mismatch["hold"]["code"], "MIGRATION_CONFLICT")
        equal = self._plan(make_issue(), project_state="백로그", notion_state="백로그")
        self.assertTrue(equal["migration_complete"])
        self.assertEqual(equal["target"], "백로그")

    def test_initial_migration_holds_when_project_status_is_blank_until_pm_resume(self):
        plan = self._plan(make_issue(), project_state=None, notion_state="진행 중")
        self.assertEqual(plan["hold"]["code"], "MIGRATION_UNSET")
        self.assertNotIn("target", plan)

    def test_initial_migration_preserves_project_when_notion_status_is_blank(self):
        plan = self._plan(make_issue(), project_state="준비 중", notion_state=None)
        self.assertEqual(plan["target"], "준비 중")
        self.assertTrue(plan["initial_migration"])

    def test_new_post_cutoff_issue_initializes_backlog_for_new_notion_row(self):
        for enabled in (False, True):
            for project_status in (None, "백로그", "add_readback"):
                with self.subTest(enabled=enabled, project_status=project_status):
                    issue = make_issue(created_at="2026-10-02T00:00:00Z")
                    self.repo_graph = RepoGraph(issues=[issue])
                    self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": issue["id"],
                        "number": ISSUE_NUMBER, "state": "open"}])
                    if project_status == "add_readback":
                        self.project = ProjectAPI()
                    else:
                        self.project = ProjectAPI([make_project_item(
                            item_id="PVTI_issue18", option=project_status)])
                    self.notion = FakeNotion([control_page(control_internal())])

                    preview = self.run_sync(dry_run=True, bidirectional_enabled=enabled)
                    self.assertEqual(preview["issue_plans"][0]["target"], "백로그")
                    self.assertIsNone(preview["issue_plans"][0]["hold"])
                    self.assertEqual(self.project.add_calls, 0)
                    self.assertEqual(self.project.writes, [])
                    self.assertEqual(self.notion.writes, [])

                    result = self.run_sync(bidirectional_enabled=enabled)
                    row = next(page for page in self.notion.pages.values()
                               if page["properties"].get("번호", {}).get("number") == ISSUE_NUMBER)
                    state = se.decode_internal(
                        se.read_text(row, "동기화 내부 상태"), kind="issue",
                        project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                    self.assertEqual(result["held"], 0)
                    self.assertEqual(se.read_select(row, "작업 상태"), "백로그")
                    if enabled:
                        self.assertEqual(state["baseline"]["notion_status"], "백로그")
                    else:
                        self.assertIsNone(state["baseline"])
                    self.assertTrue(state["migration_complete"])
                    if project_status == "add_readback":
                        self.assertEqual(self.project.add_calls, 1)
                        self.assertEqual(state["project_item_id"], "PVTI_added_1_0")
                    else:
                        self.assertEqual(self.project.add_calls, 0)
                    writes_after_init = len(self.project.writes)
                    stable = self.run_sync(bidirectional_enabled=enabled)
                    self.assertEqual(stable["held"], 0)
                    self.assertEqual(len(self.project.writes), writes_after_init)
                    self.assertEqual(se.read_select(row, "작업 상태"), "백로그")
                    self.assertEqual(self.project.add_calls,
                                     1 if project_status == "add_readback" else 0)
    def test_pre_cutoff_existing_unset_project_is_held_until_pm_selects_status(self):
        for enabled in (False, True):
            with self.subTest(bidirectional_enabled=enabled):
                self.repo_graph = RepoGraph(issues=[make_issue()])
                self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
                    "number": ISSUE_NUMBER, "state": "open"}])
                self.project = ProjectAPI([make_project_item(option=None)])
                self.notion = FakeNotion([control_page(control_internal()),
                    active_issue_page(task="준비 중")])
                held = self.run_sync(bidirectional_enabled=enabled)
                old_state = self._read_bidirectional_state()
                self.assertEqual(held["held"], 1)
                self.assertEqual(old_state["hold"]["code"], "MIGRATION_UNSET")
                self.assertIsNone(old_state["baseline"])
                self.assertFalse(old_state["migration_complete"])
                self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                                 "준비 중")
                self.assertEqual(self.project.writes, [])

                # PM explicitly chooses a Project status, then a new dispatch resumes.
                self.project.items[0]["fieldValues"]["nodes"] = [status_value("진행 중")]
                resumed = self.run_sync(bidirectional_enabled=enabled,
                                        resolve_issue_numbers="18", env=pm_env(913214))
                resumed_state = self._read_bidirectional_state()
                self.assertEqual(resumed["held"], 0)
                self.assertIsNone(resumed_state["hold"])
                self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                                 "진행 중")
                self.assertEqual(self.project.writes, [])

    def test_existing_unset_project_migration_is_flag_independent_and_facts_still_win(self):
        for enabled in (False, True):
            with self.subTest(flag=enabled):
                issue = make_issue(state="CLOSED", reason="COMPLETED",
                                   closed_at="2026-09-20T00:00:00Z")
                self.repo_graph = RepoGraph(issues=[issue])
                self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
                    "number": ISSUE_NUMBER, "state": "closed"}])
                self.project = ProjectAPI([make_project_item(option=None)])
                self.notion = FakeNotion([control_page(control_internal()),
                    active_issue_page(task="준비 중")])
                result = self.run_sync(bidirectional_enabled=enabled)
                self.assertEqual(result["held"], 0)
                self.assertEqual(self.project.items[0]["fieldValues"]["nodes"][0]["optionId"],
                                 gp.EXPECTED_STATUS_OPTIONS["완료"])
                self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                                 "완료")

    def test_rejected_status_restore_keeps_card_and_request_on_repeated_polls(self):
        self._configure_bidirectional_status(
            notion_status="검토 중", baseline_notion_status="백로그",
            project_status="백로그", baseline_project_status="백로그")

        first = self.run_sync(bidirectional_enabled=True)
        self.assertEqual(first["held"], 1)
        row = self.notion.pages[ISSUE_PAGE_ID]
        self.assertEqual(se.read_select(row, "작업 상태"), "백로그")
        original = self._read_bidirectional_state()
        request_id = original["request"]["id"]
        reason = original["request"]["reason"]
        self.assertEqual(original["request"]["phase"], "rejected")
        self.assertEqual(original["hold"]["code"], "STATUS_REQUEST_INVALID")
        self.assertTrue(se._confirmed_restore_echo(original, "백로그"))
        writes_after_restore = len(self.project.writes)

        for poll in range(2):
            before_issue_writes = sum(write[1] == f"/pages/{ISSUE_PAGE_ID}"
                                      for write in self.notion.writes)
            before_task_patches = len(task_status_patch_payloads(
                self.notion.writes, ISSUE_PAGE_ID))
            repeated = self.run_sync(bidirectional_enabled=True)
            state = self._read_bidirectional_state()
            self.assertEqual(repeated["held"], 0, f"poll={poll + 2}")
            self.assertEqual(se.read_select(row, "작업 상태"), "백로그")
            self.assertEqual(state["request"]["id"], request_id)
            self.assertEqual(state["request"]["target"], "검토 중")
            self.assertEqual(state["request"]["phase"], "rejected")
            self.assertEqual(state["request"]["reason"], reason)
            self.assertIsNone(state["hold"])
            self.assertIsNone(state["notion_write"])
            self.assertEqual(state["baseline"]["notion_status"], "백로그")
            self.assertGreaterEqual(sum(write[1] == f"/pages/{ISSUE_PAGE_ID}"
                                        for write in self.notion.writes), before_issue_writes)
            self.assertEqual(len(task_status_patch_payloads(
                self.notion.writes, ISSUE_PAGE_ID)), before_task_patches)
            self.assertEqual(len(self.project.writes), writes_after_restore)

        for code in ("STATUS_ORDER_UNKNOWN", "BIDIRECTIONAL_CONFLICT"):
            with self.subTest(confirmed_restore_hold=code):
                state = self._read_bidirectional_state()
                state["hold"] = se._new_hold(code, "원래 보류 사유", se._digest(code))
                state["request"]["phase"] = "held"
                state["request"]["reason"] = "원래 보류 사유"
                self.notion.pages[ISSUE_PAGE_ID]["properties"]["동기화 내부 상태"] = \
                    se.text_property(se.canonical_json(state))
                task_patches_before = len(task_status_patch_payloads(
                    self.notion.writes, ISSUE_PAGE_ID))
                repeated = self.run_sync(bidirectional_enabled=True)
                preserved = self._read_bidirectional_state()
                self.assertEqual(repeated["held"], 1)
                self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                                 "백로그")
                self.assertEqual(preserved["request"]["id"], request_id)
                self.assertEqual(preserved["request"]["reason"], "원래 보류 사유")
                self.assertEqual(preserved["hold"]["code"], code)
                self.assertEqual(len(task_status_patch_payloads(
                    self.notion.writes, ISSUE_PAGE_ID)), task_patches_before)
        self.assertEqual(self.project.writes, [])

    def test_confirmed_restore_survives_bidirectional_rollback_and_reenable(self):
        self._configure_bidirectional_status(
            notion_status="검토 중", baseline_notion_status="백로그",
            project_status="백로그", baseline_project_status="백로그")
        first = self.run_sync(bidirectional_enabled=True)
        self.assertEqual(first["held"], 1)
        state_before = self._read_bidirectional_state()
        request_before = copy.deepcopy(state_before["request"])
        marker_before = copy.deepcopy(state_before["notion_write"])
        self.assertTrue(se._confirmed_restore_echo(state_before, "백로그"))
        task_patches_before = len(task_status_patch_payloads(
            self.notion.writes, ISSUE_PAGE_ID))

        for flag in (False, False):
            row_writes_before = [write for write in self.notion.writes
                                 if write[1] == f"/pages/{ISSUE_PAGE_ID}"]
            result = self.run_sync(bidirectional_enabled=flag)
            state = self._read_bidirectional_state()
            row_writes_after = [write for write in self.notion.writes
                                if write[1] == f"/pages/{ISSUE_PAGE_ID}"]
            self.assertEqual(result["held"], 1)
            self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                             "백로그")
            self.assertEqual(state["request"], request_before)
            self.assertEqual(state["notion_write"], marker_before)
            self.assertEqual(state["hold"]["code"], "STATUS_REQUEST_INVALID")
            for _, _, payload, _ in row_writes_after[len(row_writes_before):]:
                task_patch = payload.get("properties", {}).get("작업 상태")
                self.assertFalse(task_patch and task_patch.get("select") is None)
            self.assertEqual(self.project.writes, [])

        reenabled = self.run_sync(bidirectional_enabled=True)
        terminal = self._read_bidirectional_state()
        self.assertEqual(reenabled["held"], 0)
        self.assertEqual(terminal["request"], request_before)
        self.assertIsNone(terminal["notion_write"])
        self.assertIsNone(terminal["hold"])
        self.assertEqual(terminal["baseline"]["notion_status"], "백로그")
        self.assertEqual(len(task_status_patch_payloads(
            self.notion.writes, ISSUE_PAGE_ID)), task_patches_before)
        self.assertEqual(self.project.writes, [])

        props = self.notion.pages[ISSUE_PAGE_ID]["properties"]
        self.assertEqual(props["요청 처리"]["select"]["name"], "요청 거절")
        self.assertEqual(rich_text_value(props["확인 필요"]), request_before["reason"])
        self.assertEqual(self.project.writes, [])

    def test_flag_off_held_request_preserves_valid_card_without_deferred_request(self):
        self._configure_bidirectional_status(
            notion_status="진행 중", project_status="준비 중",
            baseline_project_status="백로그")
        first = self.run_sync(bidirectional_enabled=True)
        state = self._read_bidirectional_state()
        self.assertEqual(first["held"], 1)
        self.assertEqual(state["request"]["phase"], "held")
        self.assertIsNone(state["deferred_request"])
        request_id = state["request"]["id"]
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": "준비 중"}}

        before = len(task_status_patch_payloads(self.notion.writes, ISSUE_PAGE_ID))
        paused = self.run_sync(bidirectional_enabled=False)
        preserved = self._read_bidirectional_state()
        self.assertEqual(paused["held"], 1)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(preserved["request"]["id"], request_id)
        self.assertIsNone(preserved["deferred_request"])
        self.assertEqual(task_status_patch_payloads(self.notion.writes, ISSUE_PAGE_ID)[before:], [])
        self.assertEqual(self.project.writes, [])

        resumed = self.run_sync(bidirectional_enabled=True)
        final = self._read_bidirectional_state()
        self.assertEqual(resumed["held"], 1)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(final["request"]["id"], request_id)
        self.assertIsNone(final["deferred_request"])
        self.assertEqual(task_status_patch_payloads(self.notion.writes, ISSUE_PAGE_ID)[before:], [])
        self.assertEqual(self.project.writes, [])

        # Native19 R1: carry the same unapproved held B through flag-off/on.
        self._configure_bidirectional_status(
            notion_status="준비 중", baseline_notion_status="진행 중",
            project_status="진행 중", baseline_project_status="진행 중")
        held = self._read_bidirectional_state()
        held["request"] = se.status_sync.request_record(
            "REQ_B", "준비 중", observed_from=NOW, observed_to=NOW,
            prior_notion_status="진행 중",
            project_option_id=gp.EXPECTED_STATUS_OPTIONS["진행 중"],
            phase="held", reason="held B")
        held["hold"] = se._new_hold("REQUEST_RACE", "held B", se._digest("REQ_B"))
        self.notion.pages[ISSUE_PAGE_ID]["properties"]["동기화 내부 상태"] = se.text_property(
            se.canonical_json(held))
        for flag in (False, True, True):
            self.run_sync(bidirectional_enabled=flag)
            carried = self._read_bidirectional_state()
            self.assertEqual(carried["request"]["id"], "REQ_B")
            self.assertIsNone(carried["deferred_request"], f"flag={flag}")
            self.assertIsNone(carried["resume"])
            self.assertEqual(carried["baseline"], held["baseline"])
            self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                             "준비 중")
        # An observed C, followed by a return to B, is a separate latest move.
        deferred_ids = []
        for target in ("백로그", "준비 중"):
            self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                "select": {"name": target}}
            self.run_sync(bidirectional_enabled=True)
            moved = self._read_bidirectional_state()
            self.assertEqual(moved["request"]["id"], "REQ_B")
            self.assertEqual(moved["deferred_request"]["target"], target)
            deferred_ids.append(moved["deferred_request"]["id"])
        self.assertEqual(len(set(deferred_ids + ["REQ_B"])), 3)
        self.assertEqual(self.project.writes, [])

    def test_flag_off_preserves_new_card_move_then_reenable_defers_it_from_old_request(self):
        self._configure_bidirectional_status(
            notion_status="검토 중", baseline_notion_status="백로그",
            project_status="백로그", baseline_project_status="백로그")
        rejected = self.run_sync(bidirectional_enabled=True)
        self.assertEqual(rejected["held"], 1)
        original = self._read_bidirectional_state()
        old_request = copy.deepcopy(original["request"])
        old_marker = copy.deepcopy(original["notion_write"])
        old_hold = copy.deepcopy(original["hold"])
        old_pending = copy.deepcopy(original["pending"])
        self.assertEqual(old_request["phase"], "rejected")
        self.assertTrue(se._confirmed_restore_echo(original, "백로그"))

        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": "준비 중"}}
        for _ in range(2):
            row_writes_before = [write for write in self.notion.writes
                                 if write[1] == f"/pages/{ISSUE_PAGE_ID}"]
            result = self.run_sync(bidirectional_enabled=False)
            state = self._read_bidirectional_state()
            row_writes_after = [write for write in self.notion.writes
                                if write[1] == f"/pages/{ISSUE_PAGE_ID}"]
            self.assertEqual(result["held"], 1)
            self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                             "준비 중")
            self.assertEqual(state["request"], old_request)
            self.assertEqual(state["notion_write"], old_marker)
            self.assertEqual(state["hold"], old_hold)
            self.assertEqual(state["pending"], old_pending)
            self.assertIsNone(state["deferred_request"])
            self.assertEqual(state["hold"]["code"], "STATUS_REQUEST_INVALID")
            self.assertEqual(task_status_patch_payloads(
                row_writes_after[len(row_writes_before):], ISSUE_PAGE_ID), [])
            self.assertEqual(self.project.writes, [])

        reenabled = self.run_sync(bidirectional_enabled=True)
        current = self._read_bidirectional_state()
        self.assertEqual(reenabled["held"], 1)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"),
                         "준비 중")
        self.assertEqual(current["request"]["id"], old_request["id"])
        self.assertEqual(current["request"]["target"], old_request["target"])
        self.assertEqual(current["request"]["reason"], old_request["reason"])
        self.assertEqual(current["notion_write"], old_marker)
        self.assertEqual(current["hold"], old_hold)
        self.assertEqual(current["pending"], old_pending)
        self.assertIsNotNone(current["deferred_request"])
        self.assertNotEqual(current["deferred_request"]["id"], old_request["id"])
        self.assertEqual(current["deferred_request"]["target"], "준비 중")
        self.assertEqual(current["deferred_request"]["phase"], "held")
        self.assertEqual(self.project.writes, [])

    def test_add_readback_contradiction_can_resume_only_after_exact_relation_repair(self):
        self._configure_post_cutoff_project_add()
        self.project.add_item_transform = lambda item: (
            item["content"]["repository"].update({"id": "R_foreign"}) or item)
        first = self.run_sync()
        self.assertEqual(first["held"], 1)
        _, held = self._stored_issue_state()
        returned_id = held["readback"]["returned_item_id"]
        self.assertEqual(held["hold"]["code"], "ADD_READBACK_CONTRADICTION")
        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual([query for query, _ in self.project.writes], [gp.ADD_ISSUE_MUTATION])

        # An explicit PM dispatch while the remote identity contradiction remains is safe,
        # but cannot authorize an add retry or a status write.
        unresolved = self.run_sync(resolve_issue_numbers="18", env=pm_env(913216))
        _, still_held = self._stored_issue_state()
        self.assertEqual(unresolved["held"], 1)
        self.assertEqual(still_held["hold"]["code"], "ADD_READBACK_CONTRADICTION")
        self.assertEqual(still_held["readback"]["returned_item_id"], returned_id)
        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual([query for query, _ in self.project.writes], [gp.ADD_ISSUE_MUTATION])

        # Repair the remote item without changing its returned ID. A fresh PM dispatch
        # must revalidate both direct node and complete active list before resolving.
        actual = next(item for item in self.project.items if item["id"] == returned_id)
        actual["content"]["repository"].update({
            "id": REPO_NODE, "databaseId": gp.REPOSITORY_ID,
            "nameWithOwner": gp.REPOSITORY})
        list_reads_before = self.project.item_page_calls
        recovered = self.run_sync(resolve_issue_numbers="18", env=pm_env(913217))
        _, settled = self._stored_issue_state()
        self.assertEqual(recovered["held"], 0)
        self.assertEqual(settled["project_item_id"], returned_id)
        self.assertEqual(settled["readback"]["returned_item_id"], returned_id)
        self.assertTrue(settled["readback"]["validated"])
        self.assertIsNone(settled["pending"])
        self.assertIsNone(settled["hold"])
        self.assertEqual(self.project.add_calls, 1)
        self.assertGreater(self.project.item_page_calls, list_reads_before)
        self.assertEqual([query for query, _ in self.project.writes].count(gp.ADD_ISSUE_MUTATION), 1)
        self.assertEqual([query for query, _ in self.project.writes].count(gp.SET_STATUS_MUTATION), 1)

    def test_new_issue_initialization_recovers_after_durable_add_readback_delays(self):
        for failure_mode in ("direct_and_list_delayed", "direct_network_error",
                             "returned_id_checkpoint_readback_loss"):
            with self.subTest(failure_mode=failure_mode):
                self._configure_post_cutoff_project_add()
                self.project.add_response_loss = False
                if failure_mode == "direct_and_list_delayed":
                    self.project.direct_item_visibility_delay_reads = 1
                    self.project.add_visibility_delay_snapshots = 1
                elif failure_mode == "direct_network_error":
                    self.project.fail_direct_readback_once = True
                else:
                    armed = False

                    def fail_returned_id_checkpoint(path, properties, notion):
                        nonlocal armed
                        if armed or "동기화 내부 상태" not in properties:
                            return
                        saved = json.loads(rich_text_value(properties["동기화 내부 상태"]))
                        if (saved.get("readback") or {}).get("returned_item_id"):
                            armed = True
                            notion.after_patch = None
                            notion.fail_readback_after_patch.add(path.rsplit("/", 1)[1])

                    self.notion.after_patch = fail_returned_id_checkpoint

                try:
                    first = self.run_sync(
                        bidirectional_enabled=True,
                        env={"GITHUB_RUN_ID": "r12-add-first", "GITHUB_RUN_ATTEMPT": "1"})
                except se.SyncError as exc:
                    self.assertEqual(failure_mode, "returned_id_checkpoint_readback_loss")
                    self.assertIn("synthetic page readback failure", str(exc))
                    self.assertTrue(armed)
                else:
                    self.assertNotEqual(failure_mode, "returned_id_checkpoint_readback_loss")
                    self.assertGreaterEqual(first["held"], 1)

                issue_page_id = next(page_id for page_id, page in self.notion.pages.items()
                    if page_id != CONTROL_ID and
                    page["properties"].get("종류", {}).get("select", {}).get("name") == "Issue")
                waiting = se.decode_internal(
                    se.read_text(self.notion.pages[issue_page_id], "동기화 내부 상태"),
                    kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                returned_id = waiting["readback"]["returned_item_id"]
                self.assertTrue(returned_id)
                self.assertFalse(waiting["readback"]["validated"])
                self.assertFalse(waiting["pending"]["confirmed"])
                self.assertEqual(self.project.add_calls, 1)
                self.assertEqual(sum(query in {gp.SET_STATUS_MUTATION,
                                               gp.CLEAR_STATUS_MUTATION}
                                     for query, _ in self.project.writes), 0)

                self.notion.after_patch = None
                recovered = self.run_sync(
                    bidirectional_enabled=True,
                    env={"GITHUB_RUN_ID": "r12-add-recovery", "GITHUB_RUN_ATTEMPT": "1"})
                final_row = self.notion.pages[issue_page_id]
                final_state = se.decode_internal(
                    se.read_text(final_row, "동기화 내부 상태"), kind="issue",
                    project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                self.assertEqual(recovered["held"], 0)
                self.assertEqual(se.read_select(final_row, "작업 상태"), "백로그")
                self.assertEqual(final_state["baseline"]["notion_status"], "백로그")
                self.assertEqual(final_state["project_item_id"], returned_id)
                self.assertEqual(final_state["readback"]["returned_item_id"], returned_id)
                self.assertTrue(final_state["readback"]["validated"])
                self.assertEqual(self.project.add_calls, 1)
                self.assertEqual(sum(query == gp.SET_STATUS_MUTATION
                                     for query, _ in self.project.writes), 1)

    def test_new_issue_initialization_restarts_across_notion_creation_and_display_cuts(self):
        for failure_mode in ("created_row_control_checkpoint", "task_patch_before_write",
                             "task_patch_readback_loss"):
            with self.subTest(failure_mode=failure_mode):
                issue = make_issue(created_at="2026-10-02T00:00:00Z")
                self.repo_graph = RepoGraph(issues=[issue])
                self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
                    "number": ISSUE_NUMBER, "state": "open"}])
                self.project = ProjectAPI([make_project_item(option=None)])
                self.notion = FakeNotion([control_page(control_internal())])
                if failure_mode == "created_row_control_checkpoint":
                    self.notion.fail_patch = lambda path, properties: (
                        path == f"/pages/{CONTROL_ID}" and
                        properties.get("Pending create") == se.text_property(""))
                elif failure_mode == "task_patch_before_write":
                    issue_page_id = str(UUID(int=1001))
                    self.notion.fail_patch = lambda path, properties: (
                        path == f"/pages/{issue_page_id}" and "작업 상태" in properties)
                else:
                    issue_page_id = str(UUID(int=1001))

                    def lose_task_patch_readback(path, properties, notion):
                        if path == f"/pages/{issue_page_id}" and "작업 상태" in properties:
                            notion.after_patch = None
                            notion.fail_readback_after_patch.add(issue_page_id)

                    self.notion.after_patch = lose_task_patch_readback

                with self.assertRaises(se.SyncError):
                    self.run_sync(bidirectional_enabled=True)
                self.notion.fail_patch = None
                self.notion.after_patch = None

                issue_page_id = next(page_id for page_id, page in self.notion.pages.items()
                    if page_id != CONTROL_ID and
                    page["properties"].get("종류", {}).get("select", {}).get("name") == "Issue")
                recovered = self.run_sync(bidirectional_enabled=True)
                row = self.notion.pages[issue_page_id]
                state = se.decode_internal(
                    se.read_text(row, "동기화 내부 상태"), kind="issue",
                    project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                self.assertEqual(recovered["held"], 0)
                self.assertEqual(se.read_select(row, "작업 상태"), "백로그")
                self.assertEqual(state["baseline"]["notion_status"], "백로그")
                self.assertTrue(state["migration_complete"])
                self.assertEqual(self.project.add_calls, 0)
                self.assertEqual(self.project.writes,
                                 [(gp.SET_STATUS_MUTATION, unittest.mock.ANY)])

    def test_migrated_open_issue_with_unset_project_status_stays_held_until_pm_sets_general_state(self):
        issue = make_issue()
        state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        state.update({"migration_complete": True, "project_item_id": "PVTI_issue18"})
        self.repo_graph = RepoGraph(issues=[issue])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option=None)])
        previous_success = "2026-09-29T12:00:00Z"
        saved_control_state = json.loads(control_internal())
        saved_control_state["last_success_at"] = previous_success
        control = control_page(se.canonical_json(saved_control_state))
        control["properties"]["동기화 시각"] = {"date": {"start": previous_success}}
        control["properties"]["메모"] = se.text_property("private team note")
        control["properties"]["일정"] = {"date": {"start": "2026-12-24"}}
        control["body"] = "private control-page body"
        self.notion = FakeNotion([control,
            active_issue_page(task="백로그", internal=se.canonical_json(state))])
        self.notion.normalize_minute_dates = True
        hold_now = "2026-10-08T00:00:41.987654Z"

        ordinary = self.run_sync(now=hold_now)
        self.assertEqual(ordinary["held"], 1)
        held = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(held["hold"]["code"], "PROJECT_STATUS_UNSET")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "백로그")
        self.assertEqual(self.project.writes, [])
        summary = self.notion.pages[CONTROL_ID]
        self.assertEqual(title_text(summary), f"Replica 동기화 · 부분 반영 · {hold_now}")
        self.assertEqual(summary["properties"]["GitHub URL"]["url"], se.ACTIONS_WORKFLOW_URL)
        self.assertEqual(se.read_text(summary, "확인 필요"), "보류 1건: 18")
        self.assertEqual(se.read_date(summary, "동기화 시각"), previous_success)
        self.assertEqual(se.read_date(self.notion.pages[ISSUE_PAGE_ID], "동기화 시각"),
                         "2026-10-08T00:00:00.000+00:00")
        summary_state = se.decode_internal(se.read_text(summary, "동기화 내부 상태"),
            kind="control", project_id=gp.EXPECTED_PROJECT_ID)
        self.assertEqual(summary_state["last_result"]["kind"], "partial")
        self.assertEqual(summary_state["last_result"]["at"], hold_now)
        self.assertEqual(summary_state["last_result"]["holds"], [18])
        self.assertEqual(summary_state["last_success_at"], previous_success)
        self.assertEqual(se.read_text(summary, "메모"), "private team note")
        self.assertEqual(summary["properties"]["일정"]["date"]["start"], "2026-12-24")
        self.assertEqual(summary["body"], "private control-page body")
        control = se.decode_internal(
            se.read_text(self.notion.pages[CONTROL_ID], "동기화 내부 상태"),
            kind="control", project_id=gp.EXPECTED_PROJECT_ID)
        self.assertEqual(control["last_success_at"], previous_success)

        still_unset = self.run_sync(resolve_issue_numbers="18", env=pm_env(918401))
        self.assertEqual(still_unset["held"], 1)
        still_held = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(still_held["hold"]["code"], "PROJECT_STATUS_UNSET")
        self.assertEqual(self.project.writes, [])
        control = se.decode_internal(
            se.read_text(self.notion.pages[CONTROL_ID], "동기화 내부 상태"),
            kind="control", project_id=gp.EXPECTED_PROJECT_ID)
        self.assertEqual(control["last_success_at"], previous_success)

        self.run_sync()
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "백로그")
        self.assertEqual(self.project.writes, [])

        chosen = [status_value("준비 중")]
        self.project.items[0]["fieldValues"]["nodes"] = chosen
        self.project.status_values["PVTI_issue18"] = chosen
        resumed = self.run_sync(resolve_issue_numbers="18", env=pm_env(918402))
        self.assertEqual(resumed["held"], 0)
        final_state = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertIsNone(final_state["hold"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(self.project.writes, [])

    def test_fresh_pm_resume_adopts_current_status_not_uncertain_pending_target(self):
        issue = make_issue()
        state = make_pending_status_state(issue, before="진행 중", target="진행 중",
                                          migration_complete=True)
        state["hold"] = se._new_hold("PENDING_RESULT_UNCLEAR", "pending uncertain",
            se._source_fingerprint(issue, [], {"id": "PVTI_issue18",
                "status_option_id": gp.EXPECTED_STATUS_OPTIONS["준비 중"]}))
        self.repo_graph = RepoGraph(issues=[issue])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="준비 중")])
        self.notion = FakeNotion([control_page(control_internal()),
            active_issue_page(task="진행 중", internal=se.canonical_json(state))])

        ordinary = self.run_sync()
        self.assertEqual(ordinary["held"], 1)
        still_pending = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(still_pending["pending"]["target_option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["진행 중"])
        self.assertEqual(self.project.writes, [])

        first_pm_write = len(self.notion.writes)
        result = self.run_sync(resolve_issue_numbers="18", env=pm_env(918403))
        self.assertEqual(result["held"], 0)
        saved_approvals = []
        for _, path, payload, _ in self.notion.writes[first_pm_write:]:
            if path != f"/pages/{ISSUE_PAGE_ID}":
                continue
            prop = payload.get("properties", {}).get("동기화 내부 상태")
            if prop:
                saved_approvals.append(json.loads(rich_text_value(prop)))
        self.assertTrue(saved_approvals)
        self.assertIsNone(saved_approvals[0]["pending"])
        self.assertTrue(saved_approvals[0]["resume"]["display_pending"])
        self.assertEqual(saved_approvals[0]["resume"]["approved_option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["준비 중"])
        final = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertIsNone(final["pending"])
        self.assertIsNone(final["hold"])
        self.assertTrue(final["migration_complete"])
        self.assertEqual(final["review_cycle"], 0)
        self.assertEqual(final["review_return_cycle"], 0)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(self.project.writes, [])

    def test_fresh_pm_resume_with_historical_source_delta_matches_readonly_preview(self):
        issue = make_issue()
        state = make_pending_status_state(issue, before="백로그", target="진행 중",
                                          migration_complete=True)
        state["pending"]["checkpoint"].update({"migration_complete": False,
            "review_cycle": 4, "review_return_cycle": 3})
        state["hold"] = se._new_hold("PENDING_RESULT_UNCLEAR", "pending uncertain",
                                     se._digest("pending-hold"))
        issue["updatedAt"] = "2026-10-05T00:00:00Z"
        self.repo_graph = RepoGraph(issues=[issue])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="준비 중")])
        self.notion = FakeNotion([control_page(control_internal()),
            active_issue_page(task="진행 중", internal=se.canonical_json(state))])
        env = pm_env(918405)
        internal_before_preview = se.read_text(self.notion.pages[ISSUE_PAGE_ID],
                                               "동기화 내부 상태")

        preview = self.run_sync(dry_run=True, resolve_issue_numbers="18", env=env)
        planned = preview["resolution_preview"][0]
        self.assertEqual(planned["result"], "would_resume")
        self.assertEqual(planned["target"], "준비 중")
        self.assertFalse(planned["project_change"])
        self.assertIsNone(planned["hold"])
        self.assertEqual(self.notion.writes, [])
        self.assertEqual(self.project.writes, [])
        self.assertEqual(se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
                         internal_before_preview)

        first_pm_write = len(self.notion.writes)
        actual = self.run_sync(resolve_issue_numbers="18", env=env)
        saved_approvals = []
        for _, path, payload, _ in self.notion.writes[first_pm_write:]:
            if path != f"/pages/{ISSUE_PAGE_ID}":
                continue
            prop = payload.get("properties", {}).get("동기화 내부 상태")
            if prop:
                saved_approvals.append(json.loads(rich_text_value(prop)))
        recovered = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(actual["held"], 0)
        self.assertTrue(saved_approvals)
        self.assertIsNone(saved_approvals[0]["pending"])
        self.assertEqual(saved_approvals[0]["resume"]["approved_option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["준비 중"])
        self.assertIsNone(recovered["pending"])
        self.assertIsNone(recovered["hold"])
        self.assertIsNone(recovered["resume"])
        self.assertTrue(recovered["migration_complete"])
        self.assertEqual(recovered["review_cycle"], 0)
        self.assertEqual(recovered["review_return_cycle"], 0)
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(self.project.writes, [])

    def test_pm_uncertain_pending_keeps_none_and_canonical_closure_or_review_conflicts_held(self):
        for scenario in ("unset", "closure", "review", "contradiction"):
            with self.subTest(scenario=scenario):
                pr = None
                pulls = []
                pages = [control_page(control_internal())]
                rest_rows = [{"id": ISSUE_ID, "node_id": ISSUE_NODE, "number": 18,
                              "state": "open"}]
                if scenario in {"closure", "contradiction"}:
                    duplicate = None if scenario == "closure" else {
                        "id": "I_kwDORepresentative", "url":
                        f"https://github.com/{gp.REPOSITORY}/issues/7"}
                    issue = make_issue(state="CLOSED", reason="COMPLETED", duplicate=duplicate,
                                       closed_at="2026-10-03T00:00:00Z")
                    rest_rows[0]["state"] = "closed"
                    pending_target = "완료" if scenario == "closure" else "백로그"
                elif scenario == "review":
                    pr = make_pr()
                    pr.update({"state": "OPEN", "isDraft": False, "mergedAt": None})
                    issue = make_issue(linked=[pr_reference(pr)])
                    pulls = [pr]
                    rest_rows.append({"id": PR_REST_ID, "node_id": PR_NODE, "number": 19,
                        "state": "open", "pull_request": {"url":
                            "https://api.github.com/repos/Aurelia-aurity/Replica/pulls/19"}})
                    pages.append(notion_page(PR_PAGE_ID,
                        key=se.key_for(gp.REPOSITORY_ID, PR_REST_ID), kind="PR", number=19))
                    pending_target = "검토 중"
                else:
                    issue = make_issue()
                    pending_target = "백로그"
                self.repo_graph = RepoGraph(issues=[issue], pulls=pulls)
                if pr:
                    rest_rows[1]["state"] = "open"
                    self.rest = LegacyREST(rest_rows, pull={"id": PR_DATABASE_ID,
                        "node_id": PR_NODE, "number": 19, "state": "open", "draft": False,
                        "merged": False, "base": {"ref": "main", "repo": {
                            "id": gp.REPOSITORY_ID, "node_id": REPO_NODE}}})
                    facts = {"repository_node_id": REPO_NODE, "pulls": {PR_REST_ID: pr}}
                else:
                    self.rest = LegacyREST(rest_rows)
                    facts = {"repository_node_id": REPO_NODE, "pulls": {}}
                state = make_pending_status_state(
                    issue, before="백로그", target=pending_target,
                    migration_complete=True, facts=facts)
                if scenario == "review":
                    state["pending"]["checkpoint"] = {
                        "migration_complete": True, "review_cycle": 1,
                        "review_return_cycle": 0, "reopen_last_id": None,
                        "review_pr_hash": se._digest([PR_NODE]),
                    }
                if scenario == "contradiction":
                    prior_issue = make_issue()
                    state["pending"]["facts_fingerprint"] = se._source_fingerprint(
                        prior_issue, [], {"id": "PVTI_issue18",
                                          "status_option_id": gp.EXPECTED_STATUS_OPTIONS["백로그"]})
                current_option = None if scenario == "unset" else gp.EXPECTED_STATUS_OPTIONS["준비 중"]
                state["hold"] = se._new_hold("PENDING_RESULT_UNCLEAR", "pending uncertain",
                    se._source_fingerprint(issue, se._linked_pr_facts(issue, facts)[0],
                        {"id": "PVTI_issue18", "status_option_id": current_option}))
                self.project = ProjectAPI([make_project_item(option=None if scenario == "unset"
                    else "준비 중")])
                notion_task = "백로그"
                pages.append(active_issue_page(task=notion_task,
                    internal=se.canonical_json(state)))
                self.notion = FakeNotion(pages)

                env = pm_env(918404)
                preview = self.run_sync(dry_run=True, resolve_issue_numbers="18", env=env)
                preview_plan = preview["resolution_preview"][0]
                expected_hold = {"unset": "PROJECT_STATUS_UNSET", "closure": "PENDING_RESULT_UNCLEAR",
                                 "review": "PENDING_RESULT_UNCLEAR",
                                 "contradiction": "CLOSURE_DUPLICATE_CONFLICT"}[scenario]
                self.assertEqual(preview_plan["result"], "still_held")
                self.assertEqual(preview_plan["hold"]["code"], expected_hold)
                self.assertEqual(self.notion.writes, [])
                self.assertEqual(self.project.writes, [])

                self.run_sync(resolve_issue_numbers="18", env=env)

                result_state = se.decode_internal(
                    se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
                    kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                expected_hold = {"unset": "PROJECT_STATUS_UNSET", "closure": "PENDING_RESULT_UNCLEAR",
                                 "review": "PENDING_RESULT_UNCLEAR",
                                 "contradiction": "CLOSURE_DUPLICATE_CONFLICT"}[scenario]
                self.assertEqual(result_state["hold"]["code"], expected_hold)
                self.assertIsNone(result_state["pending"])
                self.assertEqual(self.project.writes, [])

    def test_existing_notion_issue_is_registered_but_unset_status_waits_for_pm(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI()
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task="진행 중")])

        result = self.run_sync()

        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual(result["held"], 1)
        self.assertNotIn("PVTI_added_1_0", self.project.status_values)
        row = self.notion.pages[ISSUE_PAGE_ID]
        self.assertEqual(se.read_select(row, "작업 상태"), "진행 중")
        state = se.decode_internal(se.read_text(row, "동기화 내부 상태"),
                                   kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
                                   object_id=ISSUE_ID)
        self.assertFalse(state["migration_complete"])
        self.assertEqual(state["hold"]["code"], "MIGRATION_UNSET")
        self.assertEqual(state["project_item_id"], "PVTI_added_1_0")

    def test_dry_run_previews_existing_notion_migration_and_project_add(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI()
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task="진행 중")])

        result = self.run_sync(dry_run=True)

        plan = result["issue_plans"][0]
        self.assertTrue(plan["project_add_required"])
        self.assertEqual(plan["project_before"], None)
        self.assertIsNone(plan["target"])
        self.assertEqual(plan["hold"]["code"], "MIGRATION_UNSET")
        self.assertEqual(self.notion.writes, [])
        self.assertEqual(self.project.writes, [])

    def test_closed_completed_not_planned_and_duplicate_persist_migration_checkpoint(self):
        for reason in ("COMPLETED", "NOT_PLANNED", "DUPLICATE"):
            with self.subTest(reason=reason):
                duplicate = (None if reason != "DUPLICATE" else {
                    "id": "I_kwDORepresentative", "url":
                    f"https://github.com/{gp.REPOSITORY}/issues/7"})
                issue = make_issue(state="CLOSED", reason=reason, duplicate=duplicate,
                                   closed_at="2026-10-03T00:00:00Z")
                self.repo_graph = RepoGraph(issues=[issue])
                self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
                    "number": ISSUE_NUMBER, "state": "closed"}])
                self.project = ProjectAPI([make_project_item(option="백로그")])
                self.notion = FakeNotion([control_page(control_internal()),
                    active_issue_page(task="백로그")])

                self.run_sync()

                state = se.decode_internal(
                    se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
                    kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                self.assertTrue(state["migration_complete"])
                expected = {"COMPLETED": "완료", "NOT_PLANNED": None, "DUPLICATE": None}[reason]
                self.assertEqual(self.project.items[0]["fieldValues"]["nodes"] and
                    self.project.items[0]["fieldValues"]["nodes"][0]["name"] or None, expected)

    def test_automatic_closure_reopen_and_review_display_recovery_never_repeats_write(self):
        for scenario in ("closure", "reopen", "review"):
            for outcome in ("matching", "changed"):
                with self.subTest(scenario=scenario, outcome=outcome):
                    pr = None
                    pages = [control_page(control_internal())]
                    rest_rows = [{"id": ISSUE_ID, "node_id": ISSUE_NODE,
                        "number": ISSUE_NUMBER, "state": "closed" if scenario == "closure" else "open"}]
                    pulls = []
                    if scenario == "closure":
                        issue = make_issue(state="CLOSED", reason="COMPLETED",
                                           closed_at="2026-10-03T00:00:00Z")
                        before, target, notion_status = "백로그", "완료", "백로그"
                        state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
                    elif scenario == "reopen":
                        issue = make_issue(reopens=[{"id": "REOPEN_EVENT_RECOVERY",
                            "createdAt": "2026-10-04T00:00:00Z", "stateReason": None}])
                        before, target, notion_status = "진행 중", "백로그", "진행 중"
                        state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
                    else:
                        pr = make_pr()
                        pr.update({"state": "OPEN", "isDraft": False, "mergedAt": None})
                        issue = make_issue(linked=[pr_reference(pr)])
                        before, target, notion_status = "백로그", "검토 중", "백로그"
                        state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
                        pulls = [pr]
                        rest_rows[0]["state"] = "open"
                        rest_rows.append({"id": PR_REST_ID, "node_id": PR_NODE,
                            "number": 19, "state": "open",
                            "pull_request": {"url":
                                "https://api.github.com/repos/Aurelia-aurity/Replica/pulls/19"}})
                        self.rest = LegacyREST(rest_rows, pull={"id": PR_DATABASE_ID,
                            "node_id": PR_NODE, "number": 19, "state": "open",
                            "draft": False, "merged": False,
                            "base": {"ref": "main", "repo": {"id": gp.REPOSITORY_ID,
                                "node_id": REPO_NODE}}})
                        pages.append(notion_page(PR_PAGE_ID,
                            key=se.key_for(gp.REPOSITORY_ID, PR_REST_ID), kind="PR", number=19))
                    if scenario == "reopen":
                        rest_rows[0]["state"] = "open"
                        state["migration_complete"] = True
                    self.repo_graph = RepoGraph(issues=[issue], pulls=pulls)
                    if scenario != "review":
                        self.rest = LegacyREST(rest_rows)
                    self.project = ProjectAPI([make_project_item(option=before)])
                    if scenario == "reopen" or scenario == "review":
                        state["project_item_id"] = "PVTI_issue18"
                    pages.insert(1, active_issue_page(task=notion_status,
                        internal=se.canonical_json(state)))
                    self.notion = FakeNotion(pages)

                    def fail_issue_display(path, properties):
                        return path == f"/pages/{ISSUE_PAGE_ID}" and "작업 상태" in properties

                    self.notion.fail_patch = fail_issue_display
                    with self.assertRaises(se.SyncError):
                        self.run_sync()
                    self.assertEqual(len(self.project.writes), 1)
                    persisted = se.decode_internal(
                        se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
                        kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                    self.assertIsNotNone(persisted["projection"])
                    self.assertEqual(persisted["projection"]["expected_option_id"],
                                     gp.EXPECTED_STATUS_OPTIONS[target])

                    self.notion.fail_patch = None
                    if outcome == "changed":
                        changed = [status_value("준비 중")]
                        self.project.items[0]["fieldValues"]["nodes"] = changed
                        self.project.status_values["PVTI_issue18"] = changed
                    result = self.run_sync()
                    self.assertEqual(len(self.project.writes), 1)
                    final = se.decode_internal(
                        se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
                        kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                    self.assertIsNone(final["projection"])
                    if outcome == "matching":
                        self.assertIsNone(final["hold"])
                        self.assertEqual(result["held"], 0)
                        self.assertTrue(final["migration_complete"])
                        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID],
                                                        "작업 상태"), target)
                    else:
                        self.assertEqual((final.get("hold") or {}).get("code"),
                                         "PROJECTION_CHECKPOINT_CHANGED", repr(final))
                        self.assertEqual(result["held"], 1)
                        self.run_sync()
                        self.assertEqual(len(self.project.writes), 1)

    def test_cutoff_save_or_readback_failure_stops_before_any_later_write(self):
        for failure in ("patch", "readback"):
            with self.subTest(failure=failure):
                self.repo_graph = RepoGraph()
                self.rest = LegacyREST()
                self.project = ProjectAPI()
                self.notion = FakeNotion([control_page()])
                if failure == "patch":
                    self.notion.fail_patch = lambda path, props: (
                        path == f"/pages/{CONTROL_ID}" and "동기화 내부 상태" in props)
                else:
                    self.notion.fail_readback_after_patch.add(CONTROL_ID)

                with self.assertRaises(se.SyncError):
                    self.run_sync()

                self.assertEqual(len(self.notion.writes), 1)
                self.assertEqual(self.notion.writes[0][1], f"/pages/{CONTROL_ID}")
                self.assertEqual(self.notion.creates, 0)
                self.assertEqual(self.project.writes, [])

    def test_migration_pending_distinguishes_external_notion_change_from_own_marker(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option=None)])
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task="진행 중")])

        def change_task_after_pending(path, props, notion):
            if path != f"/pages/{ISSUE_PAGE_ID}" or "동기화 내부 상태" not in props:
                return
            raw = rich_text_value(props["동기화 내부 상태"])
            pending = (json.loads(raw).get("pending") or {}) if raw else {}
            if pending.get("kind") == "status":
                notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                    "select": {"name": "준비 중"}}
                notion.after_patch = None

        self.notion.after_patch = change_task_after_pending
        self.run_sync()

        row = self.notion.pages[ISSUE_PAGE_ID]
        state = se.decode_internal(se.read_text(row, "동기화 내부 상태"),
                                   kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
                                   object_id=ISSUE_ID)
        self.assertEqual(state["hold"]["code"], "MIGRATION_UNSET")
        self.assertIsNone(state["pending"])
        self.assertEqual(se.read_select(row, "작업 상태"), "진행 중")
        self.assertEqual(self.project.writes, [])

    def test_pending_initial_migration_uses_captured_notion_input_then_holds_external_change(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="진행 중")])
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task="진행 중")])

        def lose_pending_readback(path, properties, notion):
            if path != f"/pages/{ISSUE_PAGE_ID}":
                return
            internal = properties.get("동기화 내부 상태")
            if not internal:
                return
            saved = json.loads(rich_text_value(internal))
            if (saved.get("pending") or {}).get("kind") == "status":
                notion.after_patch = None
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = lose_pending_readback
        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync()
        persisted = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(persisted["pending"]["notion_status"], "진행 중")
        self.assertEqual(self.project.writes, [])

        self.notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
            "select": {"name": "준비 중"}}
        result = self.run_sync()

        recovered = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(result["held"], 1)
        self.assertEqual(recovered["hold"]["code"], "MIGRATION_INPUT_CHANGED")
        self.assertIsNone(recovered["pending"])
        self.assertEqual(self.project.writes, [])

    def test_pm_approved_first_migration_recovers_saved_pending_marker(self):
        issue = make_issue()
        state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        state["hold"] = se._new_hold("MIGRATION_CONFLICT", "initial mismatch",
                                     se._digest("migration-conflict"))
        self.repo_graph = RepoGraph(issues=[issue])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="백로그")])
        self.notion = FakeNotion([control_page(control_internal()),
            active_issue_page(task="진행 중", internal=se.canonical_json(state))])

        def lose_pending_readback(path, properties, notion):
            if path != f"/pages/{ISSUE_PAGE_ID}":
                return
            internal = properties.get("동기화 내부 상태")
            if not internal:
                return
            saved = json.loads(rich_text_value(internal))
            if (saved.get("pending") or {}).get("kind") == "status":
                notion.after_patch = None
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = lose_pending_readback
        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync(resolve_issue_numbers="18", env=pm_env(918499))
        pending_state = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(pending_state["pending"]["kind"], "status")
        self.assertEqual(pending_state["pending"]["notion_status"], "진행 중")
        self.assertEqual(pending_state["resume"]["approved_option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["백로그"])

        result = self.run_sync()

        recovered = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(result["held"], 0)
        self.assertIsNone(recovered["pending"])
        self.assertIsNone(recovered["hold"])
        self.assertTrue(recovered["migration_complete"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "백로그")
        self.assertEqual(self.project.writes, [])

    def test_noop_initial_migration_keeps_existing_project_status(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="진행 중")])
        control = control_page(control_internal())
        control["properties"]["메모"] = se.text_property("private team note")
        control["properties"]["일정"] = {"date": {"start": "2026-12-24"}}
        control["body"] = "private control-page body"
        self.notion = FakeNotion([control, active_issue_page(task=None)])

        self.run_sync()

        self.assertEqual(self.project.writes, [])
        self.assertEqual(self.project.items[0]["fieldValues"]["nodes"][0]["optionId"],
                         gp.EXPECTED_STATUS_OPTIONS["진행 중"])
        row = self.notion.pages[ISSUE_PAGE_ID]
        self.assertEqual(se.read_select(row, "작업 상태"), "진행 중")
        state = se.decode_internal(se.read_text(row, "동기화 내부 상태"),
                                   kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
                                   object_id=ISSUE_ID)
        self.assertTrue(state["migration_complete"])
        summary = self.notion.pages[CONTROL_ID]
        self.assertEqual(title_text(summary), f"Replica 동기화 · 전체 완료 · {NOW}")
        self.assertEqual(summary["properties"]["GitHub URL"]["url"], se.ACTIONS_WORKFLOW_URL)
        self.assertEqual(se.read_text(summary, "확인 필요"), "")
        self.assertEqual(se.read_date(summary, "동기화 시각"), NOW)
        control_state = se.decode_internal(se.read_text(summary, "동기화 내부 상태"),
            kind="control", project_id=gp.EXPECTED_PROJECT_ID)
        self.assertEqual(control_state["last_result"]["kind"], "complete")
        self.assertEqual(control_state["last_result"]["at"], NOW)
        self.assertEqual(control_state["last_success_at"], NOW)
        self.assertEqual(se.read_text(summary, "메모"), "private team note")
        self.assertEqual(summary["properties"]["일정"]["date"]["start"], "2026-12-24")
        self.assertEqual(summary["body"], "private control-page body")

    def test_failed_hold_display_remains_durable_when_values_later_match(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="백로그")])
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task="진행 중")])
        self.notion.fail_patch = lambda path, props: (
            path == f"/pages/{ISSUE_PAGE_ID}" and
            rich_text_value(props.get("확인 필요", {})).startswith("보류:"))

        with self.assertRaises(se.SyncError):
            self.run_sync()
        state = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
                                   kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
                                   object_id=ISSUE_ID)
        self.assertEqual(state["hold"]["code"], "MIGRATION_CONFLICT")

        self.project.status_values["PVTI_issue18"] = [status_value("진행 중")]
        self.project.items[0]["fieldValues"]["nodes"] = [status_value("진행 중")]
        self.run_sync()

        row = self.notion.pages[ISSUE_PAGE_ID]
        persisted = se.decode_internal(se.read_text(row, "동기화 내부 상태"),
                                       kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
                                       object_id=ISSUE_ID)
        self.assertEqual(persisted["hold"]["code"], "MIGRATION_CONFLICT")
        self.assertTrue(se.read_text(row, "확인 필요").startswith("보류:"))
        self.assertEqual(self.project.writes, [])

    def test_failed_pm_resume_display_recovers_projection_without_project_mutation(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="진행 중")])
        state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        state["hold"] = se._new_hold("MIGRATION_CONFLICT", "migration conflict", se._digest("held"))
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task=None, internal=se.canonical_json(state))])

        def fail_completed_display(path, props):
            if path != f"/pages/{ISSUE_PAGE_ID}" or "작업 상태" not in props:
                return False
            next_state = json.loads(rich_text_value(props["동기화 내부 상태"]))
            return next_state["migration_complete"] and not next_state["hold"] and not next_state["resume"]

        self.notion.fail_patch = fail_completed_display
        env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "Just-Simple0",
               "GITHUB_TRIGGERING_ACTOR": "Just-Simple0",
               "GITHUB_RUN_ID": "918274", "GITHUB_RUN_ATTEMPT": "1",
               "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID),
               "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "a" * 40,
               "SYNC_APPROVED_SHA": "a" * 40}
        with self.assertRaises(se.SyncError):
            self.run_sync(resolve_issue_numbers="18", env=env)

        state_after_failure = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(state_after_failure["hold"]["code"], "MIGRATION_CONFLICT")
        self.assertTrue(state_after_failure["resume"]["display_pending"])
        self.assertEqual(self.project.writes, [])

        self.run_sync()

        final_state = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
                                         kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
                                         object_id=ISSUE_ID)
        self.assertIsNone(final_state["hold"])
        self.assertIsNone(final_state["resume"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "진행 중")
        self.assertEqual(self.project.writes, [])

    def test_failed_pm_resume_display_keeps_hold_after_project_option_changes_until_reapproval(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="진행 중")])
        state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        state["hold"] = se._new_hold("MIGRATION_CONFLICT", "migration conflict", se._digest("held"))
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task=None, internal=se.canonical_json(state))])

        def fail_completed_display(path, props):
            if path != f"/pages/{ISSUE_PAGE_ID}" or "작업 상태" not in props:
                return False
            next_state = json.loads(rich_text_value(props["동기화 내부 상태"]))
            return next_state["migration_complete"] and not next_state["hold"] and not next_state["resume"]

        self.notion.fail_patch = fail_completed_display
        env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "Just-Simple0",
               "GITHUB_TRIGGERING_ACTOR": "Just-Simple0",
               "GITHUB_RUN_ID": "918275", "GITHUB_RUN_ATTEMPT": "1",
               "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID),
               "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "a" * 40,
               "SYNC_APPROVED_SHA": "a" * 40}
        with self.assertRaises(se.SyncError):
            self.run_sync(resolve_issue_numbers="18", env=env)

        checkpointed = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID],
            "동기화 내부 상태"), kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
            object_id=ISSUE_ID)
        self.assertEqual(checkpointed["resume"]["approved_option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["진행 중"])
        self.assertEqual(checkpointed["resume"]["expected_option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["진행 중"])
        self.assertIsNotNone(checkpointed["resume"]["expected_fingerprint"])

        self.project.status_values["PVTI_issue18"] = [status_value("준비 중")]
        self.project.items[0]["fieldValues"]["nodes"] = [status_value("준비 중")]
        self.notion.fail_patch = None
        counts = self.run_sync()

        held = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID],
            "동기화 내부 상태"), kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
            object_id=ISSUE_ID)
        self.assertEqual(counts["held"], 1)
        self.assertEqual(held["hold"]["code"], "RESUME_CHECKPOINT_CHANGED")
        self.assertIsNone(held["resume"])
        self.assertTrue(se.read_text(self.notion.pages[ISSUE_PAGE_ID], "확인 필요").startswith("보류:"))
        self.assertIsNone(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"))
        self.assertEqual(self.project.writes, [])

        reapproval_env = {**env, "GITHUB_RUN_ID": "918277"}
        notion_writes_before_preview = len(self.notion.writes)
        preview = self.run_sync(dry_run=True, resolve_issue_numbers="18", env=reapproval_env)
        self.assertEqual(preview["resolution_preview"][0]["result"], "would_resume")
        self.assertEqual(preview["resolution_preview"][0]["project_before"], "준비 중")
        self.assertEqual(len(self.notion.writes), notion_writes_before_preview)
        self.assertEqual(self.project.writes, [])
        self.run_sync(resolve_issue_numbers="18", env=reapproval_env)
        approved = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID],
            "동기화 내부 상태"), kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
            object_id=ISSUE_ID)
        self.assertIsNone(approved["hold"])
        self.assertIsNone(approved["resume"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "준비 중")
        self.assertEqual(self.project.writes, [])

    def test_failed_semantic_pm_resume_display_recovers_after_cosmetic_metadata_change(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="진행 중")])
        state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        state["hold"] = se._new_hold("MIGRATION_CONFLICT", "migration conflict", se._digest("held"))
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task=None, internal=se.canonical_json(state))])

        def fail_completed_display(path, props):
            if path != f"/pages/{ISSUE_PAGE_ID}" or "작업 상태" not in props:
                return False
            next_state = json.loads(rich_text_value(props["동기화 내부 상태"]))
            return next_state["migration_complete"] and not next_state["hold"] and not next_state["resume"]

        self.notion.fail_patch = fail_completed_display
        env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "Just-Simple0",
               "GITHUB_TRIGGERING_ACTOR": "Just-Simple0",
               "GITHUB_RUN_ID": "918278", "GITHUB_RUN_ATTEMPT": "1",
               "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID),
               "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "a" * 40,
               "SYNC_APPROVED_SHA": "a" * 40}
        with self.assertRaises(se.SyncError):
            self.run_sync(resolve_issue_numbers="18", env=env)

        checkpointed = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID],
            "동기화 내부 상태"), kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
            object_id=ISSUE_ID)
        self.assertEqual(checkpointed["resume"]["expected_option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["진행 중"])
        self.repo_graph.issues[0]["updatedAt"] = "2026-10-05T00:00:00Z"
        self.notion.fail_patch = None
        notion_writes_before_preview = len(self.notion.writes)
        project_writes_before_preview = len(self.project.writes)
        preview = self.run_sync(dry_run=True)
        self.assertNotEqual(preview["issue_plans"][0].get("hold", {}).get("code"),
                            "RESUME_CHECKPOINT_CHANGED")
        self.assertEqual(len(self.notion.writes), notion_writes_before_preview)
        self.assertEqual(len(self.project.writes), project_writes_before_preview)
        counts = self.run_sync()

        held = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID],
            "동기화 내부 상태"), kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
            object_id=ISSUE_ID)
        self.assertEqual(counts["held"], 0)
        self.assertIsNone(held["hold"])
        self.assertIsNone(held["resume"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "진행 중")
        self.assertEqual(self.project.writes, [])

    def test_legacy_raw_pm_resume_checkpoint_keeps_updated_at_sensitive_hash(self):
        issue = make_issue()
        item = {"id": "PVTI_issue18",
                "status_option_id": gp.EXPECTED_STATUS_OPTIONS["진행 중"]}
        legacy_resume = {
            "actor_id": gp.EXPECTED_PM_USER_ID,
            "run_id": "legacy-checkpoint",
            "approved_option_id": item["status_option_id"],
            "fingerprint": se._source_fingerprint(issue, [], item),
            "display_pending": True,
            "expected_option_id": item["status_option_id"],
            "expected_fingerprint": se._source_fingerprint(issue, [], item),
        }
        facts = {"repository_node_id": REPO_NODE, "pulls": {}}
        self.assertTrue(se._resume_checkpoint_matches(legacy_resume, issue, facts, item))

        changed_issue = {**issue, "updatedAt": "2026-10-05T00:00:00Z"}

        self.assertFalse(se._resume_checkpoint_matches(legacy_resume, changed_issue, facts, item))

    def test_failed_pm_resume_recovers_when_automatic_reopen_checkpoint_differs_from_approval(self):
        reopened = make_issue(reopens=[{"id": "REOPEN_EVENT_1",
                                       "createdAt": "2026-10-04T00:00:00Z",
                                       "stateReason": None}])
        self.repo_graph = RepoGraph(issues=[reopened])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="진행 중")])
        state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        state["hold"] = se._new_hold("MIGRATION_CONFLICT", "migration conflict", se._digest("held"))
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task=None, internal=se.canonical_json(state))])

        def fail_completed_display(path, props):
            if path != f"/pages/{ISSUE_PAGE_ID}" or "작업 상태" not in props:
                return False
            next_state = json.loads(rich_text_value(props["동기화 내부 상태"]))
            return next_state["migration_complete"] and not next_state["hold"] and not next_state["resume"]

        self.notion.fail_patch = fail_completed_display
        env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "Just-Simple0",
               "GITHUB_TRIGGERING_ACTOR": "Just-Simple0",
               "GITHUB_RUN_ID": "918276", "GITHUB_RUN_ATTEMPT": "1",
               "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID),
               "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "a" * 40,
               "SYNC_APPROVED_SHA": "a" * 40}
        with self.assertRaises(se.SyncError):
            self.run_sync(resolve_issue_numbers="18", env=env)

        checkpointed = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID],
            "동기화 내부 상태"), kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
            object_id=ISSUE_ID)
        self.assertEqual(checkpointed["resume"]["approved_option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["진행 중"])
        self.assertEqual(checkpointed["resume"]["expected_option_id"],
                         gp.EXPECTED_STATUS_OPTIONS["백로그"])
        invalid = copy.deepcopy(checkpointed)
        invalid["resume"]["expected_option_id"] = "PVTSSF_unrecognized"
        with self.assertRaises(se.SyncError):
            se.decode_internal(se.canonical_json(invalid), kind="issue",
                               project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(len(self.project.writes), 1)

        self.notion.fail_patch = None
        self.run_sync()

        recovered = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID],
            "동기화 내부 상태"), kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
            object_id=ISSUE_ID)
        self.assertIsNone(recovered["hold"])
        self.assertIsNone(recovered["resume"])
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "백로그")
        self.assertEqual(len(self.project.writes), 1)

    def test_legacy_resume_without_expected_checkpoint_is_accepted_but_never_replayed(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="진행 중")])
        state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        state["hold"] = se._new_hold("MIGRATION_CONFLICT", "migration conflict", se._digest("held"))
        actual_item = {"id": "PVTI_issue18",
                       "status_option_id": gp.EXPECTED_STATUS_OPTIONS["진행 중"]}
        state["resume"] = {"actor_id": gp.EXPECTED_PM_USER_ID, "run_id": "legacy-run",
            "approved_option_id": gp.EXPECTED_STATUS_OPTIONS["진행 중"],
            "fingerprint": se._source_fingerprint(make_issue(), [], actual_item),
            "display_pending": True}
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task=None, internal=se.canonical_json(state))])

        counts = self.run_sync()

        persisted = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID],
            "동기화 내부 상태"), kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
            object_id=ISSUE_ID)
        self.assertEqual(counts["held"], 1)
        self.assertEqual(persisted["hold"]["code"], "RESUME_CHECKPOINT_CHANGED")
        self.assertIsNone(persisted["resume"])
        self.assertTrue(se.read_text(self.notion.pages[ISSUE_PAGE_ID], "확인 필요").startswith("보류:"))
        self.assertEqual(self.project.writes, [])

    def test_returned_add_id_waits_for_direct_and_list_visibility(self):
        for visibility_case in ("list_only", "both_absent"):
            with self.subTest(visibility_case=visibility_case):
                self._configure_post_cutoff_project_add()
                self.project.add_response_loss = False
                if visibility_case == "list_only":
                    self.project.direct_item_visibility_delay_reads = 1
                else:
                    self.project.direct_item_visibility_delay_reads = 2
                    self.project.add_visibility_delay_snapshots = 2

                first = self.run_sync()

                _, waiting = self._stored_issue_state()
                returned_id = waiting["readback"]["returned_item_id"]
                self.assertEqual(first["held"], 1)
                self.assertEqual(waiting["pending"]["kind"], "add")
                self.assertFalse(waiting["pending"]["confirmed"])
                self.assertFalse(waiting["readback"]["validated"])
                self.assertEqual(waiting["readback"]["attempts"], 0)
                self.assertEqual(waiting["readback"]["last_result"], "not_visible")
                self.assertEqual(self.project.add_calls, 1)
                self.assertEqual([query for query, _ in self.project.writes],
                                 [gp.ADD_ISSUE_MUTATION])
                self.assertEqual(sum(query in {gp.SET_STATUS_MUTATION,
                                               gp.CLEAR_STATUS_MUTATION}
                                     for query, _ in self.project.writes), 0)
                self.assertIn(("direct", returned_id, False),
                              self.project.readback_visibility_events)
                if visibility_case == "list_only":
                    self.assertIn(("list", returned_id, True),
                                  self.project.readback_visibility_events)
                else:
                    self.assertIn(("list", returned_id, False),
                                  self.project.readback_visibility_events)

                confirmed_states = []
                def capture_confirmed_add(path, properties, notion):
                    raw = properties.get("동기화 내부 상태")
                    if not path.startswith("/pages/") or not raw:
                        return
                    saved = json.loads(rich_text_value(raw))
                    pending = saved.get("pending") or {}
                    if pending.get("kind") == "add" and pending.get("confirmed"):
                        confirmed_states.append(saved)

                run_ids = ["98101"] if visibility_case == "list_only" else ["98201", "98202"]
                for index, run_id in enumerate(run_ids):
                    self.notion.after_patch = capture_confirmed_add
                    self.run_sync(env={"GITHUB_RUN_ID": run_id,
                                       "GITHUB_RUN_ATTEMPT": "1"})
                    _, state = self._stored_issue_state()
                    self.assertEqual(self.project.add_calls, 1)
                    self.assertEqual(state["readback"]["returned_item_id"], returned_id)
                    self.assertEqual(state["readback"]["attempts"], index + 1)
                    self.assertEqual(state["readback"]["reservation"]["run_id"], run_id)
                    if visibility_case == "both_absent" and index == 0:
                        self.assertFalse(state["pending"]["confirmed"])
                        self.assertFalse(state["readback"]["validated"])
                        self.assertEqual(state["readback"]["last_result"], "not_visible")
                        self.assertEqual(sum(query in {gp.SET_STATUS_MUTATION,
                                                       gp.CLEAR_STATUS_MUTATION}
                                             for query, _ in self.project.writes), 0)

                self.notion.after_patch = None
                _, settled = self._stored_issue_state()
                self.assertIsNone(settled["pending"])
                self.assertEqual(settled["project_item_id"], returned_id)
                self.assertEqual(settled["readback"]["returned_item_id"], returned_id)
                self.assertTrue(settled["readback"]["validated"])
                self.assertTrue(confirmed_states)
                self.assertTrue(all(state["readback"]["validated"] and
                                    state["pending"]["project_item_id"] == returned_id
                                    for state in confirmed_states))
                visibility_pair = [("direct", returned_id, True),
                                   ("list", returned_id, True)]
                self.assertTrue(any(self.project.readback_visibility_events[index:index + 2] ==
                                    visibility_pair
                                    for index in range(len(self.project.readback_visibility_events) - 1)))
                status_mutations_after_confirmation = sum(
                    query in {gp.SET_STATUS_MUTATION, gp.CLEAR_STATUS_MUTATION}
                    for query, _ in self.project.writes)

                self.run_sync(env={"GITHUB_RUN_ID": "98301",
                                   "GITHUB_RUN_ATTEMPT": "1"})

                _, repeated = self._stored_issue_state()
                self.assertEqual(repeated["project_item_id"], returned_id)
                self.assertEqual(repeated["readback"]["returned_item_id"], returned_id)
                self.assertTrue(repeated["readback"]["validated"])
                self.assertEqual(self.project.add_calls, 1)
                self.assertEqual(sum(query in {gp.SET_STATUS_MUTATION,
                                               gp.CLEAR_STATUS_MUTATION}
                                     for query, _ in self.project.writes),
                                 status_mutations_after_confirmation)

    def test_project_add_readback_retries_complete_absence_and_accepts_blank_initial_status(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI()
        self.project.add_visibility_delay_snapshots = 1
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task="진행 중")])

        first = self.run_sync()

        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual(self.project.post_add_snapshot_calls, 1)
        self.assertEqual(self.project.sleep_calls, [])
        _, waiting = self._stored_issue_state()
        self.assertEqual(waiting["readback"]["attempts"], 0)
        self.assertEqual(waiting["readback"]["last_result"], "not_visible")
        self.assertEqual(first["held"], 1)

        self.repo_graph.issues[0]["updatedAt"] = "2026-10-07T00:00:00Z"
        self.repo_graph.issues[0]["title"] = "Cosmetic title edit"
        self.repo_graph.issues[0]["body"] = "Cosmetic body edit"

        self.run_sync()

        self.assertEqual(self.project.post_add_snapshot_calls, 3)
        self.assertNotIn("PVTI_added_1_0", self.project.status_values)
        _, state = self._stored_issue_state()
        self.assertEqual(state["project_item_id"], "PVTI_added_1_0")
        self.assertFalse(state["migration_complete"])
        self.assertEqual(state["hold"]["code"], "MIGRATION_UNSET")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "진행 중")
        self.assertIsNone(state["pending"])

    def test_project_add_readback_network_failure_preserves_snapshot_and_processes_next_issue(self):
        first = make_issue(created_at="2026-10-03T00:00:00Z")
        second = make_issue(created_at="2026-10-03T00:00:00Z")
        second.update(id="I_kwDOIssue19", databaseId=556, number=19,
                      url=f"https://github.com/{gp.REPOSITORY}/issues/19")
        self.repo_graph = RepoGraph(issues=[first, second])
        self.rest = LegacyREST(issues=[
            {"id": ISSUE_ID, "node_id": ISSUE_NODE, "number": ISSUE_NUMBER, "state": "open"},
            {"id": 556, "node_id": "I_kwDOIssue19", "number": 19, "state": "open"},
        ])
        self.project = ProjectAPI()
        self.project.add_response_loss = False
        self.project.fail_post_add_snapshot_once = True
        def attach_added_issue(project, item, variables):
            if variables.get("content") == "I_kwDOIssue19":
                item["content"].update({"id": "I_kwDOIssue19", "databaseId": 556,
                                         "number": 19})
        self.project.after_add = attach_added_issue
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task="진행 중"),
                                  active_issue_page(task="백로그", page_id=SECOND_ISSUE_PAGE_ID,
                                                    issue_id=556, number=19)])

        counts = self.run_sync()

        self.assertEqual(self.project.add_calls, 2)
        self.assertEqual(self.project.post_add_snapshot_calls, 3)
        first_state = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        second_state = se.decode_internal(
            se.read_text(self.notion.pages[SECOND_ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=556)
        self.assertEqual(counts["held"], 1)
        self.assertEqual(first_state["readback"]["last_result"], "network_error")
        self.assertEqual(first_state["readback"]["attempts"], 0)
        self.assertEqual(first_state["pending"]["kind"], "add")
        self.assertTrue(second_state["migration_complete"])
        self.assertIsNone(second_state["pending"])
        self.assertEqual(second_state["project_item_id"], "PVTI_added_2_0")
        control = se.decode_internal(
            se.read_text(self.notion.pages[CONTROL_ID], "동기화 내부 상태"),
            kind="control", project_id=gp.EXPECTED_PROJECT_ID)
        self.assertEqual(control["last_result"]["kind"], "partial")
        self.assertEqual(control["last_result"]["holds"], [ISSUE_NUMBER])

        self.project.fail_direct_readback_once = True
        self.run_sync()
        _, retried = self._stored_issue_state()
        self.assertEqual(retried["readback"]["attempts"], 1)
        self.assertEqual(retried["readback"]["last_result"], "network_error")
        self.assertEqual(self.project.add_calls, 2)

    def test_confirmed_add_checkpoint_never_rebinds_replacement_item_in_normal_or_pm_run(self):
        for path in ("ordinary_then_pm", "pm_direct"):
            with self.subTest(path=path):
                original_item_id = self._leave_confirmed_add_marker_after_readback_loss()
                replacement = make_project_item(item_id="PVTI_replacement_B", option=None)
                self.project.items = [replacement]

                if path == "ordinary_then_pm":
                    normal = self.run_sync()
                    self.assertEqual(normal["held"], 1)
                    _, held = self._stored_issue_state()
                    self.assertEqual(held["pending"]["project_item_id"], original_item_id)
                    self.assertEqual(held["pending"]["checkpoint"]["item_id"], original_item_id)
                    self.assertIsNone(held["project_item_id"])
                    self.assertEqual(held["hold"]["code"], "PROJECT_ADD_UNCERTAIN")

                    preview = self.run_sync(dry_run=True, resolve_issue_numbers="18",
                                             env=pm_env(918402))
                    self.assertEqual(preview["resolution_preview"][0]["result"], "still_held")

                resumed = self.run_sync(resolve_issue_numbers="18", env=pm_env(918403))
                _, recovered = self._stored_issue_state()
                self.assertEqual(resumed["held"], 1)
                self.assertTrue(recovered["pending"]["confirmed"])
                self.assertEqual(recovered["pending"]["project_item_id"], original_item_id)
                self.assertEqual(recovered["pending"]["checkpoint"]["item_id"], original_item_id)
                self.assertIsNone(recovered["project_item_id"])
                self.assertEqual(recovered["hold"]["code"], "PROJECT_ADD_UNCERTAIN")
                self.assertIsNone(recovered["resume"])
                self.assertEqual([item["id"] for item in self.project.items],
                                 ["PVTI_replacement_B"])
                self.assertEqual(self.project.add_calls, 1)
                self.assertEqual([query for query, _ in self.project.writes],
                                 [gp.ADD_ISSUE_MUTATION])

    def test_confirmed_add_checkpoint_recovers_same_item_without_readding(self):
        original_item_id = self._leave_confirmed_add_marker_after_readback_loss()

        result = self.run_sync(resolve_issue_numbers="18", env=pm_env(918404))

        _, recovered = self._stored_issue_state()
        self.assertEqual(result["held"], 0)
        self.assertIsNone(recovered["pending"])
        self.assertIsNone(recovered["hold"])
        self.assertEqual(recovered["project_item_id"], original_item_id)
        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual([query for query, _ in self.project.writes].count(gp.ADD_ISSUE_MUTATION), 1)

    def test_project_add_readback_scans_second_page_for_match_and_returned_id_conflict(self):
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch):
                self._configure_post_cutoff_project_add()
                unrelated = make_project_item(item_id="PVTI_unrelated_foreign", issue_id=992,
                                              issue_node="I_foreign")
                unrelated["content"]["repository"]["id"] = "R_foreign"

                def place_items_on_two_pages(project, added, _variables):
                    if mismatch:
                        added["content"]["repository"]["id"] = "R_foreign"
                    project.item_pages = [[unrelated], [added]]

                self.project.after_add = place_items_on_two_pages
                if mismatch:
                    self.run_sync()
                    _, pending_state = self._stored_issue_state()
                    self.assertTrue(pending_state["pending"])
                    self.assertFalse(pending_state["pending"]["confirmed"])
                    self.assertEqual(self.project.post_add_snapshot_calls, 1)
                    self.assertEqual(self.project.sleep_calls, [])
                    self.assertEqual(pending_state["readback"]["last_result"], "contradiction")
                    self.assertEqual(pending_state["hold"]["code"], "ADD_READBACK_CONTRADICTION")
                    self.assertEqual([query for query, _ in self.project.writes],
                                     [gp.ADD_ISSUE_MUTATION])
                else:
                    self.run_sync()
                    _, recovered = self._stored_issue_state()
                    self.assertIsNone(recovered["pending"])
                    self.assertEqual(recovered["project_item_id"], "PVTI_added_1_0")
                    self.assertEqual(self.project.post_add_snapshot_calls, 1)
                self.assertEqual(self.project.add_calls, 1)

    def test_project_add_readback_budget_exhaustion_keeps_pending_for_pm_recovery(self):
        self._configure_post_cutoff_project_add()
        self.project.add_visibility_delay_snapshots = 1 + (2 * se.PROJECT_ADD_READBACK_MAX_ATTEMPTS)
        self.run_sync()  # immediate check is separate from the three follow-up runs
        for _ in range(se.PROJECT_ADD_READBACK_MAX_ATTEMPTS):
            result = self.run_sync()

        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual(self.project.post_add_snapshot_calls,
                         1 + (2 * se.PROJECT_ADD_READBACK_MAX_ATTEMPTS))
        self.assertEqual(self.project.sleep_calls, [])
        self.assertEqual([query for query, _ in self.project.writes], [gp.ADD_ISSUE_MUTATION])
        _, pending_state = self._stored_issue_state()
        self.assertEqual(pending_state["pending"]["kind"], "add")
        self.assertFalse(pending_state["pending"]["confirmed"])
        self.assertIsNone(pending_state["pending"]["project_item_id"])
        self.assertEqual(pending_state["pending"]["checkpoint"], {"content_id": ISSUE_NODE})
        self.assertEqual(pending_state["readback"]["attempts"], 3)
        self.assertEqual(pending_state["readback"]["last_result"], "not_visible")
        self.assertEqual(pending_state["hold"]["code"], "ADD_READBACK_EXHAUSTED")
        self.assertGreaterEqual(result["held"], 1)

        ordinary = self.run_sync()
        _, held_state = self._stored_issue_state()
        self.assertGreaterEqual(ordinary["held"], 1)
        self.assertEqual(held_state["hold"]["code"], "ADD_READBACK_EXHAUSTED")
        self.assertEqual(self.project.add_calls, 1)

        preview = self.run_sync(dry_run=True, resolve_issue_numbers="18", env=pm_env(918320))
        self.assertEqual(preview["resolution_preview"][0]["result"], "would_resume")
        self.assertEqual(self.project.add_calls, 1)
        self.project.fail_direct_readback_once = True
        self.run_sync(resolve_issue_numbers="18", env=pm_env(918321))
        _, unverified_resume = self._stored_issue_state()
        self.assertEqual(unverified_resume["pending"]["kind"], "add")
        self.assertFalse(unverified_resume["readback"]["validated"])
        self.assertEqual(unverified_resume["hold"]["code"], "ADD_READBACK_EXHAUSTED")
        self.assertEqual(self.project.add_calls, 1)
        self.run_sync(resolve_issue_numbers="18", env=pm_env(918322))
        _, resumed_state = self._stored_issue_state()
        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual(resumed_state["project_item_id"], "PVTI_added_1_0")
        self.assertIsNone(resumed_state["pending"])
        self.assertIsNone(resumed_state["hold"])

    def test_pm_add_resume_requires_direct_id_and_list_validation(self):
        self._configure_post_cutoff_project_add()
        self.project.add_visibility_delay_snapshots = (
            1 + (2 * se.PROJECT_ADD_READBACK_MAX_ATTEMPTS))
        self.run_sync()
        for _ in range(se.PROJECT_ADD_READBACK_MAX_ATTEMPTS):
            self.run_sync()
        _, exhausted = self._stored_issue_state()
        self.assertEqual(exhausted["hold"]["code"], "ADD_READBACK_EXHAUSTED")
        self.assertFalse(exhausted["readback"]["validated"])
        direct_reads = self.project.direct_item_reads

        self.project.fail_direct_readback_once = True
        self.run_sync(resolve_issue_numbers="18", env=pm_env(928320))

        _, unverified = self._stored_issue_state()
        self.assertEqual(self.project.direct_item_reads, direct_reads + 1)
        self.assertEqual(unverified["pending"]["kind"], "add")
        self.assertFalse(unverified["readback"]["validated"])
        self.assertEqual(unverified["hold"]["code"], "ADD_READBACK_EXHAUSTED")
        self.assertEqual(self.project.add_calls, 1)

        self.run_sync(resolve_issue_numbers="18", env=pm_env(928321))

        _, confirmed = self._stored_issue_state()
        self.assertEqual(confirmed["project_item_id"], "PVTI_added_1_0")
        self.assertIsNone(confirmed["pending"])
        self.assertIsNone(confirmed["hold"])
        self.assertEqual(self.project.add_calls, 1)

    def test_add_readback_waiting_is_partial_even_when_notification_holds_are_empty(self):
        self._configure_post_cutoff_project_add()
        self.project.add_visibility_delay_snapshots = 1
        result = {}
        fresh = {"date": "Thu, 08 Oct 2026 00:00:00 GMT",
                 "cache_control": "no-cache, no-store", "age": "0", "x_cache": None}

        def checked(mark, callback):
            callback()
            return "2026-10-08T00:00:00Z", 3

        with patch.object(observation_clock, "verify_snapshot", side_effect=checked), \
                patch.object(observation_clock, "fetch_fresh_date", return_value=fresh):
            self.run_sync(bidirectional_enabled=True, notification_result=result)

        _, state = self._stored_issue_state()
        self.assertEqual(result["holds"], [])
        self.assertTrue(result["partial"])
        self.assertTrue(result["scan_complete"])
        self.assertEqual(state["pending"]["kind"], "add")
        self.assertFalse(state["readback"]["validated"])
        control = next(page for page in self.notion.pages.values()
                       if se.read_text(page, "동기화 키") == se.CONTROL_KEY)
        last_result = json.loads(rich_text_value(
            control["properties"]["동기화 내부 상태"]))["last_result"]
        self.assertEqual(last_result["kind"], "partial")

    def test_main_writes_add_readback_waiting_as_partial_report_with_no_holds(self):
        env = {"NOTION_SYNC_ENABLED": "true", "GITHUB_EVENT_NAME": "schedule",
               "GITHUB_TOKEN": "test", "PROJECT_TOKEN": "test", "NOTION_TOKEN": "test"}

        def partial_sync(*_args, notification_result=None, **_kwargs):
            notification_result.update(scan_complete=True, holds=[], partial=True,
                                       observed_at="2026-10-08T00:00:00Z",
                                       observation_uncertainty_seconds=3)
            return {"failed": 0}

        with patch.dict(se.os.environ, env), \
                patch.object(se, "_load_config", return_value=config()), \
                patch.object(gp, "GraphQLClient", return_value=object()), \
                patch.object(gp, "RESTClient", return_value=object()), \
                patch.object(se, "NotionClient", return_value=object()), \
                patch.object(se, "sync", side_effect=partial_sync), \
                patch.object(nr, "write") as write_report:
            self.assertEqual(se.main(["--notification-report", "report.json"]), 0)

        self.assertEqual(write_report.call_args.kwargs["kind"], "partial")
        self.assertEqual(write_report.call_args.kwargs["holds"], [])
        self.assertTrue(write_report.call_args.kwargs["scan_complete"])

    def test_project_add_returned_item_mismatches_stop_without_readback_retry(self):
        for scenario in ("foreign_repository", "another_issue", "pull_request_content",
                         "archived_returned_id", "alternate_active_target_id",
                         "active_archive_conflict"):
            with self.subTest(scenario=scenario):
                self._configure_post_cutoff_project_add()
                if scenario == "foreign_repository":
                    def transform(item):
                        item["content"]["repository"]["id"] = "R_foreign"
                        return item
                    self.project.add_item_transform = transform
                elif scenario == "another_issue":
                    other = make_issue()
                    other.update({"id": "I_kwDOIssue19", "databaseId": 556, "number": 19,
                                  "url": f"https://github.com/{gp.REPOSITORY}/issues/19"})
                    self.repo_graph = RepoGraph(issues=[make_issue(created_at="2026-10-03T00:00:00Z"),
                                                        other])
                    self.rest = LegacyREST(issues=[
                        {"id": ISSUE_ID, "node_id": ISSUE_NODE, "number": ISSUE_NUMBER, "state": "open"},
                        {"id": 556, "node_id": other["id"], "number": 19, "state": "open"},
                    ])
                    def transform(item):
                        item["content"].update({"id": other["id"], "databaseId": 556,
                                                "number": 19})
                        return item
                    self.project.add_item_transform = transform
                elif scenario == "pull_request_content":
                    def transform(item):
                        item["content"].update({"__typename": "PullRequest", "id": "PR_wrong",
                                                "databaseId": 4782243920, "number": 19})
                        return item
                    self.project.add_item_transform = transform
                elif scenario == "archived_returned_id":
                    self.project.add_item_transform = lambda item: {**item, "isArchived": True}
                elif scenario == "alternate_active_target_id":
                    def transform(item):
                        item["id"] = "PVTI_alternate_target"
                        return item
                    self.project.add_item_transform = transform
                    self.project.add_result_item_id_override = "PVTI_returned_not_in_snapshot"
                else:
                    def add_archived_twin(project, item, _variables):
                        archived = copy.deepcopy(item)
                        archived["id"] = "PVTI_archived_race"
                        archived["isArchived"] = True
                        project.items.append(archived)
                    self.project.after_add = add_archived_twin

                self.run_sync()

                self.assertEqual(self.project.add_calls, 1)
                expected_snapshot_calls = {"another_issue": 2, "pull_request_content": 0}
                self.assertEqual(self.project.post_add_snapshot_calls,
                                 expected_snapshot_calls.get(scenario, 1))
                self.assertEqual(self.project.sleep_calls, [])
                self.assertEqual([query for query, _ in self.project.writes], [gp.ADD_ISSUE_MUTATION])
                _, state = self._stored_issue_state()
                self.assertEqual(state["pending"]["kind"], "add")
                self.assertFalse(state["pending"]["confirmed"])
                self.assertIsNone(state["pending"]["project_item_id"])
                self.assertEqual(state["readback"]["last_result"], "contradiction")
                self.assertEqual(state["hold"]["code"], "ADD_READBACK_CONTRADICTION")

    def test_project_add_readback_partial_pagination_fails_without_retry(self):
        self._configure_post_cutoff_project_add()

        def return_incomplete_items(project, _item, _variables):
            project.next_project_items_override = {
                "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None}, "totalCount": 1}

        self.project.after_add = return_incomplete_items
        self.run_sync()

        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual(self.project.post_add_snapshot_calls, 1)
        self.assertEqual(self.project.sleep_calls, [])
        self.assertEqual([query for query, _ in self.project.writes], [gp.ADD_ISSUE_MUTATION])
        _, state = self._stored_issue_state()
        self.assertEqual(state["pending"]["kind"], "add")
        self.assertFalse(state["pending"]["confirmed"])
        self.assertEqual(state["pending"]["checkpoint"], {"content_id": ISSUE_NODE})
        self.assertEqual(state["readback"]["last_result"], "contradiction")
        self.assertEqual(state["hold"]["code"], "ADD_READBACK_CONTRADICTION")

    def test_malformed_project_add_response_keeps_pending_and_never_reads_or_readds(self):
        for response in ({}, {"addProjectV2ItemById": None},
                         {"addProjectV2ItemById": {"item": None}},
                         {"addProjectV2ItemById": {"item": {"id": ""}}},
                         {"addProjectV2ItemById": {"item": {"id": 17}}}):
            with self.subTest(response=response):
                self._configure_post_cutoff_project_add()
                self.project.add_response_override = response

                with self.assertRaisesRegex(se.SyncError, "Project 추가 결과 불명"):
                    self.run_sync()

                self.assertEqual(self.project.add_calls, 1)
                self.assertEqual(self.project.post_add_snapshot_calls, 0)
                self.assertEqual([query for query, _ in self.project.writes], [gp.ADD_ISSUE_MUTATION])
                _, state = self._stored_issue_state()
                self.assertEqual(state["pending"]["kind"], "add")
                self.assertFalse(state["pending"]["confirmed"])
                self.assertEqual(state["pending"]["checkpoint"], {"content_id": ISSUE_NODE})

    def test_lost_project_add_response_never_retries_for_zero_one_or_duplicates(self):
        for observed in (0, 1, 2):
            with self.subTest(observed=observed):
                self.repo_graph = RepoGraph(issues=[make_issue(created_at="2026-10-03T00:00:00Z")])
                self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
                    "number": ISSUE_NUMBER, "state": "open"}])
                self.project = ProjectAPI()
                self.project.add_response_loss = True
                self.project.add_response_loss_count = observed
                self.notion = FakeNotion([control_page(control_internal())])

                with self.assertRaises(se.SyncError):
                    self.run_sync()
                self.assertEqual(self.project.add_calls, 1)
                key = se.key_for(gp.REPOSITORY_ID, ISSUE_ID)
                row = next(page for page in self.notion.pages.values()
                           if se.read_text(page, "동기화 키") == key)
                pending = se.decode_internal(se.read_text(row, "동기화 내부 상태"),
                    kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                self.assertEqual(pending["pending"]["kind"], "add")
                self.assertIsNone(pending["hold"])

                if observed == 2:
                    with self.assertRaises(gp.SyncError):
                        self.run_sync()
                    self.assertEqual(self.project.add_calls, 1)
                    continue

                # A later ordinary pass records a durable hold; it never adopts one
                # item or repeats the add mutation by itself.
                self.run_sync()
                row = next(page for page in self.notion.pages.values()
                           if se.read_text(page, "동기화 키") == key)
                held = se.decode_internal(se.read_text(row, "동기화 내부 상태"),
                    kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                self.assertEqual(held["hold"]["code"], "PROJECT_ADD_UNCERTAIN")
                self.assertEqual(self.project.add_calls, 1)

                env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "Just-Simple0",
                       "GITHUB_TRIGGERING_ACTOR": "Just-Simple0",
                       "GITHUB_RUN_ID": f"91831{observed}",
                       "GITHUB_RUN_ATTEMPT": "1",
                       "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID),
               "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "a" * 40,
               "SYNC_APPROVED_SHA": "a" * 40}
                result = self.run_sync(resolve_issue_numbers="18", env=env)
                self.assertEqual(self.project.add_calls, 1)
                row = next(page for page in self.notion.pages.values()
                           if se.read_text(page, "동기화 키") == key)
                if observed == 0:
                    self.assertEqual(result["held"], 1)
                else:
                    resolved = se.decode_internal(se.read_text(row, "동기화 내부 상태"),
                        kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                    self.assertIsNone(resolved["hold"])
                    self.assertEqual(resolved["project_item_id"], "PVTI_added_1_0")
                self.assertEqual(len(self.project.items), observed)

    def test_issue_specific_prewrite_source_and_project_races_hold_a_and_process_b(self):
        for race in ("source", "project"):
            with self.subTest(race=race):
                first = make_issue(state="CLOSED", reason="COMPLETED",
                                   closed_at="2026-10-03T00:00:00Z")
                second = copy.deepcopy(first)
                second.update({"id": "I_kwDOIssue19", "databaseId": 556, "number": 19,
                               "url": f"https://github.com/{gp.REPOSITORY}/issues/19"})
                self.repo_graph = RepoGraph(issues=[first, second])
                self.rest = LegacyREST(issues=[
                    {"id": ISSUE_ID, "node_id": ISSUE_NODE, "number": ISSUE_NUMBER, "state": "closed"},
                    {"id": 556, "node_id": second["id"], "number": 19, "state": "closed"},
                ])
                self.project = ProjectAPI([
                    make_project_item(item_id="PVTI_issue18", option="백로그"),
                    make_project_item(item_id="PVTI_issue19", option="백로그", issue_id=556,
                                      issue_node=second["id"], number=19),
                ])
                self.notion = FakeNotion([control_page(control_internal()),
                    active_issue_page(task="백로그"),
                    active_issue_page(page_id=SECOND_ISSUE_PAGE_ID, issue_id=556,
                                      number=19, task="백로그")])
                changed = {"done": False}

                def race_after_pending(path, properties, _notion):
                    if changed["done"] or path != f"/pages/{ISSUE_PAGE_ID}":
                        return
                    internal = properties.get("동기화 내부 상태")
                    if not internal:
                        return
                    state = json.loads(rich_text_value(internal))
                    if not state.get("pending") or state["pending"]["kind"] != "status":
                        return
                    changed["done"] = True
                    if race == "source":
                        self.repo_graph.issues[0]["updatedAt"] = "2026-10-07T00:00:00Z"
                    else:
                        self.project.items[0]["fieldValues"]["nodes"] = [status_value("준비 중")]

                self.notion.after_patch = race_after_pending
                counts = self.run_sync()

                self.assertTrue(changed["done"])
                self.assertEqual(counts["held"], 1 if race == "project" else 0)
                self.assertEqual(self.project.add_calls, 0)
                written_items = [variables.get("item") for _, variables in self.project.writes]
                self.assertEqual(written_items, ["PVTI_issue19"] if race == "project"
                                 else ["PVTI_issue18", "PVTI_issue19"])
                held = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID],
                    "동기화 내부 상태"), kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
                    object_id=ISSUE_ID)
                if race == "project":
                    self.assertEqual(held["hold"]["code"], "SOURCE_CHANGED_BEFORE_WRITE")
                    self.assertIsNone(held["pending"])
                    self.assertTrue(se.read_text(self.notion.pages[ISSUE_PAGE_ID],
                        "확인 필요").startswith("보류:"))
                else:
                    self.assertIsNone(held["hold"])
                    self.assertIsNone(held["pending"])
                    self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID],
                                                    "작업 상태"), "완료")
                self.assertEqual(se.read_select(self.notion.pages[SECOND_ISSUE_PAGE_ID], "작업 상태"),
                                 "완료")
                control_state = se.decode_internal(
                    se.read_text(self.notion.pages[CONTROL_ID], "동기화 내부 상태"),
                    kind="control", project_id=gp.EXPECTED_PROJECT_ID)
                if race == "project":
                    self.assertIsNone(control_state["last_success_at"])
                else:
                    self.assertEqual(control_state["last_success_at"], NOW)

    def test_historical_fingerprint_delta_can_resume_but_same_run_detail_race_holds_a_and_processes_b(self):
        first = make_issue()
        pending = make_pending_status_state(first, before="진행 중", target="진행 중",
                                            migration_complete=True)
        pending["hold"] = se._new_hold("PENDING_RESULT_UNCLEAR", "pending uncertain",
            se._source_fingerprint(first, [], {"id": "PVTI_issue18",
                "status_option_id": gp.EXPECTED_STATUS_OPTIONS["준비 중"]}))
        second = make_issue(state="CLOSED", reason="COMPLETED",
                            closed_at="2026-10-03T00:00:00Z")
        second.update({"id": "I_kwDOIssue19", "databaseId": 556, "number": 19,
                       "url": f"https://github.com/{gp.REPOSITORY}/issues/19"})
        self.repo_graph = RepoGraph(issues=[first, second])
        original_request = self.repo_graph.request

        def latest_issue_race(query, variables=None, *, mutation=False):
            data, server_time = original_request(query, variables, mutation=mutation)
            if ("node(id:$id)" in query or "node(id: $id)" in query) and \
                    (variables or {}).get("id") == ISSUE_NODE and data.get("node"):
                data["node"]["updatedAt"] = "2026-10-06T00:00:00Z"
            return data, server_time

        self.repo_graph.request = latest_issue_race
        self.rest = LegacyREST(issues=[
            {"id": ISSUE_ID, "node_id": ISSUE_NODE, "number": 18, "state": "open"},
            {"id": 556, "node_id": second["id"], "number": 19, "state": "closed"},
        ])
        self.project = ProjectAPI([
            make_project_item(option="준비 중"),
            make_project_item(item_id="PVTI_issue19", option="백로그", issue_id=556,
                              issue_node=second["id"], number=19),
        ])
        self.notion = FakeNotion([control_page(control_internal()),
            active_issue_page(task="백로그", internal=se.canonical_json(pending)),
            active_issue_page(page_id=SECOND_ISSUE_PAGE_ID, issue_id=556, number=19,
                              task="백로그")])

        result = self.run_sync(resolve_issue_numbers="18", env=pm_env(918410))

        held = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID],
            "동기화 내부 상태"), kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
            object_id=ISSUE_ID)
        self.assertEqual(result["held"], 1)
        self.assertEqual(held["hold"]["code"], "SOURCE_CHANGED_BEFORE_WRITE")
        self.assertIsNone(held["pending"])
        self.assertEqual([variables.get("item") for _, variables in self.project.writes],
                         ["PVTI_issue19"])
        self.assertEqual(se.read_select(self.notion.pages[SECOND_ISSUE_PAGE_ID], "작업 상태"),
                         "완료")

    def test_pending_pr_and_archive_changes_hold_a_but_process_healthy_b(self):
        for race in ("draft", "link_removed", "archived"):
            with self.subTest(race=race):
                pr = make_pr()
                linked = pr_reference(pr)
                first = make_issue(state="CLOSED", reason="COMPLETED",
                    closed_at="2026-10-03T00:00:00Z", linked=[linked])
                second = make_issue(state="CLOSED", reason="COMPLETED",
                    closed_at="2026-10-03T00:00:00Z")
                second.update({"id": "I_kwDOIssue20", "databaseId": 557, "number": 20,
                               "url": f"https://github.com/{gp.REPOSITORY}/issues/20"})
                self.repo_graph = RepoGraph(issues=[first, second], pulls=[pr])
                self.rest = LegacyREST(issues=[
                    {"id": ISSUE_ID, "node_id": ISSUE_NODE, "number": 18, "state": "closed"},
                    {"id": 557, "node_id": second["id"], "number": 20, "state": "closed"},
                    {"id": PR_REST_ID, "node_id": PR_NODE, "number": 19, "state": "closed",
                     "pull_request": {"url": "https://api.github.com/repos/Aurelia-aurity/Replica/pulls/19"}},
                ], pull={"id": PR_DATABASE_ID, "node_id": PR_NODE, "number": 19,
                    "state": "closed", "draft": False, "merged": True,
                    "base": {"ref": "main", "repo": {"id": gp.REPOSITORY_ID,
                        "node_id": REPO_NODE}}})
                self.project = ProjectAPI([
                    make_project_item(item_id="PVTI_issue18", option="백로그"),
                    make_project_item(item_id="PVTI_issue20", option="백로그", issue_id=557,
                                      issue_node=second["id"], number=20),
                ])
                pr_page = notion_page(PR_PAGE_ID,
                    key=se.key_for(gp.REPOSITORY_ID, PR_REST_ID), kind="PR", number=19)
                self.notion = FakeNotion([control_page(control_internal()),
                    active_issue_page(task="백로그"),
                    active_issue_page(page_id=SECOND_ISSUE_PAGE_ID, issue_id=557,
                                      number=20, task="백로그"), pr_page])
                changed = {"value": False}

                def change_after_pending(path, properties, _notion):
                    if changed["value"] or path != f"/pages/{ISSUE_PAGE_ID}":
                        return
                    internal = properties.get("동기화 내부 상태")
                    if not internal:
                        return
                    saved = json.loads(rich_text_value(internal))
                    if not saved.get("pending") or saved["pending"]["kind"] != "status":
                        return
                    changed["value"] = True
                    if race == "draft":
                        ref = self.repo_graph.issues[0][
                            "closedByPullRequestsReferences"]["nodes"][0]
                        ref["state"] = "OPEN"
                        ref["isDraft"] = True
                    elif race == "link_removed":
                        issue = self.repo_graph.issues[0]
                        issue["linked_prs"] = []
                        issue["closedByPullRequestsReferences"] = conn()
                    else:
                        self.project.items[0]["isArchived"] = True

                self.notion.after_patch = change_after_pending
                counts = self.run_sync()

                self.assertTrue(changed["value"])
                self.assertEqual(counts["held"], 1)
                self.assertEqual([vars.get("item") for _, vars in self.project.writes],
                                 ["PVTI_issue20"])
                held = se.decode_internal(
                    se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
                    kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                expected = "PROJECT_ITEM_ARCHIVED" if race == "archived" else "SOURCE_CHANGED_BEFORE_WRITE"
                self.assertEqual(held["hold"]["code"], expected)
                self.assertIsNone(held["pending"])
                self.assertEqual(se.read_select(self.notion.pages[SECOND_ISSUE_PAGE_ID],
                                                "작업 상태"), "완료")
                control_state = se.decode_internal(
                    se.read_text(self.notion.pages[CONTROL_ID], "동기화 내부 상태"),
                    kind="control", project_id=gp.EXPECTED_PROJECT_ID)
                self.assertIsNone(control_state["last_success_at"])

    def test_reopen_pending_checkpoint_order_and_cutoff_are_preflighted_globally(self):
        post_cutoff = {"id": "R_POST", "createdAt": "2026-10-04T00:00:00Z"}
        prior = {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"}
        cases = ("checkpoint_rewind", "pending_pre_cutoff", "pending_cutoff_boundary",
                 "baseline_after_cutoff", "checkpoint_pre_cutoff_disguise",
                 "closed_forward_same_time_other_id")
        for case in cases:
            with self.subTest(case=case):
                issue_a = make_issue(state="CLOSED", reason="COMPLETED",
                                     closed_at="2026-10-03T00:00:00Z")
                issue_b = make_issue(reopens=[prior, post_cutoff])
                issue_b.update({"id": "I_kwDOIssue19", "databaseId": 556, "number": 19,
                    "url": f"https://github.com/{gp.REPOSITORY}/issues/19"})
                events = [prior, post_cutoff]
                baseline_id, last_id = "R_PRE", "R_POST"
                pending_event = None
                checkpoint_last = "R_PRE"
                if case == "closed_forward_same_time_other_id":
                    other = {"id": "R_POST_OTHER", "createdAt": "2026-10-04T00:00:00Z"}
                    events = [prior, post_cutoff, other]
                    issue_b.update({"state": "CLOSED", "stateReason": "COMPLETED",
                                    "closedAt": "2026-10-05T00:00:00Z", "reopens": events,
                                    "timelineItems": conn(events)})
                    baseline_id, last_id = "R_PRE", "R_PRE"
                    checkpoint_last = "R_POST"
                elif case == "pending_pre_cutoff":
                    earlier = {"id": "R_MID_PRE", "createdAt": "2026-09-29T00:00:00Z"}
                    events = [prior, earlier]
                    issue_b["reopens"] = events
                    issue_b["timelineItems"] = conn(events)
                    last_id = "R_PRE"
                    pending_event = "R_MID_PRE"
                    checkpoint_last = pending_event
                elif case == "checkpoint_pre_cutoff_disguise":
                    earlier = {"id": "R_MID_PRE", "createdAt": "2026-09-29T00:00:00Z"}
                    events = [prior, earlier]
                    issue_b["reopens"] = events
                    issue_b["timelineItems"] = conn(events)
                    baseline_id, last_id = None, None
                    checkpoint_last = "R_PRE"
                elif case == "pending_cutoff_boundary":
                    boundary = {"id": "R_BOUNDARY", "createdAt": CUTOFF}
                    events = [prior, boundary]
                    issue_b["reopens"] = events
                    issue_b["timelineItems"] = conn(events)
                    last_id = "R_PRE"
                    pending_event = "R_BOUNDARY"
                    checkpoint_last = pending_event
                elif case == "baseline_after_cutoff":
                    baseline_id, last_id = "R_POST", "R_POST"
                    checkpoint_last = "R_POST"

                pending_state = make_pending_status_state(
                    issue_b, item_id="PVTI_issue19", before="준비 중", target="백로그",
                    migration_complete=True)
                pending_state["reopen_baseline_id"] = baseline_id
                pending_state["reopen_last_id"] = last_id
                pending_state["pending"]["event_id"] = pending_event
                pending_state["pending"]["checkpoint"]["reopen_last_id"] = checkpoint_last
                pending_state["pending"]["facts_fingerprint"] = se._source_fingerprint(
                    issue_b, [], {"id": "PVTI_issue19",
                                 "status_option_id": gp.EXPECTED_STATUS_OPTIONS["준비 중"]})

                self.repo_graph = RepoGraph(issues=[issue_a, issue_b])
                self.rest = LegacyREST(issues=[
                    {"id": ISSUE_ID, "node_id": ISSUE_NODE, "number": ISSUE_NUMBER,
                     "state": "closed"},
                    {"id": 556, "node_id": issue_b["id"], "number": 19,
                     "state": issue_b["state"].lower()},
                ])
                self.project = ProjectAPI([
                    make_project_item(option="백로그"),
                    make_project_item(item_id="PVTI_issue19", issue_id=556,
                                      issue_node=issue_b["id"], number=19,
                                      option="백로그"),
                ])
                self.notion = FakeNotion([
                    control_page(control_internal()),
                    active_issue_page(task="백로그"),
                    active_issue_page(page_id=SECOND_ISSUE_PAGE_ID, issue_id=556,
                        number=19, task="준비 중", internal=se.canonical_json(pending_state)),
                ])

                with self.assertRaises((se.SyncError, gp.SyncError)):
                    self.run_sync()

                self.assertEqual(self.notion.writes, [])
                self.assertEqual(self.project.writes, [])
                self.assertEqual(self.repo_graph.mutations, 0)

    def test_equal_reopen_checkpoint_recovers_partial_status_commit(self):
        events = [
            {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"},
            {"id": "R_POST", "createdAt": "2026-10-04T00:00:00Z"},
        ]
        issue = make_issue(reopens=events)
        state = make_pending_status_state(issue, before="준비 중", target="백로그",
                                          migration_complete=True)
        state["reopen_baseline_id"] = "R_PRE"
        state["reopen_last_id"] = "R_POST"
        state["review_pr_hash"] = se._digest([])
        state["pending"]["event_id"] = "R_POST"
        state["pending"]["confirmed"] = True
        state["pending"]["checkpoint"]["reopen_last_id"] = "R_POST"
        state["pending"]["facts_fingerprint"] = se._source_fingerprint(
            issue, [], {"id": "PVTI_issue18",
                        "status_option_id": gp.EXPECTED_STATUS_OPTIONS["준비 중"]})
        self.repo_graph = RepoGraph(issues=[issue])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="백로그")])
        self.notion = FakeNotion([control_page(control_internal()),
            active_issue_page(task="준비 중", internal=se.canonical_json(state))])

        counts = self.run_sync()

        recovered = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(counts["held"], 0)
        self.assertIsNone(recovered["pending"])
        self.assertEqual(recovered["reopen_last_id"], "R_POST")
        self.assertEqual(self.project.writes, [])

    def test_confirmed_reopen_checkpoint_equality_recovers_after_committed_readback_loss(self):
        events = [
            {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"},
            {"id": "R_POST", "createdAt": "2026-10-04T00:00:00Z"},
        ]
        issue = make_issue(reopens=events)
        state = make_pending_status_state(issue, before="준비 중", target="백로그",
                                          migration_complete=True)
        state["reopen_baseline_id"] = "R_PRE"
        state["reopen_last_id"] = "R_PRE"
        state["pending"]["event_id"] = "R_POST"
        state["pending"]["checkpoint"]["reopen_last_id"] = "R_POST"
        state["pending"]["facts_fingerprint"] = se._source_fingerprint(
            issue, [], {"id": "PVTI_issue18",
                        "status_option_id": gp.EXPECTED_STATUS_OPTIONS["준비 중"]})
        self.repo_graph = RepoGraph(issues=[issue])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="백로그")])
        self.notion = FakeNotion([control_page(control_internal()),
            active_issue_page(task="준비 중", internal=se.canonical_json(state))])
        self.notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        with self.assertRaises(se.SyncError):
            self.run_sync()

        committed = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertTrue(committed["pending"]["confirmed"])
        self.assertEqual(committed["reopen_last_id"], "R_POST")
        self.assertTrue(all(committed[field] == committed["pending"]["checkpoint"][field]
                            for field in se.STATUS_CHECKPOINT_KEYS))
        self.assertEqual(self.project.writes, [])

        counts = self.run_sync()

        recovered = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(counts["held"], 0)
        self.assertIsNone(recovered["pending"])
        self.assertEqual(recovered["reopen_last_id"], "R_POST")
        self.assertEqual(self.project.writes, [])

    def test_project_status_confirmed_marker_readback_loss_recovers_unpromoted_checkpoint_once(self):
        events = [
            {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"},
            {"id": "R_POST", "createdAt": "2026-10-04T00:00:00Z"},
        ]
        issue = make_issue(reopens=events)
        state = se.new_issue_state(ISSUE_ID, gp.EXPECTED_PROJECT_ID)
        state.update({"migration_complete": True, "project_item_id": "PVTI_issue18",
                      "reopen_baseline_id": "R_PRE", "reopen_last_id": "R_PRE"})
        self.repo_graph = RepoGraph(issues=[issue])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI([make_project_item(option="진행 중")])
        self.notion = FakeNotion([control_page(control_internal()),
            active_issue_page(task="진행 중", internal=se.canonical_json(state))])

        def fail_confirmed_marker_readback(path, properties, notion):
            if path != f"/pages/{ISSUE_PAGE_ID}":
                return
            prop = properties.get("동기화 내부 상태")
            if not prop:
                return
            saved = json.loads(rich_text_value(prop))
            pending = saved.get("pending")
            if (pending and pending["kind"] == "status" and pending["confirmed"] and
                    len(self.project.writes) == 1):
                notion.after_patch = None
                notion.fail_readback_after_patch.add(ISSUE_PAGE_ID)

        self.notion.after_patch = fail_confirmed_marker_readback

        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync()

        project_writes_after_first_run = len(self.project.writes)
        self.assertEqual(project_writes_after_first_run, 1)
        committed = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertTrue(committed["pending"]["confirmed"])
        self.assertEqual(committed["reopen_last_id"], "R_PRE")
        self.assertEqual(committed["pending"]["event_id"], "R_POST")
        self.assertNotEqual(committed["reopen_last_id"],
                            committed["pending"]["checkpoint"]["reopen_last_id"])

        counts = self.run_sync()

        recovered = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(counts["held"], 0)
        self.assertIsNone(recovered["pending"])
        self.assertEqual(recovered["reopen_last_id"], "R_POST")
        self.assertEqual(len(self.project.writes), project_writes_after_first_run)

    def test_closed_initial_baseline_and_forward_checkpoint_recover_each_confirmed_save(self):
        prior = {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"}
        issue_a = make_issue()
        issue_b = make_issue(state="CLOSED", reason="COMPLETED", reopens=[prior],
                             closed_at="2026-09-30T00:00:00Z")
        issue_b.update({"id": "I_kwDOIssue19", "databaseId": 556, "number": 19,
                        "url": f"https://github.com/{gp.REPOSITORY}/issues/19"})
        self.repo_graph = RepoGraph(issues=[issue_a, issue_b])
        self.rest = LegacyREST(issues=[
            {"id": ISSUE_ID, "node_id": ISSUE_NODE, "number": ISSUE_NUMBER, "state": "open"},
            {"id": 556, "node_id": issue_b["id"], "number": 19, "state": "closed"},
        ])
        self.project = ProjectAPI([
            make_project_item(option="백로그"),
            make_project_item(item_id="PVTI_issue19", issue_id=556,
                              issue_node=issue_b["id"], number=19, option="백로그"),
        ])
        self.notion = FakeNotion([
            control_page(control_internal()),
            active_issue_page(task="백로그"),
            active_issue_page(page_id=SECOND_ISSUE_PAGE_ID, issue_id=556, number=19,
                              task="백로그"),
        ])
        failure_stages = {"initial_confirmed": True, "recovered_confirmed": True}

        def fail_confirmed_readback(path, properties, notion):
            if path != f"/pages/{SECOND_ISSUE_PAGE_ID}":
                return
            prop = properties.get("동기화 내부 상태")
            if not prop:
                return
            saved = json.loads(rich_text_value(prop))
            pending = saved.get("pending")
            if not pending or pending["kind"] != "status" or not pending["confirmed"]:
                return
            if (failure_stages["initial_confirmed"] and
                    saved["reopen_baseline_id"] is None and
                    saved["reopen_last_id"] is None):
                failure_stages["initial_confirmed"] = False
                notion.fail_readback_after_patch.add(SECOND_ISSUE_PAGE_ID)
            elif (failure_stages["recovered_confirmed"] and
                    saved["reopen_baseline_id"] == "R_PRE" and
                    saved["reopen_last_id"] == "R_PRE"):
                failure_stages["recovered_confirmed"] = False
                notion.fail_readback_after_patch.add(SECOND_ISSUE_PAGE_ID)

        self.notion.after_patch = fail_confirmed_readback

        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync()
        after_apply = se.decode_internal(
            se.read_text(self.notion.pages[SECOND_ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=556)
        self.assertTrue(after_apply["pending"]["confirmed"])
        self.assertIsNone(after_apply["reopen_baseline_id"])
        self.assertIsNone(after_apply["reopen_last_id"])
        self.assertEqual(after_apply["pending"]["checkpoint"]["reopen_last_id"], "R_PRE")
        self.assertEqual(len(self.project.writes), 1)

        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync()
        after_recovery_checkpoint = se.decode_internal(
            se.read_text(self.notion.pages[SECOND_ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=556)
        self.assertTrue(after_recovery_checkpoint["pending"]["confirmed"])
        self.assertEqual(after_recovery_checkpoint["reopen_baseline_id"], "R_PRE")
        self.assertEqual(after_recovery_checkpoint["reopen_last_id"], "R_PRE")
        self.assertEqual(len(self.project.writes), 1)

        counts = self.run_sync()

        recovered = se.decode_internal(
            se.read_text(self.notion.pages[SECOND_ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=556)
        healthy = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(counts["held"], 0)
        self.assertIsNone(recovered["pending"])
        self.assertEqual(recovered["reopen_baseline_id"], "R_PRE")
        self.assertEqual(recovered["reopen_last_id"], "R_PRE")
        self.assertTrue(healthy["migration_complete"])
        self.assertEqual(len(self.project.writes), 1)

    def test_closed_forward_reopen_checkpoint_recovers_without_repeating_project_mutation(self):
        events = [
            {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"},
            {"id": "R_POST", "createdAt": "2026-10-04T00:00:00Z"},
        ]
        issue_a = make_issue()
        issue_b = make_issue(state="CLOSED", reason="COMPLETED", reopens=events,
                             closed_at="2026-10-05T00:00:00Z")
        issue_b.update({"id": "I_kwDOIssue19", "databaseId": 556, "number": 19,
                        "url": f"https://github.com/{gp.REPOSITORY}/issues/19"})
        state = se.new_issue_state(556, gp.EXPECTED_PROJECT_ID)
        state.update({"migration_complete": True, "project_item_id": "PVTI_issue19",
                      "reopen_baseline_id": "R_PRE", "reopen_last_id": "R_PRE"})
        self.repo_graph = RepoGraph(issues=[issue_a, issue_b])
        self.rest = LegacyREST(issues=[
            {"id": ISSUE_ID, "node_id": ISSUE_NODE, "number": ISSUE_NUMBER, "state": "open"},
            {"id": 556, "node_id": issue_b["id"], "number": 19, "state": "closed"},
        ])
        self.project = ProjectAPI([
            make_project_item(option="백로그"),
            make_project_item(item_id="PVTI_issue19", issue_id=556,
                              issue_node=issue_b["id"], number=19, option="백로그"),
        ])
        self.notion = FakeNotion([
            control_page(control_internal()),
            active_issue_page(task="백로그"),
            active_issue_page(page_id=SECOND_ISSUE_PAGE_ID, issue_id=556, number=19,
                              task="완료", internal=se.canonical_json(state)),
        ])

        def fail_confirmed_readback(path, properties, notion):
            if path != f"/pages/{SECOND_ISSUE_PAGE_ID}":
                return
            prop = properties.get("동기화 내부 상태")
            if not prop:
                return
            saved = json.loads(rich_text_value(prop))
            pending = saved.get("pending")
            if pending and pending["kind"] == "status" and pending["confirmed"]:
                notion.after_patch = None
                notion.fail_readback_after_patch.add(SECOND_ISSUE_PAGE_ID)

        self.notion.after_patch = fail_confirmed_readback

        with self.assertRaisesRegex(se.SyncError, "synthetic page readback failure"):
            self.run_sync()
        committed = se.decode_internal(
            se.read_text(self.notion.pages[SECOND_ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=556)
        self.assertTrue(committed["pending"]["confirmed"])
        self.assertIsNone(committed["pending"]["event_id"])
        self.assertEqual(committed["reopen_last_id"], "R_PRE")
        self.assertEqual(committed["pending"]["checkpoint"]["reopen_last_id"], "R_POST")
        self.assertEqual(len(self.project.writes), 1)

        counts = self.run_sync()

        recovered = se.decode_internal(
            se.read_text(self.notion.pages[SECOND_ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=556)
        healthy = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(counts["held"], 0)
        self.assertIsNone(recovered["pending"])
        self.assertEqual(recovered["reopen_baseline_id"], "R_PRE")
        self.assertEqual(recovered["reopen_last_id"], "R_POST")
        self.assertTrue(healthy["migration_complete"])
        self.assertEqual(len(self.project.writes), 1)

    def test_stale_closed_forward_pending_holds_per_issue_and_processes_healthy_issue(self):
        prior = {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"}
        checkpoint_event = {"id": "R_POST", "createdAt": "2026-10-04T00:00:00Z"}
        next_event = {"id": "R_NEXT", "createdAt": "2026-10-06T00:00:00Z"}
        old_issue = make_issue(state="CLOSED", reason="COMPLETED",
                               reopens=[prior, checkpoint_event],
                               closed_at="2026-10-05T00:00:00Z")
        for variant in ("reopened", "closed_later", "old_open_closed_later"):
            with self.subTest(variant=variant):
                events = [prior, checkpoint_event, next_event]
                if variant == "reopened":
                    current = make_issue(state="OPEN", reopens=events)
                    current["updatedAt"] = "2026-10-06T00:00:00Z"
                    pending_source = old_issue
                    before, target = "백로그", "완료"
                    pending_option, pending_task = "완료", "완료"
                else:
                    if variant == "closed_later":
                        current = make_issue(state="CLOSED", reason="COMPLETED",
                                             reopens=events, closed_at="2026-10-08T00:00:00Z")
                        current["updatedAt"] = "2026-10-08T00:00:00Z"
                        pending_source = old_issue
                        before, target = "백로그", "완료"
                        pending_option, pending_task = "완료", "완료"
                    else:
                        old_open = make_issue(state="OPEN",
                                              reopens=[prior, checkpoint_event])
                        current = make_issue(state="CLOSED", reason="COMPLETED",
                                             reopens=[prior, checkpoint_event],
                                             closed_at="2026-10-05T00:00:00Z")
                        current["updatedAt"] = "2026-10-05T00:00:00Z"
                        pending_source = old_open
                        before, target = "진행 중", "백로그"
                        pending_option, pending_task = "백로그", "백로그"
                state = make_pending_status_state(pending_source, before=before, target=target,
                                                  migration_complete=True)
                state["reopen_baseline_id"] = "R_PRE"
                state["reopen_last_id"] = "R_PRE"
                state["pending"]["checkpoint"]["reopen_last_id"] = "R_POST"
                if variant == "old_open_closed_later":
                    state["pending"]["event_id"] = "R_POST"
                self._install_historical_pending_snapshot(current, state,
                    pending_option=pending_option, pending_task=pending_task)

                counts = self.run_sync()

                held = se.decode_internal(
                    se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
                    kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                healthy = se.decode_internal(
                    se.read_text(self.notion.pages[SECOND_ISSUE_PAGE_ID], "동기화 내부 상태"),
                    kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=556)
                self.assertEqual(counts["held"], 1)
                self.assertEqual(held["hold"]["code"], "PENDING_FACTS_CHANGED")
                self.assertIsNone(held["pending"])
                self.assertTrue(healthy["migration_complete"])
                self.assertEqual(self.project.writes, [])

    def test_stale_closed_pending_can_be_fresh_pm_resumed_from_current_canonical_status(self):
        prior = {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"}
        checkpoint_event = {"id": "R_POST", "createdAt": "2026-10-04T00:00:00Z"}
        next_event = {"id": "R_NEXT", "createdAt": "2026-10-06T00:00:00Z"}
        old_issue = make_issue(state="CLOSED", reason="COMPLETED",
                               reopens=[prior, checkpoint_event],
                               closed_at="2026-10-05T00:00:00Z")
        current = make_issue(state="OPEN", reopens=[prior, checkpoint_event, next_event])
        current["updatedAt"] = "2026-10-06T00:00:00Z"
        state = make_pending_status_state(old_issue, before="백로그", target="완료",
                                          migration_complete=True)
        state["reopen_baseline_id"] = "R_PRE"
        state["reopen_last_id"] = "R_PRE"
        state["pending"]["checkpoint"]["reopen_last_id"] = "R_POST"
        state["hold"] = se._new_hold("PENDING_RESULT_UNCLEAR", "PM 확인 대기",
                                     se._digest("old-uncertain-result"))
        self._install_historical_pending_snapshot(current, state, pending_option="백로그",
                                                  pending_task="백로그")
        env = pm_env(918612)
        before = se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태")

        preview = self.run_sync(dry_run=True, resolve_issue_numbers="18", env=env)

        planned = preview["resolution_preview"][0]
        self.assertEqual(planned["result"], "would_resume")
        self.assertEqual(planned["target"], "백로그")
        self.assertFalse(planned["project_change"])
        self.assertEqual(self.notion.writes, [])
        self.assertEqual(self.project.writes, [])
        self.assertEqual(se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"), before)

        counts = self.run_sync(resolve_issue_numbers="18", env=env)

        resumed = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(counts["held"], 0)
        self.assertIsNone(resumed["pending"])
        self.assertIsNone(resumed["hold"])
        self.assertEqual(resumed["reopen_last_id"], "R_NEXT")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "백로그")
        self.assertEqual(self.project.writes, [])

    def test_matching_open_forged_forward_checkpoint_fails_globally(self):
        events = [
            {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"},
            {"id": "R_POST", "createdAt": "2026-10-04T00:00:00Z"},
        ]
        issue = make_issue(state="OPEN", reopens=events)
        state = make_pending_status_state(issue, before="백로그", target="완료",
                                          migration_complete=True)
        state["reopen_baseline_id"] = "R_PRE"
        state["reopen_last_id"] = "R_PRE"
        state["pending"]["checkpoint"]["reopen_last_id"] = "R_POST"
        self._install_historical_pending_snapshot(issue, state)

        with self.assertRaises((se.SyncError, gp.SyncError)):
            self.run_sync()

        self.assertEqual(self.notion.writes, [])
        self.assertEqual(self.project.writes, [])
        self.assertEqual(self.repo_graph.mutations, 0)

    def test_historical_fingerprint_does_not_mask_malformed_pending_reopen_checkpoint(self):
        prior = {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"}
        checkpoint_event = {"id": "R_POST", "createdAt": "2026-10-04T00:00:00Z"}
        next_event = {"id": "R_NEXT", "createdAt": "2026-10-06T00:00:00Z"}
        old_issue = make_issue(state="CLOSED", reason="COMPLETED",
                               reopens=[prior, checkpoint_event],
                               closed_at="2026-10-05T00:00:00Z")
        for case in ("missing_id", "rewind", "cutoff_boundary"):
            with self.subTest(case=case):
                events = [prior, checkpoint_event, next_event]
                if case == "cutoff_boundary":
                    events.append({"id": "R_BOUNDARY", "createdAt": CUTOFF})
                current = make_issue(state="OPEN", reopens=events)
                current["updatedAt"] = "2026-10-06T00:00:00Z"
                state = make_pending_status_state(old_issue, before="백로그", target="완료",
                                                  migration_complete=True)
                state["reopen_baseline_id"] = "R_PRE"
                state["reopen_last_id"] = "R_PRE"
                state["pending"]["checkpoint"]["reopen_last_id"] = "R_POST"
                if case == "missing_id":
                    state["pending"]["checkpoint"]["reopen_last_id"] = "R_FAKE"
                elif case == "rewind":
                    state["reopen_last_id"] = "R_POST"
                    state["pending"]["checkpoint"]["reopen_last_id"] = "R_PRE"
                self._install_historical_pending_snapshot(current, state)

                with self.assertRaises((se.SyncError, gp.SyncError)):
                    self.run_sync()

                self.assertEqual(self.notion.writes, [])
                self.assertEqual(self.project.writes, [])
                self.assertEqual(self.repo_graph.mutations, 0)

    def test_historical_fingerprint_cannot_advance_checkpoint_to_pre_cutoff_event(self):
        prior = {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"}
        middle = {"id": "R_MID_PRE", "createdAt": "2026-09-29T00:00:00Z"}
        checkpoint_event = {"id": "R_POST", "createdAt": "2026-10-04T00:00:00Z"}
        next_event = {"id": "R_NEXT", "createdAt": "2026-10-06T00:00:00Z"}
        old_issue = make_issue(state="CLOSED", reason="COMPLETED",
                               reopens=[prior, checkpoint_event],
                               closed_at="2026-10-05T00:00:00Z")
        current = make_issue(state="OPEN", reopens=[prior, middle,
                              checkpoint_event, next_event])
        current["updatedAt"] = "2026-10-06T00:00:00Z"
        state = make_pending_status_state(old_issue, before="백로그", target="완료",
                                          migration_complete=True)
        state["reopen_baseline_id"] = "R_PRE"
        state["reopen_last_id"] = "R_PRE"
        state["pending"]["checkpoint"]["reopen_last_id"] = "R_MID_PRE"
        self._install_historical_pending_snapshot(current, state)

        with self.assertRaises((se.SyncError, gp.SyncError)):
            self.run_sync()

        self.assertEqual(self.notion.writes, [])
        self.assertEqual(self.project.writes, [])
        self.assertEqual(self.repo_graph.mutations, 0)

    def test_matching_pending_event_must_be_current_canonical_type_and_latest(self):
        prior = {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"}
        checkpoint_event = {"id": "R_POST", "createdAt": "2026-10-04T00:00:00Z"}
        next_event = {"id": "R_NEXT", "createdAt": "2026-10-06T00:00:00Z"}
        for scenario in ("closed_requires_no_event", "open_requires_latest_event",
                         "open_missing_event", "closed_applied_event",
                         "open_applied_event_not_latest", "closed_forward_wrong_target"):
            with self.subTest(scenario=scenario):
                if scenario in {"closed_requires_no_event", "closed_applied_event",
                                "closed_forward_wrong_target"}:
                    current = make_issue(state="CLOSED", reason="COMPLETED",
                        reopens=[prior, checkpoint_event], closed_at="2026-10-05T00:00:00Z")
                    before = "백로그"
                    target = "백로그" if scenario == "closed_forward_wrong_target" else "완료"
                else:
                    events = [prior, checkpoint_event]
                    if scenario in {"open_requires_latest_event", "open_applied_event_not_latest"}:
                        events.append(next_event)
                    current = make_issue(state="OPEN", reopens=events)
                    before = "준비 중" if scenario == "open_missing_event" else "진행 중"
                    target = before if scenario == "open_missing_event" else "백로그"
                state = make_pending_status_state(current, before=before, target=target,
                                                  migration_complete=True)
                state["reopen_baseline_id"] = "R_PRE"
                if scenario == "open_missing_event":
                    state["reopen_last_id"] = "R_PRE"
                    state["pending"]["checkpoint"]["reopen_last_id"] = "R_PRE"
                    project_status = notion_status = "준비 중"
                elif scenario == "closed_forward_wrong_target":
                    state["reopen_last_id"] = "R_PRE"
                    state["pending"]["checkpoint"]["reopen_last_id"] = "R_POST"
                    project_status = notion_status = "백로그"
                else:
                    state["reopen_last_id"] = "R_POST"
                    state["pending"]["event_id"] = "R_POST"
                    state["pending"]["checkpoint"]["reopen_last_id"] = "R_POST"
                    project_status = "완료" if scenario in {
                        "closed_requires_no_event", "closed_applied_event"} else "백로그"
                    notion_status = before
                    if scenario in {"closed_applied_event", "open_applied_event_not_latest"}:
                        state["review_pr_hash"] = se._digest([])
                        state["pending"]["checkpoint"] = {
                            field: state[field] for field in se.STATUS_CHECKPOINT_KEYS}
                        state["pending"]["confirmed"] = True
                self._install_historical_pending_snapshot(
                    current, state, pending_option=project_status, pending_task=notion_status)

                with self.assertRaises((se.SyncError, gp.SyncError)):
                    self.run_sync()

                self.assertEqual(self.notion.writes, [])
                self.assertEqual(self.project.writes, [])
                self.assertEqual(self.repo_graph.mutations, 0)

    def test_equal_applied_reopen_checkpoint_must_match_canonical_review_target_and_hash(self):
        prior = {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"}
        latest = {"id": "R_POST", "createdAt": "2026-10-04T00:00:00Z"}
        for scenario in ("wrong_target_with_ready_pr", "wrong_checkpoint_hash"):
            with self.subTest(scenario=scenario):
                pulls = []
                if scenario == "wrong_target_with_ready_pr":
                    pr = make_pr()
                    pr.update({"state": "OPEN", "isDraft": False, "mergedAt": None})
                    issue = make_issue(reopens=[prior, latest], linked=[pr_reference(pr)])
                    pulls = [pr]
                    facts = {"repository_node_id": REPO_NODE, "pulls": {PR_REST_ID: pr}}
                    before, target = "진행 중", "백로그"
                    project_status = "백로그"
                else:
                    issue = make_issue(reopens=[prior, latest])
                    facts = {"repository_node_id": REPO_NODE, "pulls": {}}
                    before, target = "준비 중", "백로그"
                    project_status = "백로그"
                state = make_pending_status_state(issue, before=before, target=target,
                                                  migration_complete=True, facts=facts)
                state["reopen_baseline_id"] = "R_PRE"
                state["reopen_last_id"] = "R_POST"
                state["pending"]["event_id"] = "R_POST"
                state["pending"]["confirmed"] = True
                if scenario == "wrong_target_with_ready_pr":
                    state["review_cycle"] = 1
                    state["review_pr_hash"] = se._digest([PR_NODE])
                else:
                    state["review_cycle"] = 1
                    state["review_pr_hash"] = se._digest(["stale-pr"])
                state["pending"]["checkpoint"] = {
                    field: state[field] for field in se.STATUS_CHECKPOINT_KEYS}
                state["pending"]["facts_fingerprint"] = se._source_fingerprint(
                    issue, se._linked_pr_facts(issue, facts)[0],
                    {"id": "PVTI_issue18",
                     "status_option_id": gp.EXPECTED_STATUS_OPTIONS[before]})
                self.repo_graph = RepoGraph(issues=[issue], pulls=pulls)
                rest_rows = [{"id": ISSUE_ID, "node_id": ISSUE_NODE,
                    "number": ISSUE_NUMBER, "state": "open"}]
                if pulls:
                    rest_rows.append({"id": PR_REST_ID, "node_id": PR_NODE, "number": 19,
                        "state": "open", "pull_request": {"url":
                            f"https://api.github.com/repos/{gp.REPOSITORY}/pulls/19"}})
                    self.rest = LegacyREST(rest_rows, pull={"id": PR_DATABASE_ID,
                        "node_id": PR_NODE, "number": 19, "state": "open", "draft": False,
                        "merged": False, "base": {"ref": "main", "repo": {
                            "id": gp.REPOSITORY_ID, "node_id": REPO_NODE}}})
                else:
                    self.rest = LegacyREST(rest_rows)
                self.project = ProjectAPI([make_project_item(option=project_status)])
                pages = [control_page(control_internal()),
                    active_issue_page(task=before, internal=se.canonical_json(state))]
                if pulls:
                    pages.append(notion_page(PR_PAGE_ID,
                        key=se.key_for(gp.REPOSITORY_ID, PR_REST_ID), kind="PR", number=19))
                self.notion = FakeNotion(pages)

                with self.assertRaises((se.SyncError, gp.SyncError)):
                    self.run_sync()

                self.assertEqual(self.notion.writes, [])
                self.assertEqual(self.project.writes, [])
                self.assertEqual(self.repo_graph.mutations, 0)

    def test_held_matching_pending_requires_canonical_target_and_full_checkpoint(self):
        prior = {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"}
        latest = {"id": "R_POST", "createdAt": "2026-10-04T00:00:00Z"}

        def install(issue, state, *, task, option, pulls=(), include_pr=False):
            healthy = make_issue()
            healthy.update({"id": "I_kwDOIssue20", "databaseId": 556, "number": 20,
                            "url": f"https://github.com/{gp.REPOSITORY}/issues/20"})
            self.repo_graph = RepoGraph(issues=[issue, healthy], pulls=pulls)
            rest_rows = [
                {"id": ISSUE_ID, "node_id": ISSUE_NODE, "number": ISSUE_NUMBER,
                 "state": issue["state"].lower()},
                {"id": 556, "node_id": healthy["id"], "number": 20, "state": "open"},
            ]
            if include_pr:
                rest_rows.append({"id": PR_REST_ID, "node_id": PR_NODE, "number": 19,
                    "state": "open", "pull_request": {"url":
                        f"https://api.github.com/repos/{gp.REPOSITORY}/pulls/19"}})
                self.rest = LegacyREST(rest_rows, pull={"id": PR_DATABASE_ID,
                    "node_id": PR_NODE, "number": 19, "state": "open", "draft": False,
                    "merged": False, "base": {"ref": "main", "repo": {
                        "id": gp.REPOSITORY_ID, "node_id": REPO_NODE}}})
            else:
                self.rest = LegacyREST(rest_rows)
            self.project = ProjectAPI([
                make_project_item(option=option),
                make_project_item(item_id="PVTI_issue20", issue_id=556,
                    issue_node=healthy["id"], number=20, option="백로그"),
            ])
            pages = [control_page(control_internal()),
                active_issue_page(task=task, internal=se.canonical_json(state)),
                active_issue_page(page_id=SECOND_ISSUE_PAGE_ID, issue_id=556,
                    number=20, task="백로그")]
            if include_pr:
                pages.append(notion_page(PR_PAGE_ID,
                    key=se.key_for(gp.REPOSITORY_ID, PR_REST_ID), kind="PR", number=19))
            self.notion = FakeNotion(pages)

        for scenario in ("closed_wrong_target", "open_wrong_target", "forged_review_hash"):
            with self.subTest(scenario=scenario):
                pulls = []
                if scenario == "closed_wrong_target":
                    issue = make_issue(state="CLOSED", reason="COMPLETED",
                        closed_at="2026-10-05T00:00:00Z")
                    facts = {"repository_node_id": REPO_NODE, "pulls": {}}
                    state = make_pending_status_state(issue, before="백로그", target="백로그",
                        migration_complete=True, facts=facts)
                    task, option = "백로그", "준비 중"
                else:
                    pr = make_pr()
                    pr.update({"state": "OPEN", "isDraft": False, "mergedAt": None})
                    reopens = [prior, latest] if scenario == "forged_review_hash" else []
                    issue = make_issue(reopens=reopens, linked=[pr_reference(pr)])
                    pulls = [pr]
                    facts = {"repository_node_id": REPO_NODE, "pulls": {PR_REST_ID: pr}}
                    if scenario == "open_wrong_target":
                        state = make_pending_status_state(issue, before="백로그", target="백로그",
                            migration_complete=True, facts=facts)
                        task, option = "백로그", "준비 중"
                    else:
                        state = make_pending_status_state(issue, before="진행 중",
                            target="검토 중", migration_complete=True, facts=facts)
                        state["reopen_baseline_id"] = "R_PRE"
                        state["reopen_last_id"] = "R_POST"
                        state["pending"]["event_id"] = "R_POST"
                        state["pending"]["confirmed"] = True
                        state["review_cycle"] = 1
                        state["review_return_cycle"] = 0
                        state["review_pr_hash"] = se._digest(["forged-pr"])
                        state["pending"]["checkpoint"] = {
                            field: state[field] for field in se.STATUS_CHECKPOINT_KEYS}
                        task, option = "진행 중", "준비 중"

                refs, _ = se._linked_pr_facts(issue, facts)
                state["hold"] = se._new_hold("PENDING_RESULT_UNCLEAR", "pending uncertain",
                    se._source_fingerprint(issue, refs, {"id": "PVTI_issue18",
                        "status_option_id": gp.EXPECTED_STATUS_OPTIONS[option]}))
                install(issue, state, task=task, option=option, pulls=pulls,
                        include_pr=bool(pulls))

                with self.assertRaisesRegex(
                        se.SyncError,
                        r"^pending status가 현재 canonical 이벤트/target/checkpoint와 불일치합니다$"):
                    self.run_sync()

                self.assertEqual(self.notion.writes, [])
                self.assertEqual(self.project.writes, [])
                self.assertEqual(self.repo_graph.mutations, 0)

    def test_reopen_event_equality_rejects_unconfirmed_or_forged_checkpoint(self):
        for case in ("unconfirmed_equal", "confirmed_fields_mismatch",
                     "same_time_other_id", "confirmed_distinct_time_positive"):
            with self.subTest(case=case):
                events = [
                    {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"},
                    {"id": "R_POST", "createdAt": "2026-10-04T00:00:00Z"},
                ]
                if case == "same_time_other_id":
                    events.append({"id": "R_POST_OTHER",
                                   "createdAt": "2026-10-04T00:00:00Z"})
                elif case == "confirmed_distinct_time_positive":
                    events.append({"id": "R_POST_OTHER",
                                   "createdAt": "2026-10-05T00:00:00Z"})
                issue = make_issue(reopens=events)
                state = make_pending_status_state(issue, before="준비 중", target="백로그",
                                                  migration_complete=True)
                state["reopen_baseline_id"] = "R_PRE"
                if case == "same_time_other_id":
                    state["reopen_last_id"] = "R_POST"
                    state["pending"]["event_id"] = "R_POST_OTHER"
                    state["pending"]["checkpoint"]["reopen_last_id"] = "R_POST_OTHER"
                elif case == "confirmed_distinct_time_positive":
                    state["reopen_last_id"] = "R_POST_OTHER"
                    state["pending"]["event_id"] = "R_POST_OTHER"
                    state["pending"]["checkpoint"]["reopen_last_id"] = "R_POST_OTHER"
                    state["pending"]["confirmed"] = True
                    state["review_cycle"] = state["pending"]["checkpoint"]["review_cycle"]
                else:
                    state["reopen_last_id"] = "R_POST"
                    state["pending"]["event_id"] = "R_POST"
                state["pending"]["checkpoint"]["reopen_last_id"] = "R_POST"
                if case == "same_time_other_id":
                    state["pending"]["checkpoint"]["reopen_last_id"] = "R_POST_OTHER"
                elif case == "confirmed_distinct_time_positive":
                    state["pending"]["checkpoint"]["reopen_last_id"] = "R_POST_OTHER"
                state["pending"]["confirmed"] = (
                    case in {"confirmed_fields_mismatch", "confirmed_distinct_time_positive"})
                state["review_pr_hash"] = state["pending"]["checkpoint"]["review_pr_hash"]
                if case == "confirmed_fields_mismatch":
                    state["review_cycle"] = 1
                state["pending"]["facts_fingerprint"] = se._source_fingerprint(
                    issue, [], {"id": "PVTI_issue18",
                                "status_option_id": gp.EXPECTED_STATUS_OPTIONS["준비 중"]})
                self.repo_graph = RepoGraph(issues=[issue])
                self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
                    "number": ISSUE_NUMBER, "state": "open"}])
                self.project = ProjectAPI([make_project_item(option="백로그")])
                self.notion = FakeNotion([control_page(control_internal()),
                    active_issue_page(task="준비 중", internal=se.canonical_json(state))])

                if case == "confirmed_distinct_time_positive":
                    result = self.run_sync()
                    self.assertEqual(result["failed"], 0)
                else:
                    expected = {
                        "unconfirmed_equal": "이미 처리된 재오픈 이벤트를 pending status가 재사용합니다",
                        "confirmed_fields_mismatch": "pending status가 현재 canonical 이벤트/target/checkpoint와 불일치합니다",
                        "same_time_other_id": "pending status Issue 재오픈 순서가 같은 시각에 모호합니다",
                    }[case]
                    with self.assertRaisesRegex((se.SyncError, gp.SyncError), expected):
                        self.run_sync()

                    self.assertEqual(self.notion.writes, [])
                    self.assertEqual(self.project.writes, [])

    def test_late_project_pagination_failure_prevents_every_write(self):
        self.project = ProjectAPI()
        self.project.fail_late_item_page = True
        self.notion = FakeNotion([control_page()])

        with self.assertRaises(gp.SyncError):
            self.run_sync()

        self.assertEqual(self.project.item_page_calls, 2)
        self.assertEqual(self.project.writes, [])
        self.assertEqual(self.notion.writes, [])

    def test_late_malformed_pending_checkpoint_fails_preflight_before_any_write(self):
        for case in ("event_checkpoint_mismatch", "event_missing_from_timeline",
                     "add_content_mismatch", "baseline_missing", "last_missing",
                     "baseline_order_reversed"):
            with self.subTest(case=case):
                first = make_issue(state="CLOSED", reason="COMPLETED",
                                   closed_at="2026-10-03T00:00:00Z")
                second = copy.deepcopy(first)
                second.update({"id": "I_kwDOIssue19", "databaseId": 556, "number": 19,
                               "url": f"https://github.com/{gp.REPOSITORY}/issues/19"})
                if case == "baseline_order_reversed":
                    events = [{"id": "REOPEN_EARLIER", "createdAt": "2026-10-02T00:00:00Z"},
                              {"id": "REOPEN_LATER", "createdAt": "2026-10-03T00:00:00Z"}]
                    second["reopens"] = events
                    second["timelineItems"] = conn(events)
                elif case == "event_checkpoint_mismatch":
                    events = [{"id": "REOPEN_EVENT_A", "createdAt": "2026-10-02T00:00:00Z"},
                              {"id": "REOPEN_EVENT_B", "createdAt": "2026-10-03T00:00:00Z"}]
                    second["reopens"] = events
                    second["timelineItems"] = conn(events)
                self.repo_graph = RepoGraph(issues=[first, second])
                self.rest = LegacyREST(issues=[
                    {"id": ISSUE_ID, "node_id": ISSUE_NODE, "number": ISSUE_NUMBER,
                     "state": "closed"},
                    {"id": 556, "node_id": second["id"], "number": 19, "state": "closed"},
                ])
                malformed = se.new_issue_state(556, gp.EXPECTED_PROJECT_ID)
                if case in {"event_checkpoint_mismatch", "event_missing_from_timeline"}:
                    malformed["project_item_id"] = "PVTI_issue19"
                    event_id = ("REOPEN_EVENT_A" if case == "event_checkpoint_mismatch"
                                else "REOPEN_EVENT_FAKE")
                    checkpoint_event = ("REOPEN_EVENT_B" if case == "event_checkpoint_mismatch"
                                        else event_id)
                    malformed["pending"] = {
                        "kind": "status", "issue_id": 556, "item_id": "PVTI_issue19",
                        "event_id": event_id,
                        "before_option_id": gp.EXPECTED_STATUS_OPTIONS["백로그"],
                        "target_option_id": gp.EXPECTED_STATUS_OPTIONS["완료"],
                        "facts_fingerprint": se._digest("pending-facts"),
                        "migration_fingerprint": None, "notion_status": "백로그",
                        "confirmed": False, "project_item_id": "PVTI_issue19",
                        "checkpoint": {"migration_complete": True, "review_cycle": 0,
                            "review_return_cycle": 0, "reopen_last_id": checkpoint_event,
                            "review_pr_hash": se._digest([])},
                    }
                    items = [make_project_item(item_id="PVTI_issue18", option="백로그"),
                             make_project_item(item_id="PVTI_issue19", option="백로그",
                                 issue_id=556, issue_node=second["id"], number=19)]
                elif case == "add_content_mismatch":
                    malformed["pending"] = {
                        "kind": "add", "issue_id": 556, "item_id": None, "event_id": None,
                        "before_option_id": None, "target_option_id": None,
                        "facts_fingerprint": se._digest("pending-facts"),
                        "migration_fingerprint": None, "notion_status": "백로그",
                        "confirmed": False, "project_item_id": None,
                        "checkpoint": {"content_id": "I_wrong_content"},
                    }
                    items = [make_project_item(item_id="PVTI_issue18", option="백로그")]
                else:
                    items = [make_project_item(item_id="PVTI_issue18", option="백로그"),
                             make_project_item(item_id="PVTI_issue19", option="백로그",
                                 issue_id=556, issue_node=second["id"], number=19)]
                    if case == "baseline_missing":
                        malformed["reopen_baseline_id"] = "REOPEN_BASELINE_MISSING"
                    elif case == "last_missing":
                        malformed["reopen_last_id"] = "REOPEN_LAST_MISSING"
                    else:
                        malformed["reopen_baseline_id"] = "REOPEN_LATER"
                        malformed["reopen_last_id"] = "REOPEN_EARLIER"
                self.project = ProjectAPI(items)
                self.notion = FakeNotion([
                    control_page(control_internal()), active_issue_page(task="백로그"),
                    active_issue_page(page_id=SECOND_ISSUE_PAGE_ID, issue_id=556, number=19,
                        task="백로그", internal=se.canonical_json(malformed)),
                ])

                with self.assertRaises((se.SyncError, gp.SyncError)):
                    self.run_sync()

                self.assertEqual(self.notion.writes, [])
                self.assertEqual(self.project.writes, [])

    def test_archived_project_item_variants_hold_without_duplicate_add(self):
        active = make_project_item(item_id="PVTI_active", option="백로그")
        archived = make_project_item(item_id="PVTI_archived", option="백로그", archived=True)
        scenarios = (
            ("archived-only", [archived], None),
            ("active-and-archived", [active, archived], None),
            ("multiple-archived", [
                make_project_item(item_id="PVTI_archived_a", archived=True),
                make_project_item(item_id="PVTI_archived_b", archived=True)], None),
            ("archived-on-later-page", [active, archived], [[active], [archived]]),
        )
        for name, items, pages in scenarios:
            with self.subTest(name=name):
                self.repo_graph = RepoGraph(issues=[make_issue()])
                self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
                    "number": ISSUE_NUMBER, "state": "open"}])
                self.project = ProjectAPI(items)
                self.project.item_pages = pages
                self.notion = FakeNotion([control_page(control_internal()),
                                          active_issue_page(task="백로그")])

                counts = self.run_sync()

                self.assertEqual(counts["held"], 1)
                self.assertEqual(self.project.add_calls, 0)
                self.assertEqual(self.project.writes, [])
                state = se.decode_internal(
                    se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
                    kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
                self.assertEqual(state["hold"]["code"], "PROJECT_ITEM_ARCHIVED")


if __name__ == "__main__":
    unittest.main()
