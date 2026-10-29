"""Frozen synthetic corpus boundary, deterministic single-speaker chunks and audit."""

from collections import Counter
from pathlib import Path
import re
from typing import Final, Literal, assert_never
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field
import typer

ROOT: Final = Path(__file__).resolve().parent
CHUNKER_VERSION: Final = "paragraph-same-speaker-v1"
Split = Literal["dev", "test"]
Role = Literal["provider", "persona", "other"]
QuestionType = Literal[
    "single", "multi", "unanswerable", "false_premise", "ambiguity", "ordinary_chat"
]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Persona(FrozenModel):
    persona_id: str
    name: str
    display_name: str
    provider_address: str | None
    age: int
    relationship: str
    speech_register: str = Field(alias="register")


class Turn(FrozenModel):
    speaker: str = Field(min_length=1)
    text: str = Field(min_length=1)


class Record(FrozenModel):
    record_id: UUID
    text_version_id: UUID
    alias: str
    persona_id: str
    family_id: str
    split: Split
    kind: Literal["diary", "chat"]
    date: str
    speaker: str
    text: str
    turns: tuple[Turn, ...]


class Support(FrozenModel):
    chunk_id: UUID
    quote: str = Field(min_length=1)
    answer: str = Field(min_length=1)


class Question(FrozenModel):
    question_id: str
    persona_id: str
    family_id: str
    split: Split
    type: QuestionType
    question: str = Field(min_length=1)
    gold_chunk_ids: tuple[UUID, ...]
    support: tuple[Support, ...]
    expected_answer: str | None
    note: str
    required_slots: tuple[str, ...]
    slot_surface_forms: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class Corpus(FrozenModel):
    schema_version: Literal[1]
    dataset_id: str
    synthetic_only: Literal[True]
    provenance: str
    gold_policy: str
    personas: tuple[Persona, ...]
    records: tuple[Record, ...]
    questions: tuple[Question, ...]


class Evidence(FrozenModel):
    """Only the five fields accepted by the draft evidence contract."""

    chunk_id: UUID
    record_id: UUID
    text_version_id: UUID
    content: str
    speaker_role: Role


class Chunk(Evidence):
    persona_id: str
    family_id: str
    split: Split
    speaker: str
    date: str
    ordinal: int
    record_alias: str
    source_turns: tuple[int, ...]

    def evidence(self) -> Evidence:
        return Evidence.model_validate(
            self.model_dump(include=set(Evidence.model_fields))
        )

    def embedding_text(self) -> str:
        """No gold, split, family, question, or expected answer enters the encoder."""
        return f"{self.date} | {self.speaker} ({self.speaker_role})\n{self.content}"


def load_corpus(path: Path = ROOT / "corpus.json") -> Corpus:
    return Corpus.model_validate_json(path.read_text(encoding="utf-8"))


def speaker_role(speaker: str, persona: Persona) -> Role:
    if speaker == "나":
        return "provider"
    if speaker == persona.name:
        return "persona"
    return "other"


def chunk_record(record: Record, persona: Persona) -> tuple[Chunk, ...]:
    """Diaries split at blank paragraphs; chat groups at most two same-speaker turns.

    Never cross record/version/date/speaker boundaries. No overlap or truncation.
    Fixture paragraphs are deliberately short; encoder budgets are checked at run time.
    """
    groups: list[tuple[str, str, tuple[int, ...]]] = []
    match record.kind:
        case "diary":
            groups.extend(
                (record.speaker, paragraph.strip(), ())
                for paragraph in record.text.split("\n\n")
                if paragraph.strip()
            )
        case "chat":
            for index, turn in enumerate(record.turns):
                if (
                    groups
                    and groups[-1][0] == turn.speaker
                    and len(groups[-1][2]) < 2
                ):
                    speaker, content, indices = groups[-1]
                    groups[-1] = (
                        speaker, f"{content}\n{turn.text}", (*indices, index)
                    )
                else:
                    groups.append((turn.speaker, turn.text, (index,)))
        case unreachable:
            assert_never(unreachable)
    return tuple(
        Chunk(
            chunk_id=uuid5(NAMESPACE_URL, f"{record.text_version_id}:{ordinal}"),
            record_id=record.record_id,
            text_version_id=record.text_version_id,
            content=content,
            speaker_role=speaker_role(speaker, persona),
            persona_id=record.persona_id,
            family_id=record.family_id,
            split=record.split,
            speaker=speaker,
            date=record.date,
            ordinal=ordinal,
            record_alias=record.alias,
            source_turns=indices,
        )
        for ordinal, (speaker, content, indices) in enumerate(groups, start=1)
    )


def chunk_corpus(corpus: Corpus) -> tuple[Chunk, ...]:
    personas = {persona.persona_id: persona for persona in corpus.personas}
    return tuple(
        chunk
        for record in corpus.records
        for chunk in chunk_record(record, personas[record.persona_id])
    )


def particle_errors(text: str, nouns: set[str]) -> list[str]:
    """Flag obvious particle mismatches on known names/nouns, not all Korean grammar.

    Only inspect a particle followed by whitespace/punctuation/end. This avoids
    treating nickname suffixes (가람이는), copulas (밤이인데), or noun substrings as
    standalone case particles. Authored prose is never repaired automatically.
    """
    pairs = {"와": "과", "를": "을", "는": "은", "가": "이"}
    reverse = {closed: opened for opened, closed in pairs.items()}
    errors: list[str] = []
    for noun in sorted(nouns):
        if not noun or not ("가" <= noun[-1] <= "힣"):
            continue
        has_final = (ord(noun[-1]) - ord("가")) % 28 != 0
        pattern = re.compile(
            rf"(?<![가-힣]){re.escape(noun)}(?P<particle>[와과을를은는이가])(?=$|[\s,.!?。])"
        )
        for match in pattern.finditer(text):
            actual = match.group("particle")
            expected = pairs.get(actual, actual) if has_final else reverse.get(actual, actual)
            if actual != expected:
                errors.append(f"{noun}{actual} -> {noun}{expected}")
    return errors


class CorpusIntegrityError(ValueError):
    def __init__(self, failures: list[str]) -> None:
        self.failures = failures
        super().__init__("\n".join(failures))


def validate(corpus: Corpus) -> tuple[Chunk, ...]:
    """Reject invalid IDs, family leakage, unsupported gold and role misattribution.

    Exact quotes and slot surfaces prove correspondence, not semantic entailment.
    Semantic gold and absence rationales were written/reviewed before retrieval.
    """
    failures: list[str] = []
    ids = {p.persona_id for p in corpus.personas}
    for label, values in (
        ("persona", [p.persona_id for p in corpus.personas]),
        ("record", [str(r.record_id) for r in corpus.records]),
        ("version", [str(r.text_version_id) for r in corpus.records]),
        ("question", [q.question_id for q in corpus.questions]),
    ):
        if len(values) != len(set(values)):
            failures.append(f"duplicate {label} id")
    for record in corpus.records:
        if record.persona_id not in ids:
            failures.append(f"unknown persona: {record.alias}")
        match record.kind:
            case "diary":
                if not record.text.strip() or record.turns:
                    failures.append(f"invalid diary: {record.alias}")
                target = next(
                    (p.name for p in corpus.personas if p.persona_id == record.persona_id),
                    None,
                )
                if record.speaker != target:
                    failures.append(f"diary speaker: {record.alias}")
            case "chat":
                if record.text or not record.turns:
                    failures.append(f"invalid chat: {record.alias}")
            case unreachable:
                assert_never(unreachable)
    families: dict[str, set[Split]] = {}
    family_personas: dict[str, set[str]] = {}
    known_nouns = {p.name for p in corpus.personas} | {
        t.speaker for r in corpus.records for t in r.turns
    } | {"책", "우산", "화분", "수첩", "편지", "공책", "보람", "노을", "늘봄"}
    for record in corpus.records:
        families.setdefault(record.family_id, set()).add(record.split)
        family_personas.setdefault(record.family_id, set()).add(record.persona_id)
        texts = [record.text, *(t.text for t in record.turns)]
        for text in texts:
            failures.extend(
                f"particle: {record.alias}: {error}"
                for error in particle_errors(text, known_nouns)
            )
    for question in corpus.questions:
        families.setdefault(question.family_id, set()).add(question.split)
        failures.extend(
            f"particle: {question.question_id}: {error}"
            for error in particle_errors(question.question, known_nouns)
        )
    for family, splits in families.items():
        if len(splits) != 1:
            failures.append(f"family split leakage: {family}")
    for family, owners in family_personas.items():
        if len(owners) != 1:
            failures.append(f"family reused across personas: {family}")
    if failures:
        raise CorpusIntegrityError(failures)
    chunks = chunk_corpus(corpus)
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    if len(by_id) != len(chunks):
        failures.append("duplicate chunk id")
    for question in corpus.questions:
        label = question.question_id
        if question.persona_id not in ids:
            failures.append(f"unknown question persona: {label}")
        gold = question.gold_chunk_ids
        if len(gold) != len(set(gold)):
            failures.append(f"duplicate gold: {label}")
        if tuple(s.chunk_id for s in question.support) != gold:
            failures.append(f"support/gold mismatch: {label}")
        needs_evidence = question.type not in {"unanswerable", "ordinary_chat"}
        if bool(gold) != needs_evidence:
            failures.append(f"answerability/gold mismatch: {label}")
        if needs_evidence != bool(question.expected_answer):
            failures.append(f"answerability/answer mismatch: {label}")
        if needs_evidence != bool(question.required_slots):
            failures.append(f"answerability/slots mismatch: {label}")
        if len(question.required_slots) != len(set(question.required_slots)):
            failures.append(f"duplicate required slot: {label}")
        if set(question.slot_surface_forms) - set(question.required_slots):
            failures.append(f"unknown slot surface key: {label}")
        support_text = "\n".join(s.quote for s in question.support)
        for slot in question.required_slots:
            surfaces = (slot, *question.slot_surface_forms.get(slot, ()))
            if any(not surface.strip() for surface in surfaces):
                failures.append(f"blank slot/surface: {label}")
            elif not any(surface in support_text for surface in surfaces):
                failures.append(f"required slot absent from gold support: {label}: {slot}")
        if any(not forms for forms in question.slot_surface_forms.values()):
            failures.append(f"empty slot surfaces: {label}")
        if question.type in {"multi", "ambiguity"} and len(gold) < 2:
            failures.append(f"multi requires two chunks: {label}")
        if question.type == "single" and len(gold) != 1:
            failures.append(f"single requires one chunk: {label}")
        if not needs_evidence and not question.note:
            failures.append(f"missing absence rationale: {label}")
        for support in question.support:
            chunk = by_id.get(support.chunk_id)
            if chunk is None:
                failures.append(f"missing gold chunk: {label}")
                continue
            if (
                chunk.persona_id != question.persona_id
                or chunk.family_id != question.family_id
                or chunk.split != question.split
            ):
                failures.append(f"gold scope/family/split: {label}")
            if chunk.speaker_role != "persona":
                failures.append(f"non-persona factual gold: {label}")
            if support.quote not in chunk.content or support.answer not in support.quote:
                failures.append(f"gold answer/quote absent: {label}")
    if failures:
        raise CorpusIntegrityError(failures)
    return chunks


def main() -> None:
    corpus = load_corpus()
    chunks = validate(corpus)
    print(
        f"VALID: personas={len(corpus.personas)} records={len(corpus.records)} "
        f"chunks={len(chunks)} questions={len(corpus.questions)}"
    )
    for split in ("dev", "test"):
        questions = [q for q in corpus.questions if q.split == split]
        counts = dict(sorted(Counter(q.type for q in questions).items()))
        print(
            f"{split}: records={sum(r.split == split for r in corpus.records)} "
            f"chunks={sum(c.split == split for c in chunks)} "
            f"questions={len(questions)} types={counts}"
        )
    print("PASS: unique IDs; disjoint families; valid scoped gold; exact support quotes")
    print("PASS: unanswerable/ordinary gold empty; all factual gold persona-spoken")
    print("PASS: per-persona event families; known-name/noun particle checks")
    print("PASS: factual required slots covered by exact gold support surfaces")


if __name__ == "__main__":
    typer.run(main)
