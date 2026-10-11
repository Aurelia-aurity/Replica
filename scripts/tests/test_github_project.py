import copy
import io
import unittest
from datetime import datetime, timezone

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_project as gp


REPO_NODE = "R_kgDOGitHubRepo"
PR_NODE = "PR_kwDOUv77_s8AAAABHQtAUA"
REST_ISSUE_ID = 5756422334
PR_DATABASE_ID = 4782243920
STATUS_FIELD = "PVTSSF_test_status"
ISSUE_NODE = "I_kwDOIssueNode"


def connection(nodes=(), *, more=False, cursor=None, total=None):
    value = {"nodes": list(nodes), "pageInfo": {"hasNextPage": more, "endCursor": cursor}}
    if total is not None:
        value["totalCount"] = total
    return value


def merged_pr():
    empty = connection()
    return {
        "id": PR_NODE, "databaseId": PR_DATABASE_ID, "number": 19, "title": "merged feature",
        "url": "https://github.com/Aurelia-aurity/Replica/pull/19",
        "createdAt": "2026-09-01T00:00:00Z", "updatedAt": "2026-09-03T00:00:00Z",
        "state": "MERGED", "isDraft": False, "mergedAt": "2026-09-02T00:00:00Z",
        "baseRefName": "main", "baseRepository": {"id": REPO_NODE, "databaseId": gp.REPOSITORY_ID,
            "nameWithOwner": gp.REPOSITORY}, "author": {"login": "contributor"},
        "assignees": empty, "labels": empty, "closingIssuesReferences": empty,
    }


class FactsGraphQL:
    def __init__(self, pull=None):
        self.pull = merged_pr() if pull is None else pull
        self.queries = []
        self.server_time = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def request(self, query, variables=None, *, mutation=False):
        self.queries.append(query)
        if "viewer" in query:
            return {"viewer": {"id": "U_viewer", "login": "test"}}, self.server_time
        repo = {"id": REPO_NODE, "databaseId": gp.REPOSITORY_ID,
                "nameWithOwner": gp.REPOSITORY,
                "owner": {"id": "O_owner", "login": "Aurelia-aurity"}}
        if "pullRequests(first:" in query:
            repo["pullRequests"] = connection([self.pull], total=1)
            return {"repository": repo}, None
        if "issues(first:" in query:
            repo["issues"] = connection([], total=0)
            return {"repository": repo}, None
        raise AssertionError("Unexpected GraphQL query")


def rest_issue_row(*, issue_id=REST_ISSUE_ID, node_id=PR_NODE):
    return {"id": issue_id, "node_id": node_id, "number": 19, "state": "closed",
            "pull_request": {"url": "https://api.github.com/repos/Aurelia-aurity/Replica/pulls/19"}}


def rest_pull(*, database_id=PR_DATABASE_ID, node_id=PR_NODE):
    return {"id": database_id, "node_id": node_id, "number": 19, "state": "closed",
            "draft": False, "merged": True,
            "base": {"ref": "main", "repo": {"id": gp.REPOSITORY_ID,
                "node_id": REPO_NODE}}}


class FactsREST:
    def __init__(self, rows=None, pull=None):
        self.rows = [rest_issue_row()] if rows is None else rows
        self.pull = rest_pull() if pull is None else pull
        self.calls = []

    def request(self, path):
        self.calls.append(path)
        if path == f"/repos/{gp.REPOSITORY}":
            return {"id": gp.REPOSITORY_ID, "full_name": gp.REPOSITORY}
        if "/issues?" in path:
            return self.rows
        if path.endswith("/pulls/19"):
            return self.pull
        raise AssertionError("Unexpected REST path")


class ProjectReader:
    def __init__(self, items, *, incomplete_field_values=False, later_values=None, item_pages=None,
                 direct_items=None):
        self.items = items
        self.item_pages = item_pages
        self.direct_items = direct_items or {}
        self.incomplete_field_values = incomplete_field_values
        self.later_values = later_values or {}
        self.queries = []
        self.expected_cursors = {}
        self.expected_field_cursors = {}

    def request(self, query, variables=None, *, mutation=False):
        variables = copy.deepcopy(variables or {})
        self.queries.append((query, variables))
        if query == gp.PROJECT_FIELDS_QUERY:
            self._check_project_request(query, variables)
            fields = [
                {"__typename": "ProjectV2SingleSelectField", "id": STATUS_FIELD, "name": "Status",
                 "options": [{"id": value, "name": name}
                             for name, value in gp.EXPECTED_STATUS_OPTIONS.items()]},
                {"__typename": "ProjectV2IterationField", "id": "PVTIF_iteration", "name": "Iteration"},
            ]
            return {"node": self._project(fields=connection(fields))}, None
        if query == gp.PROJECT_ITEMS_QUERY:
            self._check_project_request(query, variables)
            if self.item_pages is None:
                pages = [self.items]
            else:
                pages = self.item_pages
            after = (variables or {}).get("after")
            page_index = 0 if after is None else int(after.rsplit("-", 1)[1])
            rows = pages[page_index]
            for row in rows:
                field_info = row.get("fieldValues", {}).get("pageInfo", {})
                if field_info.get("hasNextPage"):
                    self.expected_field_cursors[row["id"]] = field_info.get("endCursor")
            more = page_index + 1 < len(pages)
            cursor = f"item-page-{page_index + 1}" if more else None
            total = sum(len(page) for page in pages)
            if more:
                self.expected_cursors[query] = cursor
            return {"node": self._project(items=connection(rows, more=more, cursor=cursor,
                                                             total=total))}, None
        if query == gp.ITEM_FIELD_VALUES_QUERY:
            item_id = variables.get("id")
            expected_after = self.expected_field_cursors.get(item_id)
            if (not isinstance(item_id, str) or not item_id or
                    variables.get("after") != expected_after):
                raise gp.SyncError("fixture rejected wrong item ID or field-values cursor")
            return {"node": {"id": item_id,
                              "fieldValues": connection(self.later_values.get(item_id, []))}}, None
        if query == gp.PROJECT_ITEM_QUERY:
            item_id = variables.get("id")
            return {"node": self.direct_items.get(item_id)}, None
        raise AssertionError("Unexpected Project query")

    def _check_project_request(self, query, variables):
        if (set(variables) != {"id", "after"} or
                variables.get("id") != gp.EXPECTED_PROJECT_ID):
            raise gp.SyncError("fixture rejected wrong Project ID")
        cursor = variables.get("after")
        expected = self.expected_cursors.get(query)
        if cursor != expected:
            raise gp.SyncError("fixture rejected missing or unexpected Project cursor")
        self.expected_cursors.pop(query, None)

    @staticmethod
    def _project(*, fields=None, items=None):
        return {"id": gp.EXPECTED_PROJECT_ID, "number": 4,
                "owner": {"id": gp.EXPECTED_PROJECT_OWNER_ID, "login": "Just-Simple0"},
                "fields": fields or connection(), "items": items or connection([], total=0)}


def project_item(item_id, *, archived=False, option_id=None, field_values=None,
                 more=False, cursor=None):
    if field_values is None:
        field_values = ([{"__typename": "ProjectV2ItemFieldSingleSelectValue",
                          "id": "PVTSV_test_value", "updatedAt": "2026-10-08T00:00:00Z",
                          "field": {"id": STATUS_FIELD},
                          "optionId": option_id or gp.EXPECTED_STATUS_OPTIONS["진행 중"],
                          "name": "진행 중"}] if option_id else [])
    return {"id": item_id, "isArchived": archived,
            "content": {"__typename": "Issue", "id": ISSUE_NODE, "databaseId": 41,
                        "repository": {"id": REPO_NODE, "databaseId": gp.REPOSITORY_ID,
                                       "nameWithOwner": gp.REPOSITORY}},
            "fieldValues": {"nodes": list(field_values),
                            **({} if more else {"pageInfo": {"hasNextPage": False, "endCursor": None}}),
                            **({"pageInfo": {"hasNextPage": True, "endCursor": cursor}} if more else {})}}


class GitHubProjectTests(unittest.TestCase):
    def test_direct_project_item_id_readback_validates_project_and_status_metadata(self):
        row = project_item("PVTI_returned", option_id=gp.EXPECTED_STATUS_OPTIONS["진행 중"])
        row["project"] = {"id": gp.EXPECTED_PROJECT_ID, "number": gp.PROJECT_NUMBER,
                          "owner": {"id": gp.EXPECTED_PROJECT_OWNER_ID}}
        reader = ProjectReader([], direct_items={"PVTI_returned": row})

        actual = gp.fetch_project_item(reader, gp.EXPECTED_PROJECT_ID, "PVTI_returned", STATUS_FIELD)

        self.assertEqual(actual["id"], "PVTI_returned")
        self.assertEqual(actual["status_value_id"], "PVTSV_test_value")
        self.assertEqual(actual["status_updated_at"], "2026-10-08T00:00:00Z")
        self.assertEqual([query for query, _ in reader.queries], [gp.PROJECT_ITEM_QUERY])
        self.assertEqual(reader.queries[0][1], {"id": "PVTI_returned"})

    def test_direct_project_item_lookup_can_report_visibility_lag(self):
        reader = ProjectReader([])
        self.assertIsNone(gp.fetch_project_item(
            reader, gp.EXPECTED_PROJECT_ID, "PVTI_not_visible", STATUS_FIELD,
            allow_missing=True))

    def test_direct_project_item_rejects_invalid_identity_status_and_archive_metadata(self):
        valid = project_item("PVTI_target", option_id=gp.EXPECTED_STATUS_OPTIONS["진행 중"])
        valid["project"] = {"id": gp.EXPECTED_PROJECT_ID, "number": gp.PROJECT_NUMBER,
                            "owner": {"id": gp.EXPECTED_PROJECT_OWNER_ID}}
        invalid_rows = {
            "returned item ID": lambda row: row.update(id="PVTI_other"),
            "wrong project ID": lambda row: row["project"].update(id="PVT_wrong"),
            "wrong project number": lambda row: row["project"].update(number=99),
            "wrong owner": lambda row: row["project"]["owner"].update(id="U_wrong"),
            "archived": lambda row: row.update(isArchived=True),
            "wrong content type": lambda row: row["content"].update(__typename="PullRequest"),
            "missing content ID": lambda row: row["content"].update(id=None),
            "wrong repository": lambda row: row["content"]["repository"].update(
                nameWithOwner="other/repository"),
            "wrong repository database ID": lambda row: row["content"]["repository"].update(
                databaseId=999),
            "wrong status value type": lambda row: row["fieldValues"]["nodes"][0].update(
                __typename="ProjectV2ItemFieldTextValue"),
            "unknown status option": lambda row: row["fieldValues"]["nodes"][0].update(
                optionId="PVT_invalid"),
            "missing value ID": lambda row: row["fieldValues"]["nodes"][0].update(id=None),
            "missing updatedAt": lambda row: row["fieldValues"]["nodes"][0].update(
                updatedAt=None),
            "invalid updatedAt": lambda row: row["fieldValues"]["nodes"][0].update(
                updatedAt="yesterday"),
        }
        for label, corrupt in invalid_rows.items():
            with self.subTest(corruption=label):
                row = copy.deepcopy(valid)
                corrupt(row)
                with self.assertRaises(gp.SyncError):
                    gp.fetch_project_item(ProjectReader([], direct_items={"PVTI_target": row}),
                        gp.EXPECTED_PROJECT_ID, "PVTI_target", STATUS_FIELD)

    def test_direct_project_item_does_not_adopt_valid_option_from_wrong_status_field(self):
        wrong_field = project_item("PVTI_target", field_values=[{
            "__typename": "ProjectV2ItemFieldSingleSelectValue",
            "id": "PVTSV_other_field", "updatedAt": "2026-10-08T00:00:00Z",
            "field": {"id": "PVTSSF_not_status"},
            "optionId": gp.EXPECTED_STATUS_OPTIONS["진행 중"], "name": "진행 중"}])
        wrong_field["project"] = {"id": gp.EXPECTED_PROJECT_ID,
            "number": gp.PROJECT_NUMBER,
            "owner": {"id": gp.EXPECTED_PROJECT_OWNER_ID}}

        actual = gp.fetch_project_item(
            ProjectReader([], direct_items={"PVTI_target": wrong_field}),
            gp.EXPECTED_PROJECT_ID, "PVTI_target", STATUS_FIELD)

        self.assertEqual(actual["id"], "PVTI_target")
        self.assertEqual(actual["status_field_id"], STATUS_FIELD)
        self.assertIsNone(actual["status_option_id"])
        self.assertIsNone(actual["status_value_id"])
        self.assertIsNone(actual["status_updated_at"])

    def test_merged_pr_is_in_complete_graphql_scan_and_keeps_legacy_issue_key(self):
        graph, rest = FactsGraphQL(), FactsREST()
        facts = gp.fetch_repository_facts(graph, rest)
        self.assertIn("states: [OPEN, CLOSED, MERGED]", gp.PULLS_QUERY)
        self.assertIn(REST_ISSUE_ID, facts["pulls"])
        pull = facts["pulls"][REST_ISSUE_ID]
        self.assertEqual(pull["state"], "MERGED")
        self.assertEqual(pull["databaseId"], PR_DATABASE_ID)
        self.assertEqual(pull["legacy_issue_id"], REST_ISSUE_ID)
        self.assertEqual(pull["id"], PR_NODE)
        self.assertNotIn(PR_DATABASE_ID, facts["pulls"])

    def test_missing_legacy_rest_row_fails_closed(self):
        with self.assertRaises(gp.SyncError):
            gp.fetch_repository_facts(FactsGraphQL(), FactsREST(rows=[]))

    def test_rest_node_or_pull_database_id_conflict_fails_closed(self):
        for rest in (FactsREST(rows=[rest_issue_row(node_id="PR_wrong")]),
                     FactsREST(pull=rest_pull(database_id=123))):
            with self.subTest(rest=rest):
                with self.assertRaises(gp.SyncError):
                    gp.fetch_repository_facts(FactsGraphQL(), rest)

    def test_project_field_values_continue_pagination_and_include_iteration_field(self):
        first_item = project_item("PVTI_item", more=True, cursor="field-cursor")
        status_value = {"__typename": "ProjectV2ItemFieldSingleSelectValue",
                        "id": "PVTSV_later_value", "updatedAt": "2026-10-08T00:00:00Z",
                        "field": {"id": STATUS_FIELD},
                        "optionId": gp.EXPECTED_STATUS_OPTIONS["완료"], "name": "완료"}
        reader = ProjectReader([first_item], later_values={"PVTI_item": [status_value]})
        result = gp.fetch_project(reader, project_id=gp.EXPECTED_PROJECT_ID,
            owner_id=gp.EXPECTED_PROJECT_OWNER_ID, status_field_id=STATUS_FIELD,
            status_options=gp.EXPECTED_STATUS_OPTIONS, repository_node_id=REPO_NODE)
        self.assertEqual(result["items"][41]["status_option_id"], gp.EXPECTED_STATUS_OPTIONS["완료"])
        self.assertIn("ProjectV2IterationField", gp.PROJECT_FIELDS_QUERY)
        self.assertTrue(any(query == gp.ITEM_FIELD_VALUES_QUERY for query, _ in reader.queries))
        field_query = next(variables for query, variables in reader.queries
                           if query == gp.ITEM_FIELD_VALUES_QUERY)
        self.assertEqual(field_query, {"id": "PVTI_item", "after": "field-cursor"})

    def test_project_reader_rejects_wrong_project_id_and_field_value_cursor(self):
        reader = ProjectReader([])
        for query in (gp.PROJECT_FIELDS_QUERY, gp.PROJECT_ITEMS_QUERY):
            with self.subTest(query=query), self.assertRaises(gp.SyncError):
                reader.request(query, {"id": "PVT_wrong", "after": None})
        reader.expected_field_cursors["PVTI_expected"] = "end-cursor-17"
        for variables in (
                {"id": "PVTI_wrong", "after": "end-cursor-17"},
                {"id": "PVTI_expected", "after": None},
                {"id": "PVTI_expected", "after": "other-cursor"}):
            with self.subTest(variables=variables), self.assertRaises(gp.SyncError):
                reader.request(gp.ITEM_FIELD_VALUES_QUERY, variables)

    def test_project_items_continuation_is_bound_to_exact_end_cursor(self):
        second = project_item("PVTI_page_2")
        second["content"].update(id="I_kwDOIssue19", databaseId=42, number=19)
        pages = [[project_item("PVTI_page_1")], [second]]
        reader = ProjectReader(pages[0], item_pages=pages)
        gp.fetch_project(reader, project_id=gp.EXPECTED_PROJECT_ID,
            owner_id=gp.EXPECTED_PROJECT_OWNER_ID, status_field_id=STATUS_FIELD,
            status_options=gp.EXPECTED_STATUS_OPTIONS, repository_node_id=REPO_NODE)
        item_requests = [variables for query, variables in reader.queries
                         if query == gp.PROJECT_ITEMS_QUERY]
        self.assertEqual(item_requests, [
            {"id": gp.EXPECTED_PROJECT_ID, "after": None},
            {"id": gp.EXPECTED_PROJECT_ID, "after": "item-page-1"}])

        negative = ProjectReader([], item_pages=[pages[0], pages[1]])
        negative.expected_cursors[gp.PROJECT_ITEMS_QUERY] = "item-page-1"
        with self.assertRaises(gp.SyncError):
            negative.request(gp.PROJECT_ITEMS_QUERY,
                {"id": gp.EXPECTED_PROJECT_ID, "after": "wrong-cursor"})

    def test_project_queries_select_union_field_ids_through_concrete_fragments(self):
        for query in (gp.PROJECT_ITEMS_QUERY, gp.ITEM_FIELD_VALUES_QUERY, gp.PROJECT_ITEM_QUERY):
            with self.subTest(query=query):
                self.assertIn("field { ... on ProjectV2SingleSelectField { id } }", query)
                self.assertNotIn("field { id }", query)

    def test_project_items_query_requests_archived_and_active_items(self):
        self.assertIn("archivedStates: [ARCHIVED, NOT_ARCHIVED]", gp.PROJECT_ITEMS_QUERY)

    def test_add_readback_observes_returned_id_before_repository_filter_without_rejecting_other_repos(self):
        returned = project_item("PVTI_returned")
        returned["content"]["repository"]["id"] = "R_foreign"
        unrelated = project_item("PVTI_unrelated", field_values=[],)
        unrelated["content"].update({"id": "I_other", "databaseId": 42, "number": 19})
        unrelated["content"]["repository"]["id"] = "R_another_foreign"
        reader = ProjectReader([returned, unrelated])

        result = gp.fetch_project(reader, project_id=gp.EXPECTED_PROJECT_ID,
            owner_id=gp.EXPECTED_PROJECT_OWNER_ID, status_field_id=STATUS_FIELD,
            status_options=gp.EXPECTED_STATUS_OPTIONS, repository_node_id=REPO_NODE,
            add_readback_item_id="PVTI_returned", add_target_issue_id=41,
            add_target_issue_node_id=ISSUE_NODE)

        self.assertEqual(result["items"], {})
        self.assertEqual(result["add_readback_items"], [{
            "item_id": "PVTI_returned", "is_archived": False, "content_type": "Issue",
            "content_id": ISSUE_NODE, "content_database_id": 41,
            "repository_id": "R_foreign", "repository_database_id": gp.REPOSITORY_ID,
        }])
        self.assertEqual(result["item_count"], 2)

    def test_add_readback_observes_target_issue_by_node_or_database_id(self):
        by_database_id = project_item("PVTI_wrong_node")
        by_database_id["content"]["id"] = "I_wrong"
        by_node_id = project_item("PVTI_wrong_database")
        by_node_id["content"]["databaseId"] = 42
        reader = ProjectReader([by_database_id, by_node_id])

        result = gp.fetch_project(reader, project_id=gp.EXPECTED_PROJECT_ID,
            owner_id=gp.EXPECTED_PROJECT_OWNER_ID, status_field_id=STATUS_FIELD,
            status_options=gp.EXPECTED_STATUS_OPTIONS, repository_node_id=REPO_NODE,
            add_readback_item_id="PVTI_not_present", add_target_issue_id=41,
            add_target_issue_node_id=ISSUE_NODE)

        self.assertEqual({row["item_id"] for row in result["add_readback_items"]},
                         {"PVTI_wrong_node", "PVTI_wrong_database"})
        self.assertEqual(result["items"][41]["content_id"], "I_wrong")
        self.assertEqual(result["items"][42]["content_id"], ISSUE_NODE)

    def test_archived_replica_items_are_found_active_and_archived_in_all_pages(self):
        scenarios = (
            ([project_item("PVTI_archived", archived=True)], {}, ["PVTI_archived"]),
            ([project_item("PVTI_active"), project_item("PVTI_archived", archived=True)],
             {}, ["PVTI_archived"]),
            ([project_item("PVTI_active")],
             {"later": [project_item("PVTI_archived", archived=True)]},
             ["PVTI_archived"]),
            ([], {"later": [project_item("PVTI_archived_a", archived=True),
                             project_item("PVTI_archived_b", archived=True)]},
             ["PVTI_archived_a", "PVTI_archived_b"]),
        )
        for first_page, later, expected_archived in scenarios:
            with self.subTest(expected_archived=expected_archived):
                pages = [first_page]
                if "later" in later:
                    pages.append(later["later"])
                reader = ProjectReader(first_page, item_pages=pages)
                result = gp.fetch_project(reader, project_id=gp.EXPECTED_PROJECT_ID,
                    owner_id=gp.EXPECTED_PROJECT_OWNER_ID, status_field_id=STATUS_FIELD,
                    status_options=gp.EXPECTED_STATUS_OPTIONS, repository_node_id=REPO_NODE)
                self.assertEqual([entry["id"] for entry in result["archived_items"].get(41, [])],
                                 expected_archived)
                self.assertEqual(result["items"].get(41, {}).get("id"),
                                 next((item["id"] for item in first_page if not item["isArchived"]), None))
                self.assertEqual(result["item_count"], sum(len(page) for page in pages))

    def test_field_values_missing_page_info_fails_closed(self):
        item = project_item("PVTI_item", more=True, cursor=None)
        reader = ProjectReader([item])
        with self.assertRaises(gp.SyncError):
            gp.fetch_project(reader, project_id=gp.EXPECTED_PROJECT_ID,
                owner_id=gp.EXPECTED_PROJECT_OWNER_ID, status_field_id=STATUS_FIELD,
                status_options=gp.EXPECTED_STATUS_OPTIONS, repository_node_id=REPO_NODE)

    def test_field_values_without_page_info_fails_closed(self):
        item = project_item("PVTI_item")
        del item["fieldValues"]["pageInfo"]
        reader = ProjectReader([item])
        with self.assertRaises(gp.SyncError):
            gp.fetch_project(reader, project_id=gp.EXPECTED_PROJECT_ID,
                owner_id=gp.EXPECTED_PROJECT_OWNER_ID, status_field_id=STATUS_FIELD,
                status_options=gp.EXPECTED_STATUS_OPTIONS, repository_node_id=REPO_NODE)

    def test_archived_replica_issue_is_indexed_alongside_active_item(self):
        rows = [project_item("PVTI_active"), project_item("PVTI_archived", archived=True)]
        reader = ProjectReader(rows)
        result = gp.fetch_project(reader, project_id=gp.EXPECTED_PROJECT_ID,
            owner_id=gp.EXPECTED_PROJECT_OWNER_ID, status_field_id=STATUS_FIELD,
            status_options=gp.EXPECTED_STATUS_OPTIONS, repository_node_id=REPO_NODE)
        self.assertEqual(result["items"][41]["id"], "PVTI_active")
        self.assertEqual(result["archived_items"][41][0]["id"], "PVTI_archived")

    def test_duplicate_active_issue_items_fail_closed(self):
        rows = [project_item("PVTI_a"), project_item("PVTI_b")]
        reader = ProjectReader(rows)
        with self.assertRaises(gp.SyncError):
            gp.fetch_project(reader, project_id=gp.EXPECTED_PROJECT_ID,
                owner_id=gp.EXPECTED_PROJECT_OWNER_ID, status_field_id=STATUS_FIELD,
                status_options=gp.EXPECTED_STATUS_OPTIONS, repository_node_id=REPO_NODE)

    def test_rest_client_allows_exact_repository_root_and_subtree_only(self):
        class Response(io.BytesIO):
            pass

        class Opener:
            def __init__(self):
                self.urls = []

            def open(self, request, timeout):
                self.urls.append(request.full_url)
                return Response(b'{"ok": true}')

        opener = Opener()
        client = gp.RESTClient("test-token", opener=opener, sleep=lambda _delay: None)
        root = f"/repos/{gp.REPOSITORY}"
        self.assertEqual(client.request(root), {"ok": True})
        self.assertEqual(client.request(root + "/issues?state=all"), {"ok": True})
        self.assertEqual(len(opener.urls), 2)
        self.assertTrue(all(url.startswith("https://api.github.com" + root) for url in opener.urls))
        for blocked in (root + "-evil/issues", "/repos/other/repo/issues",
                        "https://evil.example/repos/Aurelia-aurity/Replica/issues",
                        root + "/../other/issues"):
            with self.subTest(blocked=blocked):
                with self.assertRaises(gp.SyncError):
                    client.request(blocked)
        self.assertEqual(len(opener.urls), 2)


if __name__ == "__main__":
    unittest.main()
