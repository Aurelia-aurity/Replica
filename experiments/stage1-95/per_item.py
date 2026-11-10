"""Question-only query decomposition and budgeted retrieval-v2 composition."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import re
from typing import Final
from uuid import UUID

from corpus import Chunk, Evidence, Question, Record
from retrieval import Matrix, trace_query
from retrieval_v2 import ContextConfig, evidence_json, restore_context

THRESHOLD: Final = 0.5458711981773378


def split_items(question: str) -> tuple[str, ...]:
    """Conservative explicit-clause split; retain original context in each query.

    Only separators with a following interrogative/clause are recognized.
    Unmarked shared-predicate constructions need an authored question-only plan.
    No gold IDs, support, slots, labels or answers are accepted by this planner.
    """
    clauses = re.split(r"\s*(?:;|그리고|또는)\s*|,\s+(?=다음|그\s*뒤|오후|어디|무엇|뭘)", question)
    clauses = list(dict.fromkeys(clause.strip() for clause in clauses if clause.strip()))
    if len(clauses) < 2:
        return (question,)
    return tuple(f"{question}\n검색할 요구 항목: {clause}" for clause in clauses)


@dataclass(frozen=True, slots=True)
class SearchIndex:
    chunks: tuple[Chunk, ...]
    records: tuple[Record, ...]
    vectors: Matrix


@dataclass(frozen=True, slots=True)
class ItemQuery:
    text: str
    vector: Matrix


@dataclass(frozen=True, slots=True)
class ItemResult:
    query: str
    retrieved_ids: tuple[UUID, ...]
    retained_ids: tuple[UUID, ...]
    abstained: bool


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    evidence: tuple[Evidence, ...]
    items: tuple[ItemResult, ...]


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    count_tokens: Callable[[str], int]
    context: ContextConfig = ContextConfig(k=3, window=3, budget=2000)


def retrieve_items(
    question: Question, queries: Sequence[ItemQuery], index: SearchIndex,
    *, policy: RetrievalPolicy,
) -> RetrievalResult:
    """Run unchanged v2 per item, then merge whole source records round-robin.

    Record groups retain v2 source windows. Prioritize the top-scored record for
    each item before lower-ranked records. Never slice a chunk to fit a cap.
    Exact support recall is an evaluation metric, not a selection input.

    The four inputs are independent domain values: source question, item
    vectors, reusable search index and tokenizer/context policy.
    """
    if not queries or len({q.text for q in queries}) != len(queries):
        raise ValueError("Item queries must be nonempty and unique")
    if question.split != "dev" or any(c.split != "dev" for c in index.chunks):
        raise ValueError("Per-item retrieval is DEV-only")
    traces = []
    groups = []
    for query in queries:
        item_question = question.model_copy(update={"question": query.text})
        raw = trace_query(item_question, query.vector, index.vectors, index.chunks)
        trace = restore_context(
            raw, THRESHOLD, index.chunks, index.records, policy.context, policy.count_tokens,
        )
        traces.append(trace)
        score = {hit.chunk_id: hit.score for hit in raw.top5[:policy.context.k]}
        record_scores: dict[UUID, float] = {}
        for chunk in index.chunks:
            if chunk.chunk_id in score:
                record_scores[chunk.record_id] = max(
                    record_scores.get(chunk.record_id, -1.0), score[chunk.chunk_id],
                )
        records = sorted({e.record_id for e in trace.evidence},
                         key=lambda rid: (-record_scores[rid], str(rid)))
        groups.append([tuple(e for e in trace.evidence if e.record_id == rid) for rid in records])
    order = {chunk.chunk_id: (chunk.date, str(chunk.record_id), chunk.ordinal) for chunk in index.chunks}
    selected: dict[UUID, Evidence] = {}
    for rank in range(max(map(len, groups), default=0)):
        for item_groups in groups:
            if rank >= len(item_groups):
                continue
            candidate = {**selected, **{e.chunk_id: e for e in item_groups[rank]}}
            canonical = tuple(sorted(candidate.values(), key=lambda e: order[e.chunk_id]))
            if policy.count_tokens(evidence_json(canonical)) <= policy.context.budget:
                selected = candidate
    evidence = tuple(sorted(selected.values(), key=lambda e: order[e.chunk_id]))
    ids = set(selected)
    return RetrievalResult(evidence, tuple(
        ItemResult(query.text, tuple(e.chunk_id for e in trace.evidence),
                   tuple(e.chunk_id for e in trace.evidence if e.chunk_id in ids),
                   trace.predicted_no_evidence)
        for query, trace in zip(queries, traces, strict=True)
    ))
