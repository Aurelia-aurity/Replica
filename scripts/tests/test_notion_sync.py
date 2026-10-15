import copy
import importlib.util
import io
import json
import sys
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

MODULE = Path(__file__).resolve().parents[1] / "notion_sync.py"
spec = importlib.util.spec_from_file_location("notion_sync", MODULE)
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sync_engine as se
SOURCE = "00000000-0000-4000-8000-000000000001"
CONTROL = "00000000-0000-4000-8000-000000000002"
DATE = "2026-10-04T00:00:00Z"


def issue(number=1, **overrides):
    value = dict(id=1000 + number, number=number, title="작업", state="open", updated_at=DATE,
                 user={"login": "author"}, assignees=[{"login": "b"}, {"login": "a"}],
                 labels=[{"name": "speech,AI"}, {"name": "bug"}])
    value.update(overrides)
    return value


def page(page_id, properties):
    defaults = {name: {kind: [] if kind in {"title", "rich_text"} else None}
                for name, kind in s.SCHEMA.items()}
    return dict(id=page_id, parent={"data_source_id": SOURCE}, archived=False, in_trash=False,
                properties={**defaults, **copy.deepcopy(properties)}, body="팀 회의 메모")


def original_issue_properties(*, title="작업", state="Open"):
    return {
        "제목": s.text_property(title, "title"),
        "종류": {"select": {"name": "Issue"}},
        "번호": {"number": 1},
        "GitHub URL": {"url": f"https://github.com/{s.REPOSITORY}/issues/1"},
        "GitHub 상태": {"select": {"name": state}},
        "작성자": s.text_property("author"),
        "담당자": s.text_property(s.array_text(["b", "a"])),
        "라벨": s.text_property(s.array_text(["speech,AI", "bug"])),
        "GitHub 수정": {"date": {"start": DATE}},
        "동기화 키": s.text_property(s.key_for(s.REPOSITORY_ID, 1001)),
    }


class Github:
    def __init__(self, rows=None):
        self.rows = [issue()] if rows is None else rows
        self.details, self.fail_on, self.calls = {}, None, []

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append(path)
        if self.fail_on and self.fail_on in path:
            raise s.SyncError("fixture read failure")
        if path.endswith(s.REPOSITORY):
            return {"id": s.REPOSITORY_ID, "full_name": s.REPOSITORY}
        if "/pulls/" in path:
            return copy.deepcopy(self.details[int(path.rsplit("/", 1)[1])])
        n = int(path.rsplit("page=", 1)[1])
        return copy.deepcopy(self.rows[(n - 1) * 100:n * 100])


class Notion:
    def __init__(self):
        self.pages = {CONTROL: page(CONTROL, {"동기화 키": s.text_property(s.CONTROL_KEY),
                         "종류": {"select": {"name": "Sync"}}, "Pending create": s.text_property("")})}
        self.writes, self.calls = [], []
        self.read_snapshots = []
        self.create_error = None
        self.create_committed = False
        self.create_response_transform = None
        self.fail_query_cursor = False
        self.incomplete = False
        self.get_created_failure = False
        self.readback_transform = None
        self.clear_failure = False
        self.page_size = 100

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(payload), kwargs))
        if method == "GET" and path.startswith("/data_sources/"):
            if path != f"/data_sources/{SOURCE}":
                raise s.SyncError("fixture rejected wrong data source target")
            props = {name: {"type": kind} for name, kind in s.SCHEMA.items()}
            for name, options in s.OPTIONS.items():
                props[name]["select"] = {"options": [{"name": v} for v in options]}
            return {"id": SOURCE, "properties": props}
        if method == "POST" and path == f"/data_sources/{SOURCE}/query":
            if self.fail_query_cursor and payload.get("start_cursor"):
                raise s.SyncError("fixture late query failure")
            rows = [p for p in self.pages.values() if s.is_archived(p) == payload["is_archived"]]
            offset = int(payload.get("start_cursor", "0"))
            more = offset + self.page_size < len(rows)
            return copy.deepcopy(dict(results=rows[offset:offset + self.page_size], has_more=more,
                next_cursor=str(offset + self.page_size) if more else None,
                request_status={"type": "incomplete" if self.incomplete else "complete"}))
        if method == "GET":
            if not path.startswith("/pages/"):
                raise s.SyncError("fixture rejected unexpected GET path")
            pid = path.rsplit("/", 1)[1]
            if self.get_created_failure and pid != CONTROL:
                raise s.SyncError("fixture created readback failure")
            if pid not in self.pages:
                raise s.SyncError("fixture rejected unknown page target")
            result = copy.deepcopy(self.pages[pid])
            if self.readback_transform:
                result = self.readback_transform(result, method, path)
            self.read_snapshots.append((path, copy.deepcopy(result)))
            return result
        if method not in {"PATCH", "POST"}:
            raise s.SyncError("fixture rejected unexpected method")
        self.writes.append((method, path, copy.deepcopy(payload), kwargs))
        if method == "PATCH":
            if not path.startswith("/pages/"):
                raise s.SyncError("fixture rejected unexpected PATCH path")
            pid = path.rsplit("/", 1)[1]
            if pid not in self.pages:
                raise s.SyncError("fixture rejected unknown PATCH page")
            if (self.pages[pid].get("parent", {}).get("data_source_id") != SOURCE or
                    s.is_archived(self.pages[pid])):
                raise s.SyncError("fixture rejected wrong or inactive PATCH page")
            self._validate_page_properties(payload.get("properties"))
            if self.clear_failure and payload["properties"].get("Pending create") == s.text_property(""):
                raise s.SyncError("fixture fence clear failure")
            self.pages[pid]["properties"].update(copy.deepcopy(payload["properties"]))
            return copy.deepcopy(self.pages[pid])
        if path != "/pages" or payload.get("parent") != {"data_source_id": SOURCE}:
            raise s.SyncError("fixture rejected wrong create path/parent")
        self._validate_page_properties(payload.get("properties"))
        pid = str(UUID(int=len(self.pages) + 10))
        if not self.create_error or self.create_committed:
            self.pages[pid] = page(pid, payload["properties"])
        if self.create_error:
            raise s.SyncError(self.create_error)
        response = {"id": pid}
        if self.create_response_transform:
            response = self.create_response_transform(response)
        return response

    @staticmethod
    def _validate_page_properties(properties):
        if not isinstance(properties, dict):
            raise s.SyncError("fixture rejected missing properties")
        for name, value in properties.items():
            kind = s.SCHEMA.get(name)
            if (kind is None or not isinstance(value, dict) or set(value) != {kind}):
                raise s.SyncError("fixture rejected unknown property or type")
            if kind == "select" and value[kind] is not None:
                selected = value[kind].get("name")
                if selected not in s.OPTIONS.get(name, set()):
                    raise s.SyncError("fixture rejected unknown select option")

    def pending(self):
        return s.read_text(self.pages[CONTROL], "Pending create")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.gh, self.no = Github(), Notion()

    def run_sync(self, **kwargs):
        return s.sync(self.gh, self.no, SOURCE, CONTROL, **kwargs)

    def existing(self, number=1):
        props = s.github_snapshot(Github([issue(number)]))[s.key_for(s.REPOSITORY_ID, 1000 + number)]
        pid = str(UUID(int=100 + number))
        self.no.pages[pid] = page(pid, props)
        return self.no.pages[pid]

    def test_first_run_and_rerun_preserve_manual_properties_and_body(self):
        self.assertEqual(self.run_sync()["created"], 1)
        row = next(p for pid, p in self.no.pages.items() if pid != CONTROL)
        create_payload = next(payload["properties"] for method, path, payload, _
                              in self.no.writes if method == "POST" and path == "/pages")
        self.assertEqual({name: create_payload[name] for name in original_issue_properties()},
                         original_issue_properties())
        self.assertEqual(row["parent"], {"data_source_id": SOURCE})
        self.assertEqual(row["properties"]["제목"], original_issue_properties()["제목"])
        row["properties"].update({"작업 상태": {"select": {"name": "진행 중"}},
          "일정": {"date": {"start": "2026-10-05", "end": "2026-10-06"}}, "메모": s.text_property("팀 메모")})
        manual = copy.deepcopy({k: row["properties"][k] for k in ["작업 상태", "일정", "메모"]})
        self.gh.rows[0]["title"] = "수정 작업"
        expected_updated = original_issue_properties(title="수정 작업")
        self.no.writes.clear()
        self.assertEqual(self.run_sync()["updated"], 1)
        self.assertEqual(len(self.no.pages), 2)
        self.assertEqual({k: row["properties"][k] for k in manual}, manual)
        self.assertEqual(row["body"], "팀 회의 메모")
        page_updates = [payload["properties"] for method, path, payload, _ in self.no.writes
                        if method == "PATCH" and path == f"/pages/{row['id']}"]
        self.assertEqual(len(page_updates), 1)
        self.assertEqual({name: page_updates[0][name] for name in expected_updated},
                         expected_updated)
        self.assertIn("동기화 시각", page_updates[0])
        self.assertTrue(any(path == f"/pages/{row['id']}" and
                            all(snapshot["properties"].get(name) == value
                                for name, value in page_updates[0].items())
                            for path, snapshot in self.no.read_snapshots),
                        "업데이트한 managed properties의 GET readback이 없습니다")
        for _, _, payload, _ in self.no.writes:
            self.assertFalse(set(manual) & set(payload["properties"]))
            self.assertNotIn("children", payload)

    def test_fake_binds_source_path_page_target_and_create_parent(self):
        notion = Notion()
        with self.assertRaises(s.SyncError):
            notion.request("GET", "/data_sources/00000000-0000-4000-8000-000000000099")
        with self.assertRaises(s.SyncError):
            notion.request("POST", "/data_sources/00000000-0000-4000-8000-000000000099/query",
                           {"is_archived": False})
        with self.assertRaises(s.SyncError):
            notion.request("GET", "/pages/00000000-0000-4000-8000-000000000099")
        with self.assertRaises(s.SyncError):
            notion.request("POST", "/pages", {"parent": {
                "data_source_id": "00000000-0000-4000-8000-000000000099"}, "properties": {}})
        wrong_parent_id = str(UUID(int=777))
        notion.pages[wrong_parent_id] = page(wrong_parent_id, {})
        notion.pages[wrong_parent_id]["parent"] = {
            "data_source_id": "00000000-0000-4000-8000-000000000099"}
        with self.assertRaises(s.SyncError):
            notion.request("PATCH", f"/pages/{wrong_parent_id}",
                           {"properties": {"제목": s.text_property("wrong", "title")}})
        self.assertEqual(len(notion.pages), 2)

        # A valid create whose immediate GET reports another parent must fail
        # before the durable create fence is cleared.
        notion = Notion()
        notion.readback_transform = lambda response, method, path: {
            **response, "parent": {"data_source_id": "00000000-0000-4000-8000-000000000099"}
        } if method == "GET" and path.startswith("/pages/") and path != f"/pages/{CONTROL}" else response
        with self.assertRaisesRegex(s.SyncError, "부모 데이터 소스"):
            s.sync(Github(), notion, SOURCE, CONTROL)
        self.assertTrue(notion.pending())
        self.assertEqual(sum(method == "POST" for method, _, _, _ in notion.writes), 1)

    def test_create_and_update_require_exact_page_and_managed_property_readback(self):
        cases = (("create", "returned_id"), ("create", "page_id"), ("create", "parent"),
                 ("create", "managed_title"), ("update", "page_id"),
                 ("update", "parent"), ("update", "managed_title"),
                 ("update", "managed_date"))
        for operation, corruption in cases:
            with self.subTest(operation=operation, corruption=corruption):
                notion = Notion()
                github = Github()
                if operation == "create" and corruption == "returned_id":
                    notion.create_response_transform = lambda _response: {
                        "id": str(UUID(int=997))}
                if operation == "update":
                    props = s.github_snapshot(github)[s.key_for(s.REPOSITORY_ID, 1001)]
                    existing_id = str(UUID(int=101))
                    notion.pages[existing_id] = page(existing_id, props)
                    github.rows[0]["title"] = "수정 작업"

                def corrupt_readback(response, method, path):
                    if method != "GET" or path == f"/pages/{CONTROL}":
                        return response
                    if operation == "create" and corruption == "returned_id":
                        return response
                    if corruption == "page_id":
                        response["id"] = str(UUID(int=999))
                    elif corruption == "parent":
                        response["parent"] = {"data_source_id": str(UUID(int=998))}
                    elif corruption == "managed_date":
                        response["properties"]["GitHub 수정"] = {
                            "date": {"start": "2026-10-07T00:00:00+00:00"}}
                    else:
                        response["properties"]["제목"] = s.text_property(
                            "GET response mismatch", "title")
                    return response

                notion.readback_transform = corrupt_readback
                with self.assertRaises(s.SyncError):
                    s.sync(github, notion, SOURCE, CONTROL)
                if operation == "create":
                    self.assertEqual(notion.pending(), s.key_for(s.REPOSITORY_ID, 1001))
                    self.assertEqual(sum(method == "POST" and path == "/pages"
                                         for method, path, _, _ in notion.writes), 1)
                else:
                    self.assertTrue(any(path == f"/pages/{existing_id}"
                                        for path, _ in notion.read_snapshots))
                    self.assertFalse(any(method == "PATCH" and path == f"/pages/{CONTROL}"
                                         for method, path, _, _ in notion.writes))

    def test_sync_engine_schema_has_independent_21_property_contract(self):
        expected = {
            "제목": "title", "종류": "select", "번호": "number", "GitHub URL": "url",
            "GitHub 상태": "select", "작성자": "rich_text", "담당자": "rich_text",
            "라벨": "rich_text", "GitHub 수정": "date", "동기화 키": "rich_text",
            "동기화 시각": "date", "Pending create": "rich_text", "작업 상태": "select",
            "일정": "date", "메모": "rich_text", "종료 사유": "select",
            "대표 이슈": "url", "확인 필요": "rich_text", "동기화 내부 상태": "rich_text",
            "요청 처리": "select", "요청 상태": "select",
        }
        expected_options = {
            "종류": {"Issue", "PR", "Sync"},
            "GitHub 상태": {"Open", "Closed", "Draft", "Merged"},
            "작업 상태": {"백로그", "준비 중", "진행 중", "검토 중", "완료"},
            "종료 사유": {"완료", "미계획", "중복", "확인 필요"},
            "요청 처리": {"반영 대기", "반영 완료", "요청 거절", "자동 확인 중", "PM 확인 필요"},
            "요청 상태": {"백로그", "준비 중", "진행 중", "검토 중", "완료"},
        }

        class SchemaClient:
            def __init__(self, mutate=None, response_id=SOURCE):
                self.mutate = mutate
                self.response_id = response_id
                self.calls = []

            def request(self, method, path):
                self.calls.append((method, path))
                if (method, path) != ("GET", f"/data_sources/{SOURCE}"):
                    raise s.SyncError("fixture rejected wrong schema request target")
                props = {name: {"type": kind} for name, kind in expected.items()}
                for name, options in expected_options.items():
                    props[name]["select"] = {"options": [{"name": value} for value in options]}
                if self.mutate:
                    self.mutate(props)
                return {"id": self.response_id, "properties": props}

        self.assertEqual(len(expected), 21)
        client = SchemaClient()
        schema = se._schema(client, SOURCE)
        self.assertEqual(client.calls, [("GET", f"/data_sources/{SOURCE}")])
        self.assertEqual({name: value["type"] for name, value in schema["properties"].items()},
                         expected)
        for name, options in expected_options.items():
            self.assertEqual({item["name"] for item in schema["properties"][name]["select"]["options"]},
                             options)
        for mutate in (
            lambda props: props.pop("요청 상태"),
            lambda props: props["요청 처리"].update(type="rich_text"),
            lambda props: props["작업 상태"]["select"]["options"].pop(),
            lambda props: props["종류"]["select"]["options"].append({"name": "unexpected"}),
        ):
            with self.subTest(mutate=mutate), self.assertRaises(se.SyncError):
                se._schema(SchemaClient(mutate), SOURCE)
        for wrong_id in ("00000000-0000-4000-8000-000000000099", None):
            with self.subTest(response_id=wrong_id), self.assertRaises(se.SyncError):
                se._schema(SchemaClient(response_id=wrong_id), SOURCE)
        with self.assertRaises(s.SyncError):
            SchemaClient().request("PATCH", f"/data_sources/{SOURCE}")

    def test_pr_state_precedence(self):
        for state, draft, merged, expected in [("open", False, False, "Open"), ("open", True, False, "Draft"),
             ("closed", True, False, "Closed"), ("closed", False, True, "Merged")]:
            with self.subTest(expected=expected):
                gh = Github([issue(pull_request={})])
                gh.details[1] = issue(state=state, draft=draft, merged=merged, base={"repo": {"id": s.REPOSITORY_ID}})
                props = next(iter(s.github_snapshot(gh).values()))
                self.assertEqual(props["GitHub 상태"]["select"]["name"], expected)

    def test_late_reads_have_zero_writes(self):
        self.gh.rows = [issue(n) for n in range(1, 101)]
        self.gh.fail_on = "page=2"
        with self.assertRaises(s.SyncError): self.run_sync()
        self.assertEqual(self.no.writes, [])
        self.gh = Github([issue(), issue(2, pull_request={})])
        self.gh.fail_on = "/pulls/2"
        with self.assertRaises(s.SyncError): self.run_sync()
        self.assertEqual(self.no.writes, [])
        self.gh = Github()
        self.existing()
        self.no.page_size, self.no.fail_query_cursor = 1, True
        with self.assertRaises(s.SyncError): self.run_sync()
        self.assertEqual(self.no.writes, [])

    def test_pagination_and_incomplete(self):
        self.gh.rows = [issue(n) for n in range(1, 102)]
        self.no.page_size = 1
        self.existing()
        self.assertEqual(self.run_sync(dry_run=True)["source_items"], 101)
        self.no.incomplete = True
        with self.assertRaises(s.SyncError): self.run_sync()
        self.assertEqual(self.no.writes, [])

    def test_duplicate_archived_and_invalid_keys_stop_before_write(self):
        row = self.existing()
        duplicate = copy.deepcopy(row)
        duplicate["id"] = str(UUID(int=999))
        self.no.pages[duplicate["id"]] = duplicate
        self.no.page_size = 1
        with self.assertRaises(s.SyncError): self.run_sync()
        self.assertEqual(self.no.writes, [])
        del self.no.pages[duplicate["id"]]
        row["archived"] = True
        with self.assertRaises(s.SyncError): self.run_sync()
        self.assertEqual(self.no.writes, [])
        row["archived"] = False
        row["properties"]["동기화 키"] = s.text_property("malformed")
        with self.assertRaises(s.SyncError): self.run_sync()
        self.assertEqual(self.no.writes, [])

    def test_control_binding_type_and_id(self):
        for change in [lambda p:p["parent"].update(data_source_id=str(UUID(int=777))),
                       lambda p:p.update(archived=True),
                       lambda p:p["properties"].update({"종류": {"select": {"name": "Issue"}}})]:
            self.no = Notion()
            change(self.no.pages[CONTROL])
            with self.assertRaises(s.SyncError): self.run_sync()
            self.assertEqual(self.no.writes, [])

    def test_newer_timestamp_preserved_equal_timestamp_repaired(self):
        row = self.existing()
        row["properties"]["GitHub 수정"]["date"]["start"] = "2026-10-06T00:00:00+00:00"
        self.assertEqual(self.run_sync()["newer_skipped"], 1)
        self.assertFalse(any(path.endswith(row["id"]) for _, path, _, _ in self.no.writes))
        row["properties"]["GitHub 수정"]["date"]["start"] = DATE
        self.assertEqual(self.run_sync()["updated"], 1)

    def test_create_errors_keep_fence_and_block_automatic_retry(self):
        for error in ["400", "401", "403", "409", "429", "500", "timeout", "response lost"]:
            with self.subTest(error=error):
                self.no = Notion()
                self.no.create_error = error
                with self.assertRaises(s.SyncError): self.run_sync()
                pending = self.no.pending()
                self.assertTrue(s.valid_key(pending))
                self.assertEqual(sum(m == "POST" for m, _, _, _ in self.no.writes), 1)
                self.no.writes.clear()
                with self.assertRaises(s.SyncError): self.run_sync()
                self.assertEqual(self.no.writes, [])
                self.assertEqual(self.no.pending(), pending)

    def test_response_loss_with_committed_row_recovers_without_duplicate(self):
        self.no.create_error, self.no.create_committed = "lost", True
        with self.assertRaises(s.SyncError): self.run_sync()
        self.no.create_error = None
        self.assertEqual(self.run_sync()["updated"], 1)
        self.assertEqual(self.no.pending(), "")
        self.assertEqual(len(self.no.pages), 2)

    def test_readback_and_clear_failures_preserve_fence_and_last_success(self):
        for attr in ["get_created_failure", "clear_failure"]:
            self.no = Notion()
            setattr(self.no, attr, True)
            with self.assertRaises(s.SyncError): self.run_sync()
            self.assertTrue(self.no.pending())
            self.assertIsNone(self.no.pages[CONTROL]["properties"]["동기화 시각"]["date"])
            setattr(self.no, attr, False)
            self.assertEqual(self.run_sync()["updated"], 1)
            self.assertEqual(len(self.no.pages), 2)

    def test_invalid_timestamp_and_schema_have_zero_writes(self):
        self.gh.rows[0]["updated_at"] = "2026-10-04"
        with self.assertRaises(s.SyncError): self.run_sync()
        self.assertEqual(self.no.writes, [])
        self.gh = Github()
        original = self.no.request
        def changed(method, path, *args, **kwargs):
            value = original(method, path, *args, **kwargs)
            if method == "GET" and path.startswith("/data_sources/"):
                value["properties"]["일정"]["type"] = "rich_text"
            return value
        self.no.request = changed
        with self.assertRaises(s.SyncError): self.run_sync()
        self.assertEqual(self.no.writes, [])

    def test_canonical_keys_and_multivalue(self):
        self.assertNotEqual(s.key_for(1, 23), s.key_for(12, 3))
        self.assertEqual(s.array_text(["b", "a", "a"]), '["a", "b"]')
        self.assertNotEqual(s.array_text(["a,b"]), s.array_text(["a", "b"]))
        with self.assertRaises(s.SyncError): s.key_for(True, 1)
        self.assertFalse(s.valid_key("gh:01:issue:3"))

    def test_extra_schema_fields_or_options_have_zero_writes(self):
        for mode in ["extra_property", "extra_option", "missing_property", "missing_option"]:
            self.no = Notion()
            original = self.no.request
            def changed(method, path, *args, **kwargs):
                value = original(method, path, *args, **kwargs)
                if method == "GET" and path.startswith("/data_sources/"):
                    props = value["properties"]
                    if mode == "extra_property": props["unexpected"] = {"type": "rich_text"}
                    if mode == "missing_property": del props["메모"]
                    if mode == "extra_option": props["종류"]["select"]["options"].append({"name": "EXTRA"})
                    if mode == "missing_option": props["종류"]["select"]["options"].pop()
                return value
            self.no.request = changed
            with self.subTest(mode=mode), self.assertRaises(s.SyncError): self.run_sync()
            self.assertEqual(self.no.writes, [])

    def test_control_fixed_empty_fields_have_zero_writes(self):
        for name, value in {"번호": {"number": 1}, "GitHub URL": {"url": "https://github.com"},
                            "GitHub 상태": {"select": {"name": "Open"}},
                            "GitHub 수정": {"date": {"start": DATE}}}.items():
            self.no = Notion()
            self.no.pages[CONTROL]["properties"][name] = value
            with self.subTest(name=name), self.assertRaises(s.SyncError): self.run_sync()
            self.assertEqual(self.no.writes, [])

    def test_disabled_no_credentials_no_requests(self):
        with patch.dict(s.os.environ, {}, clear=True), patch.object(s, "API") as api, redirect_stdout(io.StringIO()):
            self.assertEqual(s.main([]), 0)
            api.assert_not_called()

    def test_safe_error_output(self):
        fake_env = {name:"fixture" for name in ["GITHUB_TOKEN", "NOTION_TOKEN", "NOTION_DATA_SOURCE_ID", "NOTION_CONTROL_PAGE_ID"]}
        fake_env["NOTION_SYNC_ENABLED"] = "true"
        output = io.StringIO()
        with patch.dict(s.os.environ, fake_env, clear=True), patch.object(s, "sync", side_effect=ValueError("sensitive response")), redirect_stderr(output):
            self.assertEqual(s.main([]), 1)
        self.assertNotIn("sensitive", output.getvalue())
        self.assertNotIn("fixture", output.getvalue())


class HTTPTests(unittest.TestCase):
    def test_retry_after_and_redaction_and_create_once(self):
        class Opener:
            def __init__(self, code, after="2", blocked=False):
                self.calls, self.code, self.after, self.blocked = 0, code, after, blocked
            def open(self, request, timeout):
                self.calls += 1
                raw = json.dumps({"message": "sensitive", "additional_data": {"rate_limit_reason": "public_api_request_blocked" if self.blocked else "other"}}).encode()
                raise urllib.error.HTTPError(request.full_url, self.code, "sensitive", {"Retry-After": self.after}, io.BytesIO(raw))
        delays, opener = [], Opener(429)
        api = s.API("notion", "fixture", opener=opener, sleep=delays.append)
        with self.assertRaises(s.SyncError) as caught: api.request("GET", "/pages/fixture")
        self.assertEqual(opener.calls, 4)
        self.assertEqual(delays.count(2), 3)
        self.assertNotIn("sensitive", str(caught.exception))
        for code in [400, 401, 403, 409, 429, 500, 502, 503, 504, 529]:
            opener = Opener(code)
            with self.assertRaises(s.SyncError):
                s.API("notion", "fixture", opener=opener, sleep=lambda _:None).request("POST", "/pages", {}, create=True)
            self.assertEqual(opener.calls, 1)
        for code, after, blocked in [(403,"2",False), (429,"61",False), (429,"2",True)]:
            opener = Opener(code, after, blocked)
            with self.assertRaises(s.SyncError): s.API("notion", "fixture", opener=opener, sleep=lambda _:None).request("GET", "/pages/fixture")
            self.assertEqual(opener.calls, 1)

    def test_transport_read_retry_and_create_no_retry(self):
        class Opener:
            def __init__(self): self.calls = 0
            def open(self, request, timeout):
                self.calls += 1
                raise urllib.error.URLError("sensitive remote payload")
        opener = Opener()
        api = s.API("notion", "fixture", opener=opener, sleep=lambda _:None)
        with self.assertRaises(s.SyncError) as caught: api.request("GET", "/pages/fixture")
        self.assertEqual(opener.calls, 4)
        self.assertNotIn("sensitive", str(caught.exception))
        opener.calls = 0
        with self.assertRaises(s.SyncError): api.request("POST", "/pages", {}, create=True)
        self.assertEqual(opener.calls, 1)

    def test_redirect_and_host_are_fixed(self):
        self.assertIsNone(s.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid"))
        class Opener:
            def open(self, request, timeout):
                self.url = request.full_url
                return io.BytesIO(b'{}')
        opener = Opener()
        api = s.API("notion", "fixture", opener=opener, sleep=lambda _:None)
        api.request("GET", "/pages/fixture")
        self.assertEqual(opener.url, "https://api.notion.com/v1/pages/fixture")
        with self.assertRaises(s.SyncError): api.request("GET", "//other.invalid")


class WorkflowTests(unittest.TestCase):
    @staticmethod
    def _workflow():
        return (MODULE.parents[1] / ".github/workflows/notion-sync.yml").read_text()

    @staticmethod
    def _sync_checkout_ref(content):
        import re
        sync_start = content.index("  sync:\n")
        steps_start = content.index("    steps:\n", sync_start) + len("    steps:\n")
        job = content[sync_start:]
        steps_start -= sync_start
        steps = job[steps_start:]
        starts = list(re.finditer(r"(?m)^      - ", steps))
        checkout_steps = []
        for index, match in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(steps)
            step = steps[match.start():end]
            if re.search(r"(?m)^      - uses: actions/checkout@", step):
                checkout_steps.append(step)
        if len(checkout_steps) != 1:
            raise AssertionError("sync job must have exactly one actions/checkout step")
        with_match = re.search(
            r"(?ms)^        with:\n(?P<body>(?:^          .*\n?)+)", checkout_steps[0])
        if with_match is None:
            raise AssertionError("sync checkout step is missing with inputs")
        ref_match = re.search(r"(?m)^          ref:\s*(.+)$", with_match["body"])
        if ref_match is None:
            raise AssertionError("sync checkout step is missing with.ref")
        return ref_match.group(1).strip()

    def _assert_sync_checkout_uses_event_sha(self, content, context):
        checkout_ref = self._sync_checkout_ref(content)
        self.assertEqual(self._evaluate_actions_expression(checkout_ref, context),
                         context["sha"])

    @staticmethod
    def _evaluate_actions_expression(expression, context):
        import ast

        value = expression.strip()
        if value.startswith("${{") and value.endswith("}}"):
            value = value[3:-2].strip()
        value = value.replace("format('{0}', github.actor_id)", repr(str(context["actor_id"])))
        value = value.replace("format('{0}', github.run_attempt)",
                              repr(str(context["run_attempt"])))
        names = {
            "github.repository": context["repository"],
            "github.event_name": context["event_name"],
            "github.actor_id": context["actor_id"],
            "github.actor": context["actor"],
            "github.triggering_actor": context["triggering_actor"],
            "github.run_attempt": context["run_attempt"],
            "github.sha": context["sha"],
            "github.ref": context["ref"],
            "github.base_ref": context.get("base_ref", "main"),
            "inputs.approved_sha": context["approved_sha"],
            "inputs.dry_run": context.get("dry_run", False),
            "inputs.resolve_issue_numbers": context.get("resolve_issue_numbers", ""),
            "vars.PM_GITHUB_USER_ID": context["pm_id"],
            "vars.NOTION_SYNC_ENABLED": context["enabled"],
        }
        for name in sorted(names, key=len, reverse=True):
            value = value.replace(name, repr(names[name]))
        value = value.replace("&&", " and ").replace("||", " or ")

        def evaluate(node):
            if isinstance(node, ast.Expression):
                return evaluate(node.body)
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.BoolOp):
                if isinstance(node.op, ast.And):
                    result = evaluate(node.values[0])
                    for part in node.values[1:]:
                        if not result:
                            return result
                        result = evaluate(part)
                    return result
                if isinstance(node.op, ast.Or):
                    result = evaluate(node.values[0])
                    for part in node.values[1:]:
                        if result:
                            return result
                        result = evaluate(part)
                    return result
            if isinstance(node, ast.Compare) and len(node.ops) == 1:
                left, right = evaluate(node.left), evaluate(node.comparators[0])
                if isinstance(node.ops[0], ast.Eq):
                    return left == right
                if isinstance(node.ops[0], ast.NotEq):
                    return left != right
            raise AssertionError("Unexpected workflow expression syntax")

        return evaluate(ast.parse(value, mode="eval"))

    @staticmethod
    def _base_context():
        sha = "a" * 40
        return {"repository": "Aurelia-aurity/Replica", "event_name": "workflow_dispatch",
                "actor_id": "97959897", "pm_id": "97959897", "actor": "Just-Simple0",
                "triggering_actor": "Just-Simple0", "run_attempt": "1",
                "approved_sha": sha, "sha": sha,
                "ref": "refs/heads/main", "enabled": "false",
                "dry_run": False, "resolve_issue_numbers": ""}

    def test_manual_job_gate_matrix_binds_pm_sha_ref_and_first_attempt(self):
        content = self._workflow()
        start = content.index("  sync:\n    if: >-\n") + len("  sync:\n    if: >-\n")
        end = content.index("\n    runs-on:", start)
        gate = " ".join(line.strip() for line in content[start:end].splitlines())
        base = self._base_context()
        cases = [
            ("PM main dry-run", {"dry_run": True}, True),
            ("PM main live", {"dry_run": False}, True),
            ("PM main resume", {"resolve_issue_numbers": "32"}, True),
            ("feature read-only dry-run", {"ref": "refs/heads/feat/32-bidirectional-sync",
                                               "dry_run": True}, True),
            ("feature read-only legacy branch", {"ref": "refs/heads/fix/17-project-add-readback",
                                                    "dry_run": True}, True),
            ("feature live denied", {"ref": "refs/heads/feat/32-bidirectional-sync"}, False),
            ("feature resume denied", {"ref": "refs/heads/feat/32-bidirectional-sync",
                                         "dry_run": True, "resolve_issue_numbers": "32"}, False),
            ("non-PM", {"actor_id": "123", "actor": "other", "triggering_actor": "other"}, False),
            ("different triggering actor", {"triggering_actor": "other"}, False),
            ("rerun same PM", {"run_attempt": "2"}, False),
            ("rerun other actor", {"run_attempt": "2", "triggering_actor": "other"}, False),
            ("SHA mismatch", {"approved_sha": "b" * 40}, False),
            ("empty SHA", {"approved_sha": ""}, False),
            ("truncated SHA", {"approved_sha": "a" * 39}, False),
            ("other branch", {"ref": "refs/heads/feat/unreviewed"}, False),
            ("tag", {"ref": "refs/tags/v1"}, False),
        ]
        for label, changes, expected in cases:
            with self.subTest(case=label):
                context = {**base, **changes}
                self.assertEqual(self._evaluate_actions_expression(gate, context), expected)

        self.assertIn("format('{0}', github.actor_id) == vars.PM_GITHUB_USER_ID", gate)
        self.assertIn("github.actor == github.triggering_actor", gate)
        self.assertIn("format('{0}', github.run_attempt) == '1'", gate)
        self.assertIn("inputs.approved_sha == github.sha", gate)
        self.assertIn("github.ref == 'refs/heads/main'", gate)
        self.assertIn("github.ref == 'refs/heads/fix/17-project-add-readback'", gate)
        self.assertIn("github.ref == 'refs/heads/feat/32-bidirectional-sync'", gate)

        # The shell guard must enforce the same exact ref/input matrix before checkout.
        import os
        import re
        import subprocess
        import textwrap
        guard = content.split("- name: Validate exact manual ref", 1)[1].split("- uses: actions/checkout@", 1)[0]
        script_match = re.search(r"(?ms)^        run: \|\n(?P<body>(?:^          .*\n?)+)", guard)
        self.assertIsNotNone(script_match)
        shell = textwrap.dedent(script_match["body"])
        for ref, dry_run, resolve, expected in (
            ("refs/heads/main", "false", "32", 0),
            ("refs/heads/feat/32-bidirectional-sync", "true", "", 0),
            ("refs/heads/fix/17-project-add-readback", "true", "", 0),
            ("refs/heads/feat/32-bidirectional-sync", "false", "", 1),
            ("refs/heads/feat/32-bidirectional-sync", "true", "32", 1),
        ):
            with self.subTest(shell_ref=ref, dry_run=dry_run, resolve=resolve):
                result = subprocess.run(["bash", "-e", "-c", shell], env={
                    **os.environ, "SYNC_DISPATCH_REF": ref,
                    "SYNC_DISPATCH_DRY_RUN": dry_run,
                    "SYNC_DISPATCH_RESOLVE_ISSUES": resolve,
                }, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_automatic_and_manual_checkout_use_immutable_event_sha(self):
        import re
        content = self._workflow()
        start = content.index("  sync:\n    if: >-\n") + len("  sync:\n    if: >-\n")
        end = content.index("\n    runs-on:", start)
        gate = " ".join(line.strip() for line in content[start:end].splitlines())
        base = self._base_context()
        cases = [
            ("schedule disabled", {"event_name": "schedule", "enabled": "false"}, False),
            ("schedule enabled", {"event_name": "schedule", "enabled": "true"}, True),
            ("issues enabled", {"event_name": "issues", "enabled": "true"}, True),
            ("pull request target enabled", {"event_name": "pull_request_target", "enabled": "true"}, True),
            ("pull request target disabled", {"event_name": "pull_request_target", "enabled": "false"}, False),
            ("non-main base", {"event_name": "pull_request_target", "enabled": "true", "base_ref": "feat/other"}, False),
        ]
        for label, changes, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(self._evaluate_actions_expression(gate, {**base, **changes}), expected)

        self._assert_sync_checkout_uses_event_sha(content, base)
        self._assert_sync_checkout_uses_event_sha(
            content, {**base, "event_name": "schedule"})

        checkout_ref_line = "          ref: ${{ github.sha }}"
        self.assertEqual(content.count(checkout_ref_line), 1)
        unrelated_first_ref = (
            "      - name: Unrelated reference input\n"
            "        uses: actions/cache@v4\n"
            "        with:\n"
            "          ref: ${{ github.sha }}\n")
        counterexample = content.replace(
            "      - uses: actions/checkout@", unrelated_first_ref +
            "      - uses: actions/checkout@", 1)
        checkout_start = counterexample.index("      - uses: actions/checkout@")
        checkout_end = counterexample.find("      - ", checkout_start + 1)
        if checkout_end == -1:
            checkout_end = len(counterexample)
        actual_checkout = counterexample[checkout_start:checkout_end]
        self.assertIn(checkout_ref_line, actual_checkout)
        counterexample = counterexample[:checkout_start] + actual_checkout.replace(
            checkout_ref_line, "          ref: ${{ github.ref }}", 1) + counterexample[checkout_end:]
        self.assertEqual(self._evaluate_actions_expression(
            re.search(r"(?m)^\s+ref: (.+)$", counterexample)[1], base), base["sha"])
        with self.assertRaises(AssertionError):
            self._assert_sync_checkout_uses_event_sha(counterexample, base)
        sync_enabled = re.search(r"(?m)^\s+NOTION_SYNC_ENABLED: (.+)$", content)[1]
        self.assertEqual(self._evaluate_actions_expression(sync_enabled, base), "true")
        self.assertEqual(self._evaluate_actions_expression(
            sync_enabled, {**base, "event_name": "schedule", "enabled": "false"}), "false")
        self.assertEqual(self._evaluate_actions_expression(
            sync_enabled, {**base, "event_name": "schedule", "enabled": "true"}), "true")

        verify_index = content.index("- name: Verify sync implementation")
        sync_index = content.index("- name: Sync current GitHub state")
        secrets_index = content.index("NOTION_TOKEN:")
        self.assertLess(verify_index, sync_index)
        self.assertLess(sync_index, secrets_index)
        self.assertNotIn("secrets.", content[:verify_index])

    def test_workflow_permissions_and_event_inputs_remain_fixed(self):
        import re
        content = self._workflow()
        self.assertIn("pull_request_target:", content)
        self.assertIn("workflow_dispatch:", content)
        self.assertIn("issues:", content)
        self.assertIn("schedule:", content)
        self.assertRegex(content, r"(?m)^\s+- cron: ['\"]?\*/5 \* \* \* \*['\"]?\s*$")
        self.assertIn("group: replica-notion-sync", content)
        self.assertIn("cancel-in-progress: false", content)
        self.assertRegex(content, r"actions/checkout@[0-9a-f]{40}")
        self.assertIn("actions/checkout@11d5960a326750d5838078e36cf38b85af677262", content)
        self.assertIn("ref: ${{ github.sha }}", content)
        self.assertIn("approved_sha:", content)
        self.assertIn("required: true", content)
        self.assertIn("type: string", content)
        self.assertIn("persist-credentials: false", content)
        self.assertNotIn("github.event.", content)
        self.assertNotIn("write", content)
        permission_lines = re.search(r"permissions:\n(.*?)\n\n", content, re.S)[1]
        self.assertEqual(set(permission_lines.splitlines()), {"  contents: read", "  issues: read", "  pull-requests: read"})
        self.assertLess(content.index("inputs.approved_sha == github.sha"), content.index("steps:"))
        self.assertLess(content.index("github.ref == 'refs/heads/fix/17-project-add-readback'"),
                        content.index("steps:"))
        steps = content.split("    steps:\n", 1)[1]
        self.assertEqual(steps.count("inputs.approved_sha"), 1)
        sync_step = content.split("- name: Sync current GitHub state", 1)[1]
        bidirectional = re.search(r"(?m)^          NOTION_BIDIRECTIONAL_ENABLED: (.+)$", sync_step)
        self.assertIsNotNone(bidirectional)
        self.assertEqual(bidirectional.group(1).strip(), "${{ vars.NOTION_BIDIRECTIONAL_ENABLED || 'false' }}")
        cron_contract = r"(?m)^\s+- cron: ['\"]?\*/5 \* \* \* \*['\"]?\s*$"
        self.assertRegex(content, cron_contract)
        self.assertNotRegex(content.replace("*/5 * * * *", "*/15 * * * *"),
                            cron_contract)
        self.assertNotEqual(bidirectional.group(1).strip(),
                            "${{ vars.NOTION_BIDIRECTIONAL_ENABLED || 'true' }}")
        run_script = sync_step.split("run: |", 1)[1]
        self.assertNotIn("inputs.resolve_issue_numbers", run_script)
        self.assertNotIn("inputs.diagnose_date_readback", run_script)
        self.assertIn('os.environ.get("SYNC_RESOLVE_ISSUES")', run_script)
        resolve_binding = re.search(r"(?m)^          SYNC_RESOLVE_ISSUES: (.+)$", sync_step)
        self.assertIsNotNone(resolve_binding)
        self.assertEqual(resolve_binding.group(1).strip(), "${{ inputs.resolve_issue_numbers }}")
        approved_sha_binding = re.search(r"(?m)^          SYNC_APPROVED_SHA: (.+)$", sync_step)
        self.assertIsNotNone(approved_sha_binding)
        self.assertEqual(approved_sha_binding.group(1).strip(), "${{ inputs.approved_sha }}")
        self.assertIn('os.environ.get("SYNC_DIAGNOSE_DATE_READBACK")', run_script)
        self.assertIn('args.append("--diagnose-date-readback")', run_script)
        self.assertIn("diagnose_date_readback:", content)
        self.assertIn("default: false", content[content.index("diagnose_date_readback:"):])

    def test_date_diagnostic_workflow_wrapper_builds_argv_and_guards_before_run(self):
        import os
        import re
        import sys
        import textwrap

        content = self._workflow()
        sync_step = content.split("- name: Sync current GitHub state", 1)[1]
        run_script = sync_step.split("run: |", 1)[1]
        match = re.search(
            r"(?m)^\s+python3 -B - <<'PY'\n(?P<body>.*?)^\s+PY\s*$",
            run_script,
            re.S,
        )
        self.assertIsNotNone(match)
        source = textwrap.dedent(match.group("body"))

        captured = []
        normal_env = {
            "SYNC_DRY_RUN": "true",
            "SYNC_DIAGNOSE_DATE_READBACK": "true",
            "SYNC_RESOLVE_ISSUES": "1,2",
        }
        with patch.dict(os.environ, normal_env, clear=True):
            with patch("subprocess.run", side_effect=lambda argv, check: captured.append((argv, check))):
                exec(compile(source, "notion-sync workflow wrapper", "exec"), {})
        self.assertEqual(captured, [(
            [sys.executable, "-B", "scripts/notion_sync.py", "--notification-report", "notion-notification-report.json", "--dry-run",
             "--resolve-issue-numbers", "1,2", "--diagnose-date-readback"], True)])

        captured.clear()
        default_env = {
            "GITHUB_EVENT_NAME": "schedule",
            "SYNC_DRY_RUN": "true",
            "SYNC_DIAGNOSE_DATE_READBACK": "false",
            "SYNC_RESOLVE_ISSUES": "",
        }
        with patch.dict(os.environ, default_env, clear=True):
            with patch("subprocess.run", side_effect=lambda argv, check: captured.append((argv, check))):
                exec(compile(source, "notion-sync workflow wrapper", "exec"), {})
        self.assertEqual(captured, [(
            [sys.executable, "-B", "scripts/notion_sync.py", "--notification-report", "notion-notification-report.json", "--dry-run"], True)])
        self.assertNotIn("--resolve-issue-numbers", captured[0][0])

        captured.clear()
        denied_env = {
            "SYNC_DRY_RUN": "false",
            "SYNC_DIAGNOSE_DATE_READBACK": "true",
            "SYNC_RESOLVE_ISSUES": "",
        }
        with patch.dict(os.environ, denied_env, clear=True):
            with patch("subprocess.run", side_effect=lambda *args, **kwargs: captured.append(args)):
                with self.assertRaisesRegex(SystemExit, "requires --dry-run"):
                    exec(compile(source, "notion-sync workflow wrapper", "exec"), {})
        self.assertEqual(captured, [])


if __name__ == "__main__":
    unittest.main()
