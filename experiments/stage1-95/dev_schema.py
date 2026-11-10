"""Synthetic source inputs and a physically separate evaluation-only oracle."""

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from corpus import Corpus, Evidence, FrozenModel, Persona, Question, QuestionType, Record


class PublicQuestion(FrozenModel):
    question_id: str
    persona_id: str
    family_id: str
    split: Literal["dev"] = "dev"
    type: QuestionType
    question: str


class Sources(FrozenModel):
    scope: Literal["DEV_ONLY"] = "DEV_ONLY"
    synthetic_only: Literal[True] = True
    dataset_id: str
    personas: tuple[Persona, ...]
    records: tuple[Record, ...]
    questions: tuple[PublicQuestion, ...]

    def corpus(self) -> Corpus:
        """Adapt source-only inputs without consulting any evaluation sidecar."""
        return Corpus(
            schema_version=1, dataset_id=self.dataset_id, synthetic_only=True,
            provenance="Seeded independent synthetic DEV families; no imported corpora",
            gold_policy="C evidence supplied separately; no oracle in source questions",
            personas=self.personas, records=self.records,
            questions=tuple(Question(
                **q.model_dump(), gold_chunk_ids=(), support=(), expected_answer=None,
                note="", required_slots=(),
            ) for q in self.questions),
        )


class GoldContext(FrozenModel):
    """C intervention payload only; never passed to B/S/P query construction."""

    scope: Literal["DEV_ONLY"] = "DEV_ONLY"
    evidence: Mapping[str, tuple[Evidence, ...]]


class RequiredItem(FrozenModel):
    item_index: int
    question_span: str
    gold_support_ids: tuple[UUID, ...]
    slot_surfaces: tuple[str, ...]


class Conflict(FrozenModel):
    source_ids: tuple[UUID, UUID]
    resolution: Literal["different_time", "different_speaker", "latest_persona_correction"]
    resolved_support_ids: tuple[UUID, ...]
    rationale: str


class OracleCase(FrozenModel):
    question_id: str
    family_id: str
    self_honorific_probe: bool
    required_items: tuple[RequiredItem, ...]
    expected_omissions: tuple[str, ...]
    conflicts: tuple[Conflict, ...]
    style_only_surfaces: tuple[str, ...]
    style_leakage_probe: bool
    cohort: Literal["missing_second_event", "sufficient_but_omission_risk", "control"]
    cohort_policy: Literal["predeclared_challenge_not_observed_output"] = (
        "predeclared_challenge_not_observed_output"
    )


class EvaluationSidecar(FrozenModel):
    scope: Literal["DEV_ONLY_EVALUATION_ONLY"] = "DEV_ONLY_EVALUATION_ONLY"
    cases: tuple[OracleCase, ...]
