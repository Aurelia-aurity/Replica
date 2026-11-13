"""Real loopback HTTP, actual node adapter/parser and a synthetic decoder."""
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock
from http.client import HTTPConnection

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import server
from profile_adapter import ProfileConfig
from test_profile_contract import analyze, merge, profile, uid
from test_profile_inference import Runtime
from test_server import RecordingService, call, request, serving

ANALYZE = "/v1/persona-profiles/analyze"
MERGE = "/v1/persona-profiles/merge"


def service(runtime=None, **options):
    return RecordingService(runtime, profile_config=ProfileConfig("local-test-model", 32), **options)


class ProfileServerTests(unittest.TestCase):
    def assert_error(self, result, status, request_id=uid(1)):
        self.assertEqual(result, (status, server.error_body(status, request_id)))

    def test_analyze_to_merge_returns_exact_envelopes_and_request_owned_metadata(self):
        runtime = Runtime()
        payload = analyze()
        payload["sources"].append({"record_id": uid(21), "text_version_id": uid(22),
                                   "target_utterances": ["두 번째 합성 기록이에요."]})
        with serving(service(runtime)) as port:
            status, leaf = call(port, payload, path=ANALYZE)
            self.assertEqual(status, 200)
            expected = {key: payload[key] for key in (
                "request_id", "persona_id", "source_revision", "source_snapshot_id", "node_id")}
            self.assertEqual(leaf, {**expected, "source_text_version_ids": [uid(7), uid(22)],
                                  "profile_content": profile(), "model_name": "local-test-model"})
            combined = merge()
            combined["inputs"][0] = {"node_id": leaf["node_id"],
                                     "profile_content": leaf["profile_content"]}
            status, root = call(port, combined, path=MERGE)
            self.assertEqual(status, 200)
            expected = {key: combined[key] for key in (
                "request_id", "persona_id", "source_revision", "source_snapshot_id", "node_id")}
            self.assertEqual(root, {**expected, "profile_content": profile(),
                                   "model_name": "local-test-model"})
        self.assertEqual(runtime.model.generate.call_count, 2)

    def test_default_baseline_only_and_opt_in_configuration(self):
        with serving(server.Service(Runtime())) as port:
            self.assertEqual(call(port, analyze(), path=ANALYZE), (404, {"status": 404}))
            self.assertEqual(call(port, merge(), path=MERGE), (404, {"status": 404}))
            self.assertEqual(call(port, request())[0], 200)
        with self.assertLogs("baseline.http", level="ERROR"):
            self.assertEqual(server.main(["--port", "18043", "--enable-profile-analysis"]), 1)
            self.assertEqual(server.main(["--port", "18043", "--profile-output-tokens", "32"]), 1)

    def test_raw_auth_structure_size_ready_and_context_precedence(self):
        auth = Mock(return_value=401)
        with serving(service(auth=auth)) as port:
            self.assert_error(call(port, raw=b"", headers={"Content-Length": "262145"},
                                   path=ANALYZE), 413, None)
            auth.assert_not_called()
            self.assert_error(call(port, raw=b"{broken", path=ANALYZE), 401, None)
        for code in (401, 403):
            with serving(service(auth=lambda _headers: code)) as port:
                self.assert_error(call(port, raw=b"{broken", path=MERGE), code, None)
        with serving(service()) as port:
            malformed = analyze()
            malformed["sources"][0]["target_utterances"] = ["x" * 16001, None]
            self.assert_error(call(port, malformed, path=ANALYZE), 400)
            malformed["sources"][0]["target_utterances"] = ["x" * 16001]
            self.assert_error(call(port, malformed, path=ANALYZE), 413)
            self.assert_error(call(port, analyze(), path=ANALYZE), 503)
            raw = json.dumps(analyze()).encode()
            self.assert_error(call(port, raw=raw + b" " * (262144 - len(raw)), path=ANALYZE), 503)
        runtime = Runtime()
        runtime.context_limit = 1
        current = service(runtime)
        current.active_id = uid(1)
        with serving(current) as port:
            self.assert_error(call(port, analyze(), path=ANALYZE), 422)
        runtime.model.generate.assert_not_called()

    def test_malformed_json_duplicate_keys_nonfinite_unicode_and_child_limits(self):
        raw = json.dumps(analyze())
        invalid = [
            (ANALYZE, raw.replace('"source_revision": 3', '"source_revision": NaN'), None),
            (ANALYZE, raw.replace('"source_revision": 3', '"source_revision": Infinity'), None),
            (ANALYZE, raw.replace('"source_revision": 3', '"source_revision": 1e999'), uid(1)),
            (ANALYZE, raw.replace('"source_revision": 3', '"source_revision": true'), uid(1)),
            (ANALYZE, raw.replace('"source_revision": 3', '"source_revision":3,"source_revision":4'), None),
            (ANALYZE, raw.replace('"target_utterances":', '"record_id":"x","target_utterances":'), None),
            (ANALYZE, b"\xff", None),
            (ANALYZE, raw.replace('"target_utterances": [', '"target_utterances": ["\\ud800",'), uid(1)),
        ]
        runtime = Runtime()
        with serving(service(runtime)) as port:
            for path, body, request_id in invalid:
                with self.subTest(body=str(body)[:80]):
                    self.assert_error(call(port, raw=body.encode() if isinstance(body, str) else body,
                                           path=path), 400, request_id)
            for payload in (merge(inputs=[]), merge(node_id=uid(5)),
                            merge(inputs=merge()["inputs"][:1] * 2)):
                self.assert_error(call(port, payload, path=MERGE), 400, uid(8))
            inputs = [{"node_id": uid(40 + n), "profile_content": profile()} for n in range(9)]
            self.assert_error(call(port, merge(inputs=inputs), path=MERGE), 413, uid(8))
            self.assertEqual(call(port, merge(inputs=inputs[:8]), path=MERGE)[0], 200)
            self.assertEqual(call(port, analyze(source_revision=3.0), path=ANALYZE)[0], 200)
        self.assertEqual(runtime.model.generate.call_count, 2)

    def test_invalid_output_is_safe_500_and_one_decode_without_fallback(self):
        sentinel = "PRIVATE_SYNTHETIC_SENTINEL"
        for body, tokens in (('{"bad":"' + sentinel + '"}', [7, 2]),
                             (json.dumps(profile()), [7] * 32)):
            runtime = Runtime()
            runtime.tokenizer.answer, runtime.tokens = body, tokens
            payload = analyze()
            payload["sources"][0]["target_utterances"] = [sentinel]
            with serving(service(runtime)) as port, self.assertLogs("baseline.http", level="INFO") as logs:
                self.assert_error(call(port, payload, path=ANALYZE), 500)
            self.assertNotIn(sentinel, "".join(logs.output))
            runtime.model.generate.assert_called_once()
        runtime = Runtime()
        runtime.model.generate.side_effect = RuntimeError(sentinel)
        with serving(service(runtime)) as port, self.assertLogs("baseline.http", level="INFO") as logs:
            self.assert_error(call(port, analyze(), path=ANALYZE), 500)
        self.assertNotIn(sentinel, "".join(logs.output))
        runtime.model.generate.assert_called_once()

    def test_deadline_keeps_global_id_and_slot_across_all_routes_until_completion(self):
        runtime = Runtime()
        runtime.block = True
        current = service(runtime, deadline_seconds=0.01)
        with serving(current) as port:
            self.assert_error(call(port, analyze(), path=ANALYZE), 504)
            self.assertTrue(runtime.entered.wait(5))
            self.assert_error(call(port, merge(request_id=uid(1)), path=MERGE), 409)
            self.assert_error(call(port, merge(), path=MERGE), 429, uid(8))
            self.assert_error(call(port, request(request_id=uid(1))), 409)
            self.assert_error(call(port, request(request_id=uid(20))), 429, uid(20))
            runtime.release.set()
            self.assertTrue(current.job.done.wait(5))
            current.deadline_seconds = None
            self.assertEqual(call(port, merge(), path=MERGE)[0], 200)
        self.assertEqual(runtime.model.generate.call_count, 2)

    def test_disconnect_keeps_global_id_and_slot_and_baseline_can_block_profiles(self):
        runtime = Runtime()
        runtime.block = True
        current = service(runtime)
        with serving(current) as port:
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("POST", ANALYZE, body=json.dumps(analyze()),
                                   headers={"Content-Type": "application/json"})
                self.assertTrue(current.accepted.wait(5))
            finally:
                connection.close()
            self.assert_error(call(port, merge(request_id=uid(1)), path=MERGE), 409)
            self.assert_error(call(port, request(request_id=uid(20))), 429, uid(20))
            runtime.release.set()
            self.assertTrue(current.job.done.wait(5))
            runtime.release.clear()
            current.accepted.clear()
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("POST", "/v1/baseline/generate",
                                   body=json.dumps(request(request_id=uid(30))),
                                   headers={"Content-Type": "application/json"})
                self.assertTrue(current.accepted.wait(5))
            finally:
                connection.close()
            self.assert_error(call(port, analyze(request_id=uid(30)), path=ANALYZE), 409, uid(30))
            self.assert_error(call(port, merge(), path=MERGE), 429, uid(8))
            runtime.release.set()
            self.assertTrue(current.job.done.wait(5))
        self.assertEqual(runtime.model.generate.call_count, 2)

    def test_background_loader_initializes_adapter_before_ready(self):
        entered, release = threading.Event(), threading.Event()
        runtime = Runtime()
        current = service()
        def factory():
            entered.set()
            if not release.wait(5):
                raise RuntimeError
            return runtime
        try:
            with serving(current) as port:
                current.start_loading(factory)
                self.assertTrue(entered.wait(5))
                self.assertEqual(call(port, method="GET", path="/readyz"), (503, {"ready": False}))
                self.assert_error(call(port, analyze(), path=ANALYZE), 503)
                release.set()
                self.assertTrue(current.ready.wait(5))
                self.assertEqual(call(port, method="GET", path="/readyz"), (200, {"ready": True}))
                self.assertEqual(call(port, analyze(), path=ANALYZE)[0], 200)
        finally:
            release.set()
            current.loader.join(5)


if __name__ == "__main__":
    unittest.main()
