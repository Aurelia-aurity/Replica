"""Real offline CPU KURE preparation; never opens the evaluation sidecar."""

from collections.abc import Sequence
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import sys

import typer

from corpus import validate, chunk_corpus
from dev_schema import GoldContext, Sources
from per_item import SearchIndex
from prepare95 import (
    CRITERIA_SHA256, HERE, ROOT, DevPlan, Pack, Preparation, build_rows, case_queries, check_design,
)
from retrieval import Matrix
from run_dev import validate_pack


def main(data: Path, encoder_path: Path, tokenizer_path: Path, output: Path) -> None:
    """Use only frozen public inputs, separate style sources and the C intervention.

    Four independent paths name the frozen dataset, pinned encoder, native
    tokenizer directory, and new output directory, matching the existing CLI.
    """
    for path in (data, encoder_path, tokenizer_path, output):
        if any(word in str(path.resolve()).lower()
               for word in ("heldout", "held-out", "sealed", "decision")):
            raise ValueError("Forbidden input/output path")
    manifest = json.loads((data / "manifest.json").read_text())
    if manifest["criteria_sha256"] != CRITERIA_SHA256 or (
        sha256((HERE / "CRITERIA.md").read_bytes()).hexdigest() != CRITERIA_SHA256
    ):
        raise ValueError("Frozen criteria changed")
    names = ("corpus.json", "style-sources.json", "plans.json", "gold-context.json")
    for name in names:
        if sha256((data / name).read_bytes()).hexdigest() != manifest["artifacts_sha256"][name]:
            raise ValueError("Frozen source changed")
    for name, digest in manifest["authoring_code_sha256"].items():
        if sha256((HERE / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Frozen authoring code changed")
    sources = Sources.model_validate_json((data / "corpus.json").read_text())
    style = Sources.model_validate_json((data / "style-sources.json").read_text())
    plan = DevPlan.model_validate_json((data / "plans.json").read_text())
    gold = GoldContext.model_validate_json((data / "gold-context.json").read_text())
    corpus, style_corpus = sources.corpus(), style.corpus()
    validate(corpus.model_copy(update={"questions": ()}))
    validate(style_corpus)
    check_design(corpus, plan)
    if set(gold.evidence) != {q.question_id for q in corpus.questions}:
        raise ValueError("C intervention does not cover the source questions")
    if {r.record_id for r in corpus.records} & {r.record_id for r in style_corpus.records}:
        raise ValueError("Style-only source appears in the factual archive")
    if encoder_path.name != manifest["encoder_revision"]:
        raise ValueError("Requires frozen KURE-v1 revision")
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
    chunks = chunk_corpus(corpus)
    by_question = {q.question_id: q for q in corpus.questions}
    texts = list(dict.fromkeys((
        *(c.embedding_text() for c in chunks),
        *(text for case in plan.cases for text in (
            by_question[case.question_id].question,
            *case_queries(by_question[case.question_id], case),
        )),
    )))
    if max(len(encoder.tokenizer(t, truncation=False)["input_ids"]) for t in texts) > 512:
        raise ValueError("Embedding input exceeds 512 tokens")
    print(f"CPU_ENCODER_START texts={len(texts)} chunks={len(chunks)}", flush=True)
    vectors = encoder.encode(
        texts, batch_size=8, normalize_embeddings=True, convert_to_numpy=True,
        show_progress_bar=False, prompt="",
    ).astype(np.float32)
    if not np.isfinite(vectors).all():
        raise ValueError("Nonfinite embedding")
    cached = dict(zip(texts, vectors, strict=True))

    def encode(batch: Sequence[str]) -> Matrix:
        return np.stack([cached[text] for text in batch])

    def count_tokens(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    baseline = ROOT / "ai/configs/baseline.json"
    runtime = Preparation(
        json.loads(baseline.read_text())["system_prompt"],
        SearchIndex(chunks, corpus.records, encode([c.embedding_text() for c in chunks])),
        encode, count_tokens, gold.evidence, style_corpus,
    )
    rows = build_rows(corpus, plan, runtime)
    sys.path.append(str(HERE.parent / "model77"))
    from audit import serialize, token_ids
    from smoke import load_profile

    native = AutoTokenizer.from_pretrained(
        tokenizer_path, local_files_only=True, trust_remote_code=False,
    )
    profile = load_profile("Qwen3-8B")
    lengths = [len(token_ids(native, serialize(
        native, [m.model_dump() for m in row.messages], profile,
    ))) for row in rows]
    if max(lengths) + 160 > 4096:
        raise ValueError("Native prompt plus output exceeds 4096 tokens")
    # Save no vectors: frozen pack provenance plus model revision suffice to reproduce.
    code_paths = (*sorted(HERE.glob("*.py")), ROOT / "experiments/model77/profiles.json",
                  *(ROOT / "experiments/model77" / name for name in
                    ("prepare.py", "arms.py", "audit.py", "decision.py", "smoke.py")),
                  *(ROOT / "experiments/rag" / name for name in
                    ("corpus.py", "retrieval.py", "retrieval_v2.py", "generate.py", "historical.py")))
    source_paths = (*(data / name for name in names), data / "manifest.json", baseline,
                    HERE / "CRITERIA.md", encoder_path / "config.json", tokenizer_file,
                    tokenizer_path / "tokenizer_config.json")
    pack = Pack(
        criteria_sha256=CRITERIA_SHA256,
        source_sha256={str(p): sha256(p.read_bytes()).hexdigest() for p in source_paths},
        code_sha256={str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in code_paths},
        tokenizer_sha256=sha256(tokenizer_file.read_bytes()).hexdigest(), rows=rows,
    )
    validate_pack(pack)
    output.mkdir(parents=True, exist_ok=False)
    with (output / "pack.json").open("x", encoding="utf-8") as handle:
        handle.write(pack.model_dump_json(indent=2) + "\n")
    audit = {
        "scope": "DEV_ONLY", "rows": len(rows), "encoder_device": "cpu",
        "encoder_revision": manifest["encoder_revision"],
        "tokenizer_sha256": pack.tokenizer_sha256,
        "tokenizer_config_sha256": sha256((tokenizer_path / "tokenizer_config.json").read_bytes()).hexdigest(),
        "max_native_prompt_tokens": max(lengths), "output_token_reserve": 160,
        "context_cap": 4096, "evidence_cap": 2000, "style_cap": 400,
        "oracle_opened": False, "answers_generated": 0, "judge_called": False,
        "pack_sha256": sha256((output / "pack.json").read_bytes()).hexdigest(),
        "packages": {p: importlib.metadata.version(p) for p in (
            "torch", "sentence-transformers", "tokenizers", "transformers", "numpy")},
    }
    with (output / "preparation.json").open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(audit, indent=2) + "\n")
    print("CPU_DEV_PREPARED " + json.dumps(audit), flush=True)


if __name__ == "__main__":
    typer.run(main)
