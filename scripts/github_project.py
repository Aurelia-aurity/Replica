"""Read Replica GitHub facts and Project v2 state through fixed GraphQL endpoints."""
from __future__ import annotations

import email.utils
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPOSITORY = "Aurelia-aurity/Replica"
REPOSITORY_ID = 1392442366
PROJECT_NUMBER = 4
EXPECTED_PROJECT_ID = "PVT_kwHOBda_2c4BmKKV"
PROJECT_OWNER_LOGIN = "Just-Simple0"
EXPECTED_PROJECT_OWNER_ID = "U_kgDOBda_2Q"
EXPECTED_PM_USER_ID = 97959897
STATUS_NAMES = ("백로그", "준비 중", "진행 중", "검토 중", "완료")
EXPECTED_STATUS_OPTIONS = {
    "백로그": "f75ad846", "준비 중": "96beb7e0", "진행 중": "47fc9ee4",
    "검토 중": "ff336f39", "완료": "98236657",
}
GRAPHQL_URL = "https://api.github.com/graphql"


class SyncError(Exception):
    """Fixed diagnostics only; never include remote payloads or tokens."""


def require(condition, message="GitHub 원격 데이터 검증 실패"):
    if not condition:
        raise SyncError(message)


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "GitHub 시각에 timezone이 없습니다")
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, AttributeError):
        raise SyncError("GitHub 시각 형식 오류") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GraphQLClient:
    """GraphQL reads may retry; every mutation is sent at most once."""

    def __init__(self, token, *, opener=None, sleep=time.sleep):
        require(isinstance(token, str) and token, "GitHub 인증 설정이 없습니다")
        self.sleep = sleep
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2026-03-10",
        }

    def request(self, query, variables=None, *, mutation=False):
        require(isinstance(query, str) and query.strip().startswith(("query", "mutation")), "GraphQL 문서 오류")
        require(query.lstrip().startswith("mutation") == mutation, "GraphQL 읽기/쓰기 모드 불일치")
        data = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        attempts = 1 if mutation else 4
        for attempt in range(attempts):
            self.sleep(0.4)
            request = urllib.request.Request(GRAPHQL_URL, data=data, headers=self.headers, method="POST")
            try:
                with self.opener.open(request, timeout=30) as response:
                    body = response.read()
                    headers = getattr(response, "headers", {})
                    date_value = headers.get("Date") if hasattr(headers, "get") else None
                try:
                    decoded = json.loads(body)
                except (ValueError, UnicodeError):
                    raise SyncError("GitHub GraphQL 응답 형식 오류") from None
                require(isinstance(decoded, dict) and isinstance(decoded.get("data"), dict),
                        "GitHub GraphQL 응답 구조 오류")
                require(not decoded.get("errors"), "GitHub GraphQL 조회/변경 실패")
                server_time = None
                if date_value:
                    try:
                        server_time = email.utils.parsedate_to_datetime(date_value)
                        if server_time.tzinfo is None:
                            server_time = server_time.replace(tzinfo=timezone.utc)
                        server_time = server_time.astimezone(timezone.utc)
                    except (TypeError, ValueError, OverflowError):
                        raise SyncError("GitHub 서버 시각 형식 오류") from None
                return decoded["data"], server_time
            except urllib.error.HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                retryable = status in {429, 500, 502, 503, 504, 529}
                if mutation or not retryable or attempt == attempts - 1:
                    raise SyncError(f"GitHub GraphQL HTTP {status}; 실행 중단") from None
                try:
                    delay = int(retry_after) if retry_after is not None else 2 ** attempt + random.random()
                except ValueError:
                    raise SyncError("GitHub Retry-After 형식 오류") from None
                require(0 <= delay <= 60, "GitHub Retry-After 초과; 다음 실행에서 재시도")
                self.sleep(delay)
            except (urllib.error.URLError, TimeoutError, OSError):
                if mutation or attempt == attempts - 1:
                    raise SyncError("GitHub GraphQL 연결/응답 실패; 실행 중단") from None
                self.sleep(2 ** attempt + random.random())


class RESTClient:
    """Read-only compatibility client for the legacy /issues row identifiers."""

    def __init__(self, token, *, opener=None, sleep=time.sleep):
        require(isinstance(token, str) and token, "GITHUB_TOKEN 설정이 없습니다")
        self.base = "https://api.github.com"
        self.sleep = sleep
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.headers = {"Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2026-03-10"}

    def request(self, path):
        require(isinstance(path, str) and not path.startswith("//"),
                "GitHub REST path 오류")
        parsed = urllib.parse.urlsplit(path)
        root = f"/repos/{REPOSITORY}"
        path_parts = parsed.path.split("/")
        require(not parsed.scheme and not parsed.netloc and
                (parsed.path == root or parsed.path.startswith(root + "/")) and
                all(part not in {".", ".."} for part in path_parts),
                "GitHub REST path 오류")
        url = self.base + path
        require(urllib.parse.urlsplit(url).hostname == "api.github.com", "GitHub REST host 오류")
        for attempt in range(4):
            self.sleep(0.4)
            request = urllib.request.Request(url, headers=self.headers, method="GET")
            try:
                with self.opener.open(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if status not in {429, 500, 502, 503, 504, 529} or attempt == 3:
                    raise SyncError(f"GitHub REST HTTP {status}; 실행 중단") from None
                try:
                    delay = int(retry_after) if retry_after is not None else 2 ** attempt + random.random()
                except ValueError:
                    raise SyncError("GitHub REST Retry-After 형식 오류") from None
                require(0 <= delay <= 60, "GitHub REST Retry-After 초과; 다음 실행에서 재시도")
                self.sleep(delay)
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 3:
                    raise SyncError("GitHub REST 연결/응답 실패; 실행 중단") from None
                self.sleep(2 ** attempt + random.random())
            except (ValueError, UnicodeError):
                raise SyncError("GitHub REST 응답 형식 오류") from None


def fetch_legacy_issue_ids(rest_client, issues, pulls):
    """Map GraphQL facts to the durable IDs returned by REST /issues.

    PullRequest.databaseId is deliberately not used as a Notion key: GitHub's
    legacy /issues representation has a distinct Issue ID for PR rows.
    """
    repo = rest_client.request(f"/repos/{REPOSITORY}")
    require(isinstance(repo, dict) and repo.get("id") == REPOSITORY_ID and
            repo.get("full_name", "").casefold() == REPOSITORY.casefold(),
            "GitHub REST 저장소 식별자 불일치")
    legacy_by_number, legacy_ids = {}, set()
    page = 1
    while True:
        rows = rest_client.request(
            f"/repos/{REPOSITORY}/issues?state=all&sort=created&direction=asc&per_page=100&page={page}"
        )
        require(isinstance(rows, list), "GitHub legacy /issues 페이지 형식 오류")
        for row in rows:
            require(isinstance(row, dict), "GitHub legacy /issues 행 형식 오류")
            number = _validate_positive(row.get("number"), "GitHub legacy Issue/PR 번호 오류")
            legacy_id = _validate_positive(row.get("id"), "GitHub legacy Issue/PR ID 오류")
            node_id = row.get("node_id")
            require(isinstance(node_id, str) and node_id, "GitHub legacy Issue/PR node ID 누락")
            require(row.get("state") in {"open", "closed"}, "GitHub legacy Issue/PR state 오류")
            require(number not in legacy_by_number and legacy_id not in legacy_ids,
                    "GitHub legacy /issues 중복 번호/ID")
            is_pr = "pull_request" in row
            legacy_by_number[number] = {"id": legacy_id, "node_id": node_id,
                                        "state": row["state"], "is_pr": is_pr}
            legacy_ids.add(legacy_id)
        if len(rows) < 100:
            break
        page += 1
        require(page < 100000, "GitHub legacy /issues 페이지 상한 초과")

    graph_by_number = {}
    for kind, rows in (("Issue", issues.values()), ("PR", pulls.values())):
        for row in rows:
            number = row["number"]
            require(number not in graph_by_number, "GraphQL Issue/PR 번호 중복")
            graph_by_number[number] = (kind, row)
    require(set(legacy_by_number) == set(graph_by_number),
            "GraphQL과 legacy /issues 전체 번호 집합 불일치")

    for number, (kind, row) in graph_by_number.items():
        legacy = legacy_by_number[number]
        expected_rest_state = "open" if row["state"] == "OPEN" else "closed"
        require(legacy["node_id"] == row["id"] and legacy["is_pr"] == (kind == "PR") and
                legacy["state"] == expected_rest_state,
                "GraphQL과 legacy /issues 행 식별자/state 불일치")
        if kind == "Issue":
            require(legacy["id"] == row["databaseId"],
                    "Issue GraphQL databaseId와 legacy /issues ID 불일치")
        else:
            detail = rest_client.request(f"/repos/{REPOSITORY}/pulls/{number}")
            require(isinstance(detail, dict) and detail.get("number") == number and
                    detail.get("node_id") == row["id"] and detail.get("id") == row["databaseId"],
                    "PR REST /pulls와 GraphQL ID 관계 불일치")
            require(detail.get("state") == legacy["state"] and
                    type(detail.get("draft")) is bool and type(detail.get("merged")) is bool,
                    "PR REST /pulls state/draft/merged 형식 불일치")
            base = detail.get("base")
            base_repo = base.get("repo") if isinstance(base, dict) else None
            require(isinstance(base_repo, dict) and base_repo.get("id") == REPOSITORY_ID,
                    "PR REST base repository 불일치")
            graph_base = row.get("baseRepository")
            require(isinstance(base, dict) and base.get("ref") == row.get("baseRefName") and
                    isinstance(graph_base, dict) and
                    graph_base.get("databaseId") == base_repo.get("id") and
                    graph_base.get("id") == base_repo.get("node_id"),
                    "PR REST/GraphQL base repository·branch 불일치")
            require((detail["state"] == "open") == (row["state"] == "OPEN") and
                    detail["draft"] == row["isDraft"] and
                    detail["merged"] == (row["state"] == "MERGED") and
                    (not detail["merged"] or row.get("mergedAt") is not None),
                    "PR REST와 GraphQL 상태 불일치")
        row["legacy_issue_id"] = legacy["id"]
    return legacy_by_number


def read_server_time(client):
    data, server_time = client.request("query { viewer { id login } }")
    require(isinstance(data.get("viewer"), dict), "GitHub viewer 조회 실패")
    require(server_time is not None, "GitHub 응답에 검증 가능한 Date가 없습니다")
    return server_time


def _page_info(connection):
    require(isinstance(connection, dict) and isinstance(connection.get("nodes"), list),
            "GitHub 페이지 응답 구조 오류")
    info = connection.get("pageInfo")
    require(isinstance(info, dict) and type(info.get("hasNextPage")) is bool,
            "GitHub pageInfo 누락")
    if info["hasNextPage"]:
        require(isinstance(info.get("endCursor"), str) and info["endCursor"], "GitHub cursor 누락")
    return info


def _collect_connection(client, query_template, node_id, initial, *, label):
    result = list(initial["nodes"])
    info = _page_info(initial)
    seen = set()
    cursor = info.get("endCursor")
    while info["hasNextPage"]:
        require(cursor not in seen, f"{label} cursor 반복")
        seen.add(cursor)
        data, _ = client.request(query_template, {"id": node_id, "after": cursor})
        node = data.get("node")
        require(isinstance(node, dict), f"{label} 대상 누락")
        connection = node.get("connection")
        require(isinstance(connection, dict), f"{label} 연결 누락")
        result.extend(connection["nodes"])
        info = _page_info(connection)
        cursor = info.get("endCursor")
    ids = [row.get("id") or row.get("login") or row.get("name") for row in result]
    require(all(isinstance(value, str) and value for value in ids) and len(ids) == len(set(ids)),
            f"{label} 중복/ID 오류")
    return result


def _nested_query(kind, field, selection, extra_args=""):
    return f"""query($id: ID!, $after: String) {{
      node(id: $id) {{ ... on {kind} {{ connection: {field}(first: 100, after: $after{extra_args}) {{
        nodes {{ {selection} }} pageInfo {{ hasNextPage endCursor }}
      }} }} }}
    }}"""


ISSUES_QUERY = """query($after: String) {
  repository(owner: "Aurelia-aurity", name: "Replica") {
    id databaseId nameWithOwner owner { id login }
    issues(first: 100, after: $after, states: [OPEN, CLOSED], orderBy: {field: CREATED_AT, direction: ASC}) {
      nodes {
        id databaseId number title url createdAt updatedAt closedAt state stateReason
        author { login }
        assignees(first: 100) { nodes { login } pageInfo { hasNextPage endCursor } }
        labels(first: 100) { nodes { name } pageInfo { hasNextPage endCursor } }
        duplicateOf { id databaseId number url repository { id } }
        closedByPullRequestsReferences(first: 100, includeClosedPrs: false,
                                       orderByState: false, userLinkedOnly: false, excludeUserLinked: false) {
          nodes { id databaseId number state isDraft baseRefName baseRepository { id } repository { id } url }
          pageInfo { hasNextPage endCursor }
        }
        timelineItems(first: 100, itemTypes: [REOPENED_EVENT]) {
          nodes { ... on ReopenedEvent { id createdAt stateReason } }
          pageInfo { hasNextPage endCursor }
        }
      }
      pageInfo { hasNextPage endCursor }
      totalCount
    }
  }
}"""

PULLS_QUERY = """query($after: String) {
  repository(owner: "Aurelia-aurity", name: "Replica") {
    id databaseId nameWithOwner owner { id login }
    pullRequests(first: 100, after: $after, states: [OPEN, CLOSED, MERGED], orderBy: {field: CREATED_AT, direction: ASC}) {
      nodes {
        id databaseId number title url createdAt updatedAt state isDraft mergedAt
        baseRefName baseRepository { id databaseId nameWithOwner }
        author { login }
        assignees(first: 100) { nodes { login } pageInfo { hasNextPage endCursor } }
        labels(first: 100) { nodes { name } pageInfo { hasNextPage endCursor } }
        closingIssuesReferences(first: 100) {
          nodes { id databaseId number repository { id } }
          pageInfo { hasNextPage endCursor }
        }
      }
      pageInfo { hasNextPage endCursor }
      totalCount
    }
  }
}"""


def _repository(data):
    repo = data.get("repository")
    require(isinstance(repo, dict), "GitHub 저장소 조회 실패")
    require(repo.get("databaseId") == REPOSITORY_ID and
            repo.get("nameWithOwner", "").casefold() == REPOSITORY.casefold(),
            "동기화 저장소 식별자 불일치")
    owner = repo.get("owner")
    require(isinstance(owner, dict) and owner.get("login", "").casefold() == "aurelia-aurity",
            "동기화 저장소 소유자 불일치")
    require(isinstance(repo.get("id"), str) and repo["id"], "저장소 node ID 누락")
    return repo


def _all_repository_rows(client, query, name, expected_repo_id=None):
    rows, cursor, seen = [], None, set()
    total = None
    observed_repo_id = expected_repo_id
    while True:
        data, _ = client.request(query, {"after": cursor})
        repo = _repository(data)
        if observed_repo_id is None:
            observed_repo_id = repo["id"]
        require(repo["id"] == observed_repo_id, "저장소 node ID가 실행 중 변경됨")
        connection = repo.get(name)
        info = _page_info(connection)
        if total is None:
            total = connection.get("totalCount")
            require(type(total) is int and total >= 0, "GitHub totalCount 오류")
        rows.extend(connection["nodes"])
        if not info["hasNextPage"]:
            require(len(rows) == total, "GitHub 페이지 수와 totalCount 불일치")
            return rows, observed_repo_id
        cursor = info.get("endCursor")
        require(cursor not in seen, "GitHub cursor 반복")
        seen.add(cursor)


def _read_issue_aux(client, issue):
    for field, key, selection, label in (
        ("assignees", "assignees", "login", "assignee"),
        ("labels", "labels", "name", "label"),
    ):
        initial = issue.get(key)
        require(isinstance(initial, dict), f"GitHub {label} 연결 누락")
        selection_text = f"{selection}"
        query = _nested_query("Issue", field, selection_text)
        values = _collect_connection(client, query, issue["id"], initial, label=label)
        issue[key] = {"nodes": values, "pageInfo": {"hasNextPage": False, "endCursor": None}}
    timeline = issue.get("timelineItems")
    require(isinstance(timeline, dict), "재오픈 기록 연결 누락")
    timeline_query = _nested_query(
        "Issue", "timelineItems", "... on ReopenedEvent { id createdAt stateReason }",
        ", itemTypes: [REOPENED_EVENT]",
    )
    issue["reopens"] = _collect_connection(client, timeline_query, issue["id"], timeline, label="reopen")
    linked = issue.get("closedByPullRequestsReferences")
    require(isinstance(linked, dict), "Issue-PR 종료 연결 조회 누락")
    linked_query = _nested_query(
        "Issue", "closedByPullRequestsReferences",
        "id databaseId number state isDraft baseRefName baseRepository { id } repository { id } url",
        ", includeClosedPrs: false, orderByState: false, userLinkedOnly: false, excludeUserLinked: false",
    )
    issue["linked_prs"] = _collect_connection(client, linked_query, issue["id"], linked,
                                              label="Issue-PR 종료 연결")
    return issue


ISSUE_DETAIL_QUERY = """query($id: ID!) {
  node(id: $id) {
    ... on Issue {
      id databaseId number title url createdAt updatedAt closedAt state stateReason repository { id databaseId nameWithOwner }
      author { login }
      assignees(first: 100) { nodes { login } pageInfo { hasNextPage endCursor } }
      labels(first: 100) { nodes { name } pageInfo { hasNextPage endCursor } }
      duplicateOf { id databaseId number url repository { id } }
      closedByPullRequestsReferences(first: 100, includeClosedPrs: false,
                                     orderByState: false, userLinkedOnly: false, excludeUserLinked: false) {
        nodes { id databaseId number state isDraft baseRefName baseRepository { id } repository { id } url }
        pageInfo { hasNextPage endCursor }
      }
      timelineItems(first: 100, itemTypes: [REOPENED_EVENT]) {
        nodes { ... on ReopenedEvent { id createdAt stateReason } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""


def fetch_issue_detail(client, node_id):
    data, _ = client.request(ISSUE_DETAIL_QUERY, {"id": node_id})
    issue = data.get("node")
    require(isinstance(issue, dict) and issue.get("id") == node_id, "Issue 최신 사실 재조회 실패")
    repo = issue.get("repository")
    require(isinstance(repo, dict) and repo.get("databaseId") == REPOSITORY_ID and
            repo.get("nameWithOwner", "").casefold() == REPOSITORY.casefold(),
            "Issue 최신 저장소 식별자 불일치")
    _validate_positive(issue.get("databaseId"), "GitHub Issue ID 오류")
    _validate_positive(issue.get("number"), "GitHub Issue 번호 오류")
    require(issue.get("state") in {"OPEN", "CLOSED"}, "GitHub Issue state 오류")
    parse_time(issue.get("createdAt")); parse_time(issue.get("updatedAt"))
    if issue.get("closedAt") is not None:
        parse_time(issue["closedAt"])
    return _read_issue_aux(client, issue)


def _read_pull_aux(client, pull):
    for field, key, selection, label in (
        ("assignees", "assignees", "login", "PR assignee"),
        ("labels", "labels", "name", "PR label"),
        ("closingIssuesReferences", "closingIssuesReferences",
         "id databaseId number repository { id }", "PR closure reference"),
    ):
        initial = pull.get(key)
        require(isinstance(initial, dict), f"GitHub {label} 연결 누락")
        query = _nested_query("PullRequest", field, selection)
        values = _collect_connection(client, query, pull["id"], initial, label=label)
        pull[key] = {"nodes": values, "pageInfo": {"hasNextPage": False, "endCursor": None}}
    return pull


def _validate_positive(value, message):
    require(type(value) is int and value > 0, message)
    return value


def fetch_repository_facts(client, rest_client=None):
    """Return a complete, validated Issue/PR snapshot and GitHub repo node ID."""
    require(rest_client is not None, "legacy /issues ID 매핑용 GITHUB_TOKEN REST client가 필요합니다")
    # This first response also supplies the HTTP Date cutoff before source scanning.
    data, server_time = client.request("query { viewer { id login } }")
    require(isinstance(data.get("viewer"), dict) and server_time is not None,
            "GitHub 첫 응답의 서버 시각 검증 실패")
    issue_rows, repo_node_id = _all_repository_rows(client, ISSUES_QUERY, "issues")
    pull_rows, pulls_repo_node_id = _all_repository_rows(client, PULLS_QUERY, "pullRequests", repo_node_id)
    require(pulls_repo_node_id == repo_node_id, "Issue/PR 저장소 node ID 불일치")
    issues, pulls = {}, {}
    for issue in issue_rows:
        issue_id = _validate_positive(issue.get("databaseId"), "GitHub Issue ID 오류")
        number = _validate_positive(issue.get("number"), "GitHub Issue 번호 오류")
        require(isinstance(issue.get("id"), str) and issue["id"], "GitHub Issue node ID 누락")
        require(issue.get("state") in {"OPEN", "CLOSED"}, "GitHub Issue state 오류")
        parse_time(issue.get("createdAt")); parse_time(issue.get("updatedAt"))
        if issue.get("closedAt") is not None:
            parse_time(issue["closedAt"])
        _read_issue_aux(client, issue)
        require(issue_id not in issues, "GitHub Issue ID 중복")
        issues[issue_id] = issue
    for pull in pull_rows:
        pull_id = _validate_positive(pull.get("databaseId"), "GitHub PR ID 오류")
        number = _validate_positive(pull.get("number"), "GitHub PR 번호 오류")
        require(isinstance(pull.get("id"), str) and pull["id"], "GitHub PR node ID 누락")
        require(pull.get("state") in {"OPEN", "CLOSED", "MERGED"}, "GitHub PR state 오류")
        require(type(pull.get("isDraft")) is bool, "GitHub PR draft 값 오류")
        parse_time(pull.get("createdAt")); parse_time(pull.get("updatedAt"))
        _read_pull_aux(client, pull)
        require(pull_id not in pulls, "GitHub PR ID 중복")
        pulls[pull_id] = pull
    by_number = {}
    for kind, rows in (("Issue", issues.values()), ("PR", pulls.values())):
        for row in rows:
            number = row["number"]
            require(number not in by_number, "GitHub Issue/PR 번호 중복")
            by_number[number] = kind
    legacy_by_number = fetch_legacy_issue_ids(rest_client, issues, pulls)
    canonical_pulls = {}
    for pull in pulls.values():
        legacy_id = pull["legacy_issue_id"]
        require(legacy_id not in canonical_pulls, "legacy PR row ID 중복")
        canonical_pulls[legacy_id] = pull
    return {"server_time": server_time, "repository_node_id": repo_node_id,
            "issues": issues, "pulls": canonical_pulls,
            "legacy_by_number": legacy_by_number}


PROJECT_FIELDS_QUERY = """query($id: ID!, $after: String) {
  node(id: $id) {
    ... on ProjectV2 {
      id number
      owner { id ... on User { login } ... on Organization { login } }
      fields(first: 100, after: $after) {
        nodes { __typename ... on ProjectV2SingleSelectField { id name options { id name } }
                ... on ProjectV2IterationField { id name }
                ... on ProjectV2Field { id name } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""

PROJECT_ITEMS_QUERY = """query($id: ID!, $after: String) {
  node(id: $id) {
    ... on ProjectV2 {
      id number
      owner { id ... on User { login } ... on Organization { login } }
      items(first: 100, after: $after, archivedStates: [ARCHIVED, NOT_ARCHIVED]) {
        nodes {
          id isArchived
          content { __typename ... on Issue { id databaseId number repository { id databaseId nameWithOwner } }
                    ... on PullRequest { id databaseId number repository { id databaseId nameWithOwner } } }
          fieldValues(first: 100) {
            nodes { __typename ... on ProjectV2ItemFieldSingleSelectValue {
              field { ... on ProjectV2SingleSelectField { id } } optionId name
            } }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
        totalCount
      }
    }
  }
}"""

ITEM_FIELD_VALUES_QUERY = """query($id: ID!, $after: String) {
  node(id: $id) {
    ... on ProjectV2Item {
      id fieldValues(first: 100, after: $after) {
        nodes { __typename ... on ProjectV2ItemFieldSingleSelectValue {
          field { ... on ProjectV2SingleSelectField { id } } optionId name
        } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""


def _all_item_field_values(client, item_id, initial):
    require(isinstance(initial, dict) and isinstance(initial.get("nodes"), list),
            "Project item fieldValues 형식 오류")
    values = list(initial["nodes"])
    info = _page_info(initial)
    seen, cursor = set(), info.get("endCursor")
    while info["hasNextPage"]:
        require(cursor not in seen, "Project item fieldValues cursor 반복")
        seen.add(cursor)
        data, _ = client.request(ITEM_FIELD_VALUES_QUERY, {"id": item_id, "after": cursor})
        item = data.get("node")
        require(isinstance(item, dict) and item.get("id") == item_id,
                "Project item fieldValues 대상 불일치")
        connection = item.get("fieldValues")
        require(isinstance(connection, dict), "Project item fieldValues 페이지 누락")
        values.extend(connection.get("nodes", []))
        info = _page_info(connection)
        cursor = info.get("endCursor")
    return values


def parse_status_options(raw):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise SyncError("PROJECT_STATUS_OPTIONS JSON 형식 오류") from None
    require(isinstance(value, dict) and set(value) == set(STATUS_NAMES),
            "PROJECT_STATUS_OPTIONS는 5개 상태 이름만 포함해야 합니다")
    require(value == EXPECTED_STATUS_OPTIONS, "PROJECT_STATUS_OPTIONS가 확인된 Replica option ID와 다릅니다")
    require(all(isinstance(v, str) and v.strip() for v in value.values()),
            "Project Status option ID 누락")
    require(len(set(value.values())) == len(value), "Project Status option ID 중복")
    return value


def fetch_project(client, *, project_id, owner_id, status_field_id, status_options,
                  repository_node_id):
    require(project_id == EXPECTED_PROJECT_ID, "PROJECT_ID가 확인된 Replica Project와 다릅니다")
    require(owner_id == EXPECTED_PROJECT_OWNER_ID, "PROJECT_OWNER_ID가 확인된 PM 계정과 다릅니다")
    fields, cursor, seen = [], None, set()
    project_meta = None
    while True:
        data, _ = client.request(PROJECT_FIELDS_QUERY, {"id": project_id, "after": cursor})
        project = data.get("node")
        require(isinstance(project, dict) and project.get("id") == project_id,
                "Project ID 조회 불일치")
        require(project.get("number") == PROJECT_NUMBER, "Project 번호가 4가 아닙니다")
        owner = project.get("owner")
        require(isinstance(owner, dict) and owner.get("id") == owner_id == EXPECTED_PROJECT_OWNER_ID and
                owner.get("login", "").casefold() == PROJECT_OWNER_LOGIN.casefold(),
                "Project 소유자 ID/login 불일치")
        connection = project.get("fields")
        info = _page_info(connection)
        fields.extend(connection["nodes"])
        project_meta = project
        if not info["hasNextPage"]:
            break
        cursor = info.get("endCursor")
        require(cursor not in seen, "Project field cursor 반복")
        seen.add(cursor)
    field_ids = [field.get("id") for field in fields]
    require(all(isinstance(value, str) and value for value in field_ids) and
            len(field_ids) == len(set(field_ids)), "Project field 중복/ID 오류")
    status_fields = [f for f in fields if f.get("id") == status_field_id]
    require(len(status_fields) == 1 and status_fields[0].get("__typename") == "ProjectV2SingleSelectField",
            "PROJECT_STATUS_FIELD_ID 타입/존재 검증 실패")
    require(status_fields[0].get("name") == "Status", "PROJECT_STATUS_FIELD_ID 이름이 Status가 아닙니다")
    actual_options = {o.get("name"): o.get("id") for o in status_fields[0].get("options", [])}
    require(actual_options == status_options, "Project Status option 이름/ID 불일치")

    rows, cursor, seen = [], None, set()
    total = None
    while True:
        data, _ = client.request(PROJECT_ITEMS_QUERY, {"id": project_id, "after": cursor})
        project = data.get("node")
        require(isinstance(project, dict) and project.get("id") == project_id,
                "Project ID 조회 불일치")
        require(project.get("number") == PROJECT_NUMBER and
                project.get("owner", {}).get("id") == owner_id,
                "Project owner/number가 페이지 사이 변경됨")
        connection = project.get("items")
        info = _page_info(connection)
        if total is None:
            total = connection.get("totalCount")
            require(type(total) is int and total >= 0, "Project item totalCount 오류")
        rows.extend(connection["nodes"])
        if not info["hasNextPage"]:
            require(len(rows) == total, "Project item page count 불일치")
            break
        cursor = info.get("endCursor")
        require(cursor not in seen, "Project item cursor 반복")
        seen.add(cursor)
    issue_items, archived_issue_items = {}, {}
    seen_item_ids = set()
    for item in rows:
        require(isinstance(item.get("id"), str) and item["id"], "Project item ID 누락")
        require(item["id"] not in seen_item_ids, "Project item ID가 전체 페이지에서 중복됩니다")
        seen_item_ids.add(item["id"])
        require(type(item.get("isArchived")) is bool, "Project item archived 값 누락")
        field_values = item.get("fieldValues")
        require(isinstance(field_values, dict), "Project fieldValues 조회 오류")
        all_values = _all_item_field_values(client, item["id"], field_values)
        content = item.get("content")
        if content is None:
            continue
        kind = content.get("__typename")
        if kind == "PullRequest":
            continue
        if kind not in {"Issue", "DraftIssue"}:
            require(kind == "DraftIssue", "알 수 없는 Project content type")
            continue
        repo = content.get("repository")
        if not isinstance(repo, dict) or repo.get("id") != repository_node_id:
            continue
        if kind == "DraftIssue":
            continue
        issue_id = _validate_positive(content.get("databaseId"), "Project Issue DB ID 오류")
        require(isinstance(content.get("id"), str) and content["id"], "Project Issue content node ID 누락")
        if item["isArchived"]:
            archived_issue_items.setdefault(issue_id, []).append(
                {"id": item["id"], "content_id": content["id"], "is_archived": True})
            continue
        value = None
        status_count = 0
        for field_value in all_values:
            if field_value.get("field", {}).get("id") == status_field_id:
                require(field_value.get("__typename") == "ProjectV2ItemFieldSingleSelectValue",
                        "Project Status value 형식 오류")
                status_count += 1
                value = field_value.get("optionId")
                require(value in status_options.values(), "Project에 미등록 Status option ID")
        require(status_count <= 1, "Project item Status 값이 중복 조회되었습니다")
        require(issue_id not in issue_items, "동일 Issue의 Project item이 중복됩니다")
        issue_items[issue_id] = {"id": item["id"], "status_option_id": value,
                                 "is_archived": False, "content_id": content["id"]}
    return {"project_id": project_id, "owner_id": owner_id, "status_field_id": status_field_id,
            "status_options": dict(status_options), "items": issue_items,
            "archived_items": archived_issue_items,
            "item_count": len(rows), "project_node": project_meta}


PROJECT_ITEM_QUERY = """query($id: ID!) {
  node(id: $id) {
    ... on ProjectV2Item {
      id isArchived project { id number owner { id } }
      content { __typename ... on Issue { id databaseId repository { id databaseId nameWithOwner } } }
      fieldValues(first: 100) {
        nodes { __typename ... on ProjectV2ItemFieldSingleSelectValue {
          field { ... on ProjectV2SingleSelectField { id } } optionId name
        } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""


def fetch_project_item(client, project_id, item_id, status_field_id, *, allow_archived=False):
    data, _ = client.request(PROJECT_ITEM_QUERY, {"id": item_id})
    item = data.get("node")
    require(isinstance(item, dict) and item.get("id") == item_id and
            type(item.get("isArchived")) is bool and (allow_archived or not item["isArchived"]),
            "Project item readback ID/활성 상태 불일치")
    project = item.get("project")
    require(isinstance(project, dict) and project.get("id") == project_id and
            project.get("number") == PROJECT_NUMBER and
            project.get("owner", {}).get("id") == EXPECTED_PROJECT_OWNER_ID,
            "Project item 소속 Project 불일치")
    content = item.get("content")
    require(isinstance(content, dict) and content.get("__typename") == "Issue" and
            isinstance(content.get("id"), str), "Project item content가 Issue가 아닙니다")
    repo = content.get("repository")
    require(isinstance(repo, dict) and repo.get("databaseId") == REPOSITORY_ID and
            repo.get("nameWithOwner", "").casefold() == REPOSITORY.casefold(),
            "Project item Issue 저장소 불일치")
    if item["isArchived"]:
        return {"id": item_id, "content_id": content["id"],
                "status_option_id": None, "is_archived": True}
    connection = item.get("fieldValues")
    require(isinstance(connection, dict), "Project item fieldValues 연결 누락")
    all_values = _all_item_field_values(client, item_id, connection)
    status = None
    status_count = 0
    for value in all_values:
        if value.get("field", {}).get("id") == status_field_id:
            require(value.get("__typename") == "ProjectV2ItemFieldSingleSelectValue",
                    "Project item Status value 타입 오류")
            status_count += 1
            status = value.get("optionId")
            require(status is None or status in EXPECTED_STATUS_OPTIONS.values(),
                    "Project item Status option ID 미등록")
    require(status_count <= 1, "Project item Status 값이 중복 조회되었습니다")
    return {"id": item["id"], "content_id": content["id"],
            "status_option_id": status, "is_archived": False}


SET_STATUS_MUTATION = """mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field,
    value: {singleSelectOptionId: $option}}) { projectV2Item { id } }
}"""

CLEAR_STATUS_MUTATION = """mutation($project: ID!, $item: ID!, $field: ID!) {
  clearProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field}) {
    projectV2Item { id }
  }
}"""

ADD_ISSUE_MUTATION = """mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } }
}"""


def set_project_status(client, project_id, item_id, field_id, option_id):
    data, _ = client.request(SET_STATUS_MUTATION,
                             {"project": project_id, "item": item_id, "field": field_id,
                              "option": option_id}, mutation=True)
    require(isinstance(data.get("updateProjectV2ItemFieldValue"), dict), "Project Status 변경 응답 누락")


def clear_project_status(client, project_id, item_id, field_id):
    data, _ = client.request(CLEAR_STATUS_MUTATION,
                             {"project": project_id, "item": item_id, "field": field_id}, mutation=True)
    require(isinstance(data.get("clearProjectV2ItemFieldValue"), dict), "Project Status 비우기 응답 누락")


def add_project_issue(client, project_id, content_id):
    data, _ = client.request(ADD_ISSUE_MUTATION,
                             {"project": project_id, "content": content_id}, mutation=True)
    require(isinstance(data.get("addProjectV2ItemById"), dict), "Project Issue 추가 응답 누락")


def resolve_actor(client, login):
    require(isinstance(login, str) and login and len(login) <= 39, "GitHub actor 형식 오류")
    query = "query($login: String!) { user(login: $login) { databaseId login } }"
    data, _ = client.request(query, {"login": login})
    user = data.get("user")
    require(isinstance(user, dict) and user.get("login", "").casefold() == login.casefold(),
            "workflow actor를 확인할 수 없습니다")
    user_id = _validate_positive(user.get("databaseId"), "workflow actor numeric ID 오류")
    require(user_id == EXPECTED_PM_USER_ID, "보류 재개는 지정 PM actor만 수행할 수 있습니다")
    return user_id
