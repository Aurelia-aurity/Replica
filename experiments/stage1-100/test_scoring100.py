"""Hand-calculated exact math and full fixed-population annotation validation."""

from math import sqrt

import pytest

from scoring100 import (
    Annotation100, ClaimWitness, JudgeReceipt, ScoringInput,
    binomial_cdf, cp_upper, primary, score,
)
from synthetic100 import author


@pytest.mark.parametrize(("gains", "losses", "expected"), [
    (0, 0, 1), (3, 0, 1 / 8), (3, 1, 5 / 16), (5, 0, 1 / 32), (0, 3, 1),
])
def test_mcnemar_when_discordance_is_hand_countable(gains, losses, expected):
    # Given: the conditional binomial has <=5 discordant pairs.
    # When: test on a small population for hand arithmetic.
    result = primary(gains, losses, 10)
    # Then: exact tail probabilities, not normal approximations.
    assert result.p_exact == expected


def test_primary_when_effect_threshold_binds():
    # Given / When: 17 and 18 improvements, no losses, on fixed 360.
    below, at = primary(17, 0), primary(18, 0)
    # Then: significance alone cannot pass the 5pp gate.
    assert below.p_exact < .05 and not below.passed
    assert at.passed and at.error_reduction == .05


@pytest.mark.parametrize(("k", "n", "alpha", "expected"), [
    (0, 1, .05, .95), (1, 1, .05, 1),
    (0, 2, .05, 1 - sqrt(.05)), (1, 2, .05, sqrt(.95)),
    (0, 300, .0125, 1 - .0125 ** (1 / 300)),
])
def test_cp_when_binomial_polynomial_has_closed_form(k, n, alpha, expected):
    # Given / When: invert a constant/linear/quadratic binomial CDF.
    upper = cp_upper(k, n, alpha)
    # Then: match its analytic root, including both CP boundary cases.
    assert upper == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize(("n", "maximum", "margin"), [(1320, 25, .03), (720, 11, .03), (300, 6, .05)])
def test_ni_when_passing_harm_boundary_is_crossed(n, maximum, margin):
    # Given: fixed design denominators and Bonferroni per-gate alpha.
    # When: invert CP for the largest passing and first failing harm counts.
    at, above = cp_upper(maximum, n), cp_upper(maximum + 1, n)
    # Then: conservative paired harm, not a net success difference.
    assert at <= margin < above


def test_binomial_when_large_n_would_overflow_old_formula():
    # Given: CDF(0;1320,.01) = .99**1320 exactly.
    # When: use the large-population stable implementation.
    result = binomial_cdf(0, 1320, .01)
    # Then: no float conversion of enormous integer binomial coefficients.
    assert result == pytest.approx(.99 ** 1320, rel=1e-12)


@pytest.fixture(scope="module")
def labelled():
    authored = author()
    errors = {c.question_id for c in authored.plan.cases if c.type == "single"}
    errors = set(sorted(errors)[:18])
    oracles = {o.question_id: o for o in authored.sidecar.cases}
    annotations = []
    for c in authored.plan.cases:
        fact = next(f for f in oracles[c.question_id].facts if f.fact_subject == "provider")
        for condition in ("x", "y", "z"):
            error = condition == "x" and c.question_id in errors
            witnesses = (ClaimWitness(
                response_span="unit synthetic assertion", asserted_subject="persona",
                source_subject="provider", utterance_owner="provider", source_ids=(fact.source_id,),
                asserted_event_time=fact.event_date, source_event_time=fact.event_date,
                speaker_error=True, temporal_error=False,
            ),) if error else ()
            annotations.append(Annotation100(
                question_id=c.question_id, condition_id=condition,
                speaker_error=error, temporal_error=False, grounding=not error,
                core_correct=not error if c.type in ("single", "multi", "ambiguity", "false_premise") else None,
                safe_abstention=True if c.type == "unanswerable" else None,
                chat_success=True if c.type == "ordinary_chat" else None,
                rationale="Synthetic unit-test label; not a judge result", witnesses=witnesses,
            ))
    data = ScoringInput(
        scope="DEV_ONLY_EVALUATION_ONLY", arm_key={"B": "x", "A": "y", "C": "z"},
        judge_receipt=JudgeReceipt(
            grounding_route="hybrid_jev_terra", style_route="terra",
            route_config_sha256="0" * 64, audits_complete=True,
            adjudications_complete=True, unresolved_cases=0,
        ), annotations=tuple(annotations),
    )
    return authored.plan, data


def test_full_scoring_when_all_units_are_resolved(labelled):
    # Given: 18 synthetic B-only errors, zero harms; C remains diagnostic.
    plan, data = labelled
    # When: score the complete fixed population.
    result = score(plan, data)
    # Then: denominators and conjunction are correct, not selected from B successes.
    assert result["primary"]["gains"] == 18
    assert result["primary"]["total"] == 360
    assert result["all_gates_pass"]
    assert [g["total"] for g in result["ni"].values()] == [1320, 720, 300, 300]
    assert all(g["alpha"] == .0125 for g in result["ni"].values())


@pytest.mark.parametrize("defect", ("missing", "duplicate", "inapplicable", "witness"))
def test_scoring_when_annotations_cannot_support_a_verdict(labelled, defect):
    # Given: one defect in otherwise complete synthetic labels.
    plan, data = labelled
    rows = list(data.annotations)
    match defect:
        case "missing":
            rows.pop()
        case "duplicate":
            rows[-1] = rows[0]
        case "inapplicable":
            rows[-1] = rows[-1].model_copy(update={"core_correct": None, "chat_success": None, "safe_abstention": None})
        case "witness":
            i = next(i for i, a in enumerate(rows) if a.speaker_error)
            rows[i] = rows[i].model_copy(update={"witnesses": ()})
    # When / Then: unresolved/missing cases cannot be dropped or treated as no error.
    with pytest.raises(ValueError):
        score(plan, data.model_copy(update={"annotations": tuple(rows)}))
