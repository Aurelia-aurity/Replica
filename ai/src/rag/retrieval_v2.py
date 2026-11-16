"""Port of #59 retrieval v2; no ten-item pruning or storage/encoder policy."""
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
import json
from typing import assert_never

from .types import Chunk, Evidence, Hit, Record, Retrieval

THRESHOLD = 0.5458711981773378


@dataclass(frozen=True, slots=True)
class ContextConfig:
    k: int = 3
    window: int = 3
    budget: int = 2000

    def __post_init__(self) -> None:
        if not 1 <= self.k <= 5 or self.window < 0 or not 2 <= self.budget <= 2000:
            raise ValueError


def exact_search(query, vectors, chunks: Sequence[Chunk], persona_id: str) -> Retrieval:
    """Use #59's normalized float32 matrix dot product without loading an encoder.

    NumPy arrays are supplied by the caller; HTTP serving does not import NumPy.
    Normalization/dimension checks belong to the future encoder/index boundary.
    """
    eligible = [i for i, chunk in enumerate(chunks) if chunk.persona_id == persona_id]
    if not eligible:
        return Retrieval(persona_id, (), predicted_no_evidence=True)
    scores = vectors[eligible] @ query
    ranking = sorted(
        ((index, float(score)) for index, score in zip(eligible, scores, strict=True)),
        key=lambda item: (-item[1], chunks[item[0]].chunk_id),
    )
    return Retrieval(persona_id, tuple(
        Hit(chunks[index].chunk_id, score) for index, score in ranking[:5]))


def evidence_json(evidence: Sequence[Evidence]) -> str:
    """Keep #59 r1's field order and pretty-printed token-count input."""
    return json.dumps([asdict(item) for item in evidence], ensure_ascii=False, indent=2)


def contextual_evidence(chunk: Chunk) -> Evidence:
    prefix = f"{chunk.date} | {chunk.speaker} (speaker_role={chunk.speaker_role}) | "
    return replace(chunk.evidence(), content="\n".join(
        prefix + line for line in chunk.content.splitlines()))


def restore_context(
    trace: Retrieval,
    chunks: Sequence[Chunk],
    records: Sequence[Record],
    count_tokens: Callable[[str], int],
    config: ContextConfig = ContextConfig(),
    threshold: float = THRESHOLD,
) -> Retrieval:
    """Restore whole chunks; caller supplies the same authorized archive as ranking.

    IDs and source boundaries are caller inputs, never issued or inferred here.
    Empty eligible archives abstain; unresolvable hits fail explicitly.
    """
    if not trace.top5 or trace.top5[0].score < threshold:
        return replace(trace, evidence=(), topk_evidence=(), predicted_no_evidence=True)
    # Scope neighbors as well as hits. No DB authorization claim is made here.
    by_id = {c.chunk_id: c for c in chunks if c.persona_id == trace.persona_id}
    record_map = {r.record_id: r for r in records if r.persona_id == trace.persona_id}
    scores: dict[str, float] = {}
    windows: dict[str, set[int]] = {}
    hit_ids: set[str] = set()
    for hit in trace.top5[:config.k]:
        chunk = by_id[hit.chunk_id]
        record = record_map[chunk.record_id]
        if chunk.text_version_id != record.text_version_id:
            raise ValueError
        hit_ids.add(chunk.chunk_id)
        scores[record.record_id] = max(scores.get(record.record_id, hit.score), hit.score)
        turns = windows.setdefault(record.record_id, set())
        for turn in chunk.source_turns:
            turns.update(range(max(0, turn - config.window),
                               min(record.turn_count, turn + config.window + 1)))
    selected = []
    for chunk in by_id.values():
        if chunk.record_id not in scores:
            continue
        record = record_map[chunk.record_id]
        if chunk.text_version_id != record.text_version_id:
            raise ValueError
        match record.kind:
            case "diary":
                selected.append(chunk)
            case "chat":
                if set(chunk.source_turns) & windows[record.record_id]:
                    selected.append(chunk)
            case unreachable:
                assert_never(unreachable)
    selected.sort(key=lambda c: (c.date, c.record_id, c.ordinal))
    candidates = tuple(contextual_evidence(c) for c in selected)
    for record_id in scores:
        group = [c for c in selected if c.record_id == record_id]
        if count_tokens(evidence_json([contextual_evidence(c) for c in group])) > config.budget:
            selected = [c for c in selected
                        if c.record_id != record_id or c.chunk_id in hit_ids]
    while count_tokens(evidence_json([contextual_evidence(c) for c in selected])) > config.budget:
        if not selected:
            raise ValueError
        lowest = min({c.record_id for c in selected}, key=lambda rid: (scores[rid], rid))
        selected = [c for c in selected if c.record_id != lowest]
    accepted = tuple(contextual_evidence(c) for c in selected)
    if not accepted:
        raise ValueError
    return replace(trace, evidence=accepted, topk_evidence=candidates,
                   predicted_no_evidence=False)
