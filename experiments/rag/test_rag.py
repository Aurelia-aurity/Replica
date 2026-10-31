"""Deterministic corpus, role, ranking, threshold and contract regression checks."""

from uuid import NAMESPACE_URL, uuid5

import numpy as np
import pytest
from pydantic import ValidationError

from corpus import (
    CorpusIntegrityError,
    Evidence,
    Question,
    QuestionType,
    Split,
    chunk_corpus,
    chunk_record,
    load_corpus,
    particle_errors,
    validate,
)
from retrieval import (
    Hit,
    Trace,
    calibrate,
    detection,
    exact_search,
    ranking_metrics,
    select_evidence,
)


@pytest.fixture
def corpus():
    return load_corpus()


def test_corpus_integrity_when_frozen_fixture_loaded(corpus):
    # Given: synthetic corpus with pre-retrieval gold.
    # When: run the actual validator.
    chunks = validate(corpus)
    # Then: all required counts and six question types exist in each split.
    assert (len(corpus.personas), len(corpus.records), len(chunks)) == (5, 100, 400)
    assert len({r.family_id for r in corpus.records}) == 50
    for split in ("dev", "test"):
        questions = [q for q in corpus.questions if q.split == split]
        assert len(questions) == 40
        assert {q.type for q in questions} == {
            "single", "multi", "unanswerable", "false_premise", "ambiguity", "ordinary_chat",
        }


def test_chunks_stable_when_input_order_reversed(corpus):
    # Given: same immutable records in a different traversal order.
    reversed_corpus = corpus.model_copy(update={"records": tuple(reversed(corpus.records))})
    # When: chunk both orders.
    original = {c.chunk_id: c for c in chunk_corpus(corpus)}
    reordered = {c.chunk_id: c for c in chunk_corpus(reversed_corpus)}
    # Then: chunk identity/content does not depend on traversal order.
    assert original == reordered


def test_required_slots_mandatory_when_question_parsed(corpus):
    # Given: a serialized question with its slot annotation removed.
    data = corpus.questions[0].model_dump()
    del data["required_slots"]
    # When / Then: the boundary refuses an unannotated question.
    with pytest.raises(ValidationError):
        Question.model_validate(data)


@pytest.mark.parametrize("mutation", [
    "missing", "empty", "blank", "duplicate", "unknown_surface",
    "blank_surface", "empty_surface", "nonfactual",
])
def test_required_slots_rejected_when_annotation_invalid(corpus, mutation):
    # Given: a single invalid slot contract, independent of the authored prose.
    questions = list(corpus.questions)
    q = questions[0]
    match mutation:
        case "missing":
            q = q.model_copy(update={"required_slots": ("absent-slot",)})
        case "empty":
            q = q.model_copy(update={"required_slots": ()})
        case "blank":
            q = q.model_copy(update={"required_slots": (" ",)})
        case "duplicate":
            q = q.model_copy(update={"required_slots": (q.required_slots[0],) * 2})
        case "unknown_surface":
            q = q.model_copy(update={"slot_surface_forms": {"unknown": ("surface",)}})
        case "blank_surface":
            q = q.model_copy(update={
                "slot_surface_forms": {q.required_slots[0]: ("",)},
            })
        case "empty_surface":
            q = q.model_copy(update={
                "slot_surface_forms": {q.required_slots[0]: ()},
            })
        case "nonfactual":
            q = q.model_copy(update={
                "type": "unanswerable", "gold_chunk_ids": (), "support": (),
                "expected_answer": None, "note": "No evidence.",
            })
    questions[0] = q
    damaged = corpus.model_copy(update={"questions": tuple(questions)})
    # When / Then: neither missing facts nor vacuous annotations pass.
    with pytest.raises(CorpusIntegrityError):
        validate(damaged)


def test_required_slot_surface_when_gold_uses_alternative(corpus):
    # Given: a canonical slot represented by an explicitly listed gold surface.
    q = corpus.questions[0]
    changed = q.model_copy(update={
        "required_slots": ("canonical-slot",),
        "slot_surface_forms": {"canonical-slot": (q.support[0].answer,)},
    })
    changed_corpus = corpus.model_copy(update={
        "questions": (changed, *corpus.questions[1:]),
    })
    # When: validate the real support-to-chunk correspondence and surface.
    chunks = validate(changed_corpus)
    # Then: a nonliteral but declared surface is supported.
    assert chunks == chunk_corpus(corpus)


def test_required_slots_reject_when_fact_outside_support(corpus):
    # Given: an omitted fact still present in the same full gold chunk.
    q = next(q for q in corpus.questions if len(q.required_slots) > 1)
    slot = next(s for s in q.required_slots if s != q.support[0].answer)
    narrowed = q.support[0].model_copy(update={"quote": q.support[0].answer})
    changed = q.model_copy(update={
        "required_slots": (slot,), "slot_surface_forms": {},
        "support": (narrowed,),
    })
    changed_corpus = corpus.model_copy(update={
        "questions": (changed, *corpus.questions[1:]),
    })
    # When / Then: full-chunk presence cannot hide an incomplete support quote.
    with pytest.raises(CorpusIntegrityError, match="required slot absent"):
        validate(changed_corpus)


def test_diary_paragraphs_when_blank_lines_present(corpus):
    # Given: one diary with two paragraphs.
    record = corpus.records[0]
    # When: chunk it.
    chunks = chunk_record(record, corpus.personas[0])
    # Then: exact paragraph content and original metadata survive.
    assert [c.content for c in chunks] == record.text.split("\n\n")
    assert all(c.speaker_role == "persona" and c.date == record.date for c in chunks)
    assert all(c.record_id == record.record_id for c in chunks)


def test_chat_roles_when_provider_persona_other_interleave(corpus):
    # Given: provider is "나", persona is the selected other party.
    record = corpus.records[1]
    record = record.model_copy(update={
        "turns": (
            record.turns[0], record.turns[1], record.turns[3],
            record.turns[4], record.turns[5],
        ),
    })
    # When: contiguous same-speaker turns are grouped.
    chunks = chunk_record(record, corpus.personas[0])
    # Then: no role is assigned to someone else's words.
    assert [c.speaker_role for c in chunks] == ["provider", "persona", "other", "persona"]
    assert [c.source_turns for c in chunks] == [(0,), (1, 2), (3,), (4,)]
    assert chunks[1].content == "\n".join(t.text for t in record.turns[1:3])


def test_chat_group_limit_when_three_same_speaker_turns(corpus):
    # Given: three contiguous identical-speaker turns.
    record = corpus.records[1]
    turn = record.turns[1]
    record = record.model_copy(update={"turns": (turn, turn, turn)})
    # When: chunk it.
    chunks = chunk_record(record, corpus.personas[0])
    # Then: bounded groups preserve each source turn exactly once.
    assert [c.source_turns for c in chunks] == [(0, 1), (2,)]


def test_id_changes_when_text_version_changes(corpus):
    # Given: a new immutable text version.
    record = corpus.records[0]
    changed = record.model_copy(update={"text_version_id": uuid5(NAMESPACE_URL, "new-version")})
    # When: chunk the two versions.
    old = chunk_record(record, corpus.personas[0])
    new = chunk_record(changed, corpus.personas[0])
    # Then: stale chunks cannot collide with the new version.
    assert {c.chunk_id for c in old}.isdisjoint(c.chunk_id for c in new)


@pytest.mark.parametrize("mutation", [
    "duplicate_record", "duplicate_question", "duplicate_persona", "split_leak",
    "missing_gold", "wrong_persona", "wrong_quote", "wrong_answer",
    "nonempty_unanswerable", "provider_gold",
])
def test_validator_rejects_when_fixture_corrupted(corpus, mutation):
    # Given: one independently corrupted boundary fixture.
    questions = list(corpus.questions)
    records = list(corpus.records)
    personas = list(corpus.personas)
    q = questions[0]
    match mutation:
        case "duplicate_record":
            records.append(records[0])
        case "duplicate_question":
            questions.append(q)
        case "duplicate_persona":
            personas.append(personas[0])
        case "split_leak":
            questions[0] = q.model_copy(update={"split": "test"})
        case "missing_gold":
            missing = uuid5(NAMESPACE_URL, "missing")
            questions[0] = q.model_copy(update={
                "gold_chunk_ids": (missing,),
                "support": (q.support[0].model_copy(update={"chunk_id": missing}),),
            })
        case "wrong_persona":
            questions[0] = q.model_copy(update={"persona_id": "p2"})
        case "wrong_quote":
            questions[0] = q.model_copy(update={
                "support": (q.support[0].model_copy(update={"quote": "없는 문장"}),),
            })
        case "wrong_answer":
            questions[0] = q.model_copy(update={
                "support": (q.support[0].model_copy(update={"answer": "없는 답"}),),
            })
        case "nonempty_unanswerable":
            questions[0] = q.model_copy(update={"type": "unanswerable"})
        case "provider_gold":
            provider = chunk_corpus(corpus)[2]
            questions[0] = q.model_copy(update={
                "gold_chunk_ids": (provider.chunk_id,),
                "support": (q.support[0].model_copy(update={
                    "chunk_id": provider.chunk_id, "quote": provider.content,
                    "answer": provider.content,
                }),),
            })
    damaged = corpus.model_copy(update={
        "records": tuple(records), "questions": tuple(questions), "personas": tuple(personas),
    })
    # When / Then: validation fails, rather than silently skipping bad labels.
    with pytest.raises(CorpusIntegrityError):
        validate(damaged)


def test_exact_search_filters_when_other_persona_scores_higher(corpus):
    # Given: unauthorized-persona chunk has cosine 1; allowed chunks have 0.
    chunks = chunk_corpus(corpus)
    vectors = np.zeros((len(chunks), 2), dtype=np.float32)
    foreign = next(i for i, c in enumerate(chunks) if c.persona_id == "p2")
    vectors[foreign] = [1.0, 0.0]
    # When: search only p1, including deterministic score ties.
    hits = exact_search(np.array([1.0, 0.0], dtype=np.float32), vectors, chunks, "p1")
    # Then: no foreign chunk survives and ties use UUID order.
    assert all(chunks[i].persona_id == "p1" for i, _ in hits)
    assert [str(chunks[i].chunk_id) for i, _ in hits] == sorted(
        str(c.chunk_id) for c in chunks if c.persona_id == "p1"
    )


def probe(corpus, score: float, kind: QuestionType, split: Split = "dev") -> Trace:
    question = corpus.questions[0].model_copy(update={"type": kind, "split": split})
    chunk = chunk_corpus(corpus)[0]
    if kind in {"unanswerable", "ordinary_chat"}:
        question = question.model_copy(update={"gold_chunk_ids": (), "support": ()})
    return Trace(
        question=question,
        ranked_ids=(chunk.chunk_id,),
        top5=(Hit(chunk_id=chunk.chunk_id, score=score, record_alias=chunk.record_alias),),
        topk_evidence=(chunk.evidence(),),
    )


def test_calibration_when_dev_classes_separable(corpus):
    # Given: a clear separation, plus irrelevant high-scoring ordinary chat.
    traces = [
        probe(corpus, 0.2, "unanswerable"),
        probe(corpus, 0.8, "single"),
        probe(corpus, 0.99, "ordinary_chat"),
    ]
    # When: select the frozen policy's DEV threshold.
    result = calibrate(traces)
    # Then: perfect absence detection, with the lowest threshold above 0.2.
    assert 0.2 < result.threshold < 0.8
    assert result.detection.f1 == 1
    assert result.detection.false_empty_rate == 0
    assert result.detection.eligible == 2


def test_calibration_rejects_when_test_included(corpus):
    # Given: an accidental TEST trace in a calibration batch.
    traces = [probe(corpus, 0.2, "unanswerable"), probe(corpus, 0.8, "single", "test")]
    # When / Then: refuse any threshold fitting.
    with pytest.raises(ValueError, match="DEV-only"):
        calibrate(traces)


def test_threshold_when_score_exactly_equal(corpus):
    # Given: exact threshold equality.
    trace = probe(corpus, 0.5, "single")
    # When: select evidence with >= threshold.
    selected = select_evidence(trace, 0.5)
    # Then: equality is accepted consistently by export and detection.
    assert selected.evidence == trace.topk_evidence
    assert not selected.predicted_no_evidence
    assert detection([trace], 0.5).false_empty_rate == 0


def test_metrics_when_multichunk_gold_ranked_separately(corpus):
    # Given: first gold at rank 1, second gold at rank 3.
    chunks = chunk_corpus(corpus)
    trace = probe(corpus, 0.8, "multi")
    question = trace.question.model_copy(update={
        "gold_chunk_ids": (chunks[0].chunk_id, chunks[2].chunk_id),
    })
    trace = trace.model_copy(update={
        "question": question,
        "ranked_ids": tuple(c.chunk_id for c in chunks[:3]),
    })
    # When: compute all-gold and any-gold recall independently.
    result = ranking_metrics([trace])
    # Then: partial retrieval does not count as complete support.
    assert result.recall_all == {"1": 0.0, "3": 1.0, "5": 1.0}
    assert result.recall_any == {"1": 1.0, "3": 1.0, "5": 1.0}
    assert result.mrr == 1.0


def test_mrr_when_gold_beyond_top_five(corpus):
    # Given: an actual full ranking with first relevant hit seventh.
    chunks = chunk_corpus(corpus)
    trace = probe(corpus, 0.8, "single")
    trace = trace.model_copy(update={
        "question": trace.question.model_copy(update={"gold_chunk_ids": (chunks[6].chunk_id,)}),
        "ranked_ids": tuple(c.chunk_id for c in chunks[:7]),
    })
    # When: compute ranking metrics.
    result = ranking_metrics([trace])
    # Then: MRR is not incorrectly truncated to top five.
    assert result.mrr == pytest.approx(1 / 7)
    assert result.recall_any["5"] == 0


def test_evidence_export_when_chunk_has_sidecar_metadata(corpus):
    # Given: a full offline chunk with role/date/family metadata.
    chunk = chunk_corpus(corpus)[0]
    # When: produce the wire-shaped evidence.
    evidence = chunk.evidence()
    # Then: exactly five contract fields, with no similarity/gold leakage.
    assert set(evidence.model_dump()) == {
        "chunk_id", "record_id", "text_version_id", "content", "speaker_role",
    }
    assert Evidence.model_validate_json(evidence.model_dump_json()) == evidence


@pytest.mark.parametrize("text,noun", [
    ("보람와 갔다.", "보람"), ("노을와 왔다.", "노을"),
    ("늘봄와 먹었다.", "늘봄"), ("가온를 만났다.", "가온"),
    ("해솔는 쉰다.", "해솔"), ("가람가 왔다.", "가람"),
    ("누리과 왔다.", "누리"), ("여울를 봤다.", "여울"),
    ("우산을 챙겼다.", "우산"),
])
def test_particles_flagged_when_known_noun_has_wrong_ending(text, noun):
    # Given: a mismatched case particle, except the last valid control.
    # When: check the known noun boundary.
    errors = particle_errors(text, {noun})
    # Then: each actual mismatch is caught without flagging the valid control.
    assert bool(errors) == (text != "우산을 챙겼다.")


@pytest.mark.parametrize("text,noun", [
    ("가람이는 먼저 갔다.", "가람"),
    ("이름은 밤이인데 털은 희다.", "밤이"),
    ("누리와 갔다.", "누리"),
    ("보람과 먹었다.", "보람"),
    ("해솔이 말했다.", "해솔"),
])
def test_particles_preserved_when_particle_or_suffix_is_valid(text, noun):
    # Given: valid particles, a nickname suffix, or a copula.
    # When: check them.
    errors = particle_errors(text, {noun})
    # Then: the guard does not rewrite natural prose.
    assert errors == []
