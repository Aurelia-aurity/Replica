"""Internal synthetic DEV v3 schemas; no service contract additions."""

from typing import Final, Literal
from uuid import UUID

from pydantic import Field

from corpus import Chunk, FrozenModel, QuestionType, Role, validate
from dev_schema import Sources
from generate import Message

COUNTS: Final = {
    "single": 360, "multi": 120, "ambiguity": 120,
    "false_premise": 120, "unanswerable": 300, "ordinary_chat": 300,
}
SUBTYPES: Final = {
    "single": {"direct": 180, "reported_context": 180},
    "multi": {"two_events": 120},
    "ambiguity": {"dated_events": 120},
    "false_premise": {"gentle": 60, "pressure": 60},
    "unanswerable": {"provider_only": 150, "missing": 150},
    "ordinary_chat": {"plain": 150, "fact_like": 150},
}
REGISTERS: Final = ("반말", "해요체", "하게체")
Arm = Literal["B", "A", "C"]


class Case100(FrozenModel):
    question_id: str
    family_id: str
    type: QuestionType
    subtype: str
    speech_register: Literal["반말", "해요체", "하게체"] = Field(alias="register")


class Plan100(FrozenModel):
    """Membership only: no gold, slots, supports, or answer-conditioned queries."""

    split: Literal["dev"] = "dev"
    cases: tuple[Case100, ...]


class Identity100(FrozenModel):
    family_id: str
    persona_name: str
    provider_name: str
    provider_source_alias: Literal["나"] = "나"


class Identities100(FrozenModel):
    scope: Literal["SOURCE_IDENTITY_ONLY"] = "SOURCE_IDENTITY_ONLY"
    cases: tuple[Identity100, ...]


class Fact100(FrozenModel):
    source_id: UUID
    utterance_owner: Role
    fact_subject: Role
    event_id: str
    event_date: str
    source_date: str
    place: str
    item: str
    activity: str
    quote: str


class Oracle100(FrozenModel):
    question_id: str
    family_id: str
    facts: tuple[Fact100, ...]
    required_support_ids: tuple[UUID, ...]
    required_surfaces: tuple[str, ...]
    distractor_ids: tuple[UUID, ...]
    unknowns: tuple[str, ...]
    correction: str | None


class Sidecar100(FrozenModel):
    scope: Literal["DEV_ONLY_EVALUATION_ONLY"] = "DEV_ONLY_EVALUATION_ONLY"
    cases: tuple[Oracle100, ...]


class Row100(FrozenModel):
    id: str
    question_id: str
    persona_id: str
    arm: Arm
    messages: tuple[Message, Message]
    messages_sha256: str
    evidence_ids: tuple[UUID, ...]


class Pack100(FrozenModel):
    scope: Literal["DEV_ONLY"] = "DEV_ONLY"
    status: Literal["DRAFT_NOT_FROZEN"] = "DRAFT_NOT_FROZEN"
    source_sha256: dict[str, str]
    code_sha256: dict[str, str]
    tokenizer_sha256: str
    tokenizer_config_sha256: str
    rows: tuple[Row100, ...]


def check_design(sources: Sources, plan: Plan100) -> None:
    """Validate fixed predeclared units, membership, and register allocation."""
    from collections import Counter

    questions = {q.question_id: q for q in sources.questions}
    if len(questions) != 1320 or len(sources.personas) != 1320:
        raise ValueError("Requires 1320 independent synthetic DEV families")
    if len({q.family_id for q in questions.values()}) != 1320:
        raise ValueError("Repeated family")
    if len({q.persona_id for q in questions.values()}) != 1320:
        raise ValueError("Repeated persona")
    if len(plan.cases) != 1320 or {c.question_id for c in plan.cases} != set(questions):
        raise ValueError("Plan must cover every question exactly once")
    personas = {p.persona_id: p for p in sources.personas}
    for case in plan.cases:
        q = questions[case.question_id]
        if (case.family_id, case.type, case.speech_register) != (
            q.family_id, q.type, personas[q.persona_id].speech_register,
        ):
            raise ValueError("Plan/source membership mismatch")
    expected = {
        (kind, sub, register): n // 3
        for kind, subtypes in SUBTYPES.items()
        for sub, n in subtypes.items() for register in REGISTERS
    }
    if Counter((c.type, c.subtype, c.speech_register) for c in plan.cases) != expected:
        raise ValueError("Fixed subtype/register allocation changed")


def validate_sources(sources: Sources) -> tuple[Chunk, ...]:
    """Reuse source validation per independent family, avoiding quadratic regex banks."""
    from collections import defaultdict

    corpus = sources.corpus()
    records = defaultdict(list)
    for record in corpus.records:
        records[record.persona_id].append(record)
    if set(records) != {p.persona_id for p in sources.personas}:
        raise ValueError("Source persona ownership mismatch")
    chunks = []
    for persona in corpus.personas:
        local = corpus.model_copy(update={
            "personas": (persona,), "records": tuple(records[persona.persona_id]), "questions": (),
        })
        if any(r.family_id != persona.persona_id for r in local.records):
            raise ValueError("Foreign family record")
        chunks.extend(validate(local))
    for ids in ([r.record_id for r in corpus.records], [r.text_version_id for r in corpus.records],
                [c.chunk_id for c in chunks]):
        if len(set(ids)) != len(ids):
            raise ValueError("Repeated source ID")
    # Return canonical corpus order, not grouped validation order.
    from corpus import chunk_corpus
    return chunk_corpus(corpus)
