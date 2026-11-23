"""Supabase Storage 클라이언트: 실제 Storage에 연결하지 않고 응답 형식 가정만 시험한다."""

import asyncio
import base64
import json
import unittest
from datetime import datetime, timezone

from app.clients.storage import BUCKET, SupabaseStorage, StorageUnavailable, _token_expiry
from app.db import Settings

SETTINGS = Settings("https://example.supabase.co", "sb_publishable_fake", "sb_secret_fake")
PATH = "00000000-0000-4000-8000-000000000001/00000000-0000-4000-8000-000000000002/original"


def jwt(payload: dict) -> str:
    part = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"x.{part}.y"


class Recorder(SupabaseStorage):
    def __init__(self, status, payload):
        super().__init__(SETTINGS)
        self.status, self.payload, self.calls = status, payload, []

    def _call(self, method, path, *, max_bytes, body=None, extra=None, truncate=False):
        self.calls.append((method, path, body, dict(extra or {})))
        raw = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()
        if truncate:
            return self.status, raw[:max_bytes]
        if len(raw) > max_bytes:
            raise StorageUnavailable()
        return self.status, raw


def run(coro):
    return asyncio.run(coro)


class StorageClientTests(unittest.TestCase):
    def test_signed_upload_url_is_built_from_the_relative_path_and_token_expiry(self):
        exp = 1_900_000_000
        token = jwt({"exp": exp})
        storage = Recorder(200, {"url": f"/object/upload/sign/{BUCKET}/{PATH}?token={token}", "token": token})
        signed = run(storage.create_upload(PATH, mime_type="text/plain"))
        self.assertEqual(storage.calls[0][:2], ("POST", f"/storage/v1/object/upload/sign/{BUCKET}/{PATH}"))
        self.assertTrue(signed.url.startswith(f"https://example.supabase.co/storage/v1/object/upload/sign/{BUCKET}/"))
        self.assertEqual(dict(signed.headers), {"Content-Type": "text/plain"})
        self.assertEqual(signed.expires_at, datetime.fromtimestamp(exp, tz=timezone.utc))

    def test_unreadable_token_expiry_falls_back_to_two_hours(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual((_token_expiry("not-a-jwt", now) - now).total_seconds(), 7200)

    def test_bad_signed_upload_responses_are_unavailable(self):
        for status, payload in ((200, {"url": "/x"}), (500, {"url": "/x", "token": "t"}), (200, {"url": "ftp://h/x", "token": "t"}),
                                (200, b"not json")):
            with self.subTest(status=status), self.assertRaises(StorageUnavailable):
                run(Recorder(status, payload).create_upload(PATH, mime_type="text/plain"))

    def test_object_size_reads_size_and_treats_missing_as_none(self):
        self.assertEqual(run(Recorder(200, {"size": 42}).object_size(PATH)), 42)
        self.assertIsNone(run(Recorder(404, {"error": "not_found"}).object_size(PATH)))
        self.assertIsNone(run(Recorder(400, {"error": "not_found"}).object_size(PATH)))
        for status, payload in ((200, {"size": "42"}), (200, {"size": -1}), (200, {}), (500, {"size": 1}), (200, b"x")):
            with self.subTest(payload=payload), self.assertRaises(StorageUnavailable):
                run(Recorder(status, payload).object_size(PATH))

    def test_read_object_is_bounded_and_needs_200(self):
        self.assertEqual(run(Recorder(200, b"abc").read_object(PATH, max_bytes=3)), b"abc")
        with self.assertRaises(StorageUnavailable):
            run(Recorder(200, b"abcd").read_object(PATH, max_bytes=3))
        with self.assertRaises(StorageUnavailable):
            run(Recorder(404, b"").read_object(PATH, max_bytes=3))

    def test_read_head_asks_for_a_range_and_accepts_200_or_206(self):
        storage = Recorder(206, b"RIFF1234WAVE")
        self.assertEqual(run(storage.read_head(PATH, size=16)), b"RIFF1234WAVE")
        method, path, _, extra = storage.calls[-1]
        self.assertEqual((method, path), ("GET", f"/storage/v1/object/{BUCKET}/{PATH}"))
        self.assertEqual(extra, {"Range": "bytes=0-15"})
        self.assertEqual(run(Recorder(200, b"x" * 100).read_head(PATH, size=4)), b"xxxx")  # Range를 무시해도 앞부분만
        for status in (404, 416, 500):
            with self.subTest(status=status):
                with self.assertRaises(StorageUnavailable):
                    run(Recorder(status, b"").read_head(PATH, size=16))

    def test_read_head_over_http_reads_only_the_head_even_if_the_server_ignores_range(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        seen = []
        body = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x01" * (2 * 1024 * 1024)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append(self.headers.get("Range"))
                self.send_response(200)  # Range를 무시하고 전체를 보낸다
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass  # 클라이언트가 앞부분만 읽고 닫으면 끊긴다

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            local = Settings(f"http://127.0.0.1:{server.server_port}", "sb_publishable_fake", "sb_secret_fake",
                             allow_local_http=True)
            head = run(SupabaseStorage(local).read_head(PATH, size=16))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual((head, seen), (body[:16], ["bytes=0-15"]))

    def test_object_path_segments_are_quoted_and_the_secret_key_is_a_header_only(self):
        storage = Recorder(200, {"size": 1})
        run(storage.object_size("a b/c?d/original"))
        self.assertEqual(storage.calls[0][1], f"/storage/v1/object/info/{BUCKET}/a%20b/c%3Fd/original")
        self.assertEqual(storage._headers, {"apikey": "sb_secret_fake"})  # 새 secret 키는 apikey로만 보낸다
        legacy = SupabaseStorage(Settings("https://example.supabase.co", "sb_publishable_fake", "legacy.service.jwt"))
        self.assertEqual(legacy._headers["Authorization"], "Bearer legacy.service.jwt")


if __name__ == "__main__":
    unittest.main()
