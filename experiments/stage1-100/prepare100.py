"""Prepare B/A/C on CPU, without answers, oracle access, freeze or submission."""

from collections.abc import Sequence
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import sys
from typing import Final

import typer

from corpus import Corpus, Evidence, chunk_corpus
from dev_schema import GoldContext, Sources
from generate import render_messages
from per_item import ItemQuery, RetrievalPolicy, SearchIndex, retrieve_items
from prepare95 import Preparation
from renderer100 import render_o3
from retrieval import Matrix
from retrieval_v2 import contextual_evidence, evidence_json
from schema100 import Arm, Pack100, Row100, check_design, Plan100

HERE: Final = Path(__file__).resolve().parent
ROOT: Final = HERE.parents[1]
INPUTS: Final = ("corpus.json", "plans.json", "identity-ledger.json", "gold-context.json")


def reject_forbidden(*paths: Path) -> None:
    if any(word in str(p.resolve()).lower() for p in paths
           for word in ("heldout", "held-out", "sealed", "aihub", "ai_top_100", "nikl", ".env")):
        raise ValueError("Forbidden source path")


def build_rows(corpus: Corpus, runtime: Preparation) -> tuple[Row100, ...]:
    """Reuse #95 B's whole-question retrieval and exact r1 message serializer."""
    if not corpus.synthetic_only or any(
        row.split != "dev" for row in (*corpus.questions, *corpus.records)
    ):
        raise ValueError("Synthetic DEV only")
    if tuple(chunk_corpus(corpus)) != runtime.index.chunks or corpus.records != runtime.index.records:
        raise ValueError("Search index does not match source")
    if runtime.gold_context is None or set(runtime.gold_context) != {q.question_id for q in corpus.questions}:
        raise ValueError("C requires an exact separate question-to-gold map")
    by_id = {c.chunk_id: c for c in runtime.index.chunks}
    personas = {p.persona_id: p for p in corpus.personas}
    rows = []
    ordered = list(corpus.questions)
    random.Random(100).shuffle(ordered)
    for q in ordered:
        # Exactly the B retrieval call from prepare95.build_rows, no O1/O2/P/S.
        vector = runtime.encode((q.question,))[0]
        base = retrieve_items(
            q, (ItemQuery(q.question, vector),), runtime.index,
            policy=RetrievalPolicy(runtime.count_tokens),
        ).evidence
        candidate = render_o3(base, by_id)
        gold = runtime.gold_context[q.question_id]
        if len({e.chunk_id for e in gold}) != len(gold) or any(
            by_id[e.chunk_id].persona_id != q.persona_id
            or e.speaker_role != "persona" or e != contextual_evidence(by_id[e.chunk_id])
            for e in gold
        ):
            raise ValueError("C must contain only exact original persona chunks")
        if q.type in ("unanswerable", "ordinary_chat") and gold:
            raise ValueError("Safe/chat C must have empty context")
        conditions: tuple[tuple[Arm, tuple[Evidence, ...]], ...] = (
            ("B", base), ("A", candidate), ("C", gold),
        )
        for arm, evidence in conditions:
            if runtime.count_tokens(evidence_json(evidence)) > 2000:
                raise ValueError("Evidence overflow; no A-only pruning or resampling")
            messages = render_messages(runtime.system, personas[q.persona_id], q.question, evidence)
            payload = "\n".join(m.model_dump_json() for m in messages)
            rows.append(Row100(
                id=f"{q.question_id}:{arm}", question_id=q.question_id, persona_id=q.persona_id,
                arm=arm, messages=messages, messages_sha256=sha256(payload.encode()).hexdigest(),
                evidence_ids=tuple(e.chunk_id for e in evidence),
            ))
    return tuple(rows)


def main(data: Path, encoder_path: Path, tokenizer_path: Path, output: Path) -> None:
    """Draft pack preparation with local pinned KURE and native tokenizer only."""
    reject_forbidden(data, encoder_path, tokenizer_path, output)
    manifest = json.loads((data / "manifest.json").read_text())
    if manifest["status"] != "DRAFT_NOT_FROZEN":
        raise ValueError("This implementation supports draft preparation, not a freeze")
    for name in INPUTS:
        if sha256((data / name).read_bytes()).hexdigest() != manifest["artifacts_sha256"][name]:
            raise ValueError("Source hash mismatch")
    for name, digest in manifest["authoring_code_sha256"].items():
        if sha256((HERE / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Authoring code changed")
    sources = Sources.model_validate_json((data / "corpus.json").read_text())
    plan = Plan100.model_validate_json((data / "plans.json").read_text())
    gold = GoldContext.model_validate_json((data / "gold-context.json").read_text())
    check_design(sources, plan)
    if encoder_path.name != manifest["encoder_revision"]:
        raise ValueError("KURE snapshot revision mismatch")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      TOKENIZERS_PARALLELISM="false", HF_DATASETS_OFFLINE="1")
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer
    from tokenizers import Tokenizer
    from transformers import AutoTokenizer

    torch.set_num_threads(4)
    encoder = SentenceTransformer(str(encoder_path), device="cpu", trust_remote_code=False)
    encoder.max_seq_length = 512
    tokenizer_file = tokenizer_path / "tokenizer.json"
    tokenizer = Tokenizer.from_file(str(tokenizer_file))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    corpus = sources.corpus()
    chunks = chunk_corpus(corpus)
    texts = list(dict.fromkeys((
        *(c.embedding_text() for c in chunks), *(q.question for q in corpus.questions),
    )))
    if max(len(encoder.tokenizer(t, truncation=False)["input_ids"]) for t in texts) > 512:
        raise ValueError("Embedding input exceeds 512 tokens")
    print(f"CPU_ENCODER_START texts={len(texts)} chunks={len(chunks)}", flush=True)
    vectors = encoder.encode(texts, batch_size=8, normalize_embeddings=True,
                             convert_to_numpy=True, show_progress_bar=False, prompt="").astype(np.float32)
    if not np.isfinite(vectors).all():
        raise ValueError("Nonfinite embeddings")
    cache = dict(zip(texts, vectors, strict=True))

    def encode(batch: Sequence[str]) -> Matrix:
        return np.stack([cache[t] for t in batch])

    def count_tokens(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    baseline = ROOT / "ai/configs/baseline.json"
    rows = build_rows(corpus, Preparation(
        json.loads(baseline.read_text())["system_prompt"],
        SearchIndex(chunks, corpus.records, encode([c.embedding_text() for c in chunks])),
        encode, count_tokens, gold.evidence,
    ))
    sys.path.append(str(ROOT / "experiments/model77"))
    from audit import serialize, token_ids
    from smoke import load_profile

    native = AutoTokenizer.from_pretrained(tokenizer_path, local_files_only=True, trust_remote_code=False)
    lengths = [len(token_ids(native, serialize(native, [m.model_dump() for m in r.messages],
                                              load_profile("Qwen3-8B")))) for r in rows]
    if max(lengths) + 160 > 4096:
        raise ValueError("Native prompt plus output overflow; no truncation")
    code_paths = (
        *sorted(HERE.glob("*.py")),
        *(ROOT / "experiments/stage1-95" / n for n in ("prepare95.py", "per_item.py", "dev_schema.py")),
        *(ROOT / "experiments/rag" / n for n in ("corpus.py", "retrieval.py", "retrieval_v2.py", "generate.py")),
        *(ROOT / "experiments/model77" / n for n in ("profiles.json", "prepare.py", "arms.py", "audit.py", "decision.py", "smoke.py")),
    )
    pack = Pack100(
        source_sha256={str(p): sha256(p.read_bytes()).hexdigest()
                       for p in (*(data / n for n in INPUTS), data / "manifest.json", baseline)},
        code_sha256={str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in code_paths},
        tokenizer_sha256=sha256(tokenizer_file.read_bytes()).hexdigest(),
        tokenizer_config_sha256=sha256((tokenizer_path / "tokenizer_config.json").read_bytes()).hexdigest(),
        rows=rows,
    )
    from run100 import validate_pack
    validate_pack(pack)
    output.mkdir(parents=True, exist_ok=False)
    with (output / "pack.json").open("x", encoding="utf-8") as handle:
        handle.write(pack.model_dump_json(indent=2) + "\n")
    with (output / "preparation.json").open("x", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "status": "DRAFT_NOT_FROZEN", "rows": len(rows), "encoder_device": "cpu",
            "max_native_prompt_tokens": max(lengths), "output_tokens": 160,
            "rendered_to_source": "Row evidence_ids align with payload order; original source retained",
            "oracle_opened": False, "answers_generated": 0, "judges_run": 0,
            "pack_sha256": sha256((output / "pack.json").read_bytes()).hexdigest(),
        }, indent=2) + "\n")
    print(f"CPU_DEV_DRAFT_PREPARED rows={len(rows)} max_native_prompt_tokens={max(lengths)}")


if __name__ == "__main__":
    typer.run(main)
