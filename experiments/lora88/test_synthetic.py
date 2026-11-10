"""Authored-source invariants; no prompt prose pinning or downloads."""

from collections import Counter

import pytest

from experiments.lora88.synthetic import (
    REGISTERS, answer, content_signature, metadata, scenarios,
)


def test_family_split_and_counts_when_authoring_fresh_dev() -> None:
    # Given
    cases = scenarios()
    # When
    train = [c for c in cases if c.split == "train"]
    validation = [c for c in cases if c.split == "validation"]
    # Then
    assert (len(train), len(validation)) == (300, 60)
    assert not {c.family for c in train} & {c.family for c in validation}
    assert not {c.event for c in train} & {c.event for c in validation}
    assert not {c.question for c in train} & {c.question for c in validation}
    assert sum(c.long for c in train) * 3 == 360
    assert sum(c.probe for c in validation) * 3 == 54
    assert Counter(c.kind for c in train[:30]) == {
        "single": 6, "multi": 6, "false_premise": 6, "ambiguity": 3,
        "unanswerable": 6, "ordinary_chat": 3,
    }


def test_rewrites_preserve_authored_content_when_register_changes() -> None:
    # Given
    cases = scenarios()
    # When
    signatures = [
        [content_signature(case, answer(case, register), register) for register in REGISTERS]
        for case in cases
    ]
    # Then
    assert all(len(set(group)) == 1 for group in signatures)
    assert all(all(slot in case.record for slot in case.slots) for case in cases)


def test_corrupt_rewrite_rejects_when_outside_authored_grammar() -> None:
    # Given
    case = scenarios()[0]
    # When / Then
    with pytest.raises(ValueError):
        content_signature(case, "unsupported-content", "해요체")


def test_validation_persona_and_register_denominators_when_expanded() -> None:
    # Given
    cases = [c for c in scenarios() if c.split == "validation"]
    # When
    labels = [metadata(c, register) for c in cases for register in REGISTERS]
    # Then
    assert sorted(Counter(r["persona_id"] for r in labels).values()) == [30] * 6
    assert sorted(Counter(r["register"] for r in labels).values()) == [60] * 3
    assert len({r["question_id"] for r in labels}) == 180
