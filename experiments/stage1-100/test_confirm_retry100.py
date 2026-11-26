"""Checkpoint-resume behavior with real subprocesses, no network or judges."""

import json
from pathlib import Path
import sys

import pytest

from retry_confirm100 import run

FAKE = '''
import json, sys
from pathlib import Path
out, mode = Path(sys.argv[1]), sys.argv[2]
cp = out / "semantic/checkpoints"
cp.mkdir(parents=True, exist_ok=True)
started = cp / ("grounding-" + "a"*64 + ".raw.started.json")
raw = cp / ("grounding-" + "a"*64 + ".raw.json")
attempt = out / "calls.jsonl"
with attempt.open("a") as f:
    f.write(json.dumps({"payload": "identical-payload", "digest": "a"*64}) + "\\n")
started.write_text(json.dumps({"stage":"grounding", "payload_sha256":"a"*64}))
count = len(attempt.read_text().splitlines())
if count == 2 and mode not in ("twice", "budget"):
    raw.write_text(json.dumps({"returncode":0,"stdout":"valid labels"}))
    (out / "resolved.json").write_text('{"annotations":[]}')
    sys.exit(0)
if mode == "transport" or mode == "twice":
    raw.write_text(json.dumps({"returncode":1,"stdout":"","stderr":"provider capacity 503"}))
    print("Semantic Terra call failed; do not retry")
elif mode == "timeout":
    print("subprocess.TimeoutExpired: timed out after 660 seconds")
elif mode == "budget":
    print("BudgetExhausted:grounding")
else:
    raw.write_text(json.dumps({"returncode":0,"stdout":"malformed label"}))
    print("Witness span is not exact response text")
sys.exit(1)
'''


@pytest.mark.parametrize("mode", ["transport", "timeout", "format"])
def test_retry_when_one_allowed_failure_resumes_identical_payload(tmp_path: Path, mode: str):
    # Given: a real sequential child records the same payload hash on both calls.
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE)
    output, retries = tmp_path / "out", tmp_path / "retries"
    # When: fail once, then succeed without discarding prior labels.
    rc = run((sys.executable, str(fake), str(output), mode), tmp_path, output, retries)
    # Then: one retry is logged before cache removal; raw failure evidence survives.
    assert rc == 0
    calls = [json.loads(s) for s in (output / "calls.jsonl").read_text().splitlines()]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert len((retries / "retries.jsonl").read_text().splitlines()) == 1
    assert len(list((retries / "failed-0000").glob("*.started.json"))) == 1
    assert json.loads((output / "run-complete.json").read_text())["retries"] == 1


def test_retry_when_identical_payload_fails_twice_is_inconclusive(tmp_path: Path):
    # Given: capacity failure persists for the very same payload.
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE)
    output, retries = tmp_path / "out", tmp_path / "retries"
    # When: the second attempt also has no labels.
    rc = run((sys.executable, str(fake), str(output), "twice"), tmp_path, output, retries)
    # Then: never a third call and never a valid score.
    assert rc == 1
    assert len((output / "calls.jsonl").read_text().splitlines()) == 2
    result = json.loads((output / "run-inconclusive.json").read_text())
    assert result["comparison_validity"] == "INCONCLUSIVE"
    assert result["reason"] == "second failure for identical payload"


def test_retry_when_budget_is_exhausted_does_not_retry(tmp_path: Path):
    # Given: exhaustion is not a no-label capacity/transport or format error.
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE)
    output, retries = tmp_path / "out", tmp_path / "retries"
    # When: the child reports BudgetExhausted.
    rc = run((sys.executable, str(fake), str(output), "budget"), tmp_path, output, retries)
    # Then: fail closed after one attempt.
    assert rc == 1
    assert len((output / "calls.jsonl").read_text().splitlines()) == 1
    assert not (retries / "retries.jsonl").exists()
