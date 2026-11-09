"""Pure paired-selection/majority regressions, no live provider."""

import pytest

from adjudication100 import discordant, majority
from scoring100 import Annotation100, ClaimWitness
from uuid import UUID


def label(condition="x", **changes):
    return Annotation100(
        question_id="q1", condition_id=condition, speaker_error=False, temporal_error=False,
        grounding=True, core_correct=True, safe_abstention=None, chat_success=None,
        rationale="Resolved synthetic no-error claim.", witnesses=(),
    ).model_copy(update=changes)


@pytest.mark.parametrize("field", ("grounding", "core_correct", "safe_abstention", "chat_success"))
def test_trigger_when_any_gated_field_disagrees(field):
    # Given: paired responses differ on one gated field.
    b = label(**{field: True})
    a = label("y", **{field: False})
    # When: select with either ordering.
    selected = (discordant(b, a, primary=False), discordant(a, b, primary=False))
    # Then: both response sides are treated symmetrically.
    assert selected == (True, True)


@pytest.mark.parametrize(("field", "primary", "expected"), [
    ("speaker_error", True, True), ("speaker_error", False, False),
    ("temporal_error", True, False),
])
def test_trigger_when_diagnostic_or_primary_field_disagrees(field, primary, expected):
    # Given: NI fields are concordant.
    b, a = label(), label("y", **{field: True})
    # When / Then: only the primary speaker endpoint adds a trigger.
    assert discordant(b, a, primary=primary) == expected


def test_trigger_when_pair_is_concordant():
    # Given / When / Then: same labels with opaque distinct conditions need no repeat.
    assert not discordant(label(), label("y"), primary=True)


def test_majority_when_fields_have_different_winning_runs():
    # Given: no single run represents the majority vector.
    first = label(grounding=False)
    second = label(core_correct=False)
    third = label()
    # When: count each response field independently.
    resolved = majority((first, second, third))
    # Then: two true votes per field and null applicability retained.
    assert resolved.grounding and resolved.core_correct
    assert resolved.safe_abstention is None and resolved.chat_success is None


def test_majority_when_minority_witness_should_not_survive():
    # Given: only run one claims a transfer.
    witness = ClaimWitness(
        response_span="가방", asserted_subject="persona", source_subject="provider",
        utterance_owner="provider", source_ids=(UUID(int=1),),
        asserted_event_time="unspecified", source_event_time="2024-01-01",
        speaker_error=True, temporal_error=False,
    )
    first = label(speaker_error=True, witnesses=(witness,))
    # When: two later votes disagree.
    resolved = majority((first, label(), label()))
    # Then: the false majority does not carry a positive minority witness.
    assert not resolved.speaker_error and not resolved.witnesses


def test_majority_when_positive_votes_have_witnesses():
    # Given: two runs assert a source transfer with exact witnesses.
    witness = ClaimWitness(
        response_span="가방", asserted_subject="persona", source_subject="provider",
        utterance_owner="provider", source_ids=(UUID(int=1),),
        asserted_event_time="unspecified", source_event_time="2024-01-01",
        speaker_error=True, temporal_error=False,
    )
    positive = label(speaker_error=True, witnesses=(witness,))
    # When / Then: retain a matching witness for the positive majority.
    assert majority((positive, label(), positive)).witnesses == (witness,)


@pytest.mark.parametrize("defect", ("identity", "applicability", "count"))
def test_majority_when_independent_votes_are_not_comparable(defect):
    # Given: changed identity/applicability or missing third run.
    votes = (label(), label(), label())
    if defect == "identity":
        votes = (label(), label(), label("other"))
    elif defect == "applicability":
        votes = (label(), label(), label(core_correct=None))
    else:
        votes = (label(), label())
    # When / Then: never manufacture a majority from incomparable labels.
    with pytest.raises(ValueError):
        majority(votes)
