"""Prepare explicit synthetic DEV inputs; never generate model answers."""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import random
from typing import Final, Literal, assert_never
from uuid import UUID

import typer

from corpus import Corpus, Evidence, FrozenModel, chunk_corpus, load_corpus, validate
from exemplars import Intent, StyleRequest, UtteranceTag, add_style_reference, select_exemplars
from generate import Message, render_messages
from per_item import ItemQuery, RetrievalPolicy, SearchIndex, retrieve_items, split_items
from retrieval import Matrix
from retrieval_v2 import contextual_evidence, evidence_json

ROOT: Final = Path(__file__).resolve().parents[2]
HERE: Final = Path(__file__).resolve().parent
CRITERIA_SHA256: Final = "2ce2fd086a832c5a751b46ebff45aa19d3b66d5dc90ae42aac16b9f7499c19d1"
Arm = Literal["B", "C", "S", "P"]


class CasePlan(FrozenModel):
    question_id: str
    item_spans: tuple[str, ...] = ()
    intent: Intent
    self_honorific_probe: bool


class DevPlan(FrozenModel):
    split: Literal["dev"]
    cases: tuple[CasePlan, ...]
    utterance_tags: tuple[UtteranceTag, ...]


class PreparedRow(FrozenModel):
    id: str
    question_id: str
    persona_id: str
    arm: Arm
    messages: tuple[Message, Message]
    messages_sha256: str
    evidence_ids: tuple[UUID, ...]
    item_retained_ids: tuple[tuple[UUID, ...], ...] = ()


class Pack(FrozenModel):
    scope: Literal["DEV_ONLY"] = "DEV_ONLY"
    split: Literal["dev"] = "dev"
    criteria_sha256: str
    source_sha256: dict[str, str]
    code_sha256: dict[str, str]
    tokenizer_sha256: str
    rows: tuple[PreparedRow, ...]


@dataclass(frozen=True, slots=True)
class Preparation:
    system: str
    index: SearchIndex
    encode: Callable[[Sequence[str]], Matrix]
    count_tokens: Callable[[str], int]


def build_rows(corpus: Corpus, plan: DevPlan, runtime: Preparation) -> tuple[PreparedRow, ...]:
    """Build B/C/S/P without exposing gold metadata to query planning/selection."""
    if not corpus.synthetic_only or any(
        row.split != "dev" for row in (*corpus.questions, *corpus.records)
    ):
        raise ValueError("Only synthetic DEV inputs are allowed")
    questions = {q.question_id: q for q in corpus.questions}
    if len(questions) != len(corpus.questions) or (
        len(plan.cases) != len(questions)
        or {p.question_id for p in plan.cases} != set(questions)
    ):
        raise ValueError("Plan must cover each question exactly once")
    if tuple(chunk_corpus(corpus)) != runtime.index.chunks or corpus.records != runtime.index.records:
        raise ValueError("Search index does not match the source corpus")
    personas = {p.persona_id: p for p in corpus.personas}
    by_chunk = {c.chunk_id: c for c in runtime.index.chunks}
    policies = RetrievalPolicy(runtime.count_tokens)
    rows = []
    ordered = list(plan.cases)
    random.Random(95).shuffle(ordered)
    for case in ordered:
        q = questions[case.question_id]
        if any(not span.strip() or span not in q.question for span in case.item_spans):
            raise ValueError("Authored item spans must be literal question substrings")
        queries = tuple(f"{q.question}\n검색할 요구 항목: {s}" for s in case.item_spans) if (
            case.item_spans) else split_items(q.question)
        match q.type:
            case "multi" | "ambiguity":
                if len(set(queries)) < 2:
                    raise ValueError("Multi-event DEV requires at least two question-only items")
            case "single" | "false_premise" | "unanswerable" | "ordinary_chat":
                pass
            case unreachable:
                assert_never(unreachable)
        vectors = runtime.encode((q.question, *queries))
        base = retrieve_items(q, (ItemQuery(q.question, vectors[0]),), runtime.index, policy=policies)
        per_item = retrieve_items(q, tuple(
            ItemQuery(text, vector) for text, vector in zip(queries, vectors[1:], strict=True)
        ), runtime.index, policy=policies)
        gold = tuple(contextual_evidence(by_chunk[cid]) for cid in q.gold_chunk_ids)
        if any(by_chunk[cid].persona_id != q.persona_id for cid in q.gold_chunk_ids):
            raise ValueError("Foreign persona in gold evidence")
        persona = personas[q.persona_id]
        request = StyleRequest.model_validate({
            "persona_id": q.persona_id, "register": persona.speech_register, "intent": case.intent,
        })
        examples = select_exemplars(corpus, request, plan.utterance_tags)
        base_messages = render_messages(runtime.system, persona, q.question, base.evidence)
        style_messages = add_style_reference(base_messages, examples, runtime.count_tokens)
        conditions: tuple[tuple[Arm, tuple[Evidence, ...], tuple[Message, Message]], ...] = (
            ("B", base.evidence, base_messages),
            ("C", gold, render_messages(runtime.system, persona, q.question, gold)),
            ("S", base.evidence, style_messages),
            ("P", per_item.evidence, render_messages(runtime.system, persona, q.question, per_item.evidence)),
        )
        for arm, evidence, messages in conditions:
            if runtime.count_tokens(evidence_json(evidence)) > 2000:
                raise ValueError("Evidence exceeds the common 2000-token cap")
            match arm:
                case "P":
                    item_ids = tuple(item.retained_ids for item in per_item.items)
                case "B" | "C" | "S":
                    item_ids = ()
                case unreachable:
                    assert_never(unreachable)
            payload = "\n".join(m.model_dump_json() for m in messages)
            rows.append(PreparedRow(
                id=f"{q.question_id}:{arm}", question_id=q.question_id, persona_id=q.persona_id,
                arm=arm, messages=messages, messages_sha256=sha256(payload.encode()).hexdigest(),
                evidence_ids=tuple(e.chunk_id for e in evidence),
                item_retained_ids=item_ids,
            ))
    return tuple(rows)


def check_design(corpus: Corpus, plan: DevPlan) -> None:
    """Frozen 360-family feasibility design, including predeclared style probes."""
    if len(corpus.questions) != 360 or len({q.family_id for q in corpus.questions}) != 360:
        raise ValueError("Requires 360 distinct DEV question families")
    if Counter(q.type for q in corpus.questions) != dict.fromkeys(
        ("single", "multi", "ambiguity", "false_premise", "unanswerable", "ordinary_chat"), 60
    ):
        raise ValueError("Requires 60 questions per frozen type")
    personas = {p.persona_id: p for p in corpus.personas}
    cells = Counter((q.type, personas[q.persona_id].speech_register) for q in corpus.questions)
    if len(cells) != 18 or set(cells.values()) != {20}:
        raise ValueError("Requires 20 questions per type/register cell")
    by_id = {q.question_id: q for q in corpus.questions}
    probes = Counter((by_id[c.question_id].type, personas[by_id[c.question_id].persona_id].speech_register)
                     for c in plan.cases if c.self_honorific_probe)
    if len(probes) != 18 or set(probes.values()) != {10}:
        raise ValueError("Requires 10 self-honorific probes per type/register cell")


def main(
    corpus_path: Path, plan_path: Path, encoder_path: Path, tokenizer_path: Path, output: Path,
) -> None:
    """Offline CPU preparation on a separately authored, frozen DEV dataset.

    The five independent CLI paths identify source corpus, annotations, encoder,
    tokenizer and output; retaining explicit flags avoids an opaque path bag.
    """
    import json
    import os

    # Reject forbidden source locations before reading any bytes.
    for path in (corpus_path, plan_path, encoder_path, tokenizer_path):
        if any(term in str(path.resolve()).lower() for term in ("heldout", "held-out", "sealed", "decision")):
            raise ValueError("Forbidden input path")
    if sha256((HERE / "CRITERIA.md").read_bytes()).hexdigest() != CRITERIA_SHA256:
        raise ValueError("Frozen criteria changed")
    corpus = load_corpus(corpus_path)
    plan = DevPlan.model_validate_json(plan_path.read_text())
    validate(corpus)
    check_design(corpus, plan)
    if encoder_path.name != "8b418a58414668e75532ed045c22d9ca018ae2b2":
        raise ValueError("Requires the frozen KURE-v1 snapshot revision")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    import numpy as np
    from sentence_transformers import SentenceTransformer
    from tokenizers import Tokenizer

    encoder = SentenceTransformer(str(encoder_path), device="cpu", trust_remote_code=False)
    encoder.max_seq_length = 512
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.no_truncation()
    tokenizer.no_padding()

    def encode(texts: Sequence[str]) -> Matrix:
        if max(len(encoder.tokenizer(t, truncation=False)["input_ids"]) for t in texts) > 512:
            raise ValueError("Embedding input exceeds 512 tokens")
        vectors = encoder.encode(list(texts), batch_size=8, normalize_embeddings=True,
                                 convert_to_numpy=True, show_progress_bar=False, prompt="")
        if not np.isfinite(vectors).all():
            raise ValueError("Nonfinite embedding")
        return vectors.astype(np.float32)

    chunks = chunk_corpus(corpus)
    baseline_path = ROOT / "ai/configs/baseline.json"
    runtime = Preparation(
        json.loads(baseline_path.read_text())["system_prompt"],
        SearchIndex(chunks, corpus.records, encode([c.embedding_text() for c in chunks])),
        encode, lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids),
    )
    code = (*sorted(HERE.glob("*.py")), ROOT / "experiments/model77/profiles.json",
            *(ROOT / "experiments/model77" / name for name in
              ("prepare.py", "arms.py", "audit.py", "decision.py", "smoke.py")),
            *(ROOT / "experiments/rag" / name for name in
              ("corpus.py", "retrieval.py", "retrieval_v2.py", "generate.py", "historical.py")))
    paths = (corpus_path, plan_path, baseline_path, HERE / "CRITERIA.md",
             encoder_path / "config.json", tokenizer_path)
    pack = Pack(criteria_sha256=CRITERIA_SHA256,
                source_sha256={str(p): sha256(p.read_bytes()).hexdigest() for p in paths},
                code_sha256={str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in code},
                tokenizer_sha256=sha256(tokenizer_path.read_bytes()).hexdigest(),
                rows=build_rows(corpus, plan, runtime))
    with output.open("x", encoding="utf-8") as handle:
        handle.write(pack.model_dump_json(indent=2) + "\n")
    print(f"DEV_PREPARED rows={len(pack.rows)} answers_generated=0")


if __name__ == "__main__":
    typer.run(main)
