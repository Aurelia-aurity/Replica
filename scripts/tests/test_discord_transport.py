import base64
import copy
import io
import json
import os
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_transport as dt
import discord_notify as dn
from test_discord_notify import SHA, run, report


class Transport(unittest.TestCase):
    def test_ledger_failure_readback_uses_only_immutable_get_as_corroboration(self):
        intended = dn.new_state(); intended["seen"].append("new")
        stale = dn.new_state(); stale["seen"].append("old")
        data = dt.canonical(intended)
        blob_sha = dt.hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        commit_sha = "c" * 40
        calls = []

        class GH:
            def repo(inner, path, **kwargs):
                calls.append((path, kwargs))
                if not path:
                    return {"id": dt.REPO_ID, "full_name": dt.REPO}
                if kwargs.get("method") == "PUT":
                    return {"content": {"sha": blob_sha}, "commit": {"sha": commit_sha}}
                if path == f"/contents/{dt.STATE_FILE}?ref={dt.BRANCH}":
                    return {"type": "file", "path": dt.STATE_FILE, "encoding": "base64",
                            "sha": "d" * 40, "content": base64.b64encode(dt.canonical(stale)).decode()[:48] + "\n" +
                            base64.b64encode(dt.canonical(stale)).decode()[48:]}
                if path == f"/contents/{dt.STATE_FILE}?ref={commit_sha}":
                    return {"type": "file", "path": dt.STATE_FILE, "encoding": "base64",
                            "sha": blob_sha, "content": base64.b64encode(data).decode()[:48] + "\n" +
                            base64.b64encode(data).decode()[48:]}
                raise AssertionError("Unexpected ledger request")

        ledger = dt.Ledger(GH())
        self.assertEqual(ledger.load(), stale)
        with self.assertRaises(dt.LedgerFailure) as caught:
            ledger.save(intended, purpose="pending")
        error = caught.exception
        self.assertEqual(error.code, "readback_mismatch")
        self.assertEqual(error.purpose, "pending")
        self.assertEqual(error.operation, "immutable_check")
        self.assertEqual(error.immutable_check, "expected")
        self.assertEqual(error.intended_blob_sha, blob_sha)
        self.assertEqual(error.put_blob_sha, blob_sha)
        self.assertNotEqual(error.observed_blob_sha, blob_sha)
        self.assertEqual(error.observed_blob_sha, "d" * 40)
        self.assertEqual(error.commit_sha, commit_sha)
        self.assertEqual(error.expected_digest, dt.hashlib.sha256(data).hexdigest())
        immutable_calls = [path for path, _ in calls if "?ref=" + commit_sha in path]
        self.assertEqual(immutable_calls, [f"/contents/{dt.STATE_FILE}?ref={commit_sha}"])
        self.assertEqual(sum(kwargs.get("method") == "PUT" for _, kwargs in calls), 1)

    def test_ledger_save_missing_put_metadata_is_optional_when_readback_matches(self):
        state = dn.new_state(); captured = []

        class GH:
            def repo(inner, path, **kwargs):
                captured.append((path, kwargs))
                if not path:
                    return {"id": dt.REPO_ID, "full_name": dt.REPO}
                if kwargs.get("method") == "PUT":
                    return {"message": "updated"}
                return {"type": "file", "path": dt.STATE_FILE, "encoding": "base64", "sha": "a" * 40,
                        "content": base64.b64encode(dt.canonical(state)).decode()}

        ledger = dt.Ledger(GH()); ledger.load(); ledger.save(state, purpose="reconcile")
        self.assertEqual(sum(kwargs.get("method") == "PUT" for _, kwargs in captured), 1)
        self.assertFalse(any("?ref=" in path and "automation%" in path for path, _ in captured))

    def test_ledger_readback_mismatch_without_commit_metadata_does_not_guess(self):
        intended = dn.new_state(); intended["seen"].append("intended")
        stale = dn.new_state(); stale["seen"].append("observed")
        calls = []

        class GH:
            def repo(inner, path, **kwargs):
                calls.append((path, kwargs))
                if not path: return {"id": dt.REPO_ID, "full_name": dt.REPO}
                if kwargs.get("method") == "PUT": return {"message": "accepted"}
                return {"type": "file", "path": dt.STATE_FILE, "encoding": "base64", "sha": "d" * 40,
                        "content": base64.b64encode(dt.canonical(stale)).decode()}

        ledger = dt.Ledger(GH()); ledger.load()
        with self.assertRaises(dt.LedgerFailure) as caught:
            ledger.save(intended, purpose="reconcile")
        self.assertEqual(caught.exception.code, "readback_mismatch")
        self.assertEqual(caught.exception.immutable_check, "unavailable")
        self.assertFalse(any("?ref=" in path and path.rsplit("?ref=", 1)[-1] != dt.BRANCH
                             for path, _ in calls))

    def test_malformed_put_response_is_classified_without_exposing_body(self):
        state = dn.new_state()

        class GH:
            def repo(inner, path, **kwargs):
                if not path: return {"id": dt.REPO_ID, "full_name": dt.REPO}
                if kwargs.get("method") == "PUT": raise dt.Error("Invalid JSON data")
                return {"type": "file", "path": dt.STATE_FILE, "encoding": "base64", "sha": "a" * 40,
                        "content": base64.b64encode(dt.canonical(state)).decode()}

        ledger = dt.Ledger(GH()); ledger.load()
        with self.assertRaises(dt.LedgerFailure) as caught:
            ledger.save(state)
        self.assertEqual(caught.exception.code, "put_response_invalid")
        self.assertNotIn("private-body", str(caught.exception))

    def test_ledger_put_rejection_is_typed_redacted_and_not_retried(self):
        state = dn.new_state(); calls = []

        class GH:
            def repo(inner, path, **kwargs):
                calls.append((path, kwargs))
                if not path:
                    return {"id": dt.REPO_ID, "full_name": dt.REPO}
                if kwargs.get("method") == "PUT":
                    raise dt.HTTPError(409, {"X-Debug": "secret-header"}, b"private payload")
                return {"type": "file", "path": dt.STATE_FILE, "encoding": "base64", "sha": "a" * 40,
                        "content": base64.b64encode(dt.canonical(state)).decode()}

        ledger = dt.Ledger(GH()); ledger.load()
        with self.assertRaises(dt.LedgerFailure) as caught:
            ledger.save(state, purpose="delivered")
        error = caught.exception
        self.assertEqual((error.code, error.status, error.purpose), ("put_rejected", 409, "delivered"))
        self.assertNotIn("private", str(error))
        self.assertNotIn("secret", repr(error.__dict__))
        self.assertEqual(sum(kwargs.get("method") == "PUT" for _, kwargs in calls), 1)

    def test_real_ledger_boundary_pending_and_delivered_ambiguous_puts_never_duplicate_post(self):
        message = dn.payload("title", "synthetic", dn.object_url(18), {"owner": "111111111111111111"})
        key = "incident:synthetic"

        class StorageGH:
            def __init__(self, state, fail_call, fail_after_write):
                self.data = dt.canonical(state)
                self.fail_call = fail_call
                self.fail_after_write = fail_after_write
                self.put_count = 0

            def _blob_sha(self, data):
                return dt.hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()

            def repo(self, path, **kwargs):
                if not path:
                    return {"id": dt.REPO_ID, "full_name": dt.REPO}
                if kwargs.get("method") == "PUT":
                    self.put_count += 1
                    if self.put_count == self.fail_call and not self.fail_after_write:
                        raise dt.HTTPError(503)
                    self.data = base64.b64decode(kwargs["payload"]["content"])
                    if self.put_count == self.fail_call:
                        raise dt.Error("Remote request outcome unknown")
                    return {"content": {"sha": self._blob_sha(self.data)},
                            "commit": {"sha": f"{self.put_count:040x}"}}
                if path == f"/contents/{dt.STATE_FILE}?ref={dt.BRANCH}":
                    return {"type": "file", "path": dt.STATE_FILE, "encoding": "base64",
                            "sha": self._blob_sha(self.data),
                            "content": base64.b64encode(self.data).decode()}
                raise AssertionError("Unexpected remote operation")

        for mode, fail_call, after_write, expected_posts, expected_status in (
                ("pending_rejected", 1, False, 0, "ready"),
                ("pending_ambiguous_persisted", 1, True, 0, "pending"),
                ("delivered_ambiguous_persisted", 2, True, 1, "delivered")):
            with self.subTest(mode=mode):
                initial = dn.new_state(); dn.queue(initial, key, message)
                gh = StorageGH(initial, fail_call, after_write)
                ledger = dt.Ledger(gh)
                state = ledger.load(purpose="reconcile")
                posts = []

                class Webhook:
                    def __init__(self, deliveries): self.deliveries = deliveries
                    def send(self, payload):
                        self.deliveries.append(payload)
                        return "333333333333333333"

                with self.assertRaises(dt.LedgerFailure):
                    dn.deliver(state, ledger, Webhook(posts))
                self.assertEqual(len(posts), expected_posts,
                                 f"posts={len(posts)} puts={gh.put_count} durable={dt.json_data(gh.data)}")
                self.assertEqual(gh.put_count, fail_call)
                durable_ledger = dt.Ledger(gh)
                durable_state = durable_ledger.load(purpose="reconcile")
                self.assertEqual(durable_state["outbox"][key]["status"], expected_status)
                followup_calls = []
                followup_posts = Webhook(followup_calls)
                if expected_status == "pending":
                    self.assertEqual(dn.deliver(durable_state, durable_ledger, followup_posts), 1)
                else:
                    self.assertEqual(dn.deliver(durable_state, durable_ledger, followup_posts), 0)
                self.assertEqual(len(followup_calls), 1 if mode == "pending_rejected" else 0)
                self.assertEqual(len(posts), expected_posts)

    def test_ledger_failure_metadata_rejects_untrusted_values(self):
        error = dt.LedgerFailure("untrusted-code", purpose="secret-purpose", operation="secret-operation",
                                 intended_blob_sha="token", observed_blob_sha="webhook",
                                 commit_sha="a" * 40,
                                 expected_digest="webhook", immutable_check="secret")
        self.assertEqual(error.code, "load_unknown")
        self.assertIsNone(error.purpose)
        self.assertEqual(error.operation, "validate")
        self.assertIsNone(error.intended_blob_sha)
        self.assertIsNone(error.observed_blob_sha)
        self.assertEqual(error.commit_sha, "a" * 40)
        self.assertIsNone(error.expected_digest)
        self.assertEqual(error.immutable_check, "unavailable")

    def test_ledger_readback_preserves_typed_http_failure(self):
        state = dn.new_state()

        class GH:
            def repo(inner, path, **kwargs):
                if not path:
                    return {"id": dt.REPO_ID, "full_name": dt.REPO}
                if kwargs.get("method") == "PUT":
                    return {}
                raise dt.HTTPError(503, {"X-Debug": "hidden"}, b"hidden body")

        ledger = dt.Ledger(GH()); ledger.sha = "a" * 40
        with self.assertRaises(dt.LedgerFailure) as caught:
            ledger.save(state, purpose="reconcile")
        self.assertEqual((caught.exception.code, caught.exception.status), ("load_rejected", 503))
        self.assertEqual(caught.exception.purpose, "reconcile")
        self.assertEqual(caught.exception.operation, "readback")
        self.assertIsNotNone(caught.exception.intended_blob_sha)
        self.assertEqual(caught.exception.expected_digest, dt.hashlib.sha256(dt.canonical(state)).hexdigest())
        self.assertNotIn("hidden", str(caught.exception))

    def test_malformed_contents_shapes_are_safe_load_failures(self):
        cases = [[], None,
                 {"type": "file", "path": dt.STATE_FILE, "encoding": "base64", "sha": "a" * 40,
                  "content": None},
                 {"type": "file", "path": dt.STATE_FILE, "encoding": "base64", "sha": "a" * 40,
                  "content": ["not", "text"]},
                 {"type": "file", "path": dt.STATE_FILE, "encoding": "base64", "sha": 7,
                  "content": "e30="}]
        for response in cases:
            with self.subTest(response_type=type(response).__name__):
                class GH:
                    def repo(inner, path, **kwargs):
                        if not path: return {"id": dt.REPO_ID, "full_name": dt.REPO}
                        return response
                ledger = dt.Ledger(GH())
                with self.assertRaises(dt.LedgerFailure) as caught:
                    ledger.load(purpose="recovery")
                self.assertEqual((caught.exception.code, caught.exception.purpose,
                                  caught.exception.operation),
                                 ("load_invalid", "recovery", "readback"))

    def test_nonhttp_and_invalid_json_readback_errors_keep_safe_reason_and_metadata(self):
        state = dn.new_state()
        for source_error, expected_code in ((dt.Error("Remote request outcome unknown"), "load_unknown"),
                                            (dt.Error("Invalid JSON data"), "load_invalid")):
            with self.subTest(code=expected_code):
                class GH:
                    def repo(inner, path, **kwargs):
                        if not path: return {"id": dt.REPO_ID, "full_name": dt.REPO}
                        if kwargs.get("method") == "PUT":
                            return {"content": {"sha": "b" * 40}, "commit": {"sha": "c" * 40}}
                        raise source_error
                ledger = dt.Ledger(GH()); ledger.sha = "a" * 40
                with self.assertRaises(dt.LedgerFailure) as caught:
                    ledger.save(state, purpose="pending")
                error = caught.exception
                self.assertEqual((error.code, error.purpose, error.operation),
                                 (expected_code, "pending", "readback"))
                self.assertEqual(error.intended_blob_sha, dt.hashlib.sha1(
                    b"blob " + str(len(dt.canonical(state))).encode() + b"\0" + dt.canonical(state)).hexdigest())
                self.assertEqual(error.put_blob_sha, "b" * 40)
                self.assertEqual(error.commit_sha, "c" * 40)
                self.assertNotIn("Remote request", str(error))
                self.assertNotIn("Invalid JSON", str(error))

    def test_diagnostic_cli_only_accepts_fixed_host_get(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import discord_ledger_diagnose as diag
        command = diag._command(diag.BRANCH_CONTENT_RESOURCE)
        self.assertEqual(command[command.index("--method") + 1], "GET")
        self.assertNotIn("token", " ".join(command).lower())
        with self.assertRaises(diag.DiagnosticUnavailable):
            diag._command("repos/Aurelia-aurity/Replica/contents/discord-state.json", method="POST")
        with self.assertRaises(diag.DiagnosticUnavailable):
            diag._command("repos/other/repository/contents/discord-state.json")
        with self.assertRaises(diag.DiagnosticUnavailable):
            diag._command("repos/Aurelia-aurity/Replica/issues/48")
        self.assertEqual(diag.MAX_SAMPLES * diag.MAX_REQUESTS_PER_SAMPLE, 18)

    def test_diagnostic_ref_requires_exact_branch_and_commit_binding(self):
        import discord_ledger_diagnose as diag
        sha = "a" * 40
        valid = {"ref": diag.REF_NAME, "object": {"type": "commit", "sha": sha}}
        with patch.object(diag, "_run_readonly", return_value=valid):
            self.assertEqual(diag.GhApi().get_ref(), sha)

        invalid = (
            {"ref": "refs/heads/main", "object": {"type": "commit", "sha": sha}},
            {"object": {"type": "commit", "sha": sha}},
            {"ref": diag.REF_NAME, "object": {"type": "blob", "sha": sha}},
            {"ref": diag.REF_NAME, "object": {"sha": sha}},
        )
        for value in invalid:
            with self.subTest(value=value), patch.object(diag, "_run_readonly", return_value=value):
                report = diag.sample(diag.GhApi())
                self.assertEqual(report["status"], "unavailable")
                self.assertEqual(report["codes"], ["invalid_shape"])

        data = b"synthetic-ledger"
        blob_sha = dt.hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        stable_responses = [
            valid,
            {"sha": sha, "tree": {"sha": "b" * 40}},
            {"sha": "b" * 40, "truncated": False, "tree": [
                {"path": dt.STATE_FILE, "mode": "100644", "type": "blob", "sha": blob_sha}]},
            {"type": "file", "path": dt.STATE_FILE, "encoding": "base64", "sha": blob_sha,
             "content": base64.b64encode(data).decode()},
            {"type": "file", "path": dt.STATE_FILE, "encoding": "base64", "sha": blob_sha,
             "content": base64.b64encode(data).decode()},
            valid,
        ]
        for value in invalid:
            with self.subTest(final_ref=value), patch.object(
                    diag, "_run_readonly", side_effect=[*stable_responses[:-1], value]):
                report = diag.sample(diag.GhApi())
                self.assertEqual(report["status"], "unavailable")
                self.assertEqual(report["codes"], ["invalid_shape"])

    def test_diagnostic_cli_contains_duplicate_and_deep_json_failures(self):
        import contextlib
        import discord_ledger_diagnose as diag

        sentinel = "private-nested-response-body"
        duplicate = (b'{"ref":"' + diag.REF_NAME.encode() + b'","object":{"type":"commit",'
                     b'"sha":"' + b"a" * 40 + b'","sha":"' + b"b" * 40 + b'"}}')
        deeply_nested = b"[" * 10_000 + b'"' + sentinel.encode() + b'"' + b"]" * 10_000

        class FakeProcess:
            def __init__(self, output):
                read_fd, write_fd = os.pipe()
                os.write(write_fd, output)
                os.close(write_fd)
                self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            def wait(self, timeout=None): return 0
            def kill(self): pass

        valid_json = (b'{"ref":"' + diag.REF_NAME.encode() + b'","object":{"type":"commit",'
                      b'"sha":"' + b"a" * 40 + b'"}}')
        for label, payload, injected_recursion in (("duplicate_keys", duplicate, False),
                                                   ("deep_nesting", deeply_nested, False),
                                                   ("injected_recursion", valid_json, True)):
            with self.subTest(case=label):
                process = FakeProcess(payload)
                stdout = io.StringIO()
                stderr = io.StringIO()
                with patch.object(diag.subprocess, "Popen", return_value=process), \
                     contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    if injected_recursion:
                        with patch.object(diag.json, "loads", side_effect=RecursionError(sentinel)):
                            result = diag.main(["--samples", "1"])
                    else:
                        result = diag.main(["--samples", "1"])
                self.assertEqual(result, 1)
                report = json.loads(stdout.getvalue())
                self.assertEqual(report["samples"][0]["status"], "unavailable")
                self.assertEqual(report["samples"][0]["codes"], ["invalid_json"])
                self.assertNotIn(sentinel, stdout.getvalue())
                self.assertNotIn(payload.decode(), stdout.getvalue())
                self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())

    def test_diagnostic_duplicate_keys_are_rejected_at_all_six_response_positions(self):
        import contextlib
        import discord_ledger_diagnose as diag

        commit_sha, tree_sha = "a" * 40, "b" * 40
        data = b"synthetic-ledger"
        blob_sha = dt.hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        content = {"type": "file", "path": dt.STATE_FILE, "encoding": "base64",
                   "sha": blob_sha, "content": base64.b64encode(data).decode()}
        valid = [
            {"ref": diag.REF_NAME, "object": {"type": "commit", "sha": commit_sha}},
            {"sha": commit_sha, "tree": {"sha": tree_sha}},
            {"sha": tree_sha, "truncated": False, "tree": [
                {"path": dt.STATE_FILE, "mode": "100644", "type": "blob", "sha": blob_sha}]},
            content,
            content,
            {"ref": diag.REF_NAME, "object": {"type": "commit", "sha": commit_sha}},
        ]
        duplicates = [
            '{"ref":' + json.dumps(diag.REF_NAME) + ',"object":{"type":"commit","sha":"' +
            commit_sha + '","sha":"' + "c" * 40 + '"}}',
            '{"sha":"' + commit_sha + '","sha":"' + "c" * 40 + '","tree":{"sha":"' +
            tree_sha + '"}}',
            '{"sha":"' + tree_sha + '","truncated":false,"tree":[],"tree":[]}',
            '{"type":"file","path":' + json.dumps(dt.STATE_FILE) +
            ',"encoding":"base64","sha":"' + blob_sha + '","sha":"' + "c" * 40 +
            '","content":"' + base64.b64encode(data).decode() + '"}',
            '{"type":"file","path":' + json.dumps(dt.STATE_FILE) +
            ',"encoding":"base64","sha":"' + blob_sha + '","sha":"' + "c" * 40 +
            '","content":"' + base64.b64encode(data).decode() + '"}',
            '{"ref":' + json.dumps(diag.REF_NAME) + ',"ref":"refs/heads/main",' +
            '"object":{"type":"commit","sha":"' + commit_sha + '"}}',
        ]

        class FakeProcess:
            def __init__(self, output):
                read_fd, write_fd = os.pipe()
                os.write(write_fd, output)
                os.close(write_fd)
                self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            def wait(self, timeout=None): return 0
            def kill(self): pass

        for index, raw_duplicate in enumerate(duplicates):
            with self.subTest(response_position=index + 1):
                outputs = [json.dumps(item, separators=(",", ":")).encode() for item in valid]
                outputs[index] = raw_duplicate.encode()
                response_body = outputs[index].decode()
                stdout, stderr = io.StringIO(), io.StringIO()

                def popen(*_args, **_kwargs):
                    return FakeProcess(outputs.pop(0))

                with patch.object(diag.subprocess, "Popen", side_effect=popen), \
                     contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = diag.main(["--samples", "1"])
                report = json.loads(stdout.getvalue())
                self.assertEqual(result, 1)
                self.assertEqual(report["samples"][0]["status"], "unavailable")
                self.assertEqual(report["samples"][0]["codes"], ["invalid_json"])
                self.assertNotIn(response_body, stdout.getvalue())
                self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())
                self.assertEqual(len(outputs), 5 - index)

    def test_diagnostic_json_encoding_and_utf8_depth_boundaries(self):
        import contextlib
        import discord_ledger_diagnose as diag

        commit_sha, tree_sha = "a" * 40, "b" * 40
        data = b"synthetic-ledger"
        blob_sha = dt.hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        content = {"type": "file", "path": dt.STATE_FILE, "encoding": "base64",
                   "sha": blob_sha, "content": base64.b64encode(data).decode()}
        note = 'escaped quote: " and brackets [ ] { } and backslash \\'

        def ref_json(array_depth, *, include_sentinel=False):
            details = {"ref": diag.REF_NAME,
                       "object": {"type": "commit", "sha": commit_sha},
                       "note": note + (" " + "UNIQUE_PRIVATE_RESPONSE_BODY" if include_sentinel else "")}
            prefix = json.dumps(details, separators=(",", ":"))[:-1] + ',"extra":'
            return (prefix + "[" * array_depth + "0" + "]" * array_depth + "}").encode("utf-8")

        class FakeProcess:
            def __init__(self, output):
                read_fd, write_fd = os.pipe()
                os.write(write_fd, output)
                os.close(write_fd)
                self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            def wait(self, timeout=None): return 0
            def kill(self): pass

        # Root object plus 127 arrays is exactly 128 structural levels.
        valid_responses = [ref_json(127),
                           json.dumps({"sha": commit_sha, "tree": {"sha": tree_sha}}).encode(),
                           json.dumps({"sha": tree_sha, "truncated": False, "tree": [
                               {"path": dt.STATE_FILE, "mode": "100644", "type": "blob",
                                "sha": blob_sha}]}).encode(),
                           json.dumps(content).encode(), json.dumps(content).encode(),
                           json.dumps({"ref": diag.REF_NAME,
                                       "object": {"type": "commit", "sha": commit_sha}}).encode()]
        valid_outputs = list(valid_responses)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(diag.subprocess, "Popen", side_effect=lambda *_a, **_k:
                          FakeProcess(valid_outputs.pop(0))), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = diag.main(["--samples", "1"])
        valid_report = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(valid_report["samples"][0]["status"], "stable")
        self.assertFalse(valid_outputs)
        self.assertEqual(stderr.getvalue(), "")

        for label, payload in (("depth_129", ref_json(128, include_sentinel=True)),
                               ("utf8_bom", json.dumps({"ref": diag.REF_NAME,
                                "object": {"type": "commit", "sha": commit_sha}}).encode("utf-8-sig"))):
            with self.subTest(case=label):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(diag.subprocess, "Popen", return_value=FakeProcess(payload)), \
                     contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = diag.main(["--samples", "1"])
                report = json.loads(stdout.getvalue())
                self.assertEqual(result, 1)
                self.assertEqual(report["samples"][0]["status"], "unavailable")
                self.assertEqual(report["samples"][0]["codes"], ["invalid_json"])
                self.assertNotIn("UNIQUE_PRIVATE_RESPONSE_BODY", stdout.getvalue())
                self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())

        counterexample = (json.dumps({"ref": diag.REF_NAME,
                        "object": {"type": "commit", "sha": commit_sha}, "note": '"'},
                        separators=(",", ":"))[:-1] + ',"extra":' + "[" * 200 + "0" + "]" * 200 + "}")
        encoded_responses = []
        for encoding in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
            encoded_responses.append((encoding + "_no_bom", counterexample.encode(encoding)))
            encoded_responses.append((encoding + "_bom", ("\ufeff" + counterexample).encode(encoding)))
        for encoding, payload in encoded_responses:
            with self.subTest(encoding=encoding):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(diag.subprocess, "Popen", return_value=FakeProcess(payload)), \
                     contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = diag.main(["--samples", "1"])
                report = json.loads(stdout.getvalue())
                self.assertEqual(result, 1)
                self.assertEqual(report["samples"][0]["status"], "unavailable")
                self.assertEqual(report["samples"][0]["codes"], ["invalid_json"])
                self.assertNotIn(counterexample, stdout.getvalue())
                self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())

    def test_diagnostic_cli_binds_content_to_validated_commit_tree_entry(self):
        import discord_ledger_diagnose as diag
        commit_sha, tree_sha, blob_sha = "a" * 40, "b" * 40, "c" * 40
        with patch.object(diag, "_run_readonly", side_effect=[
                {"sha": commit_sha, "tree": {"sha": tree_sha}},
                {"sha": tree_sha, "truncated": False, "tree": [
                    {"path": dt.STATE_FILE, "mode": "100644", "type": "blob", "sha": blob_sha}]}]) as call:
            api = diag.GhApi()
            self.assertEqual(api.get_commit_tree(commit_sha), tree_sha)
            self.assertEqual(api.get_tree_blob(tree_sha), blob_sha)
        self.assertEqual(call.call_args_list[0].args[0], diag.COMMIT_PREFIX + commit_sha)
        self.assertEqual(call.call_args_list[1].args[0], diag.TREE_PREFIX + tree_sha)
        with patch.object(diag, "_run_readonly", return_value={"sha": "d" * 40, "tree": {"sha": tree_sha}}):
            with self.assertRaises(diag.DiagnosticUnavailable):
                diag.GhApi().get_commit_tree(commit_sha)

    def test_diagnostic_cli_classifies_stable_raced_inconsistent_and_unavailable(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import discord_ledger_diagnose as diag
        data_a, data_b = b"synthetic-a", b"synthetic-b"
        sha_a = dt.hashlib.sha1(b"blob " + str(len(data_a)).encode() + b"\0" + data_a).hexdigest()
        sha_b = dt.hashlib.sha1(b"blob " + str(len(data_b)).encode() + b"\0" + data_b).hexdigest()

        class API:
            def __init__(self, refs, contents, tree_blobs=None):
                self.refs, self.contents = iter(refs), iter(contents)
                self.tree_blobs = iter(tree_blobs or [sha_a])
                self.requests = 0
            def get_ref(self):
                self.requests += 1
                value = next(self.refs)
                if isinstance(value, Exception): raise value
                return value
            def get_commit_tree(self, sha):
                self.requests += 1
                return "e" * 40
            def get_tree_blob(self, sha):
                self.requests += 1
                value = next(self.tree_blobs)
                if isinstance(value, Exception): raise value
                return value
            def get_contents(self, ref):
                self.requests += 1
                value = next(self.contents)
                if isinstance(value, Exception): raise value
                return value

        stable_api = API(["a" * 40, "a" * 40], [(sha_a, data_a), (sha_a, data_a)], [sha_a])
        stable = diag.sample(stable_api)
        raced = diag.sample(API(["a" * 40, "b" * 40], [(sha_a, data_a), (sha_a, data_a)], [sha_a]))
        inconsistent = diag.sample(API(["a" * 40, "a" * 40], [(sha_a, data_a), (sha_b, data_b)], [sha_a]))
        tree_mismatch = diag.sample(API(["a" * 40, "a" * 40], [(sha_a, data_a), (sha_a, data_a)], [sha_b]))
        unavailable = diag.sample(API(["a" * 40], [], [diag.DiagnosticUnavailable("timeout")]))
        self.assertEqual([stable["status"], raced["status"], inconsistent["status"], unavailable["status"]],
                         ["stable", "raced", "inconsistent", "unavailable"])
        self.assertEqual(tree_mismatch["status"], "inconsistent")
        self.assertIn("immutable_tree_disagree", tree_mismatch["codes"])
        self.assertEqual(stable_api.requests, diag.MAX_REQUESTS_PER_SAMPLE)

    def test_diagnostic_cli_output_never_contains_ledger_bytes(self):
        import contextlib
        import discord_ledger_diagnose as diag
        secret_like = b"synthetic-private-message-body"
        blob_sha = dt.hashlib.sha1(b"blob " + str(len(secret_like)).encode() + b"\0" + secret_like).hexdigest()

        class API:
            def get_ref(self): return "a" * 40
            def get_commit_tree(self, sha): return "e" * 40
            def get_tree_blob(self, sha): return blob_sha
            def get_contents(self, ref): return blob_sha, secret_like

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(diag.main(["--samples", "1"], api_factory=API), 0)
        self.assertNotIn(secret_like.decode(), output.getvalue())
        report = dt.json_data(output.getvalue().encode())
        self.assertEqual(report["samples"][0]["status"], "stable")
        self.assertFalse(report["freshness_claimed"])

    def test_content_accepts_only_base64_ascii_line_wrapping(self):
        import discord_ledger_diagnose as diag
        raw = b"synthetic-content"
        sha = dt.hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        wrapped = base64.b64encode(raw).decode()
        wrapped = wrapped[:5] + "\n" + wrapped[5:]
        value = {"type": "file", "path": dt.STATE_FILE, "encoding": "base64", "sha": sha,
                 "content": wrapped}
        self.assertEqual(diag._content(value), (sha, raw))
        value["content"] = wrapped.replace("\n", "\v")
        with self.assertRaises(diag.DiagnosticUnavailable):
            diag._content(value)

    def test_diagnostic_process_enforces_output_and_time_bounds(self):
        import discord_ledger_diagnose as diag

        class FakeProcess:
            def __init__(self, output=b"", keep_open=False):
                read_fd, write_fd = os.pipe()
                if output:
                    os.write(write_fd, output)
                if not keep_open:
                    os.close(write_fd)
                self.write_fd = write_fd if keep_open else None
                self.stdout = os.fdopen(read_fd, "rb", buffering=0)
                self.killed = False
            def kill(self):
                self.killed = True
                if self.write_fd is not None:
                    os.close(self.write_fd)
                    self.write_fd = None
            def wait(self, timeout=None): return 0

        output_process = FakeProcess(b"{" * 11)
        with patch.object(diag, "MAX_OUTPUT_BYTES", 10), \
             patch.object(diag.subprocess, "Popen", return_value=output_process) as popen:
            with self.assertRaises(diag.DiagnosticUnavailable) as caught:
                diag._run_readonly(diag.REF_RESOURCE)
        self.assertEqual(caught.exception.code, "output_limit")
        self.assertTrue(output_process.killed)
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(popen.call_args.args[0][popen.call_args.args[0].index("--method") + 1], "GET")

        timeout_process = FakeProcess(keep_open=True)

        class Selector:
            def register(self, *_args): pass
            def select(self, _timeout): return []
            def close(self): pass

        with patch.object(diag.selectors, "DefaultSelector", Selector), \
             patch.object(diag.subprocess, "Popen", return_value=timeout_process):
            with self.assertRaises(diag.DiagnosticUnavailable) as caught:
                diag._run_readonly(diag.REF_RESOURCE)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertTrue(timeout_process.killed)

    def test_diagnostic_cleanup_wait_is_bounded_and_preserves_primary_limit_code(self):
        import discord_ledger_diagnose as diag

        class SlowCleanupProcess:
            def __init__(self):
                read_fd, write_fd = os.pipe()
                os.write(write_fd, b"0123456789")
                os.close(write_fd)
                self.stdout = os.fdopen(read_fd, "rb", buffering=0)
                self.wait_calls = []
            def kill(self): pass
            def wait(self, timeout=None):
                self.wait_calls.append(timeout)
                raise diag.subprocess.TimeoutExpired("gh", timeout)

        process = SlowCleanupProcess()
        with patch.object(diag, "MAX_OUTPUT_BYTES", 5), \
             patch.object(diag.subprocess, "Popen", return_value=process):
            with self.assertRaises(diag.DiagnosticUnavailable) as caught:
                diag._run_readonly(diag.REF_RESOURCE)
        self.assertEqual(caught.exception.code, "output_limit")
        self.assertEqual(process.wait_calls, [diag.CLEANUP_TIMEOUT_SECONDS])

    def test_cli_redacts_child_stderr_and_invalid_or_failed_child_output(self):
        import contextlib
        import discord_ledger_diagnose as diag

        sentinel = "private-webhook-token-child-stderr"

        class FakeProcess:
            def __init__(self, output, status):
                read_fd, write_fd = os.pipe()
                os.write(write_fd, output)
                os.close(write_fd)
                self.stdout = os.fdopen(read_fd, "rb", buffering=0)
                self.status = status
                self.stderr_payload = sentinel
                self.killed = False
            def wait(self, timeout=None): return self.status
            def kill(self): self.killed = True

        for output, status, expected_code in ((sentinel.encode(), 0, "invalid_json"),
                                               (b"private child failure", 17, "gh_failed")):
            with self.subTest(status=status):
                process = FakeProcess(output, status)
                captured = io.StringIO()
                with patch.object(diag.subprocess, "Popen", return_value=process) as popen, \
                     contextlib.redirect_stdout(captured), contextlib.redirect_stderr(io.StringIO()):
                    result = diag.main(["--samples", "1"])
                self.assertEqual(result, 1)
                self.assertIs(popen.call_args.kwargs["stderr"], diag.subprocess.DEVNULL)
                self.assertEqual(popen.call_args.kwargs["stdin"], diag.subprocess.DEVNULL)
                self.assertFalse(popen.call_args.kwargs["shell"])
                self.assertEqual(process.stderr_payload, sentinel)
                self.assertNotIn(sentinel, captured.getvalue())
                self.assertNotIn("private child failure", captured.getvalue())
                report = json.loads(captured.getvalue())
                self.assertEqual(report["samples"][0]["codes"], [expected_code])
                self.assertEqual(report["samples"][0]["status"], "unavailable")

    def test_pagination_total_truncation_fails(self):
        gh = dt.GitHub("synthetic")
        with patch.object(gh, "call", return_value=({"total_count": 2, "workflow_runs": [{}]}, {})):
            with self.assertRaises(dt.Error): gh.pages("/actions/runs", "workflow_runs")
        with patch.object(gh, "call", side_effect=[([{}] * 100, {"Link": '<x>; rel="next"'}),
                                                   ([{}], {})]) as calls:
            self.assertEqual(len(gh.pages("/issues")), 101)
            self.assertIn("page=2", calls.call_args[0][0])

    def test_ledger_cas_bot_both_roles_and_readback(self):
        state = dn.new_state(); captured = []
        class GH:
            def repo(inner, path, **kwargs):
                captured.append((path, kwargs))
                if not path: return {"id": dt.REPO_ID, "full_name": dt.REPO}
                if kwargs.get("method") == "PUT": return {}
                return {"type": "file", "path": dt.STATE_FILE, "encoding": "base64", "sha": SHA,
                        "content": base64.b64encode(dt.canonical(state)).decode()}
        ledger = dt.Ledger(GH()); self.assertEqual(ledger.load(), state)
        ledger.save(state)
        mutation = next(k["payload"] for _, k in captured if k.get("method") == "PUT")
        self.assertEqual(mutation["branch"], dt.BRANCH)
        self.assertEqual(mutation["sha"], SHA)
        self.assertEqual(mutation["author"], dt.BOT)
        self.assertEqual(mutation["committer"], dt.BOT)

    def test_orphan_bootstrap_and_response_loss_exact_readback(self):
        state = dn.new_state(); mutations = []
        class GH:
            ready = False
            def repo(inner, path, **kwargs):
                if not path: return {"id": dt.REPO_ID, "full_name": dt.REPO}
                if kwargs.get("method") == "POST":
                    mutations.append((path, kwargs["payload"]))
                    if path == "/git/refs":
                        inner.ready = True; raise dt.Error("lost response")
                    return {"sha": SHA}
                if not inner.ready: raise dt.HTTPError(404)
                return {"type": "file", "path": dt.STATE_FILE, "encoding": "base64", "sha": SHA,
                        "content": base64.b64encode(dt.canonical(state)).decode()}
        ledger = dt.Ledger(GH()); self.assertIsNone(ledger.load())
        ledger.save(state, bootstrap=True)
        commit = next(x for p, x in mutations if p == "/git/commits")
        self.assertEqual(commit["parents"], [])
        self.assertEqual(commit["author"], dt.BOT); self.assertEqual(commit["committer"], dt.BOT)
        ref = next(x for p, x in mutations if p == "/git/refs")
        self.assertEqual(ref["sha"], SHA)
        tree = next(x for p, x in mutations if p == "/git/trees")
        self.assertEqual(len(tree["tree"]), 1)
        self.assertFalse(any("main" in p for p, _ in mutations))

    def test_bootstrap_missing_commit_sha_aborts_before_ref_post(self):
        state = dn.new_state(); mutations = []

        class GH:
            def repo(inner, path, **kwargs):
                if not path:
                    return {"id": dt.REPO_ID, "full_name": dt.REPO}
                if kwargs.get("method") == "POST":
                    mutations.append(path)
                    if path == "/git/commits":
                        return {"message": "commit response without required SHA"}
                    return {"sha": SHA}
                raise AssertionError("Bootstrap without commit SHA must not read back")

        ledger = dt.Ledger(GH())
        with self.assertRaises(dt.LedgerFailure) as caught:
            ledger.save(state, bootstrap=True)
        self.assertEqual((caught.exception.code, caught.exception.purpose,
                          caught.exception.operation), ("put_unknown", "bootstrap", "put"))
        self.assertEqual(mutations, ["/git/blobs", "/git/trees", "/git/commits"])
        self.assertNotIn("/git/refs", mutations)

    def test_missing_file_existing_branch_cannot_reinitialize(self):
        class GH:
            def repo(inner, path, **kwargs):
                if not path: return {"id": dt.REPO_ID, "full_name": dt.REPO}
                if "/contents/" in path: raise dt.HTTPError(404)
                return {"ref": "refs/heads/" + dt.BRANCH}
        with self.assertRaises(dt.Error): dt.Ledger(GH()).load()

    def test_bad_ledger_and_duplicate_json_are_errors(self):
        bad = dn.new_state(); bad["sync_cursor"] = [1, 2, True]
        with self.assertRaises(dt.Error): dt.validate_state(bad)
        with self.assertRaises(dt.Error): dt.json_data(b'{"schema":1,"schema":2}')

    def test_sync_branch_fork_and_dispatch_rejected(self):
        good = run(1); good.update(workflow_id=dn.SYNC_ID, path=dn.SYNC_PATH, event="schedule")
        class GH:
            def repo(inner, path): raise AssertionError("Unexpected lookup")
        self.assertTrue(dn.trusted_sync(GH(), good, SHA))
        for mutation in ({"head_branch": "fix/17-project-add-readback"},
                         {"head_repository": {"id": 99}}, {"workflow_id": 999},
                         {"event": "workflow_dispatch", "actor": {"id": 99}}):
            self.assertFalse(dn.trusted_sync(GH(), {**good, **mutation}, SHA))

    def test_artifact_attempt_missing_never_falls_back(self):
        r = run(1, attempt=2)
        class GH:
            def repo(inner, path): return r
            def pages(inner, path, field):
                return [{"name": "notion-notification-1-1"}]
        with self.assertRaises(dt.Error): dt.artifact_report(GH(), r)

    def test_artifact_list_detail_binding_conflicts_are_rejected_before_download(self):
        listed = run(1)
        cases = (
            {"conclusion": "failure"},
            {"head_sha": "b" * 40},
            {"run_number": 2},
            {"run_attempt": 2},
            {"repository": {"id": dt.REPO_ID + 1}},
            {"head_repository": {"id": dt.REPO_ID + 1}},
            {"workflow_id": 101},
            {"path": ".github/workflows/other.yml"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                detail = copy.deepcopy(listed)
                detail.update(changes)
                class GH:
                    token = "synthetic"
                    def repo(inner, path): return detail
                    def pages(inner, path, field):
                        return [{"id": 99, "name": "notion-notification-1-1", "expired": False,
                                 "size_in_bytes": 200, "workflow_run": {
                                     "id": 1, "head_sha": SHA, "repository_id": dt.REPO_ID,
                                     "head_repository_id": dt.REPO_ID}}]
                with patch.object(dt, "request", side_effect=AssertionError("Mismatch must stop before download")):
                    with self.assertRaises(dt.Error): dt.artifact_report(GH(), listed)

    def test_artifact_download_rechecks_complete_binding_but_ignores_unbound_metadata(self):
        listed = run(1)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr(dt.nr.FILE_NAME, json.dumps(report(1)))
        artifact = {"id": 99, "name": "notion-notification-1-1", "expired": False,
                    "size_in_bytes": 200, "workflow_run": {
                        "id": 1, "head_sha": SHA, "repository_id": dt.REPO_ID,
                        "head_repository_id": dt.REPO_ID}}

        class GH:
            token = "synthetic"
            calls = 0
            def repo(inner, path):
                inner.calls += 1
                if inner.calls == 1:
                    # Irrelevant display metadata is intentionally not bound.
                    return {**listed, "display_title": "changed"}
                return {**listed, "run_number": 2}
            def pages(inner, path, field): return [artifact]

        with patch.object(dt, "request", return_value=(200, {}, buf.getvalue())):
            with self.assertRaises(dt.Error): dt.artifact_report(GH(), listed)

        class StableGH:
            token = "synthetic"
            calls = 0
            def repo(inner, path):
                inner.calls += 1
                return {**listed, "display_title": f"unbound-{inner.calls}"}
            def pages(inner, path, field): return [artifact]
        with patch.object(dt, "request", return_value=(200, {}, buf.getvalue())):
            accepted = dt.artifact_report(StableGH(), listed)
        self.assertEqual(accepted["run_id"], 1)

    def test_manual_actor_identity_is_bound_before_and_after_download(self):
        listed = {**run(1), "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH,
                  "event": "workflow_dispatch", "actor": {"id": int(dn.PM_ID)},
                  "triggering_actor": {"id": int(dn.PM_ID)}}
        artifact = {"id": 99, "name": "notion-notification-1-1", "expired": False,
                    "size_in_bytes": 200, "workflow_run": {
                        "id": 1, "head_sha": SHA, "repository_id": dt.REPO_ID,
                        "head_repository_id": dt.REPO_ID}}
        class DetailGH:
            token = "synthetic"
            def repo(inner, path):
                return {**listed, "actor": {"id": 22}, "triggering_actor": {"id": 22}}
            def pages(inner, path, field): return [artifact]
        with patch.object(dt, "request", side_effect=AssertionError("Actor conflict must stop before download")):
            with self.assertRaises(dt.Error): dt.artifact_report(DetailGH(), listed)

        class DownloadRaceGH:
            token = "synthetic"
            calls = 0
            def repo(inner, path):
                inner.calls += 1
                if inner.calls == 1: return listed
                return {**listed, "actor": {"id": 22}, "triggering_actor": {"id": 22}}
            def pages(inner, path, field): return [artifact]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr(dt.nr.FILE_NAME, json.dumps(report(1)))
        with patch.object(dt, "request", return_value=(200, {}, buf.getvalue())):
            with self.assertRaises(dt.Error): dt.artifact_report(DownloadRaceGH(), listed)

        class NonManualGH:
            token = "synthetic"
            calls = 0
            def repo(inner, path):
                inner.calls += 1
                return {**run(1), "actor": {"id": 22 + inner.calls}}
            def pages(inner, path, field): return [artifact]
        with patch.object(dt, "request", return_value=(200, {}, buf.getvalue())):
            accepted = dt.artifact_report(NonManualGH(), run(1))
        self.assertEqual(accepted["run_id"], 1)

    def test_artifact_race_after_download_rejected(self):
        r = run(1); buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr(dt.nr.FILE_NAME, json.dumps(report(1)))
        class GH:
            token = "synthetic"
            calls = 0
            def repo(inner, path):
                inner.calls += 1
                return r if inner.calls == 1 else {**r, "run_attempt": 2, "status": "in_progress"}
            def pages(inner, path, field):
                return [{"id": 99, "name": "notion-notification-1-1", "expired": False, "size_in_bytes": 200,
                         "workflow_run": {"id": 1, "head_sha": SHA, "repository_id": dt.REPO_ID,
                                          "head_repository_id": dt.REPO_ID}}]
        with patch.object(dt, "request", return_value=(200, {}, buf.getvalue())):
            with self.assertRaises(dt.Error): dt.artifact_report(GH(), r)

    def test_manual_guard_boundaries(self):
        env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
               "GITHUB_ACTOR_ID": dn.PM_ID, "GITHUB_ACTOR": dn.PM, "GITHUB_TRIGGERING_ACTOR": dn.PM,
               "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": SHA, "DISCORD_APPROVED_SHA": SHA}
        self.assertTrue(dn.manual_guard(env))
        for key, value in (("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_ACTOR_ID", "99"),
                           ("GITHUB_REF", "refs/heads/feature"), ("DISCORD_APPROVED_SHA", "b" * 40)):
            self.assertFalse(dn.manual_guard({**env, key: value}))

    def test_other_channel_in_same_guild_is_rejected_before_request(self):
        with patch.object(dt, "request", side_effect=AssertionError("Must not query unapproved channel")):
            with self.assertRaises(dt.Error):
                dt.Discord("https://discord.com/api/webhooks/111111111111111111/"+"a"*40,
                           "222222222222222222")

    def test_project_token_write_scope_skips_graphql(self):
        with patch.object(dt.GitHub, "call", return_value=({}, {"X-OAuth-Scopes": "read:project, project"})), \
                patch("github_project.GraphQLClient", side_effect=AssertionError("Must not query")):
            self.assertEqual(dn.project_backlog("synthetic", []), set())


class DraftSinglePost(unittest.TestCase):
    def client(self):
        return dt.Discord("https://discord.com/api/webhooks/111111111111111111/" + "a" * 40, dt.CHANNEL_ID)

    def test_no_retry_mode_limits_actual_post_on_errors_and_success(self):
        payload = {"content": "synthetic test"}
        receipt = {"id": "333333333333333333", "channel_id": dt.CHANNEL_ID,
                   "webhook_id": "111111111111111111", **payload}
        outcomes = [dt.HTTPError(429, body=b'{"retry_after":0}'), dt.HTTPError(403),
                    dt.HTTPError(500), dt.Error("ambiguous"), (200, {}, json.dumps(receipt).encode())]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__), patch.object(dt, "request") as request, \
                    patch.object(dt.time, "sleep") as sleep:
                request.side_effect = [outcome]
                if isinstance(outcome, Exception):
                    with self.assertRaises(dt.Error): self.client().send(payload, retry_rate_limit=False)
                else:
                    self.assertEqual(self.client().send(payload, retry_rate_limit=False), receipt["id"])
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args.kwargs["method"], "POST")
                sleep.assert_not_called()

    def test_operational_rate_limit_retry_remains_default(self):
        payload = {"content": "synthetic test"}
        receipt = {"id": "333333333333333333", "channel_id": dt.CHANNEL_ID,
                   "webhook_id": "111111111111111111", **payload}
        with patch.object(dt, "request", side_effect=[dt.HTTPError(429, body=b'{"retry_after":0}'),
                                                     (200, {}, json.dumps(receipt).encode())]) as request, \
                patch.object(dt.time, "sleep") as sleep:
            self.assertEqual(self.client().send(payload), receipt["id"])
            self.assertEqual(request.call_count, 2)
            sleep.assert_called_once_with(0.0)

    def test_untrusted_rate_limit_retry_time_is_definite_rejection_without_retry(self):
        bodies = (b"{}", b'{"retry_after":"later"}', b'{"retry_after":11}',
                  b'{"retry_after":true}')
        for body in bodies:
            with self.subTest(body=body), patch.object(dt, "request",
                    side_effect=dt.HTTPError(429, body=body)) as request, \
                    patch.object(dt.time, "sleep") as sleep:
                with self.assertRaises(dt.RateLimitRejected) as raised:
                    self.client().send({"content": "synthetic test"})
                self.assertEqual(raised.exception.status, 429)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args.kwargs["method"], "POST")
                sleep.assert_not_called()


class ComparePathRegression(unittest.TestCase):
    def test_actual_call_accepts_exact_compare_and_regular_route(self):
        gh = dt.GitHub("synthetic")
        path = f"/repos/{dt.REPO}/compare/{SHA}...{'b' * 40}"
        with patch.object(dt, "request", return_value=(200, {}, b'{}')) as request:
            gh.call(path)
            request.assert_called_once_with("https://api.github.com" + path, method="GET", token="synthetic", payload=None)
        with patch.object(dt, "request", return_value=(200, {}, b'[]')) as request:
            gh.repo("/issues")
            self.assertEqual(request.call_count, 1)
        with patch.object(dt, "request", return_value=(200, {}, b'{}')) as request:
            gh.repo("/contents/docs/file%20name.md?ref=automation%2Fdiscord-state")
            self.assertEqual(request.call_count, 1)

    def test_compare_malformed_encoded_and_mutating_rejected_before_http(self):
        prefix = f"/repos/{dt.REPO}/compare/"
        paths = [prefix + "main", prefix + "main%2E%2E%2Emain",
                 prefix + SHA + "%2e%2e%2e" + SHA,
                 prefix + SHA + "..." + SHA + "/extra",
                 prefix + SHA + "..." + SHA + "?x=1",
                 f"/repos/other/Replica/compare/{SHA}...{SHA}",
                 f"/repos/other/Replica/compare/main",
                 f"/repos/{dt.REPO}/compare%2Fmain",
                 f"/repos/{dt.REPO}/%63ompare/main",
                 f"/repos/{dt.REPO}/%2E/compare/main",
                 f"/repos/{dt.REPO}/%2e%2e/Replica/compare/main",
                 f"/repos/{dt.REPO}/issues/%2e%2e/pulls",
                 f"/repos/{dt.REPO}/issues%2F1",
                 f"/repos/{dt.REPO}/issues/%252e%252e/pulls",
                 f"/repos/{dt.REPO}/issues/%5C../pulls",
                 f"/repos/{dt.REPO}/issues/%0a/pulls",
                 "/repos/../issues", "relative", prefix + "A" * 40 + "..." + SHA]
        for path in paths:
            with self.subTest(path=path), patch.object(dt, "request") as request:
                with self.assertRaises(dt.Error): dt.GitHub("synthetic").call(path)
                request.assert_not_called()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method), patch.object(dt, "request") as request:
                with self.assertRaises(dt.Error):
                    dt.GitHub("synthetic").call(prefix + SHA + "..." + SHA, method=method)
                request.assert_not_called()

    def test_sync_context_uses_real_transport_ancestry_guard(self):
        source = {**run(9), "workflow_id": dn.SYNC_ID, "path": dn.SYNC_PATH, "event": "schedule"}
        for status, base, expected in [("ahead", SHA, True), ("identical", SHA, True),
                                       ("behind", SHA, False), ("ahead", "c" * 40, False)]:
            with self.subTest(status=status, base=base), patch.object(dt, "request", return_value=(
                    200, {}, dt.canonical({"status": status, "base_commit": {"sha": base}}))) as request:
                self.assertEqual(dn.sync_context(dt.GitHub("synthetic"), source, "b" * 40), expected)
                self.assertEqual(request.call_count, 1)


if __name__ == "__main__": unittest.main()
