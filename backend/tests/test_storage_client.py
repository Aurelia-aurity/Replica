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

    def _call(self, method, path, *, max_bytes, body=None, extra=None):
        self.calls.append((method, path, body, dict(extra or {})))
        raw = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()
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

    def test_object_path_segments_are_quoted_and_the_secret_key_is_a_header_only(self):
        storage = Recorder(200, {"size": 1})
        run(storage.object_size("a b/c?d/original"))
        self.assertEqual(storage.calls[0][1], f"/storage/v1/object/info/{BUCKET}/a%20b/c%3Fd/original")
        self.assertEqual(storage._headers, {"apikey": "sb_secret_fake"})  # 새 secret 키는 apikey로만 보낸다
        legacy = SupabaseStorage(Settings("https://example.supabase.co", "sb_publishable_fake", "legacy.service.jwt"))
        self.assertEqual(legacy._headers["Authorization"], "Bearer legacy.service.jwt")


if __name__ == "__main__":
    unittest.main()
