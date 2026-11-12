"""Evidence-state proxies and paired error attribution, not a semantic judge."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, assert_never

from corpus import Evidence, QuestionType

State = Literal["sufficient", "insufficient", "contradictory", "not_applicable"]


@dataclass(frozen=True, slots=True)
class EvidenceSpec:
    kind: QuestionType
    gold_ids: tuple[str, ...]
    slots: tuple[str, ...]
    surfaces: dict[str, tuple[str, ...]]


def evidence_state(spec: EvidenceSpec, evidence: Sequence[Evidence]) -> State:
    """Contradictory means corrective-to-premise, not conflicting source records.

    Gold membership and literal surfaces are conservative metadata proxies, not
    entailment. An unanswerable query has insufficient factual-answer evidence;
    empty retrieval can nevertheless be correct for safe abstention.
    """
    match spec.kind:
        case "ordinary_chat":
            return "not_applicable"
        case "unanswerable":
            return "insufficient"
        case "single" | "multi" | "ambiguity" | "false_premise":
            ids = {str(item.chunk_id) for item in evidence}
            text = "\n".join(item.content for item in evidence)
            complete = bool(spec.gold_ids) and set(spec.gold_ids) <= ids and all(
                any(surface in text for surface in (slot, *spec.surfaces.get(slot, ())))
                for slot in spec.slots
            )
            if not complete:
                return "insufficient"
            match spec.kind:
                case "false_premise":
                    return "contradictory"
                case "single" | "multi" | "ambiguity":
                    return "sufficient"
                case unreachable:
                    assert_never(unreachable)
        case unreachable:
            assert_never(unreachable)


@dataclass(frozen=True, slots=True)
class Attribution:
    pairs: int
    b_errors: int
    c_errors: int
    b_only: int
    persistent: int
    c_only: int
    neither: int

    @property
    def gap(self) -> int:
        return self.b_errors - self.c_errors


def attribute(pairs: Sequence[tuple[bool, bool]]) -> Attribution:
    """Errors remaining in C and errors removed by C are distinct paired cells.

    B-only errors are retrieval-associated, not proven causal failures. C-only
    errors must be retained: the aggregate gap is B-only minus C-only.
    """
    b_only = sum(b and not c for b, c in pairs)
    persistent = sum(b and c for b, c in pairs)
    c_only = sum(c and not b for b, c in pairs)
    return Attribution(
        len(pairs), b_only + persistent, c_only + persistent, b_only,
        persistent, c_only, sum(not b and not c for b, c in pairs),
    )
