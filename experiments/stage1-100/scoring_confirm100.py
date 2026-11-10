"""Frozen confirm-v3 scoring; the hash-bound DEV scorer stays historical."""

from dataclasses import asdict, dataclass
import json
from math import sqrt
from pathlib import Path
from statistics import NormalDist
from typing import Final, TypedDict

import typer

from schema100 import Plan100
from scoring100 import NI_ALPHA, ScoringInput, score

STATUS: Final = "FROZEN_CONFIRM_V3_2026_11_10"


@dataclass(frozen=True, slots=True)
class NetGate:
    harms: int
    gains: int
    total: int
    net_harm: float
    alpha: float
    upper: float
    margin: float
    passed: bool


def tango_upper(b: int, c: int, n: int, alpha: float = NI_ALPHA) -> float:
    """Invert Tango's uncorrected paired score test for delta = (b-c)/n.

    The restricted MLE s = p_b+p_c is the larger root of
    n*s**2 - (b+c+(b-c)*delta)*s + (b-c)*delta
      - (n-b-c)*delta**2 = 0, constrained to [abs(delta), 1].
    Score variance is (s-delta**2)/n. No continuity/variance correction.
    Reference: Tango (1998), Stat Med 17:891-908; CRAN PropCIs scoreci.mp
    uses the equivalent q21 nuisance parametrization (tested independently).
    """
    if n <= 0 or min(b, c) < 0 or b + c > n or not 0 < alpha < .5:
        raise ValueError((b, c, n, alpha))
    if b == n:
        return 1.0
    observed = (b - c) / n
    z2 = NormalDist().inv_cdf(1 - alpha) ** 2
    lo, hi = observed, 1.0
    for _ in range(80):
        delta = (lo + hi) / 2
        linear = b + c + (b - c) * delta
        constant = (b - c) * delta - (n - b - c) * delta * delta
        s = (linear + sqrt(max(0.0, linear * linear - 4 * n * constant))) / (2 * n)
        s = min(1.0, max(abs(delta), s))
        if n * (delta - observed) ** 2 <= z2 * (s - delta * delta):
            lo = delta
        else:
            hi = delta
    return (lo + hi) / 2


def net_gate(b: int, c: int, n: int, margin: float) -> NetGate:
    """The prespecified gate includes offsets but retains both discordant counts."""
    upper = tango_upper(b, c, n)
    return NetGate(b, c, n, (b - c) / n, NI_ALPHA, upper, margin, upper <= margin)


class ConfirmReport(TypedDict):
    status: str
    valid: bool
    primary: dict[str, int | float | bool]
    ni: dict[str, NetGate]
    descriptive_dev_style_ni: dict[str, dict[str, int | float | bool]]
    absolute: dict[str, dict[str, dict[str, int | float]]]
    all_gates_pass: bool
    interpretation: str


def score_confirm(plan: Plan100, data: ScoringInput) -> ConfirmReport:
    """Reuse all frozen population/witness checks; replace ONLY the NI decision."""
    if any(not c.question_id.startswith("s100-c3-") for c in plan.cases):
        raise ValueError(("Expected confirm-v3 namespace",))
    previous = score(plan, data)
    rows = {(a.question_id, a.condition_id): a for a in data.annotations}
    gates = {}
    for endpoint, old in previous["ni"].items():
        pairs = [(rows[c.question_id, data.arm_key["B"]],
                  rows[c.question_id, data.arm_key["A"]]) for c in plan.cases
                 if getattr(rows[c.question_id, data.arm_key["B"]], endpoint) is not None]
        gains = sum(bool(getattr(a, endpoint)) and not getattr(b, endpoint) for b, a in pairs)
        gates[endpoint] = net_gate(old["harms"], gains, old["total"], old["margin"])
    return {
        "status": STATUS, "valid": True, "primary": previous["primary"],
        "ni": gates, "descriptive_dev_style_ni": previous["ni"],
        "absolute": previous["absolute"],
        "all_gates_pass": bool(previous["primary"]["passed"]) and all(g.passed for g in gates.values()),
        "interpretation": "One fresh-family confirmation only; C diagnostic; no service adoption authorization",
    }


def main(plan_path: Path, annotation_path: Path, output: Path) -> None:
    """Serialize the frozen confirmation status, not the historical draft marker."""
    from prepare100 import reject_forbidden
    reject_forbidden(plan_path, annotation_path, output)
    result = score_confirm(Plan100.model_validate_json(plan_path.read_text()),
                           ScoringInput.model_validate_json(annotation_path.read_text()))
    serial = {**result, "ni": {name: asdict(gate) for name, gate in result["ni"].items()}}
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(serial, indent=2) + "\n")


if __name__ == "__main__":
    typer.run(main)
