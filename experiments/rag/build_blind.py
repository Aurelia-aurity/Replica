"""Prepare blinded hybrid-judge inputs locally; never import or invoke the judge."""

from hashlib import sha256
import json
from pathlib import Path
import random
from typing import Final, Literal, assert_never

import typer

from corpus import ROOT, Corpus, FrozenModel, Persona, Question, validate, load_corpus
from generate import Generation, InputError, default_schedule, load_generations

SEED: Final = 59
Profile = Literal["calm", "elder", "unknown"]


class Quote(FrozenModel):
    id: str
    speaker: str
    quote: str


class Gold(FrozenModel):
    facts: tuple[str, ...]
    evidence: tuple[Quote, ...]
    criterion: str


class Oracle(FrozenModel):
    gold: Gold
    unacceptable_answers: tuple[str, ...] = ()


class BlindItem(FrozenModel):
    bid: str
    input: str
    record: str
    question: str
    case_type: str
    persona_speaker: str
    oracle: Oracle
    response: str
    status: Literal["success"]
    source_response_sha256: str


class KeyItem(FrozenModel):
    question_id: str
    condition: str
    persona_id: str
    split: str
    type: str
    evidence_ids: tuple[str, ...]
    source_response_sha256: str


def judge_profile(persona: Persona) -> Profile:
    """Unknown is intentional: the existing judge cannot evaluate hageche."""
    match persona.speech_register:
        case "반말" | "다정한 반말":
            return "calm"
        case "해요체" | "존댓말":
            return "elder"
        case "하게체":
            return "unknown"
        case _:
            raise InputError(f"Unsupported corpus register: {persona.speech_register}")


def judge_case_type(question: Question) -> str:
    match question.type:
        case "single":
            return "answerable_single"
        case "multi" | "ambiguity":
            return "answerable_cross_session"
        case "unanswerable":
            return "not_in_record"
        case "false_premise":
            return "false_premise"
        case "ordinary_chat":
            return "ordinary_chat"
        case unreachable:
            assert_never(unreachable)


def assemble(
    rows: tuple[Generation, ...], corpus: Corpus, seed: int = SEED,
) -> tuple[list[BlindItem], dict[str, KeyItem], dict[str, Profile]]:
    """Use oracle-side relevant records, independent of the evidence each arm saw."""
    chunks = validate(corpus)
    by_chunk = {chunk.chunk_id: chunk for chunk in chunks}
    questions = {q.question_id: q for q in corpus.questions}
    personas = {p.persona_id: p for p in corpus.personas}
    shuffled = sorted(rows, key=lambda row: (row.condition, row.question_id))
    random.Random(seed).shuffle(shuffled)
    blind: list[BlindItem] = []
    key: dict[str, KeyItem] = {}
    profiles: dict[str, Profile] = {}
    for index, row in enumerate(shuffled, 1):
        if row.status != "success" or row.text is None or not row.text.strip():
            raise InputError(f"Cannot blind unsuccessful generation: {row.question_id}")
        question = questions[row.question_id]
        persona = personas[question.persona_id]
        # A family includes the revisits/counterexamples needed for false premises
        # and ambiguity. Include complete gold-source records across families too.
        gold_records = {by_chunk[cid].record_id for cid in question.gold_chunk_ids}
        relevant = [
            chunk for chunk in chunks
            if chunk.persona_id == question.persona_id
            and (chunk.family_id == question.family_id or chunk.record_id in gold_records)
        ]
        record = "\n".join(
            f"[{chunk.chunk_id}] {chunk.date} speaker={chunk.speaker} "
            f"speaker_role={chunk.speaker_role}\n{chunk.content}"
            for chunk in relevant
        )
        facts = list(dict.fromkeys(s.answer for s in question.support))
        if question.expected_answer and question.expected_answer not in facts:
            facts.append(question.expected_answer)
        bid = f"B{index:04d}"
        digest = sha256(row.text.encode("utf-8")).hexdigest()
        blind.append(BlindItem(
            bid=bid,
            input=question.question,
            record=record,
            question=question.question,
            case_type=judge_case_type(question),
            persona_speaker=persona.name,
            oracle=Oracle(gold=Gold(
                facts=tuple(facts),
                evidence=tuple(
                    Quote(id=str(s.chunk_id), speaker=by_chunk[s.chunk_id].speaker, quote=s.quote)
                    for s in question.support
                ),
                criterion=question.note,
            )),
            response=row.text, status="success", source_response_sha256=digest,
        ))
        key[bid] = KeyItem(
            question_id=row.question_id, condition=row.condition,
            persona_id=row.persona_id, split=row.split, type=row.type,
            evidence_ids=tuple(str(cid) for cid in row.evidence_ids),
            source_response_sha256=digest,
        )
        profiles[bid] = judge_profile(persona)
    return blind, key, profiles


def main(
    generations: Path,
    output: Path = ROOT / ".cache/blind-001",
) -> None:
    """Require a complete 240-response schedule and emit no condition labels to judges."""
    corpus = load_corpus()
    rows = load_generations(generations, default_schedule())
    blind, key, profiles = assemble(rows, corpus)
    output.mkdir(parents=True, exist_ok=False)
    artifacts = {
        "blind-items.json": [item.model_dump(mode="json") for item in blind],
        "blind-key-DO-NOT-OPEN.json": {bid: item.model_dump(mode="json") for bid, item in key.items()},
        "profiles.json": profiles,
        "preparation.json": {
            "seed": SEED,
            "items": len(blind),
            "generations_sha256": sha256(generations.read_bytes()).hexdigest(),
            "corpus_sha256": sha256((ROOT / "corpus.json").read_bytes()).hexdigest(),
            "unknown_profile_bids": [bid for bid, profile in profiles.items() if profile == "unknown"],
            "limitations": [
                "Existing live hybrid judge rejects unknown profiles (hageche); do not relabel them.",
                "Existing Terra rubric assumes 71630/family/banmal; adapt it outside this repo before judging.",
                "Ambiguity maps to answerable_cross_session; oracle criterion preserves its actual intent.",
                "No judge was invoked; grounding/style quality is unverified.",
            ],
        },
    }
    for name, value in artifacts.items():
        with (output / name).open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    unknown = sum(profile == "unknown" for profile in profiles.values())
    print(f"BLIND_PREPARED items={len(blind)} key={len(key)} seed={SEED} unknown_profiles={unknown}")


if __name__ == "__main__":
    typer.run(main)
