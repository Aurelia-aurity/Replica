"""Source-context, abstention, token budget and corrected-corpus CLI regressions."""

from hashlib import sha256
import json
import subprocess
import sys

import pytest

from corpus import ROOT, chunk_corpus, load_corpus
from generate import (
    EVIDENCE_END, EVIDENCE_START, InputError, build_schedule, default_schedule,
    load_retrieved,
)
from offline_v2 import metrics
from retrieval import Hit, Trace, calibrate, select_evidence
from retrieval_v2 import (
    ContextConfig, contextual_evidence, evidence_json, missing_slots, restore_context,
)

RESULTS = ROOT / "results/retrieval-v2"


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


@pytest.fixture(scope="module")
def chunks(corpus):
    return chunk_corpus(corpus)


@pytest.fixture(scope="module")
def count_tokens():
    # Given: the tokenizer-only cache prepared by the actual offline run.
    from tokenizers import Tokenizer
    from offline_v2 import TOKENIZER_DIR

    tokenizer = Tokenizer.from_file(str(TOKENIZER_DIR / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids)


def trace_for(corpus, hits):
    question = next(q for q in corpus.questions if q.persona_id == hits[0][0].persona_id)
    return Trace(
        question=question,
        ranked_ids=tuple(chunk.chunk_id for chunk, _ in hits),
        top5=tuple(Hit(chunk_id=c.chunk_id, score=s, record_alias=c.record_alias) for c, s in hits),
        topk_evidence=tuple(c.evidence() for c, _ in hits),
    )


@pytest.mark.parametrize("top1,empty", [(0.499, True), (0.5, False), (0.501, False)])
def test_question_abstention_preserves_low_score_hits_when_top1_accepted(
    corpus, chunks, count_tokens, top1, empty,
):
    # Given: the second retrieved record is below the per-chunk threshold.
    diaries = [r for r in corpus.records if r.persona_id == "p1" and r.kind == "diary"][:2]
    hits = [(next(c for c in chunks if c.record_id == r.record_id), s)
            for r, s in zip(diaries, (top1, 0.1), strict=True)]
    trace = trace_for(corpus, hits)
    # When: the question-level gate is evaluated once.
    restored = restore_context(
        trace, 0.5, chunks, corpus.records,
        ContextConfig(k=2, window=2, budget=2000), count_tokens,
    )
    # Then: threshold equality accepts both records, including the low-score one.
    assert (not restored.evidence) == empty
    assert restored.predicted_no_evidence == empty
    if not empty:
        assert {e.record_id for e in restored.evidence} == {r.record_id for r in diaries}
        assert hits[1][0].chunk_id not in {e.chunk_id for e in select_evidence(trace, 0.5).evidence}


def test_turn_windows_merge_dedupe_and_keep_source_order_when_hits_overlap(corpus, chunks):
    # Given: two overlapping windows in a chat, plus no other records.
    record = next(r for r in corpus.records if r.kind == "chat")
    group = [c for c in chunks if c.record_id == record.record_id]
    hits = [group[1], group[2]]
    trace = trace_for(corpus, [(c, 0.8 - i / 10) for i, c in enumerate(hits)])
    # When: expand each hit by one turn, then merge on canonical chunk boundaries.
    result = restore_context(
        trace, 0.5, chunks, corpus.records,
        ContextConfig(k=2, window=1, budget=2000), len,
    )
    turns = {i for c in hits for turn in c.source_turns
             for i in range(max(0, turn - 1), min(len(record.turns), turn + 2))}
    expected = [c for c in group if set(c.source_turns) & turns]
    # Then: exact source text and roles occur once, in turn order, with date on every line.
    assert result.evidence == tuple(contextual_evidence(c) for c in expected)
    assert len(result.evidence) == len({e.chunk_id for e in result.evidence})
    for chunk, item in zip(expected, result.evidence, strict=True):
        assert item.speaker_role == chunk.speaker_role
        assert item.content.splitlines() == [
            f"{chunk.date} | {chunk.speaker} (speaker_role={chunk.speaker_role}) | {line}"
            for line in chunk.content.splitlines()
        ]


def test_budget_removes_lowest_record_before_highest_when_date_order_differs(
    corpus, chunks, count_tokens,
):
    # Given: two diary hits deliberately reversed relative to chronology.
    records = [r for r in corpus.records if r.persona_id == "p1" and r.kind == "diary"][:2]
    group = [c for c in chunks if c.record_id in {r.record_id for r in records}]
    low, high = [next(c for c in group if c.record_id == r.record_id) for r in records]
    trace = trace_for(corpus, [(high, 0.9), (low, 0.8)])
    highest_record = tuple(contextual_evidence(c) for c in group if c.record_id == high.record_id)
    budget = count_tokens(evidence_json(highest_record))
    # When: exactly enough tokens are available for the highest-scoring whole diary.
    result = restore_context(
        trace, 0.5, chunks, corpus.records,
        ContextConfig(k=2, window=2, budget=budget), count_tokens,
    )
    # Then: no text truncation, and the lower record is removed first.
    assert result.evidence == highest_record
    assert count_tokens(evidence_json(result.evidence)) <= budget


def test_context_does_not_read_gold_or_test_labels_when_restored(corpus, chunks, count_tokens):
    # Given: identical hits with all supervised labels changed.
    chunk = chunks[1]
    trace = trace_for(corpus, [(chunk, 0.9)])
    altered = trace.model_copy(update={"question": trace.question.model_copy(update={
        "gold_chunk_ids": (), "support": (), "required_slots": (), "slot_surface_forms": {},
        "expected_answer": None, "type": "ordinary_chat", "split": "test", "family_id": "unused",
    })})
    config = ContextConfig(k=1, window=2, budget=2000)
    # When: restore both inputs through the real selector.
    first = restore_context(trace, 0.5, chunks, corpus.records, config, count_tokens)
    second = restore_context(altered, 0.5, chunks, corpus.records, config, count_tokens)
    # Then: selection depends only on score, source context and budget.
    assert first.evidence == second.evidence


def test_slot_metrics_use_content_and_declared_surfaces_when_ids_are_incomplete(corpus, chunks):
    # Given: a canonical slot represented by a declared alternative surface.
    question = corpus.questions[0].model_copy(update={
        "required_slots": ("canonical-fact", "missing-fact"),
        "slot_surface_forms": {"canonical-fact": ("actual-surface",)},
    })
    evidence = (chunks[0].evidence().model_copy(update={"content": "actual-surface"}),)
    # When: measure content coverage, ignoring gold ID membership.
    missing = missing_slots(question, evidence)
    # Then: the declared alternative is credited and absent content remains missing.
    assert missing == ("missing-fact",)


@pytest.mark.parametrize("model", ["kure", "e5"])
def test_history_hashes_and_metrics_when_original_artifacts_replayed(model):
    # Given: immutable historical corpus/source snapshots, not corrected current gold.
    from audit_results import main
    # When / Then: every recorded metric, source hash and exported object replays.
    main(model)


@pytest.mark.parametrize("version,directory", [
    ("r1", "generation"), ("r2", "generation-r2"), ("r3", "generation-r3"),
])
def test_historical_blind_loader_uses_snapshot_when_current_corpus_changed(tmp_path, version, directory):
    # Given: actual historical outputs whose gold and source text predate the correction.
    from build_blind import main
    from historical import HISTORICAL_CORPUS
    output = tmp_path / "blind"
    # When: prepare inputs locally without invoking any judge.
    main(ROOT / "results" / directory / "generations.jsonl", output, prompt_version=version)
    # Then: provenance and all 240 mappings belong to the original corpus.
    preparation = json.loads((output / "preparation.json").read_text())
    assert preparation["items"] == 240
    assert preparation["corpus_sha256"] == sha256(HISTORICAL_CORPUS.read_bytes()).hexdigest()
    assert preparation["corpus_sha256"] != sha256((ROOT / "corpus.json").read_bytes()).hexdigest()


def test_corrected_run_provenance_and_dev_selection_when_saved_results_replayed(corpus, count_tokens):
    # Given: source run recomputed on corrected corpus and a DEV-only selection record.
    saved = json.loads((RESULTS / "offline-metrics.json").read_text())
    source = json.loads((RESULTS / "kure_old/manifest.json").read_text())
    selection = json.loads((RESULTS / "kure_v2/dev-selection.json").read_text())
    traces = json.loads((RESULTS / "kure_old/traces.json").read_text())
    dev = [Trace.model_validate(row) for row in traces["dev"]]
    # When: independently replay threshold choice, and its recorded tie-break.
    fitted = calibrate(dev)
    candidates = [c for c in selection["candidates"]
                  if c["metrics"]["median_evidence_tokens"] <= 1500]
    best = min(candidates, key=lambda c: (
        -c["metrics"]["all_slots_covered"], -c["metrics"]["slot_hits"],
        c["metrics"]["median_evidence_tokens"], c["metrics"]["max_evidence_tokens"],
        c["config"]["k"], c["config"]["window"], c["config"]["budget"],
    ))
    # Then: no historical corpus, different model revision or TEST-fitted choice is used.
    assert saved["corpus_sha256"] == source["sha256"]["corpus.json"] == sha256(
        (ROOT / "corpus.json").read_bytes()
    ).hexdigest()
    assert source["encoder"] == json.loads((ROOT / "model-lock.json").read_text())["kure"]
    assert fitted.threshold == saved["threshold"]
    assert selection["split"] == "dev"
    assert best["config"] == selection["chosen"] == saved["config"]
    for condition, directory in (("B_old", "kure_old"), ("B_v2", "kure_v2")):
        raw = json.loads((RESULTS / directory / "traces.json").read_text())
        for split in ("dev", "test"):
            replay = metrics([Trace.model_validate(t) for t in raw[split]], count_tokens)
            assert replay.model_dump(mode="json") == saved["conditions"][condition][split]
    assert saved["conditions"]["B_v2"]["all"]["max_evidence_tokens"] <= saved["config"]["budget"]


def test_new_condition_schedule_uses_r1_outside_evidence_when_corrected_exports_loaded(corpus):
    # Given: independently loaded old/v2 evidence on the corrected corpus.
    old = load_retrieved(RESULTS / "kure_old/evidence.json")
    restored = load_retrieved(RESULTS / "kure_v2/evidence.json")
    original = default_schedule(evidence_path=RESULTS / "kure_old/evidence.json")
    # When: render all three new conditions.
    actual = default_schedule(
        evidence_path=RESULTS / "kure_v2/evidence.json", condition_set="retrieval-v2",
    )
    original_by_id = {(p.question_id, p.condition): p for p in original}
    # Then: only evidence payload changes from r1, and C uses corrected gold.
    assert len(actual) == 240
    assert {p.condition for p in actual} == {"B_old", "B_v2", "C"}
    for prompt in actual:
        reference = original_by_id[prompt.question_id, "C" if prompt.condition == "C" else "B"]
        assert prompt.prompt_version == "r1"
        assert prompt.messages[0] == reference.messages[0]
        before, rest = prompt.messages[1].content.split(EVIDENCE_START)
        _, after = rest.split(EVIDENCE_END)
        old_before, old_rest = reference.messages[1].content.split(EVIDENCE_START)
        _, old_after = old_rest.split(EVIDENCE_END)
        assert (before, after) == (old_before, old_after)
        if prompt.condition != "B_v2":
            assert prompt.messages == reference.messages
    assert {q.question_id for q in corpus.questions} == set(old) == set(restored)


def test_modified_context_is_rejected_when_canonical_source_differs(corpus):
    # Given: a tampered v2 export with an otherwise valid identity.
    old = load_retrieved(RESULTS / "kure_old/evidence.json")
    restored = load_retrieved(RESULTS / "kure_v2/evidence.json")
    qid = next(qid for qid, row in restored.items() if row.evidence)
    row = restored[qid]
    restored[qid] = row.model_copy(update={
        "evidence": (row.evidence[0].model_copy(update={"content": "tampered"}),),
    })
    # When / Then: new evidence cannot bypass the source-content boundary.
    with pytest.raises(InputError):
        build_schedule(corpus, old, "system", condition_set="retrieval-v2", retrieved_v2=restored)


def test_dry_run_cli_emits_240_new_conditions_without_runtime_when_v2_selected(tmp_path):
    # Given: real corrected input exports and a fresh output directory.
    output = tmp_path / "gpu-prompts"
    # When: execute the same CLI arguments as the prepared GPU job, without a model.
    result = subprocess.run([
        sys.executable, str(ROOT / "generate.py"), "--dry-run", "--output", str(output),
        "--evidence", str(RESULTS / "kure_v2/evidence.json"),
        "--condition-set", "retrieval-v2", "--prompt-version", "r1",
    ], cwd=ROOT, capture_output=True, text=True, check=True)
    # Then: exactly 80 prompts per arm and no generation side effects.
    rows = [json.loads(line) for line in (output / "prompts.jsonl").read_text().splitlines()]
    assert len(rows) == 240
    assert {c: sum(r["condition"] == c for r in rows) for c in ("B_old", "B_v2", "C")} == {
        "B_old": 80, "B_v2": 80, "C": 80,
    }
    assert not (output / "generations.jsonl").exists()
    assert "model_loaded=False" in result.stdout
