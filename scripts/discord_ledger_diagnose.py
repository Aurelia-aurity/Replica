#!/usr/bin/env python3
"""Read-only bounded comparison of Discord ledger branch and immutable contents."""
import argparse
import base64
import hashlib
import json
import os
import re
import selectors
import subprocess
import sys
import time

import discord_transport as dt

MAX_SAMPLES = 3
MAX_REQUESTS_PER_SAMPLE = 6
MAX_OUTPUT_BYTES = 12_000_000
MAX_JSON_DEPTH = 128
PROCESS_TIMEOUT_SECONDS = 8
CLEANUP_TIMEOUT_SECONDS = 1
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
REF_RESOURCE = f"repos/{dt.REPO}/git/ref/heads/{dt.BRANCH.replace('/', '%2F')}"
REF_NAME = f"refs/heads/{dt.BRANCH}"
COMMIT_PREFIX = f"repos/{dt.REPO}/git/commits/"
TREE_PREFIX = f"repos/{dt.REPO}/git/trees/"
BRANCH_CONTENT_RESOURCE = (f"repos/{dt.REPO}/contents/{dt.STATE_FILE}"
                           f"?ref={dt.BRANCH.replace('/', '%2F')}")


class DiagnosticUnavailable(Exception):
    def __init__(self, code):
        self.code = code if code in {"gh_unavailable", "gh_failed", "timeout", "output_limit",
                                     "invalid_json", "invalid_shape", "invalid_sha",
                                     "invalid_base64"} else "gh_failed"
        super().__init__(self.code)


def _command(endpoint, *, method="GET"):
    """Run one fixed-host GET through gh without inspecting credential material."""
    if method != "GET":
        raise DiagnosticUnavailable("invalid_shape")
    immutable_prefix = f"repos/{dt.REPO}/contents/{dt.STATE_FILE}?ref="
    fixed_endpoint = (endpoint == REF_RESOURCE or endpoint == BRANCH_CONTENT_RESOURCE or
                      isinstance(endpoint, str) and endpoint.startswith(immutable_prefix) and
                      SHA40.fullmatch(endpoint[len(immutable_prefix):]) is not None or
                      isinstance(endpoint, str) and endpoint.startswith(COMMIT_PREFIX) and
                      SHA40.fullmatch(endpoint[len(COMMIT_PREFIX):]) is not None or
                      isinstance(endpoint, str) and endpoint.startswith(TREE_PREFIX) and
                      SHA40.fullmatch(endpoint[len(TREE_PREFIX):]) is not None)
    if not fixed_endpoint:
        raise DiagnosticUnavailable("invalid_shape")
    return ["gh", "api", "--hostname", "github.com", "--method", "GET", endpoint]


def _stop(process):
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _run_readonly(endpoint):
    command = _command(endpoint)
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, shell=False, close_fds=True)
    except FileNotFoundError:
        raise DiagnosticUnavailable("gh_unavailable") from None
    except OSError:
        raise DiagnosticUnavailable("gh_unavailable") from None

    output = bytearray()
    deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop(process)
                raise DiagnosticUnavailable("timeout")
            events = selector.select(remaining)
            if not events:
                _stop(process)
                raise DiagnosticUnavailable("timeout")
            chunk = os.read(process.stdout.fileno(), min(65536, MAX_OUTPUT_BYTES + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_OUTPUT_BYTES:
                _stop(process)
                raise DiagnosticUnavailable("output_limit")
        status = process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _stop(process)
        raise DiagnosticUnavailable("timeout") from None
    finally:
        selector.close()
        process.stdout.close()
    if status != 0:
        raise DiagnosticUnavailable("gh_failed")

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        text = output.decode("utf-8")
        if text.startswith("\ufeff"):
            raise ValueError("UTF-8 BOM is unsupported")
        depth = 0
        in_string = escaped = False
        for char in text:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "[{":
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise ValueError("JSON nesting limit exceeded")
            elif char in "]}":
                depth -= 1
        value = json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except (ValueError, UnicodeError, RecursionError):
        raise DiagnosticUnavailable("invalid_json") from None
    if not isinstance(value, dict):
        raise DiagnosticUnavailable("invalid_shape")
    return value


def _ref_sha(value):
    try:
        ref = value["ref"]
        sha = value["object"]["sha"]
        object_type = value["object"]["type"]
    except (KeyError, TypeError):
        raise DiagnosticUnavailable("invalid_shape") from None
    if ref != REF_NAME or object_type != "commit":
        raise DiagnosticUnavailable("invalid_shape")
    if not isinstance(sha, str) or not SHA40.fullmatch(sha):
        raise DiagnosticUnavailable("invalid_sha")
    return sha


def _content(value):
    if (value.get("type") != "file" or value.get("path") != dt.STATE_FILE or
            value.get("encoding") != "base64"):
        raise DiagnosticUnavailable("invalid_shape")
    sha = value.get("sha")
    if not isinstance(sha, str) or not SHA40.fullmatch(sha):
        raise DiagnosticUnavailable("invalid_sha")
    content = value.get("content")
    if not isinstance(content, str):
        raise DiagnosticUnavailable("invalid_shape")
    try:
        encoded = re.sub(r"[ \t\r\n]", "", content)
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise DiagnosticUnavailable("invalid_base64") from None
    if len(data) > dt.MAX_STATE:
        raise DiagnosticUnavailable("output_limit")
    blob_sha = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
    if blob_sha != sha:
        raise DiagnosticUnavailable("invalid_sha")
    return sha, data


def _contents_resource(ref):
    if ref == dt.BRANCH:
        return BRANCH_CONTENT_RESOURCE
    if isinstance(ref, str) and SHA40.fullmatch(ref):
        return f"repos/{dt.REPO}/contents/{dt.STATE_FILE}?ref={ref}"
    raise DiagnosticUnavailable("invalid_sha")


class GhApi:
    def get_ref(self):
        return _ref_sha(_run_readonly(REF_RESOURCE))

    def get_contents(self, ref):
        return _content(_run_readonly(_contents_resource(ref)))

    def get_commit_tree(self, commit_sha):
        if not isinstance(commit_sha, str) or not SHA40.fullmatch(commit_sha):
            raise DiagnosticUnavailable("invalid_sha")
        value = _run_readonly(COMMIT_PREFIX + commit_sha)
        sha, tree = value.get("sha"), value.get("tree")
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if sha != commit_sha:
            raise DiagnosticUnavailable("invalid_sha")
        if not isinstance(tree_sha, str) or not SHA40.fullmatch(tree_sha):
            raise DiagnosticUnavailable("invalid_shape")
        return tree_sha

    def get_tree_blob(self, tree_sha):
        if not isinstance(tree_sha, str) or not SHA40.fullmatch(tree_sha):
            raise DiagnosticUnavailable("invalid_sha")
        value = _run_readonly(TREE_PREFIX + tree_sha)
        if value.get("sha") != tree_sha or value.get("truncated") is not False:
            raise DiagnosticUnavailable("invalid_shape")
        entries = value.get("tree")
        if not isinstance(entries, list) or len(entries) > 1000:
            raise DiagnosticUnavailable("invalid_shape")
        matches = [item for item in entries if isinstance(item, dict) and item.get("path") == dt.STATE_FILE]
        if len(matches) != 1:
            raise DiagnosticUnavailable("invalid_shape")
        entry = matches[0]
        sha = entry.get("sha")
        if (entry.get("type") != "blob" or entry.get("mode") != "100644" or
                not isinstance(sha, str) or not SHA40.fullmatch(sha)):
            raise DiagnosticUnavailable("invalid_sha")
        return sha


def sample(api):
    codes = []
    commit_before = commit_after = branch_sha = immutable_sha = tree_sha = tree_blob_sha = None
    try:
        commit_before = api.get_ref()
        tree_sha = api.get_commit_tree(commit_before)
        tree_blob_sha = api.get_tree_blob(tree_sha)
        branch_sha, branch_data = api.get_contents(dt.BRANCH)
        immutable_sha, immutable_data = api.get_contents(commit_before)
        commit_after = api.get_ref()
    except DiagnosticUnavailable as exc:
        codes.append(exc.code)
        return {"status": "unavailable", "codes": codes,
                "commit_before": commit_before, "commit_after": commit_after,
                "tree_sha": tree_sha, "tree_blob_sha": tree_blob_sha,
                "branch_blob_sha": branch_sha, "immutable_blob_sha": immutable_sha}
    if commit_before != commit_after:
        outcome = "raced"
        codes.append("ref_changed")
    elif (tree_blob_sha != immutable_sha or branch_sha != immutable_sha or
          branch_data != immutable_data):
        outcome = "inconsistent"
        codes.append("immutable_tree_disagree" if tree_blob_sha != immutable_sha else
                     "branch_immutable_disagree")
    else:
        outcome = "stable"
        codes.append("branch_immutable_match")
    return {"status": outcome, "codes": codes, "commit_before": commit_before,
            "commit_after": commit_after, "tree_sha": tree_sha, "tree_blob_sha": tree_blob_sha,
            "branch_blob_sha": branch_sha,
            "immutable_blob_sha": immutable_sha}


def main(argv=None, *, api_factory=GhApi):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, choices=range(1, MAX_SAMPLES + 1), default=MAX_SAMPLES)
    args = parser.parse_args(argv)
    api = api_factory()
    samples = [sample(api) for _ in range(args.samples)]
    counts = {key: sum(item["status"] == key for item in samples)
              for key in ("stable", "inconsistent", "raced", "unavailable")}
    report = {"repository": dt.REPO, "branch": dt.BRANCH, "path": dt.STATE_FILE,
              "read_only": True, "freshness_claimed": False,
              "sample_count": len(samples), "counts": counts, "samples": samples}
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if counts["unavailable"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
