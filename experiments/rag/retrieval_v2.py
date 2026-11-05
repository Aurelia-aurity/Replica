"""Question-level abstention and deterministic, budgeted source context."""

from collections.abc import Callable, Sequence
import json
from typing import assert_never
from uuid import UUID

from pydantic import Field

from corpus import Chunk, Evidence, FrozenModel, Question, Record
from retrieval import Trace


class ContextConfig(FrozenModel):
    k: int = Field(ge=1, le=5)
    window: int = Field(ge=0)
    budget: int = Field(ge=2, le=2000)


def evidence_json(evidence: Sequence[Evidence]) -> str:
    """Match the exact pretty-printed JSON payload rendered by prompt r1."""
    return json.dumps(
        [item.model_dump(mode="json") for item in evidence],
        ensure_ascii=False, indent=2,
    )


def contextual_evidence(chunk: Chunk) -> Evidence:
    """Keep chunk identity and role; label every original line with date/speaker."""
    prefix = f"{chunk.date} | {chunk.speaker} (speaker_role={chunk.speaker_role}) | "
    return chunk.evidence().model_copy(update={
        "content": "\n".join(prefix + line for line in chunk.content.splitlines()),
    })


def restore_context(
    trace: Trace,
    threshold: float,
    chunks: Sequence[Chunk],
    records: Sequence[Record],
    config: ContextConfig,
    count_tokens: Callable[[str], int],
) -> Trace:
    """Expand hits without labels/gold; merge windows at canonical chunk boundaries.

    Short diaries are restored whole. Oversized records fall back to their hit
    chunks, then whole chunks are removed; text is never sliced mid-line. Budget
    pruning always removes the lowest-scoring record first, with UUID ties.
    """
    if trace.top5[0].score < threshold:
        return trace.model_copy(update={
            "evidence": (), "topk_evidence": (), "predicted_no_evidence": True,
        })
    by_id = {c.chunk_id: c for c in chunks}
    record_map = {r.record_id: r for r in records}
    scores: dict[UUID, float] = {}
    windows: dict[UUID, set[int]] = {}
    hit_ids: set[UUID] = set()
    for hit in trace.top5[:config.k]:
        chunk = by_id[hit.chunk_id]
        if chunk.persona_id != trace.question.persona_id:
            raise ValueError("foreign persona hit")
        record = record_map[chunk.record_id]
        hit_ids.add(chunk.chunk_id)
        scores[record.record_id] = max(scores.get(record.record_id, hit.score), hit.score)
        turns = windows.setdefault(record.record_id, set())
        for turn in chunk.source_turns:
            turns.update(range(max(0, turn - config.window), min(
                len(record.turns), turn + config.window + 1,
            )))
    selected: list[Chunk] = []
    for chunk in chunks:
        if chunk.record_id not in scores:
            continue
        record = record_map[chunk.record_id]
        match record.kind:
            case "diary":
                selected.append(chunk)
            case "chat":
                if set(chunk.source_turns) & windows[record.record_id]:
                    selected.append(chunk)
            case unreachable:
                assert_never(unreachable)
    selected.sort(key=lambda c: (c.date, str(c.record_id), c.ordinal))
    candidates = tuple(contextual_evidence(c) for c in selected)
    # Very long entries use only retrieved chunks before record-level pruning.
    for record_id in scores:
        group = [c for c in selected if c.record_id == record_id]
        if count_tokens(evidence_json([contextual_evidence(c) for c in group])) > config.budget:
            selected = [
                c for c in selected
                if c.record_id != record_id or c.chunk_id in hit_ids
            ]
    while count_tokens(evidence_json([contextual_evidence(c) for c in selected])) > config.budget:
        lowest = min({c.record_id for c in selected}, key=lambda rid: (scores[rid], str(rid)))
        selected = [c for c in selected if c.record_id != lowest]
    accepted = tuple(contextual_evidence(c) for c in selected)
    if not accepted:
        raise ValueError("budget cannot retain the highest-scoring record")
    return trace.model_copy(update={
        "evidence": accepted, "topk_evidence": candidates, "predicted_no_evidence": False,
    })


def missing_slots(question: Question, evidence: Sequence[Evidence]) -> tuple[str, ...]:
    """Literal surfaces measure content coverage, not IDs or semantic entailment."""
    content = "\n".join(item.content for item in evidence)
    return tuple(
        slot for slot in question.required_slots
        if not any(surface in content for surface in (
            slot, *question.slot_surface_forms.get(slot, ()),
        ))
    )
