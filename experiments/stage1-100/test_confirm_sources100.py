"""Default authoring bytes and namespace-only intervention for confirmation."""

from dataclasses import replace
from pathlib import Path

import pytest

from confirm_freshness100 import audit_freshness, population
from scoring_confirm100 import STATUS, score_confirm
from synthetic100 import SEED, author
from test_scoring100 import labelled


def test_dev_authoring_when_namespace_is_default_keeps_frozen_data_bytes():
    # Given: the five committed DEV source artifacts, not newly computed hashes.
    data = Path(__file__).parent / "dev-v3"
    # When: regenerate with the original seed and default namespace.
    authored = author(SEED)
    # Then: every source/oracle byte stays identical; manifest provenance is historical.
    for name, field in (("corpus.json", "sources"), ("plans.json", "plan"),
                        ("identity-ledger.json", "identities"), ("gold-context.json", "gold"),
                        ("evaluation-sidecar.json", "sidecar")):
        assert getattr(authored, field).model_dump_json(indent=2, by_alias=True) + "\n" == (data / name).read_text()


def test_confirmation_when_only_namespace_differs_keeps_random_choices():
    # Given: identical RNG seed; namespaces affect identities, never random draws.
    base = author(10009112027)
    # When: author in the disjoint namespace.
    confirm = author(10009112027, "s100-c3")
    # Then: persona attributes/questions (excluding IDs) are unchanged.
    for old, new in zip(base.sources.personas, confirm.sources.personas, strict=True):
        assert old.model_copy(update={"persona_id": new.persona_id}) == new
    assert [q.question for q in base.sources.questions] == [q.question for q in confirm.sources.questions]
    assert not {c.question_id for c in base.plan.cases} & {c.question_id for c in confirm.plan.cases}


def test_freshness_when_a_family_is_relabelled_cannot_hide_duplicate_facts():
    # Given: disjoint namespaces alone do not make source combinations fresh.
    old = population(Path(__file__).parent / "dev-v3")
    relabelled = replace(old, family_ids=frozenset({"new-family"}), question_ids=frozenset({"new-question"}))
    # When / Then: the unchanged joint-family signatures must still block.
    with pytest.raises(ValueError):
        audit_freshness(old, relabelled)


def test_confirmation_score_when_complete_labels_are_available_has_frozen_status(labelled):
    # Given: valid complete labels from the neighboring scorer fixture.
    plan, data = labelled
    rename = lambda q: q.replace("s100-v3-", "s100-c3-")
    plan = plan.model_copy(update={"cases": tuple(c.model_copy(update={
        "question_id": rename(c.question_id), "family_id": rename(c.family_id),
    }) for c in plan.cases)})
    data = data.model_copy(update={"annotations": tuple(a.model_copy(update={
        "question_id": rename(a.question_id),
    }) for a in data.annotations)})
    # When: use the confirmation entry point rather than the frozen historical scorer.
    result = score_confirm(plan, data)
    # Then: all paired units remain, descriptive CP is separate, and status is frozen.
    assert result["status"] == STATUS
    assert result["all_gates_pass"]
    assert [g.total for g in result["ni"].values()] == [1320, 720, 300, 300]
    assert result["descriptive_dev_style_ni"]["grounding"]["harms"] == 0
