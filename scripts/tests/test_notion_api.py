import copy
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_project as gp
import notion_api as na
from sync_simple import ProjectClient
from sync_simple_fakes import OPTIONS, PAGE, SOURCE, Notion


class Opener:
    def __init__(self):
        self.requests = []
        self.error = None

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return io.BytesIO(b'{"object":"page"}')


class NotionTransportTests(unittest.TestCase):
    def test_default_client_blocks_patch_and_off_host_paths(self):
        # Given:
        opener = Opener()
        client = na.NotionClient("synthetic-token", opener=opener, sleep=lambda _: None)
        cases = [("PATCH", f"/pages/{PAGE}"), ("POST", "/pages"),
                 ("GET", "//evil.invalid"), ("GET", "/pages/../../evil"),
                 ("GET", "https://evil.invalid/pages/123")]
        for method, path in cases:
            # When / Then:
            with self.subTest(method=method, path=path), self.assertRaises(gp.SyncError):
                client.request(method, path)
        self.assertEqual(opener.requests, [])

    def test_read_uses_fixed_host_method_and_body(self):
        # Given:
        opener = Opener()
        client = na.NotionClient("synthetic-token", opener=opener, sleep=lambda _: None)
        # When:
        client.request("POST", f"/data_sources/{SOURCE}/query", {"page_size": 100})
        # Then:
        request, _ = opener.requests[0]
        self.assertEqual(request.full_url, f"https://api.notion.com/v1/data_sources/{SOURCE}/query")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"page_size": 100})

    def test_read_error_is_redacted(self):
        # Given:
        opener = Opener()
        opener.error = urllib.error.HTTPError("https://secret.invalid", 503, "private", {},
                                               io.BytesIO(b"private token"))
        client = na.NotionClient("synthetic-token", opener=opener, sleep=lambda _: None)
        # When / Then:
        with self.assertRaisesRegex(gp.SyncError, "^Notion HTTP 503; stopped$"):
            client.request("GET", f"/data_sources/{SOURCE}")
        self.assertEqual(len(opener.requests), 1)

    def test_project_default_client_blocks_mutation_before_network(self):
        # Given:
        client = ProjectClient("synthetic-token")
        # When / Then:
        with self.assertRaisesRegex(gp.SyncError, "writes disabled"):
            client.request(gp.SET_STATUS_MUTATION, {}, mutation=True)

    def test_explicit_write_client_sends_one_patch_and_never_retries_ambiguous_failure(self):
        opener = Opener()
        client = na.NotionClient("synthetic-token", allow_apply=True, opener=opener, sleep=lambda _: None)
        payload = {"properties": {"작업 상태": {"select": {"name": "진행 중"}}}}
        client.request("PATCH", f"/pages/{PAGE}", payload)
        request = opener.requests[0][0]
        self.assertEqual(request.full_url, f"https://api.notion.com/v1/pages/{PAGE}")
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(json.loads(request.data), payload)
        opener.error = urllib.error.HTTPError("https://secret.invalid", 503, "private", {},
                                               io.BytesIO(b"private token"))
        with self.assertRaisesRegex(gp.SyncError, "^Notion HTTP 503; stopped$"):
            client.request("PATCH", f"/pages/{PAGE}", payload)
        self.assertEqual(len(opener.requests), 2)
        for method, path in (("PATCH", f"/data_sources/{SOURCE}"),
                             ("DELETE", f"/pages/{PAGE}"), ("PATCH", "//evil.invalid")):
            with self.assertRaises(gp.SyncError):
                client.request(method, path, payload)
        self.assertEqual(len(opener.requests), 2)

    def test_query_pagination_preserves_archive_partition(self):
        # Given: two correctly shaped query pages, no wall-clock behavior.
        class Pages:
            def __init__(self):
                self.payloads = []

            def request(self, method, path, payload):
                self.payloads.append(copy.deepcopy(payload))
                first = "start_cursor" not in payload
                return {"object": "list", "type": "page_or_data_source", "page_or_data_source": {},
                        "results": [{"object": "page", "id": "one" if first else "two"}],
                        "has_more": first, "next_cursor": "cursor-2" if first else None,
                        "request_status": {"type": "complete"}}
        fake = Pages()
        # When:
        rows = na.query_all(fake, SOURCE, True)
        # Then:
        self.assertEqual([row["id"] for row in rows], ["one", "two"])
        self.assertEqual(fake.payloads, [{"page_size": 100, "is_archived": True},
                                        {"page_size": 100, "is_archived": True,
                                         "start_cursor": "cursor-2"}])

    def test_malformed_query_envelopes_are_rejected(self):
        # Given:
        for update in ({"has_more": 1}, {"has_more": True, "next_cursor": None},
                       {"next_cursor": "unexpected"}, {"object": "page"},
                       {"results": None}, {"request_status": None},
                       {"request_status": {"type": "unknown"}}):
            with self.subTest(update=update):
                fake = Notion()
                fake.query_transform = lambda result: {**result, **update}
                # When / Then:
                with self.assertRaises(gp.SyncError):
                    na.query_all(fake, SOURCE, False)

    def test_schema_mismatch_is_rejected(self):
        # Given: transport returning an actual schema shape with wrong status type.
        fake = Notion()
        schema = fake.request("GET", f"/data_sources/{SOURCE}")
        schema["properties"]["작업 상태"]["type"] = "status"
        class WrongSchema:
            def request(self, method, path):
                return schema
        # When / Then:
        with self.assertRaises(gp.SyncError):
            na.check_schema(WrongSchema(), SOURCE, OPTIONS)
