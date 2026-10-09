import copy
import contextlib
import io
import json
import sys
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_project as gp
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
    result = {"nodes": list(nodes), "pageInfo": {"hasNextPage": more, "endCursor": cursor}}
    if total is not None:
        result["totalCount"] = total
    return result


def rich_text_value(value):
    return "".join(part.get("plain_text", part.get("text", {}).get("content", ""))
                   for part in value.get("rich_text", []))


def title_text(page, name="제목"):
    return "".join(part.get("plain_text", part.get("text", {}).get("content", ""))
                   for part in page["properties"][name]["title"])


def status_value(name):
    return {"__typename": "ProjectV2ItemFieldSingleSelectValue",
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
        if path.endswith("/pulls/19") and self.pull is not None:
            return copy.deepcopy(self.pull)
        raise AssertionError("Unexpected legacy REST path")


class ProjectAPI:
    def __init__(self, items=()):
        self.items = list(items)
        self.writes = []
        self.status_values = {}
        self.later_field_values = {}
        self.add_calls = 0
        self.add_response_loss_count = 1
        self.add_response_loss = False
        self.add_visibility_delay_snapshots = 0
        self.post_add_snapshot_calls = 0
        self.added_item_ids = []
        self.add_item_transform = None
        self.after_add = None
        self.add_result_item_id_override = None
        self.add_response_override = None
        self.next_project_items_override = None
        self.sleep_calls = []
        self.sleep = self.sleep_calls.append
        self.item_page_calls = 0
        self.fail_late_item_page = False
        self.item_pages = None

    def request(self, query, variables=None, *, mutation=False):
        variables = variables or {}
        if mutation:
            self.writes.append((query, variables))
            if query == gp.ADD_ISSUE_MUTATION:
                self.add_calls += 1
                if self.add_response_loss:
                    for index in range(self.add_response_loss_count):
                        item = make_project_item(
                            item_id=f"PVTI_added_{self.add_calls}_{index}", option=None)
                        self.items.append(item)
                        self.added_item_ids.append(item["id"])
                    raise gp.SyncError("synthetic lost add response")
                item_id = f"PVTI_added_{self.add_calls}_0"
                item = make_project_item(item_id=item_id, option=None)
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
            item_id = variables["item"]
            if query == gp.SET_STATUS_MUTATION:
                option = variables["option"]
                name = next(name for name, value in gp.EXPECTED_STATUS_OPTIONS.items()
                            if value == option)
                value = [{"__typename": "ProjectV2ItemFieldSingleSelectValue",
                          "field": {"id": variables["field"]}, "optionId": option, "name": name}]
                self.status_values[item_id] = value
                item = next(row for row in self.items if row["id"] == item_id)
                item["fieldValues"]["nodes"] = value
                return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": item_id}}}, None
            if query == gp.CLEAR_STATUS_MUTATION:
                self.status_values[item_id] = []
                item = next(row for row in self.items if row["id"] == item_id)
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
                if self.post_add_snapshot_calls <= self.add_visibility_delay_snapshots:
                    hidden_ids = set(self.added_item_ids)
                if self.next_project_items_override is not None:
                    response_items = self.next_project_items_override
                    self.next_project_items_override = None
                    return {"node": self._project(items=response_items)}, None
            if hidden_ids:
                pages = [[row for row in page if row["id"] not in hidden_ids]
                         for page in pages]
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
            item_id = variables["id"]
            item = next(row for row in self.items if row["id"] == item_id)
            value = self.status_values.get(item_id, item["fieldValues"]["nodes"])
            return {"node": {"id": item_id, "isArchived": bool(item.get("isArchived")),
                "project": {"id": gp.EXPECTED_PROJECT_ID, "number": 4,
                            "owner": {"id": gp.EXPECTED_PROJECT_OWNER_ID}},
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
        self.creates = 0
        self.page_size = 100
        self.fail_patch = None
        self.fail_readback_after_patch = set()
        self.fail_get_once = set()
        self.after_patch = None
        self.request_status = {"type": "complete"}
        self.omit_request_status = False
        self.query_response_transform = None

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
            return copy.deepcopy(self.pages[page_id])
        self.writes.append((method, path, copy.deepcopy(payload), kwargs))
        if method == "POST" and path == "/pages":
            self.creates += 1
            new_id = str(UUID(int=1000 + self.creates))
            page = notion_page(new_id)
            page["properties"].update(copy.deepcopy(payload["properties"]))
            self.pages[new_id] = page
            return {"id": new_id}
        if method == "PATCH":
            page_id = path.rsplit("/", 1)[1]
            if self.fail_patch and self.fail_patch(path, payload["properties"]):
                self.fail_patch = None
                raise se.SyncError("synthetic page write failure")
            self.pages[page_id]["properties"].update(copy.deepcopy(payload["properties"]))
            if self.after_patch:
                self.after_patch(path, payload["properties"], self)
            if page_id in self.fail_readback_after_patch:
                self.fail_readback_after_patch.remove(page_id)
                self.fail_get_once.add(page_id)
            return copy.deepcopy(self.pages[page_id])
        raise AssertionError("Unexpected Notion request")


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
    value = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_RUN_ID": str(run_id),
             "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID)}
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
                              target="완료", migration_complete=False, facts=None):
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
        return se.sync(self.repo_graph, self.rest, self.project, self.notion,
                       config(), now=now, env=env, **kwargs)

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
                    entry["property"] != "GitHub 수정" for entry in metadata_matches]
                self.assertEqual([entry["matches"] for entry in metadata_matches],
                                 expected_metadata_results)
                self.assertEqual(diagnostic["first_metadata_mismatch"], "GitHub 수정")
                clock_shape = diagnostic["clock_shape"]
                self.assertEqual(clock_shape["property"], "동기화 시각")
                self.assertEqual(clock_shape["current_run_would_write"]["fractional_digits"], 6)
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
        self.assertEqual(se.read_text(updated, "메모"), "human note")
        self.assertEqual(updated["properties"]["일정"]["date"]["start"], "2026-10-21")
        self.assertEqual(updated["body"], "human page body")
        self.assertEqual(self.project.writes, [])

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
               "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID)}
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
               "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID)}
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

    def test_initial_migration_uses_notion_when_project_status_is_blank(self):
        plan = self._plan(make_issue(), project_state=None, notion_state="진행 중")
        self.assertEqual(plan["target"], "진행 중")
        self.assertTrue(plan["initial_migration"])

    def test_initial_migration_preserves_project_when_notion_status_is_blank(self):
        plan = self._plan(make_issue(), project_state="준비 중", notion_state=None)
        self.assertEqual(plan["target"], "준비 중")
        self.assertTrue(plan["initial_migration"])

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

        ordinary = self.run_sync()
        self.assertEqual(ordinary["held"], 1)
        held = se.decode_internal(
            se.read_text(self.notion.pages[ISSUE_PAGE_ID], "동기화 내부 상태"),
            kind="issue", project_id=gp.EXPECTED_PROJECT_ID, object_id=ISSUE_ID)
        self.assertEqual(held["hold"]["code"], "PROJECT_STATUS_UNSET")
        self.assertEqual(se.read_select(self.notion.pages[ISSUE_PAGE_ID], "작업 상태"), "백로그")
        self.assertEqual(self.project.writes, [])
        summary = self.notion.pages[CONTROL_ID]
        self.assertEqual(title_text(summary), f"Replica 동기화 · 부분 반영 · {NOW}")
        self.assertEqual(summary["properties"]["GitHub URL"]["url"], se.ACTIONS_WORKFLOW_URL)
        self.assertEqual(se.read_text(summary, "확인 필요"), "보류 1건: 18")
        self.assertEqual(se.read_date(summary, "동기화 시각"), previous_success)
        summary_state = se.decode_internal(se.read_text(summary, "동기화 내부 상태"),
            kind="control", project_id=gp.EXPECTED_PROJECT_ID)
        self.assertEqual(summary_state["last_result"]["kind"], "partial")
        self.assertEqual(summary_state["last_result"]["at"], NOW)
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

    def test_existing_notion_issue_is_registered_in_new_project_before_migration(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI()
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task="진행 중")])

        self.run_sync()

        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual(self.project.status_values["PVTI_added_1_0"][0]["optionId"],
                         gp.EXPECTED_STATUS_OPTIONS["진행 중"])
        row = self.notion.pages[ISSUE_PAGE_ID]
        self.assertEqual(se.read_select(row, "작업 상태"), "진행 중")
        state = se.decode_internal(se.read_text(row, "동기화 내부 상태"),
                                   kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
                                   object_id=ISSUE_ID)
        self.assertTrue(state["migration_complete"])
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
        self.assertEqual(plan["target"], "진행 중")
        self.assertFalse(plan["project_change"])
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
                        self.assertEqual(final["hold"]["code"], "PROJECTION_CHECKPOINT_CHANGED")
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
            if raw and json.loads(raw)["pending"]["kind"] == "status":
                notion.pages[ISSUE_PAGE_ID]["properties"]["작업 상태"] = {
                    "select": {"name": "준비 중"}}
                notion.after_patch = None

        self.notion.after_patch = change_task_after_pending
        self.run_sync()

        row = self.notion.pages[ISSUE_PAGE_ID]
        state = se.decode_internal(se.read_text(row, "동기화 내부 상태"),
                                   kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
                                   object_id=ISSUE_ID)
        self.assertEqual(state["hold"]["code"], "MIGRATION_INPUT_CHANGED")
        self.assertIsNone(state["pending"])
        self.assertEqual(se.read_select(row, "작업 상태"), "준비 중")
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
               "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID)}
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
               "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID)}
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

    def test_failed_pm_resume_display_keeps_hold_when_source_fingerprint_changes(self):
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
               "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID)}
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
        self.assertEqual(preview["issue_plans"][0]["hold"]["code"],
                         "RESUME_CHECKPOINT_CHANGED")
        self.assertEqual(len(self.notion.writes), notion_writes_before_preview)
        self.assertEqual(len(self.project.writes), project_writes_before_preview)
        counts = self.run_sync()

        held = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID],
            "동기화 내부 상태"), kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
            object_id=ISSUE_ID)
        self.assertEqual(counts["held"], 1)
        self.assertEqual(held["hold"]["code"], "RESUME_CHECKPOINT_CHANGED")
        self.assertIsNone(held["resume"])
        self.assertTrue(se.read_text(self.notion.pages[ISSUE_PAGE_ID], "확인 필요").startswith("보류:"))
        self.assertEqual(self.project.writes, [])

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
               "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID)}
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

    def test_project_add_readback_retries_complete_absence_and_accepts_blank_initial_status(self):
        self.repo_graph = RepoGraph(issues=[make_issue()])
        self.rest = LegacyREST(issues=[{"id": ISSUE_ID, "node_id": ISSUE_NODE,
            "number": ISSUE_NUMBER, "state": "open"}])
        self.project = ProjectAPI()
        self.project.add_visibility_delay_snapshots = 1
        self.notion = FakeNotion([control_page(control_internal()),
                                  active_issue_page(task="진행 중")])

        self.run_sync()

        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual(self.project.post_add_snapshot_calls, 2)
        self.assertEqual(self.project.sleep_calls, [se.PROJECT_ADD_READBACK_BACKOFF_SECONDS[0]])
        self.assertEqual(self.project.status_values["PVTI_added_1_0"][0]["optionId"],
                         gp.EXPECTED_STATUS_OPTIONS["진행 중"])
        _, state = self._stored_issue_state()
        self.assertEqual(state["project_item_id"], "PVTI_added_1_0")
        self.assertTrue(state["migration_complete"])
        self.assertIsNone(state["pending"])

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
                    with self.assertRaisesRegex(se.SyncError, "Project 추가 결과"):
                        self.run_sync()
                    _, pending_state = self._stored_issue_state()
                    self.assertTrue(pending_state["pending"])
                    self.assertFalse(pending_state["pending"]["confirmed"])
                    self.assertEqual(self.project.post_add_snapshot_calls, 1)
                    self.assertEqual(self.project.sleep_calls, [])
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
        self.project.add_visibility_delay_snapshots = se.PROJECT_ADD_READBACK_MAX_ATTEMPTS

        with self.assertRaisesRegex(se.SyncError, "Project 추가 결과 0개/대상 불일치"):
            self.run_sync()

        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual(self.project.post_add_snapshot_calls,
                         se.PROJECT_ADD_READBACK_MAX_ATTEMPTS)
        self.assertEqual(self.project.sleep_calls,
                         list(se.PROJECT_ADD_READBACK_BACKOFF_SECONDS))
        self.assertEqual([query for query, _ in self.project.writes], [gp.ADD_ISSUE_MUTATION])
        _, pending_state = self._stored_issue_state()
        self.assertEqual(pending_state["pending"]["kind"], "add")
        self.assertFalse(pending_state["pending"]["confirmed"])
        self.assertIsNone(pending_state["pending"]["project_item_id"])
        self.assertEqual(pending_state["pending"]["checkpoint"], {"content_id": ISSUE_NODE})

        ordinary = self.run_sync()
        _, held_state = self._stored_issue_state()
        self.assertGreaterEqual(ordinary["held"], 1)
        self.assertEqual(held_state["hold"]["code"], "PROJECT_ADD_UNCERTAIN")
        self.assertEqual(self.project.add_calls, 1)

        preview = self.run_sync(dry_run=True, resolve_issue_numbers="18", env=pm_env(918320))
        self.assertEqual(preview["resolution_preview"][0]["result"], "would_resume")
        self.assertEqual(self.project.add_calls, 1)
        self.run_sync(resolve_issue_numbers="18", env=pm_env(918321))
        _, resumed_state = self._stored_issue_state()
        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual(resumed_state["project_item_id"], "PVTI_added_1_0")
        self.assertIsNone(resumed_state["pending"])
        self.assertIsNone(resumed_state["hold"])

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

                with self.assertRaisesRegex(se.SyncError, "Project 추가 결과"):
                    self.run_sync()

                self.assertEqual(self.project.add_calls, 1)
                self.assertEqual(self.project.post_add_snapshot_calls, 1)
                self.assertEqual(self.project.sleep_calls, [])
                self.assertEqual([query for query, _ in self.project.writes], [gp.ADD_ISSUE_MUTATION])
                _, state = self._stored_issue_state()
                self.assertEqual(state["pending"]["kind"], "add")
                self.assertFalse(state["pending"]["confirmed"])
                self.assertIsNone(state["pending"]["project_item_id"])

    def test_project_add_readback_partial_pagination_fails_without_retry(self):
        self._configure_post_cutoff_project_add()

        def return_incomplete_items(project, _item, _variables):
            project.next_project_items_override = {
                "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None}, "totalCount": 1}

        self.project.after_add = return_incomplete_items
        with self.assertRaisesRegex(gp.SyncError, "cursor"):
            self.run_sync()

        self.assertEqual(self.project.add_calls, 1)
        self.assertEqual(self.project.post_add_snapshot_calls, 1)
        self.assertEqual(self.project.sleep_calls, [])
        self.assertEqual([query for query, _ in self.project.writes], [gp.ADD_ISSUE_MUTATION])
        _, state = self._stored_issue_state()
        self.assertEqual(state["pending"]["kind"], "add")
        self.assertFalse(state["pending"]["confirmed"])
        self.assertEqual(state["pending"]["checkpoint"], {"content_id": ISSUE_NODE})

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
                       "PM_GITHUB_USER_ID": str(gp.EXPECTED_PM_USER_ID)}
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
                self.assertEqual(counts["held"], 1)
                self.assertEqual(self.project.add_calls, 0)
                written_items = [variables.get("item") for _, variables in self.project.writes]
                self.assertEqual(written_items, ["PVTI_issue19"])
                held = se.decode_internal(se.read_text(self.notion.pages[ISSUE_PAGE_ID],
                    "동기화 내부 상태"), kind="issue", project_id=gp.EXPECTED_PROJECT_ID,
                    object_id=ISSUE_ID)
                self.assertEqual(held["hold"]["code"], "SOURCE_CHANGED_BEFORE_WRITE")
                self.assertIsNone(held["pending"])
                self.assertTrue(se.read_text(self.notion.pages[ISSUE_PAGE_ID], "확인 필요").startswith("보류:"))
                self.assertEqual(se.read_select(self.notion.pages[SECOND_ISSUE_PAGE_ID], "작업 상태"),
                                 "완료")
                control_state = se.decode_internal(
                    se.read_text(self.notion.pages[CONTROL_ID], "동기화 내부 상태"),
                    kind="control", project_id=gp.EXPECTED_PROJECT_ID)
                self.assertIsNone(control_state["last_success_at"])

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
                        ref = self.repo_graph.issues[0]["linked_prs"][0]
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
        events = [
            {"id": "R_PRE", "createdAt": "2026-09-28T00:00:00Z"},
            {"id": "R_POST", "createdAt": "2026-10-04T00:00:00Z"},
            {"id": "R_POST_OTHER", "createdAt": "2026-10-04T00:00:00Z"},
        ]
        for case in ("unconfirmed_equal", "confirmed_fields_mismatch", "same_time_other_id"):
            with self.subTest(case=case):
                issue = make_issue(reopens=events)
                state = make_pending_status_state(issue, before="준비 중", target="백로그",
                                                  migration_complete=True)
                state["reopen_baseline_id"] = "R_PRE"
                state["reopen_last_id"] = "R_POST_OTHER" if case == "same_time_other_id" else "R_POST"
                state["pending"]["event_id"] = "R_POST"
                state["pending"]["checkpoint"]["reopen_last_id"] = "R_POST"
                state["pending"]["confirmed"] = case == "confirmed_fields_mismatch"
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

                with self.assertRaises((se.SyncError, gp.SyncError)):
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
