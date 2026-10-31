"""Exact persona-scoped cosine search and DEV-only abstention calibration."""

from collections.abc import Sequence
from typing import Final
from uuid import UUID

import numpy as np
from numpy.typing import NDArray
from pydantic import Field

from corpus import Chunk, Evidence, FrozenModel, Question

K_VALUES: Final = (1, 3, 5)
Matrix = NDArray[np.float32]


class Hit(FrozenModel):
    chunk_id: UUID
    score: float
    record_alias: str


class Trace(FrozenModel):
    question: Question
    ranked_ids: tuple[UUID, ...]
    top5: tuple[Hit, ...]
    topk_evidence: tuple[Evidence, ...]
    evidence: tuple[Evidence, ...] = ()
    predicted_no_evidence: bool = False


def exact_search(
    query: Matrix, vectors: Matrix, chunks: Sequence[Chunk], persona_id: str
) -> list[tuple[int, float]]:
    """Filter before ranking; normalized float32 dot product with UUID tie-break."""
    eligible = [i for i, chunk in enumerate(chunks) if chunk.persona_id == persona_id]
    if not eligible:
        raise LookupError(persona_id)
    scores = vectors[eligible] @ query
    return sorted(
        ((index, float(score)) for index, score in zip(eligible, scores, strict=True)),
        key=lambda item: (-item[1], str(chunks[item[0]].chunk_id)),
    )


def trace_query(
    question: Question, query: Matrix, vectors: Matrix, chunks: Sequence[Chunk]
) -> Trace:
    ranking = exact_search(query, vectors, chunks, question.persona_id)
    return Trace(
        question=question,
        ranked_ids=tuple(chunks[index].chunk_id for index, _ in ranking),
        top5=tuple(
            Hit(
                chunk_id=chunks[index].chunk_id,
                score=score,
                record_alias=chunks[index].record_alias,
            )
            for index, score in ranking[:5]
        ),
        topk_evidence=tuple(chunks[index].evidence() for index, _ in ranking[:5]),
    )


class Detection(FrozenModel):
    eligible: int
    unanswerable: int
    answerable: int
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int
    precision: float
    recall: float
    f1: float
    false_empty_rate: float
    unsupported_acceptance_rate: float


def detection(traces: Sequence[Trace], threshold: float) -> Detection:
    """Positive means unanswerable; ordinary chat is not a factual absence probe."""
    eligible = [t for t in traces if t.question.type != "ordinary_chat"]
    true_positive = false_positive = false_negative = true_negative = 0
    for trace in eligible:
        predicted = trace.top5[0].score < threshold
        unanswerable = trace.question.type == "unanswerable"
        true_positive += int(predicted and unanswerable)
        false_positive += int(predicted and not unanswerable)
        false_negative += int(not predicted and unanswerable)
        true_negative += int(not predicted and not unanswerable)
    precision = true_positive / (true_positive + false_positive) if (
        true_positive + false_positive
    ) else 0.0
    recall = true_positive / (true_positive + false_negative) if (
        true_positive + false_negative
    ) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    answerable = false_positive + true_negative
    unanswerable_count = true_positive + false_negative
    return Detection(
        eligible=len(eligible),
        unanswerable=unanswerable_count,
        answerable=answerable,
        true_positive=true_positive,
        false_positive=false_positive,
        false_negative=false_negative,
        true_negative=true_negative,
        precision=precision,
        recall=recall,
        f1=f1,
        false_empty_rate=false_positive / answerable if answerable else 0.0,
        unsupported_acceptance_rate=false_negative / unanswerable_count
        if unanswerable_count else 0.0,
    )


class Calibration(FrozenModel):
    threshold: float
    policy: str = "max DEV unanswerable F1; tie: min false-empty; then min threshold"
    comparator: str = "no evidence iff top1 score < threshold"
    split: str = "dev"
    candidates: int
    detection: Detection


def calibrate(traces: Sequence[Trace]) -> Calibration:
    """Never accept TEST traces, including accidental mixed DEV/TEST calibration."""
    if not traces or any(t.question.split != "dev" for t in traces):
        raise ValueError("calibration requires nonempty DEV-only traces")
    eligible = [t for t in traces if t.question.type != "ordinary_chat"]
    labels = {t.question.type == "unanswerable" for t in eligible}
    if labels != {True, False}:
        raise ValueError("DEV calibration requires both absence and evidence labels")
    scores = sorted({t.top5[0].score for t in eligible})
    candidates = [scores[0], *(float(np.nextafter(s, np.inf)) for s in scores)]
    best = min(
        ((tau, detection(eligible, tau)) for tau in candidates),
        key=lambda pair: (-pair[1].f1, pair[1].false_empty_rate, pair[0]),
    )
    return Calibration(threshold=best[0], candidates=len(candidates), detection=best[1])


def select_evidence(trace: Trace, threshold: float) -> Trace:
    """Threshold each top-five hit; retain unthresholded candidates separately."""
    accepted = tuple(
        evidence
        for evidence, hit in zip(trace.topk_evidence, trace.top5, strict=True)
        if hit.score >= threshold
    )
    return trace.model_copy(update={
        "evidence": accepted,
        "predicted_no_evidence": trace.top5[0].score < threshold,
    })


class RankingMetrics(FrozenModel):
    count: int
    recall_all: dict[str, float | None] = Field(default_factory=dict)
    recall_any: dict[str, float | None] = Field(default_factory=dict)
    mrr: float | None


def ranking_metrics(traces: Sequence[Trace]) -> RankingMetrics:
    eligible = [t for t in traces if t.question.gold_chunk_ids]
    all_hits: dict[str, float | None] = {}
    any_hits: dict[str, float | None] = {}
    for k in K_VALUES:
        all_hits[str(k)] = sum(
            set(t.question.gold_chunk_ids) <= set(t.ranked_ids[:k]) for t in eligible
        ) / len(eligible) if eligible else None
        any_hits[str(k)] = sum(
            bool(set(t.question.gold_chunk_ids) & set(t.ranked_ids[:k])) for t in eligible
        ) / len(eligible) if eligible else None
    reciprocal_ranks = [
        next(
            (1.0 / rank for rank, chunk_id in enumerate(t.ranked_ids, start=1)
             if chunk_id in t.question.gold_chunk_ids),
            0.0,
        )
        for t in eligible
    ]
    return RankingMetrics(
        count=len(eligible), recall_all=all_hits, recall_any=any_hits,
        mrr=sum(reciprocal_ranks) / len(eligible) if eligible else None,
    )


class SplitMetrics(FrozenModel):
    questions: int
    ranking: RankingMetrics
    multi_chunk: RankingMetrics
    by_type: dict[str, RankingMetrics]
    no_evidence: Detection
    selected_all_gold_recall: float
    ordinary_chat_count: int
    ordinary_chat_empty_rate: float


def evaluate(traces: Sequence[Trace], threshold: float) -> SplitMetrics:
    answerable = [t for t in traces if t.question.gold_chunk_ids]
    ordinary = [t for t in traces if t.question.type == "ordinary_chat"]
    return SplitMetrics(
        questions=len(traces),
        ranking=ranking_metrics(traces),
        multi_chunk=ranking_metrics(
            [t for t in traces if len(t.question.gold_chunk_ids) > 1]
        ),
        by_type={
            kind: ranking_metrics([t for t in traces if t.question.type == kind])
            for kind in sorted({t.question.type for t in traces})
        },
        no_evidence=detection(traces, threshold),
        selected_all_gold_recall=sum(
            set(t.question.gold_chunk_ids) <= {e.chunk_id for e in t.evidence}
            for t in answerable
        ) / len(answerable),
        ordinary_chat_count=len(ordinary),
        ordinary_chat_empty_rate=sum(not t.evidence for t in ordinary) / len(ordinary)
        if ordinary else 0.0,
    )
