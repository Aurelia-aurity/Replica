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
    def __init__(self, items, *, incomplete_field_values=False, later_values=None, item_pages=None):
        self.items = items
        self.item_pages = item_pages
        self.incomplete_field_values = incomplete_field_values
        self.later_values = later_values or {}
        self.queries = []

    def request(self, query, variables=None, *, mutation=False):
        self.queries.append((query, variables or {}))
        if query == gp.PROJECT_FIELDS_QUERY:
            fields = [
                {"__typename": "ProjectV2SingleSelectField", "id": STATUS_FIELD, "name": "Status",
                 "options": [{"id": value, "name": name}
                             for name, value in gp.EXPECTED_STATUS_OPTIONS.items()]},
                {"__typename": "ProjectV2IterationField", "id": "PVTIF_iteration", "name": "Iteration"},
            ]
            return {"node": self._project(fields=connection(fields))}, None
        if query == gp.PROJECT_ITEMS_QUERY:
            if self.item_pages is None:
                pages = [self.items]
            else:
                pages = self.item_pages
            after = (variables or {}).get("after")
            page_index = 0 if after is None else int(after.rsplit("-", 1)[1])
            rows = pages[page_index]
            more = page_index + 1 < len(pages)
            cursor = f"item-page-{page_index + 1}" if more else None
            total = sum(len(page) for page in pages)
            return {"node": self._project(items=connection(rows, more=more, cursor=cursor,
                                                             total=total))}, None
        if query == gp.ITEM_FIELD_VALUES_QUERY:
            item_id = variables["id"]
            return {"node": {"id": item_id,
                              "fieldValues": connection(self.later_values.get(item_id, []))}}, None
        raise AssertionError("Unexpected Project query")

    @staticmethod
    def _project(*, fields=None, items=None):
        return {"id": gp.EXPECTED_PROJECT_ID, "number": 4,
                "owner": {"id": gp.EXPECTED_PROJECT_OWNER_ID, "login": "Just-Simple0"},
                "fields": fields or connection(), "items": items or connection([], total=0)}


def project_item(item_id, *, archived=False, option_id=None, field_values=None,
                 more=False, cursor=None):
    if field_values is None:
        field_values = ([{"__typename": "ProjectV2ItemFieldSingleSelectValue",
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
                        "field": {"id": STATUS_FIELD},
                        "optionId": gp.EXPECTED_STATUS_OPTIONS["완료"], "name": "완료"}
        reader = ProjectReader([first_item], later_values={"PVTI_item": [status_value]})
        result = gp.fetch_project(reader, project_id=gp.EXPECTED_PROJECT_ID,
            owner_id=gp.EXPECTED_PROJECT_OWNER_ID, status_field_id=STATUS_FIELD,
            status_options=gp.EXPECTED_STATUS_OPTIONS, repository_node_id=REPO_NODE)
        self.assertEqual(result["items"][41]["status_option_id"], gp.EXPECTED_STATUS_OPTIONS["완료"])
        self.assertIn("ProjectV2IterationField", gp.PROJECT_FIELDS_QUERY)
        self.assertTrue(any(query == gp.ITEM_FIELD_VALUES_QUERY for query, _ in reader.queries))

    def test_project_queries_select_union_field_ids_through_concrete_fragments(self):
        for query in (gp.PROJECT_ITEMS_QUERY, gp.ITEM_FIELD_VALUES_QUERY, gp.PROJECT_ITEM_QUERY):
            with self.subTest(query=query):
                self.assertIn("field { ... on ProjectV2SingleSelectField { id } }", query)
                self.assertNotIn("field { id }", query)

    def test_project_items_query_requests_archived_and_active_items(self):
        self.assertIn("archivedStates: [ARCHIVED, NOT_ARCHIVED]", gp.PROJECT_ITEMS_QUERY)

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
