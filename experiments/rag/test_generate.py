"""Prompt, export-boundary and condition-blind conversion regression checks."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from build_blind import assemble
from corpus import ROOT, Evidence, load_corpus, validate
from generate import (
    EVIDENCE_END, EVIDENCE_START, Generation, InputError,
    build_schedule, default_schedule, load_generations, load_retrieved,
)
from retrieval import Trace, select_evidence


@pytest.fixture
def corpus():
    return load_corpus()


@pytest.fixture
def retrieved():
    return load_retrieved(ROOT / "results/kure/evidence.json")


@pytest.fixture
def schedule():
    return default_schedule()


def evidence_of(prompt):
    return json.loads(prompt.messages[1].content.split(EVIDENCE_START)[1].split(EVIDENCE_END)[0])


def without_evidence(prompt):
    before, rest = prompt.messages[1].content.split(EVIDENCE_START)
    _, after = rest.split(EVIDENCE_END)
    return prompt.messages[0], before, after


def test_prompt_identity_when_only_condition_changes(schedule):
    # Given: three conditions for every question.
    grouped = {}
    for prompt in schedule:
        grouped.setdefault(prompt.question_id, []).append(prompt)
    # When: remove only the delimited evidence payload.
    stripped = [[without_evidence(p) for p in rows] for rows in grouped.values()]
    # Then: complete system/user strings outside the block are byte-identical.
    assert all(group[0] == group[1] == group[2] for group in stripped)


def test_schedule_count_and_order_when_corpus_order_reversed(corpus, retrieved):
    # Given: a reordered source corpus and retrieval mapping.
    reversed_corpus = corpus.model_copy(update={"questions": tuple(reversed(corpus.questions))})
    # When: render both inputs.
    original = build_schedule(corpus, retrieved, "system")
    reordered = build_schedule(reversed_corpus, dict(reversed(list(retrieved.items()))), "system")
    # Then: a deterministic condition-major schedule has 80 distinct questions per arm.
    assert original == reordered
    assert len(original) == len({(p.question_id, p.condition) for p in original}) == 240
    assert [(p.condition, p.question_id) for p in original] == sorted(
        (p.condition, p.question_id) for p in original
    )
    assert {c: sum(p.condition == c for p in original) for c in ("A", "B", "C")} == {
        "A": 80, "B": 80, "C": 80,
    }


def test_condition_evidence_when_real_frozen_threshold_applied(schedule, corpus):
    # Given: independently replayed frozen traces at the DEV-fitted threshold.
    threshold = json.loads((ROOT / "results/kure/calibration.json").read_text())["threshold"]
    raw = json.loads((ROOT / "results/kure/traces.json").read_text())
    traces = [Trace.model_validate(t) for rows in raw.values() for t in rows]
    selected = {t.question.question_id: select_evidence(t, threshold).evidence for t in traces}
    chunks = {c.chunk_id: c for c in validate(corpus)}
    questions = {q.question_id: q for q in corpus.questions}
    # When: decode the actual model-facing JSON.
    decoded = {(p.question_id, p.condition): evidence_of(p) for p in schedule}
    # Then: A is empty, B is thresholded (not top-k), C is exactly gold in gold order.
    for qid, question in questions.items():
        assert decoded[qid, "A"] == []
        assert decoded[qid, "B"] == [e.model_dump(mode="json") for e in selected[qid]]
        assert decoded[qid, "C"] == [
            chunks[cid].evidence().model_dump(mode="json") for cid in question.gold_chunk_ids
        ]
    assert any(not evidence for evidence in selected.values())
    assert any(evidence for evidence in selected.values())


@pytest.mark.parametrize("score,accepted", [(0.499, False), (0.5, True), (0.501, True)])
def test_b_threshold_boundary_when_export_selected(corpus, retrieved, score, accepted):
    # Given: a single candidate below, equal to, or above the threshold.
    from retrieval import Hit
    question = corpus.questions[0]
    chunk = next(c for c in validate(corpus) if c.persona_id == question.persona_id)
    trace = Trace(
        question=question, ranked_ids=(chunk.chunk_id,),
        top5=(Hit(chunk_id=chunk.chunk_id, score=score, record_alias=chunk.record_alias),),
        topk_evidence=(chunk.evidence(),),
    )
    selected = select_evidence(trace, 0.5)
    retrieved[question.question_id] = retrieved[question.question_id].model_copy(update={
        "evidence": selected.evidence, "topk_evidence": selected.topk_evidence,
        "predicted_no_evidence": selected.predicted_no_evidence,
    })
    # When: build B without reading scores, gold, or types to override the export.
    prompts = build_schedule(corpus, retrieved, "system")
    prompt = next(p for p in prompts if p.question_id == question.question_id and p.condition == "B")
    # Then: only the export's selected candidates survive.
    assert prompt.evidence_ids == ((chunk.chunk_id,) if accepted else ())


@pytest.mark.parametrize("mutation", ["foreign", "modified", "contradictory_empty"])
def test_rejects_corrupt_retrieval_when_scope_or_content_changes(corpus, retrieved, mutation):
    # Given: an independently damaged export.
    qid = corpus.questions[0].question_id
    row = retrieved[qid]
    match mutation:
        case "foreign":
            foreign = next(c for c in validate(corpus) if c.persona_id != row.persona_id)
            row = row.model_copy(update={"evidence": (foreign.evidence(),)})
        case "modified":
            altered = row.evidence[0].model_copy(update={"content": "tampered"})
            row = row.model_copy(update={"evidence": (altered,)})
        case "contradictory_empty":
            row = row.model_copy(update={"predicted_no_evidence": True})
    retrieved[qid] = row
    # When / Then: no out-of-scope or mislabeled evidence reaches a prompt.
    with pytest.raises(InputError):
        build_schedule(corpus, retrieved, "system")


def test_scope_when_all_conditions_rendered(schedule, corpus):
    # Given: the canonical chunk owner index.
    chunks = {str(c.chunk_id): c for c in validate(corpus)}
    # When: read all actual prompt evidence objects.
    items = [(p, e) for p in schedule for e in evidence_of(p)]
    # Then: every object has exactly the contract fields and the correct persona.
    for prompt, item in items:
        assert set(item) == set(Evidence.model_fields)
        assert chunks[item["chunk_id"]].persona_id == prompt.persona_id


def fake_generations(schedule):
    return tuple(
        Generation(
            **p.model_dump(exclude={"messages"}), status="success",
            raw_text=f"response-{index}", text=f"response-{index}",
        )
        for index, p in enumerate(schedule)
    )


def test_blind_inputs_when_schedule_shuffled(schedule, corpus):
    # Given: synthetic replies, not model outputs.
    rows = fake_generations(schedule)
    # When: blind the full schedule and its reversed order.
    blind, key, profiles = assemble(rows, corpus)
    reversed_result = assemble(tuple(reversed(rows)), corpus)
    # Then: keys restore conditions; judge payloads do not expose conditions or IDs.
    assert (blind, key, profiles) == reversed_result
    assert len(blind) == len(key) == len(profiles) == 240
    assert sum(p == "unknown" for p in profiles.values()) == 48
    chunks = {str(c.chunk_id): c for c in validate(corpus)}
    questions = {q.question_id: q for q in corpus.questions}
    for item in blind:
        mapping = key[item.bid]
        question = questions[mapping.question_id]
        assert item.source_response_sha256 == mapping.source_response_sha256
        assert not {"condition", "question_id", "persona_id", "evidence_ids"} & set(item.model_dump())
        assert tuple(quote.id for quote in item.oracle.gold.evidence) == tuple(
            str(cid) for cid in question.gold_chunk_ids
        )
        assert all(chunks[quote.id].persona_id == mapping.persona_id for quote in item.oracle.gold.evidence)
        assert all(
            chunk.persona_id == mapping.persona_id
            for cid, chunk in chunks.items() if f"[{cid}]" in item.record
        )
        assert item.oracle.gold.criterion == question.note


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "failed", "wrong_ids"])
def test_blind_loader_rejects_when_schedule_invalid(schedule, tmp_path, mutation):
    # Given: one invalid full-run artifact.
    rows = list(fake_generations(schedule))
    match mutation:
        case "missing":
            rows.pop()
        case "duplicate":
            rows[-1] = rows[0]
        case "failed":
            rows[0] = rows[0].model_copy(update={"status": "failed"})
        case "wrong_ids":
            rows[0] = rows[0].model_copy(update={"evidence_ids": rows[80].evidence_ids})
    artifact = tmp_path / "generations.jsonl"
    artifact.write_text("\n".join(r.model_dump_json() for r in rows), encoding="utf-8")
    # When / Then: reject partial or misattributed inputs before judge preparation.
    with pytest.raises(InputError):
        load_generations(artifact, schedule)


def test_dry_run_when_cpu_only_subprocess(tmp_path):
    # Given: a fresh CPU process and a new artifact directory.
    output = tmp_path / "dry-run"
    # When: exercise the real CLI, checking imports in the same fresh process.
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; from pathlib import Path; from generate import main; "
         "main(dry_run=True, output=Path(sys.argv[1])); "
         "assert not {'torch', 'transformers', 'runtime'} & sys.modules.keys()",
         str(output)],
        cwd=ROOT, text=True, capture_output=True, check=True,
    )
    # Then: all model-facing messages are available without loading a model.
    prompts = (output / "prompts.jsonl").read_text().splitlines()
    assert len(prompts) == 240
    assert "DRY_RUN_DONE" in result.stdout
    assert not (output / "generations.jsonl").exists()
    assert not (output / "environment.json").exists()


def test_generation_artifacts_when_existing_runtime_adapter_supplied(tmp_path, monkeypatch, schedule):
    # Given: a deterministic runtime double preserving the actual config/prompt seam.
    from generate import main
    calls = []

    class FakeRuntime:
        def __init__(self, model_path, config):
            self.config = config
            self.environment = {"model_revision": config["model_revision"], "gpu_name": "test-double"}

        def generate(self, prompt, cap):
            calls.append((self.config.copy(), prompt, cap))
            return {
                "text": "synthetic-response", "generation_ms": 1.0,
                "generated_token_count": 2, "generated_ids_sha256": "a" * 64,
                "termination": "eos", "tokens_per_second": 2000.0,
            }

    class FakeEvaluationError(RuntimeError):
        pass

    monkeypatch.setitem(sys.modules, "runtime", SimpleNamespace(Runtime=FakeRuntime))
    monkeypatch.setitem(sys.modules, "common", SimpleNamespace(EvaluationError=FakeEvaluationError))
    output = tmp_path / "generation"
    # When: drive the real orchestration without a GPU or an external model.
    main(output=output)
    # Then: 240 persisted replies match their rendered prompts, plus nine warmups.
    rows = load_generations(output / "generations.jsonl", schedule)
    assert len(calls) == 249
    assert len(rows) == 240
    assert all(row.raw_text == row.text == "synthetic-response" for row in rows)
    for prompt, call in zip(schedule, [c for i, c in enumerate(calls) if i % 83 >= 3], strict=True):
        config, user, cap = call
        assert config["system_prompt"] == prompt.messages[0].content
        assert user == prompt.messages[1].content
        assert cap == 160
        assert config["do_sample"] is False
        assert config["chat_template_kwargs"] == {"enable_thinking": False}
    environment = json.loads((output / "environment.json").read_text())
    assert environment["seed"] is None and environment["temperature"] is None
    assert len(environment["prompts_sha256"]) == 64
    assert all("evidence_used" not in json.loads(line) for line in (
        output / "generations.jsonl"
    ).read_text().splitlines())
