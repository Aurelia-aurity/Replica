"""Synthetic Project adapter + real Python helper, talking only to local Worker."""
import copy
import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlparse
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))
import github_project as gp
import project_status_request as helper

base = sys.argv[1].rstrip("/")
url = urlparse(base)
assert url.scheme == "http" and url.hostname == "127.0.0.1"
key = "synthetic-helper-key-not-a-real-key-0000"
results = []


class LocalRelay:
    def __init__(self, loss):
        self.base = base
        self.loss = loss
        self.calls = []

    def post(self, path, value):
        self.calls.append(path)
        raw = helper.canonical(value).encode()
        timestamp = str(int(time.time()))
        prefix = f"replica-helper-v1\nPOST\n{path}\n{timestamp}\n".encode()
        signature = hmac.new(key.encode(), prefix + raw, hashlib.sha256).hexdigest()
        request = urllib.request.Request(base + path, data=raw, method="POST", headers={
            "content-type": "application/json", "x-replica-timestamp": timestamp,
            "x-replica-signature": signature})
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
        if self.loss == path:
            self.loss = None
            raise helper.se.SyncError("synthetic local ACK loss after server accepted")
        return result


for number, case in enumerate(["confirmed", "attempt_ack_lost", "result_ack_lost",
                               "readback_mismatch", "known_rejection", "stale_prewrite", "noop"], 1):
    page = f"00000000-0000-4000-8000-{number:012d}"
    directory = tempfile.mkdtemp(prefix="replica-local-helper-journal-")
    journal = helper.LocalJournal(directory, str(uuid4()))
    relay = LocalRelay({"attempt_ack_lost": "/agent-attempt", "result_ack_lost": "/agent-request"}.get(case))
    current = {"issue_id": 555 + number, "item_id": f"PVTI_synthetic_{number}",
        "stamp": {"field_id": helper.STATUS_FIELD_ID,
            "option_id": gp.EXPECTED_STATUS_OPTIONS["진행 중" if case == "noop" else "백로그"],
            "value_id": "PVTSV_synthetic", "updated_at": "2026-10-01T00:00:00Z"},
        "source_fingerprint": "a" * 64}
    counter = {"reads": 0, "mutations": 0}

    def reader(issue):
        assert issue == 18
        counter["reads"] += 1
        value = copy.deepcopy(current)
        if case == "stale_prewrite" and counter["reads"] > 1:
            value["source_fingerprint"] = "b" * 64
        if case == "readback_mismatch" and counter["mutations"]:
            value["stamp"]["option_id"] = gp.EXPECTED_STATUS_OPTIONS["백로그"]
        return value

    def mutate(body):
        persisted = journal.load()
        assert persisted["may_have_sent"] is True
        counter["mutations"] += 1
        if case == "known_rejection":
            raise helper.MutationRejected("synthetic permission rejection")
        current["stamp"]["option_id"] = gp.EXPECTED_STATUS_OPTIONS[body["target"]]

    def invoke():
        return helper.run_helper(journal, relay, reader, mutate, issue_number=18,
            page_id=page, target="진행 중", options=gp.EXPECTED_STATUS_OPTIONS)

    first = invoke()
    before = counter["mutations"]
    second = invoke()
    assert counter["mutations"] == before
    assert relay.calls.count("/agent-attempt") <= 1
    expected = {"confirmed": "CONFIRMED", "attempt_ack_lost": "UNKNOWN_FENCED",
        "result_ack_lost": "CONFIRMED", "readback_mismatch": "UNKNOWN_FENCED",
        "known_rejection": "REJECTED_FENCED", "stale_prewrite": "ABORTED", "noop": "CONFIRMED_NOOP"}[case]
    assert second["outcome"] == expected, (case, first, second)
    assert not second["notion_reflected"]
    results.append({"case": case, "operation_id": journal.operation_id,
        "first": first["outcome"], "restart": second["outcome"],
        "mutation_attempts": before, "journal_directory": directory})
print(json.dumps({"scope": "real helper + local Worker/D1; synthetic Project only", "results": results}))
