"""Opt-in read-only real connection check. Never create/delete users or rows."""

import os
import unittest
from uuid import uuid4

from app.db import AuthenticationError, Database, Settings
from app.db.transport import UrllibTransport


@unittest.skipUnless(os.environ.get("REPLICA_DB_LIVE_TEST") == "1", "live checks disabled; offline by default")
class LiveServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        settings = Settings.from_env()
        # Refuse to send local project credentials to a different host.
        if settings.url.rstrip("/") != "https://hfmrhmuhcbmskhjagpzg.supabase.co":
            self.fail("live checks require the Replica project URL")
        self.settings = settings
        self.database = Database(settings)
        self.transport = UrllibTransport(settings)

    async def test_publishable_key_reaches_auth(self):
        response = await self.transport.request("GET", "/auth/v1/settings",
            headers={"apikey": self.settings.publishable_key}, params={})
        self.assertEqual(response.status, 200)
        self.assertIsInstance(response.data, dict)

    async def test_server_key_reads_all_application_tables(self):
        for table in ("personas", "chat_sessions", "chat_turns", "records", "record_text_versions",
                      "style_profiles", "profile_sources", "record_chunks", "deletion_jobs"):
            with self.subTest(table=table):
                # Do not print row contents or retrieve personal text.
                rows = await self.database._request("GET", "/rest/v1/" + table, server=True,
                    params={"select": "user_id", "limit": "1"})
                self.assertIsInstance(rows, list)

    async def test_server_read_only_rpc(self):
        chunks = await self.database._request("POST", "/rest/v1/rpc/search_record_chunks", server=True,
            body={"p_user_id": str(uuid4()), "p_persona_id": str(uuid4()),
                  "p_embedding_model": "replica-live-probe", "p_query_embedding": "[1,0]",
                  "p_limit": 1, "p_min_similarity": 0.5})
        # This search function does not write rows. No fixture or account is created.
        self.assertEqual(chunks, [])

    async def test_publishable_key_cannot_read_tables_or_execute_rpc(self):
        headers = {"apikey": self.settings.publishable_key}
        if not self.settings.publishable_key.startswith("sb_publishable_"):
            headers["Authorization"] = "Bearer " + self.settings.publishable_key
        for table in ("personas", "chat_sessions", "chat_turns", "records", "record_text_versions",
                      "style_profiles", "profile_sources", "record_chunks", "deletion_jobs"):
            with self.subTest(table=table):
                response = await self.transport.request("GET", "/rest/v1/" + table,
                    headers=headers, params={"select": "user_id", "limit": "1"})
                self.assertIn(response.status, (401, 403))
        response = await self.transport.request("POST", "/rest/v1/rpc/search_record_chunks",
            headers=headers, params={}, body={"p_user_id": str(uuid4()), "p_persona_id": str(uuid4()),
                "p_embedding_model": "replica-live-probe", "p_query_embedding": "[1,0]",
                "p_limit": 1, "p_min_similarity": 0.5})
        self.assertIn(response.status, (401, 403))

    async def test_invalid_user_token_is_rejected(self):
        with self.assertRaises(AuthenticationError):
            await self.database.for_user("replica-invalid-token")


@unittest.skipUnless(os.environ.get("REPLICA_DB_LIVE_TEST") == "1"
                    and bool(os.environ.get("REPLICA_DB_TEST_ACCESS_TOKEN")), "live user token not supplied")
class LiveDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_user_reads_and_server_rpc(self):
        # Missing configuration must fail, not silently skip after opting in.
        token = os.environ["REPLICA_DB_TEST_ACCESS_TOKEN"]
        database = Database(Settings.from_env())
        if database._settings.url.rstrip("/") != "https://hfmrhmuhcbmskhjagpzg.supabase.co":
            self.fail("live checks require the Replica project URL")
        user = await database.for_user(token)
        rows = await user.list_personas(limit=5)
        self.assertTrue(all(row["user_id"] == user.user_id for row in rows))
        # A random persona must have zero chunks; this RPC is read-only.
        # It probes server key/RPC grants without needing persisted test fixtures.
        chunks = await user._rpc("search_record_chunks", p_persona_id=str(uuid4()),
            p_embedding_model="replica-live-probe", p_query_embedding="[1,0]", p_limit=1, p_min_similarity=0.5)
        self.assertEqual(chunks, [])
