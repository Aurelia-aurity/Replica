"""Synthetic evidence-state and exact inference regressions."""

from math import isclose
from uuid import UUID

import pytest

from corpus import Evidence
from error_split import EvidenceSpec, attribute, evidence_state
from exact import clopper_pearson, harm_upper, mcnemar


@pytest.mark.parametrize("count,total,bounds", [
    (0, 8, (0.0, 0.3694166475528193)),
    (8, 8, (0.6305833524471807, 1.0)),
    (1, 2, (0.01257911709342505, 0.9874208829065749)),
])
def test_exact_interval_when_known_binomial_case(count, total, bounds):
    # Given / When: independently tabulated exact binomial endpoints.
    result = clopper_pearson(count, total)
    # Then: boundary and interior cases match, without a normal approximation.
    assert result.lower == pytest.approx(bounds[0])
    assert result.upper == pytest.approx(bounds[1])


def test_empty_interval_when_no_eligible_questions():
    # Given / When: an empty stratum.
    result = clopper_pearson(0, 0)
    # Then: absence of evidence is not a zero-width confidence interval.
    assert (result.lower, result.upper) == (None, None)


def test_harm_bound_when_no_losses_in_small_subset():
    # Given / When: 0/60 paired harms with the frozen multiple-test alpha.
    upper = harm_upper(0, 60)
    # Then: the predeclared 5 percentage point margin is not established.
    assert upper is not None and isclose(upper, 1 - 0.00625**(1 / 60))
    assert upper > 0.05


@pytest.mark.parametrize("gains,losses,p", [(5, 0, 1 / 32), (0, 5, 1), (0, 0, 1)])
def test_sign_test_when_discordant_pairs(gains, losses, p):
    # Given / When / Then: exact sign-test probabilities for known pairs.
    assert mcnemar(gains, losses) == p


@pytest.mark.parametrize("kind,state", [
    ("multi", "sufficient"), ("ambiguity", "sufficient"),
    ("false_premise", "contradictory"), ("unanswerable", "insufficient"),
    ("ordinary_chat", "not_applicable"),
])
def test_evidence_state_when_complete_support(kind, state):
    # Given: complete gold identity with an authored surface, not canonical text.
    uid = UUID(int=1)
    spec = EvidenceSpec(kind, (str(uid),), ("canonical",), {"canonical": ("surface",)})
    evidence = (Evidence(chunk_id=uid, record_id=uid, text_version_id=uid,
                         content="surface", speaker_role="persona"),)
    # When / Then: ambiguity is not mislabeled as conflicting evidence.
    assert evidence_state(spec, evidence) == state


def test_evidence_state_when_gold_id_present_but_second_fact_missing():
    # Given: a multi-event answer cannot be supported by gold identity alone.
    uid = UUID(int=1)
    spec = EvidenceSpec("multi", (str(uid),), ("first", "second"), {})
    evidence = (Evidence(chunk_id=uid, record_id=uid, text_version_id=uid,
                         content="first", speaker_role="persona"),)
    # When / Then: partial support stays insufficient.
    assert evidence_state(spec, evidence) == "insufficient"


def test_attribution_when_gold_context_introduces_an_error():
    # Given: all four paired cells, including C-only regression.
    pairs = ((True, False), (True, True), (False, True), (False, False))
    # When: attribute the contrast without discarding negative effects.
    result = attribute(pairs)
    # Then: zero net gap does not mean no retrieval-associated errors.
    assert (result.b_only, result.persistent, result.c_only, result.neither) == (1, 1, 1, 1)
    assert result.gap == 0
