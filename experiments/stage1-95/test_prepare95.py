"""Synthetic four-arm preparation and execution-boundary regression tests."""

import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

from corpus import chunk_corpus
from exemplars import UtteranceTag
from per_item import SearchIndex
from prepare95 import (
    CRITERIA_SHA256, CasePlan, DevPlan, Pack, Preparation, build_rows, check_design,
)
from run_dev import validate_pack


def preparation(corpus):
    chunks = chunk_corpus(corpus)
    vectors = np.array([[1, 0], [1, 0], [0, 1], [0, 1], [1, 1], [1, 1]], dtype=np.float32)
    def encode(texts):
        return np.array([[0, 1] if text.endswith("다음 사건") else [1, 0] for text in texts],
                        dtype=np.float32)
    # A token counter for the integration fixture, not a tokenizer accuracy test.
    return Preparation("frozen-system", SearchIndex(chunks, corpus.records, vectors),
                       encode, lambda text: len(text) // 10)


def plan_for(corpus):
    return DevPlan(split="dev", cases=(CasePlan(
        question_id="q", item_spans=("첫 사건", "다음 사건"),
        intent="recall", self_honorific_probe=True,
    ),), utterance_tags=tuple(UtteranceTag(
        record_id=r.record_id, text_version_id=r.text_version_id, turn_index=0,
        register="반말", intent="recall",
    ) for r in corpus.records))


def factual_payload(row):
    return json.loads(row.messages[1].content.split("<evidence>\n")[1].split("\n</evidence>")[0])


def test_preparation_when_four_arms_share_one_question(synthetic_corpus):
    # Given: an explicit question-only plan and verified source annotations.
    plan = plan_for(synthetic_corpus)
    # When: prepare through the real retrieval/style/prompt composition.
    rows = build_rows(synthetic_corpus, plan, preparation(synthetic_corpus))
    # Then: S cannot introduce examples into factual IDs or payload; P has item traces.
    arms = {row.arm: row for row in rows}
    assert set(arms) == {"B", "C", "S", "P"}
    assert arms["S"].evidence_ids == arms["B"].evidence_ids
    assert factual_payload(arms["S"]) == factual_payload(arms["B"])
    assert arms["S"].messages[0] == arms["B"].messages[0]
    assert len(arms["P"].item_retained_ids) == 2


def test_preparation_when_plan_contains_answer_not_in_question(synthetic_corpus):
    # Given: an authored plan with a new answer-bearing word.
    plan = plan_for(synthetic_corpus)
    bad_case = plan.cases[0].model_copy(update={"item_spans": ("secret answer",)})
    plan = plan.model_copy(update={"cases": (bad_case,)})
    # When / Then: reject before encoding or retrieval.
    with pytest.raises(ValueError):
        build_rows(synthetic_corpus, plan, preparation(synthetic_corpus))


def test_preparation_when_corpus_and_index_differ(synthetic_corpus):
    # Given: a reused index from a different source snapshot.
    runtime = preparation(synthetic_corpus)
    wrong = Preparation(runtime.system, SearchIndex((), runtime.index.records, runtime.index.vectors),
                        runtime.encode, runtime.count_tokens)
    # When / Then: no stale/foreign index can enter the prompt bundle.
    with pytest.raises(ValueError):
        build_rows(synthetic_corpus, plan_for(synthetic_corpus), wrong)


def test_design_when_small_fixture_is_not_full_run(synthetic_corpus):
    # Given / When / Then: unit fixtures do not bypass the frozen real-run count.
    with pytest.raises(ValueError):
        check_design(synthetic_corpus, plan_for(synthetic_corpus))


def test_pack_when_one_message_changed_after_hashing(synthetic_corpus):
    # Given: 360 synthetic identities with one row per arm.
    initial = build_rows(synthetic_corpus, plan_for(synthetic_corpus), preparation(synthetic_corpus))
    rows = tuple(row.model_copy(update={"id": f"q{i}:{row.arm}", "question_id": f"q{i}"})
                 for i in range(360) for row in initial)
    row = rows[0]
    bad_message = row.messages[0].model_copy(update={"content": "changed"})
    rows = (row.model_copy(update={"messages": (bad_message, row.messages[1])}), *rows[1:])
    pack = Pack(criteria_sha256=CRITERIA_SHA256, source_sha256={}, code_sha256={},
                tokenizer_sha256="not-used-in-validation", rows=rows)
    # When / Then: changed semantic messages fail before runtime import.
    with pytest.raises(ValueError):
        validate_pack(pack)


def test_cli_when_execution_not_authorized(tmp_path):
    # Given: no execution flag, with no model or input files on disk.
    root = Path(__file__).parent
    env = {k: v for k, v in os.environ.items() if k != "STAGE95_EXECUTE_DEV"}
    # When: drive the actual CLI; it must fail before touching the absent paths.
    result = subprocess.run(
        [sys.executable, str(root / "run_dev.py"),
         str(tmp_path / "missing-pack.json"), str(tmp_path / "missing-model"),
         str(tmp_path / "outputs.jsonl")], env=env, capture_output=True, text=True, check=False,
    )
    # Then: the authorization error is observable and no outputs exist.
    assert result.returncode != 0
    assert "Separate DEV execution authorization" in result.stderr
    assert not (tmp_path / "outputs.jsonl").exists()
