"""Tiny synthetic source fixtures; never load historical or sealed corpora."""

from uuid import UUID

import pytest

from corpus import Corpus, Persona, Question, Record, Turn


@pytest.fixture
def synthetic_corpus():
    persona = Persona(persona_id="p", name="가람", display_name="가람",
                      provider_address=None, age=30, relationship="친구", register="반말")
    records = tuple(Record(
        record_id=UUID(int=index), text_version_id=UUID(int=100 + index),
        alias=f"record-{index}", persona_id="p" if index < 3 else "foreign",
        family_id=f"event-{index}", split="dev", kind="chat", date=f"2026-01-0{index}",
        speaker="", text="", turns=(
            Turn(speaker="가람" if index < 3 else "다른 인물",
                 text=f"나는 사건 {index}에서 물건 {index}을 썼어."),
            Turn(speaker="나", text=f"제공자 {index}의 말이야."),
        ),
    ) for index in range(1, 4))
    question = Question(
        question_id="q", persona_id="p", family_id="query", split="dev", type="multi",
        question="첫 사건은 어디였어; 다음 사건은 무엇을 썼어?",
        gold_chunk_ids=(), support=(), expected_answer=None, note="", required_slots=(),
    )
    return Corpus(
        schema_version=1, dataset_id="stage95-unit", synthetic_only=True,
        provenance="synthetic unit fixture", gold_policy="none",
        personas=(persona, persona.model_copy(update={"persona_id": "foreign", "name": "다른 인물"})),
        records=records, questions=(question,),
    )
