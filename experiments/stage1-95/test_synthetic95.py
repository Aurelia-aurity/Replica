"""Deterministic DEV authorship, oracle isolation and exact source gold checks."""

from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from corpus import chunk_corpus
from dev_schema import PublicQuestion
from exemplars import StyleRequest, select_exemplars
from generate95 import KINDS, REGISTERS, assemble, families
from prepare95 import check_design
from synthetic95 import Templates


@pytest.fixture(scope="module")
def authored():
    bank = Templates.model_validate_json(
        (Path(__file__).parent / "templates95.json").read_text(),
    )
    return families(bank)


def test_families_when_seed_is_repeated(authored):
    # Given: an explicit finite synthetic grammar and fixed random seed.
    bank = Templates.model_validate_json(
        (Path(__file__).parent / "templates95.json").read_text(),
    )
    # When: re-author independently with the same seed and a different seed.
    repeated, changed = families(bank), families(bank, seed=95)
    # Then: the seed reproduces the full artifact, not merely IDs.
    assert repeated == authored
    assert changed != authored


def test_design_when_all_cells_are_authored(authored):
    # Given: newly sampled event tuples, each assigned one question and persona.
    sources, _, plan, _, _ = assemble(authored)
    # When: enforce the same design gate as the preparation CLI.
    check_design(sources.corpus(), plan)
    # Then: register cells are independent, not copies with changed endings.
    assert Counter(
        (f.question.type, f.persona.speech_register) for f in authored
    ) == {(k, r): 20 for k in KINDS for r in REGISTERS}
    assert Counter(
        (f.question.type, f.persona.speech_register) for f in authored if f.plan.self_honorific_probe
    ) == {(k, r): 10 for k in KINDS for r in REGISTERS}
    assert len({f.question.family_id for f in authored}) == 360
    assert len({f.question.question for f in authored}) == 360
    assert len({f.records[0].text for f in authored}) == 360
    assert len({(e.title, e.place, e.item, e.date) for f in authored for e in f.events}) == 720


def test_public_question_when_oracle_fields_are_injected(authored):
    # Given: a source question and an evaluation-only field.
    payload = authored[0].question.model_dump()
    payload["gold_support_ids"] = []
    # When / Then: reject the field at the input boundary, rather than silently discard it.
    with pytest.raises(ValidationError):
        PublicQuestion.model_validate(payload)


def test_gold_when_evaluation_items_are_resolved(authored):
    # Given: physically separate source and oracle artifacts.
    sources, _, plan, gold, oracle = assemble(authored)
    chunks = {c.chunk_id: c for c in chunk_corpus(sources.corpus())}
    questions = {q.question_id: q for q in sources.questions}
    # When: resolve the sidecar's literal support IDs against real chunking.
    resolved = [
        (case, item, chunks[cid]) for case in oracle.cases
        for item in case.required_items for cid in item.gold_support_ids
    ]
    # Then: all required surfaces are actually in persona evidence, not just labels.
    assert len(resolved) == 360
    for case, item, chunk in resolved:
        assert chunk.persona_id == questions[case.question_id].persona_id
        assert chunk.speaker_role == "persona"
        assert all(surface in chunk.content for surface in item.slot_surfaces)
        assert item.question_span in questions[case.question_id].question
        assert chunk.chunk_id in {e.chunk_id for e in gold.evidence[case.question_id]}
    assert len(plan.cases) == len(oracle.cases) == 360
    assert all(not q.gold_chunk_ids and not q.required_slots for q in sources.corpus().questions)


def test_style_when_verified_examples_are_selected(authored):
    # Given: a dedicated style archive that is absent from the factual index.
    sources, style, plan, _, oracle = assemble(authored)
    factual_ids = {r.record_id for r in sources.records}
    by_question = {c.question_id: c for c in oracle.cases}
    # When: select through the actual production experiment filter.
    selected = [
        select_exemplars(style.corpus(), StyleRequest(
            persona_id=f.persona.persona_id, register=f.persona.speech_register, intent=f.plan.intent,
        ), plan.utterance_tags) for f in authored
    ]
    # Then: exactly two correctly attributed examples; their probes cannot become B facts.
    assert all(len(examples) == 2 for examples in selected)
    for f, examples in zip(authored, selected, strict=True):
        assert all(e.record_id not in factual_ids for e in examples)
        content = "\n".join(r.text + "\n".join(t.text for t in r.turns) for r in f.records)
        assert all(surface not in content for surface in by_question[f.question.question_id].style_only_surfaces)


def test_style_when_leakage_is_limited_to_predeclared_subset(authored):
    # Given: a template bank with multiple eligible source utterances per request.
    bank = Templates.model_validate_json(
        (Path(__file__).parent / "templates95.json").read_text(),
    )
    # When: partition families using the authored probe flag, before model answers.
    probes = [f for f in authored if f.oracle.style_leakage_probe]
    normal = [f for f in authored if not f.oracle.style_leakage_probe]
    # Then: the main style comparison is not an all-leakage stress test.
    assert len(probes) == 72 and len(normal) == 288
    assert sum(f.plan.self_honorific_probe for f in probes) == 36
    assert Counter((f.question.type, f.persona.speech_register) for f in probes) == {
        (k, r): 4 for k in KINDS for r in REGISTERS
    }
    for f in normal:
        pool = bank.style_examples[f.persona.speech_register][f.plan.intent]
        assert all(turn.text in pool for turn in f.style_record.turns)
        assert not f.oracle.style_only_surfaces
    assert all(len(set(pool)) >= 6 for r in bank.style_examples.values() for pool in r.values())


def test_p_cohorts_when_challenges_are_predeclared(authored):
    # Given: no model answers or retrieval scores are available to the author.
    _, _, plan, _, oracle = assemble(authored)
    # When: inspect only predeclared question-only plans and challenge membership.
    cohorts = Counter(c.cohort for c in oracle.cases)
    # Then: both challenges exist; no planned multi-event item is answer-derived.
    assert cohorts == {"control": 240, "missing_second_event": 60, "sufficient_but_omission_risk": 60}
    by_id = {f.question.question_id: f.question.question for f in authored}
    for case in plan.cases:
        assert all(span in by_id[case.question_id] for span in case.item_spans)
    assert Counter(
        conflict.resolution for case in oracle.cases for conflict in case.conflicts
    ) == {"different_speaker": 360, "different_time": 120, "latest_persona_correction": 60}


def test_conflicts_when_later_persona_account_corrects_same_event(authored):
    # Given: old claims and corrective sources authored before retrieval.
    cases = [f for f in authored if f.question.type == "false_premise"]
    # When: resolve source timestamps and event-identifying source fields.
    pairs = [(f.records[0], f.records[1]) for f in cases]
    # Then: the correction concerns the same event, not a different day's activity.
    assert len(pairs) == 60
    for latest, earlier in pairs:
        assert latest.date > earlier.date
        assert latest.text.split(": ")[0] == earlier.text.split(": ")[0]
