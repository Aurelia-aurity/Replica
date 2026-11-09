"""Blind, paired re-adjudication policy; no provider or private sidecar access."""

from collections import Counter
from typing import Final

from scoring100 import Annotation100

GATED_FIELDS: Final = ("grounding", "core_correct", "safe_abstention", "chat_success")


def discordant(left: Annotation100, right: Annotation100, *, primary: bool) -> bool:
    """Select on initial labels only, symmetrically and without netting fields."""
    if left.question_id != right.question_id or left.condition_id == right.condition_id:
        raise ValueError("Expected distinct conditions of the same paired question")
    return any(getattr(left, f) != getattr(right, f) for f in GATED_FIELDS) or (
        primary and left.speaker_error != right.speaker_error
    )


def majority(runs: tuple[Annotation100, Annotation100, Annotation100]) -> Annotation100:
    """Fieldwise majority of exactly three independent, resolved response labels."""
    first = runs[0]
    if len(runs) != 3 or any(
        (r.question_id, r.condition_id) != (first.question_id, first.condition_id) for r in runs
    ):
        raise ValueError("Requires three labels for the same blinded response")
    updates = {}
    for field in (*GATED_FIELDS, "speaker_error", "temporal_error"):
        values = [getattr(r, field) for r in runs]
        if any(v is None for v in values) and not all(v is None for v in values):
            raise ValueError("Endpoint applicability changed across runs")
        updates[field] = Counter(values).most_common(1)[0][0]
    # A majority vector need not occur in one run. Retain exact positive witnesses
    # from agreeing runs and clear minority-only diagnostic flags, not the spans.
    witnesses = []
    for row in runs:
        for witness in row.witnesses:
            speaker = witness.speaker_error and updates["speaker_error"]
            temporal = witness.temporal_error and updates["temporal_error"]
            if speaker or temporal:
                kept = witness.model_copy(update={"speaker_error": speaker, "temporal_error": temporal})
                if kept not in witnesses:
                    witnesses.append(kept)
    if any(updates[f] and not any(getattr(w, f) for w in witnesses)
           for f in ("speaker_error", "temporal_error")):
        raise ValueError("Majority error lacks a positive source witness")
    return Annotation100.model_validate({
        **first.model_dump(), **updates, "witnesses": tuple(witnesses),
        "rationale": "Per-field majority of three independent calls. "
        + " | ".join(f"run{i + 1}: {r.rationale}" for i, r in enumerate(runs)),
    })
