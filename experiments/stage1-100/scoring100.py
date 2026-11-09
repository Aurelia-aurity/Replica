"""Exact paired DEV endpoints from complete semantic annotations; never a judge."""

from collections import Counter
from dataclasses import asdict, dataclass
from math import exp, fsum, lgamma, log, log1p
from pathlib import Path
from typing import Final, Literal, TypedDict
from uuid import UUID
import json

from pydantic import Field, StrictBool
import typer

from corpus import FrozenModel
from exact import mcnemar
from schema100 import Arm, Plan100, SUBTYPES

NI_ALPHA: Final = .0125


def binomial_cdf(k: int, n: int, p: float) -> float:
    """Log-domain terms avoid the float overflow in #95 exact.py at n=1320."""
    if not 0 <= k <= n or not 0 <= p <= 1:
        raise ValueError((k, n, p))
    if p == 0:
        return 1.0
    if p == 1:
        return float(k == n)
    terms = [lgamma(n + 1) - lgamma(j + 1) - lgamma(n - j + 1)
             + j * log(p) + (n - j) * log1p(-p) for j in range(k + 1)]
    return min(1.0, fsum(exp(t) for t in terms))


def cp_upper(k: int, n: int, alpha: float = NI_ALPHA) -> float:
    """One-sided exact Clopper-Pearson upper confidence limit."""
    if not 0 <= k <= n or n <= 0 or not 0 < alpha < 1:
        raise ValueError((k, n, alpha))
    if k == n:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if binomial_cdf(k, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


@dataclass(frozen=True, slots=True)
class Rate:
    count: int
    total: int
    rate: float
    lower: float
    upper: float


def rate(k: int, n: int) -> Rate:
    """Descriptive two-sided 95% CP interval (not the NI gate interval)."""
    return Rate(k, n, k / n, 0.0 if k == 0 else 1 - cp_upper(n - k, n, .025),
                cp_upper(k, n, .025))


@dataclass(frozen=True, slots=True)
class Primary:
    gains: int
    losses: int
    total: int
    p_exact: float
    error_reduction: float
    passed: bool


def primary(gains: int, losses: int, n: int = 360) -> Primary:
    if n <= 0 or gains + losses > n:
        raise ValueError((gains, losses, n))
    p = mcnemar(gains, losses)
    reduction = (gains - losses) / n
    return Primary(gains, losses, n, p, reduction, p <= .05 and reduction >= .05)


class ClaimWitness(FrozenModel):
    """One response span with separately identified subject, owner and event."""

    response_span: str = Field(min_length=1)
    asserted_subject: str = Field(min_length=1)
    source_subject: Literal["persona", "provider", "other"]
    utterance_owner: Literal["persona", "provider", "other"]
    source_ids: tuple[UUID, ...] = Field(min_length=1)
    asserted_event_time: str = Field(min_length=1)
    source_event_time: str = Field(min_length=1)
    speaker_error: StrictBool
    temporal_error: StrictBool


class Annotation100(FrozenModel):
    question_id: str
    condition_id: str
    speaker_error: StrictBool
    temporal_error: StrictBool
    grounding: StrictBool
    core_correct: StrictBool | None
    safe_abstention: StrictBool | None
    chat_success: StrictBool | None
    rationale: str = Field(min_length=1)
    witnesses: tuple[ClaimWitness, ...]


class JudgeReceipt(FrozenModel):
    grounding_route: Literal["hybrid_jev_terra"]
    style_route: Literal["terra"]
    # Must bind the actual unchanged policy, not reconstruct it from old aggregates.
    route_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    audits_complete: Literal[True]
    adjudications_complete: Literal[True]
    unresolved_cases: Literal[0]


class ScoringInput(FrozenModel):
    scope: Literal["DEV_ONLY_EVALUATION_ONLY"]
    arm_key: dict[Arm, str]
    judge_receipt: JudgeReceipt
    annotations: tuple[Annotation100, ...]


class GateReport(TypedDict):
    harms: int
    total: int
    alpha: float
    upper: float
    margin: float
    passed: bool


class ScoreReport(TypedDict):
    status: str
    valid: bool
    primary: dict[str, int | float | bool]
    ni: dict[str, GateReport]
    absolute: dict[str, dict[str, dict[str, int | float]]]
    all_gates_pass: bool
    interpretation: str


def score(plan: Plan100, data: ScoringInput) -> ScoreReport:
    """Reject incomplete units rather than silently selecting easy paired cases."""
    expected = {(kind, sub, reg): n // 3 for kind, subs in SUBTYPES.items()
                for sub, n in subs.items() for reg in ("반말", "해요체", "하게체")}
    if Counter((c.type, c.subtype, c.speech_register) for c in plan.cases) != expected:
        raise ValueError("Fixed population/subtypes changed")
    if len({c.family_id for c in plan.cases}) != 1320:
        raise ValueError("Repeated family")
    qids = {c.question_id for c in plan.cases}
    if len(qids) != 1320 or set(data.arm_key) != {"B", "A", "C"} or len(set(data.arm_key.values())) != 3:
        raise ValueError("Invalid question or blind condition identities")
    rows = {(a.question_id, a.condition_id): a for a in data.annotations}
    if len(rows) != len(data.annotations) or set(rows) != {
        (qid, cond) for qid in qids for cond in data.arm_key.values()
    }:
        raise ValueError("Requires all 3960 unique resolved paired annotations")
    for case in plan.cases:
        for condition in data.arm_key.values():
            row = rows[case.question_id, condition]
            applicability = (case.type in ("single", "multi", "ambiguity", "false_premise"),
                             case.type == "unanswerable", case.type == "ordinary_chat")
            for value, applicable in zip(
                (row.core_correct, row.safe_abstention, row.chat_success), applicability, strict=True,
            ):
                if (value is not None) != applicable:
                    raise ValueError("Wrong endpoint applicability")
            if row.speaker_error != any(w.speaker_error for w in row.witnesses) or (
                row.temporal_error != any(w.temporal_error for w in row.witnesses)
            ):
                raise ValueError("Error labels require matching semantic span/source witnesses")

    def paired(qid: str, arm: Arm) -> Annotation100:
        return rows[qid, data.arm_key[arm]]

    primary_ids = [c.question_id for c in plan.cases if c.type == "single"]
    gains = sum(paired(q, "B").speaker_error and not paired(q, "A").speaker_error for q in primary_ids)
    losses = sum(not paired(q, "B").speaker_error and paired(q, "A").speaker_error for q in primary_ids)
    main = primary(gains, losses)
    gates: dict[str, GateReport] = {}
    absolute: dict[str, dict[str, dict[str, int | float]]] = {}
    for endpoint, types, margin in (
        ("grounding", tuple(SUBTYPES), .03),
        ("core_correct", ("single", "multi", "ambiguity", "false_premise"), .03),
        ("safe_abstention", ("unanswerable",), .05),
        ("chat_success", ("ordinary_chat",), .05),
    ):
        ids = [c.question_id for c in plan.cases if c.type in types]
        harms = sum(bool(getattr(paired(q, "B"), endpoint)) and not getattr(paired(q, "A"), endpoint)
                    for q in ids)
        upper = cp_upper(harms, len(ids))
        gates[endpoint] = {"harms": harms, "total": len(ids), "alpha": NI_ALPHA,
                           "upper": upper, "margin": margin, "passed": upper <= margin}
        absolute[endpoint] = {
            arm: asdict(rate(sum(bool(getattr(paired(q, arm), endpoint)) for q in ids), len(ids)))
            for arm in ("B", "A", "C")
        }
    for endpoint in ("speaker_error", "temporal_error"):
        absolute[endpoint] = {
            arm: asdict(rate(sum(getattr(paired(q, arm), endpoint) for q in qids), len(qids)))
            for arm in ("B", "A", "C")
        }
    absolute["primary_speaker_error"] = {
        arm: asdict(rate(sum(paired(q, arm).speaker_error for q in primary_ids), 360))
        for arm in ("B", "A", "C")
    }
    return {
        "status": "DRAFT_NOT_FROZEN", "valid": True, "primary": asdict(main),
        "ni": gates, "absolute": absolute,
        "all_gates_pass": main.passed and all(g["passed"] for g in gates.values()),
        "interpretation": "DEV mechanism only; C diagnostic; no service adoption authorization",
    }


def main(plan_path: Path, annotation_path: Path, output: Path) -> None:
    from prepare100 import reject_forbidden
    reject_forbidden(plan_path, annotation_path, output)
    plan = Plan100.model_validate_json(plan_path.read_text())
    data = ScoringInput.model_validate_json(annotation_path.read_text())
    result = score(plan, data)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    typer.run(main)
