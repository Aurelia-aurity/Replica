"""Run pinned encoders locally, freeze DEV threshold, then evaluate TEST once."""

from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import platform
import subprocess
from time import perf_counter
from typing import Literal

import numpy as np
from pydantic import RootModel
import torch
import typer

from corpus import CHUNKER_VERSION, ROOT, FrozenModel, Split, load_corpus, validate
from retrieval import Matrix, Trace, calibrate, evaluate, select_evidence, trace_query


class Encoder(FrozenModel):
    repository: str
    revision: str
    dimensions: int
    query_prefix: str
    passage_prefix: str


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def main(
    model: Literal["kure", "e5"],
    device: Literal["mps", "cpu"] = "mps",
    batch_size: int = 8,
) -> None:
    """All outputs stay within experiments/rag; an existing run is never replaced."""
    from sentence_transformers import SentenceTransformer

    corpus = load_corpus()
    chunks = validate(corpus)
    locks = RootModel[dict[str, Encoder]].model_validate_json(
        (ROOT / "model-lock.json").read_text(encoding="utf-8")
    ).root
    spec = locks[model]
    output = ROOT / "results" / model
    output.mkdir(parents=True, exist_ok=False)
    torch.manual_seed(59)
    torch.set_num_threads(4)
    start = perf_counter()
    encoder = SentenceTransformer(
        spec.repository,
        revision=spec.revision,
        device=device,
        cache_folder=str(ROOT / ".cache" / "models"),
        trust_remote_code=False,
    )
    loaded_seconds = perf_counter() - start
    encoder.max_seq_length = 512
    documents = [spec.passage_prefix + c.embedding_text() for c in chunks]
    all_queries = [spec.query_prefix + q.question for q in corpus.questions]
    token_lengths = [
        len(encoder.tokenizer(text, truncation=False)["input_ids"])
        for text in documents + all_queries
    ]
    if max(token_lengths) > encoder.max_seq_length:
        raise ValueError(f"would truncate: max tokens {max(token_lengths)}")
    encode_start = perf_counter()
    vectors: Matrix = encoder.encode(
        documents,
        batch_size=batch_size,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=True,
        prompt="",
    ).astype(np.float32)
    if vectors.shape != (len(chunks), spec.dimensions):
        raise ValueError(f"embedding shape: {vectors.shape}")
    if not np.isfinite(vectors).all():
        raise ValueError("non-finite document embeddings")
    document_seconds = perf_counter() - encode_start
    print(f"ENCODED {model}: {vectors.shape}, max tokens={max(token_lengths)}", flush=True)

    all_traces: dict[Split, list[Trace]] = {}
    query_times: dict[Split, float] = {}
    threshold: float | None = None
    for split in ("dev", "test"):
        questions = [q for q in corpus.questions if q.split == split]
        query_start = perf_counter()
        query_vectors: Matrix = encoder.encode(
            [spec.query_prefix + q.question for q in questions],
            batch_size=batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=True,
            prompt="",
        ).astype(np.float32)
        query_times[split] = perf_counter() - query_start
        if not np.isfinite(query_vectors).all():
            raise ValueError("non-finite query embeddings")
        traces = [
            trace_query(q, v, vectors, chunks)
            for q, v in zip(questions, query_vectors, strict=True)
        ]
        if split == "dev":
            calibration = calibrate(traces)
            threshold = calibration.threshold
            (output / "calibration.json").write_text(
                calibration.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            print(f"FROZEN DEV threshold {model}: {threshold:.9f}", flush=True)
        if threshold is None:
            raise RuntimeError("TEST cannot execute without frozen DEV threshold")
        all_traces[split] = [select_evidence(t, threshold) for t in traces]
    if threshold is None:
        raise RuntimeError("missing calibration")
    metrics = {
        split: evaluate(traces, threshold).model_dump(mode="json")
        for split, traces in all_traces.items()
    }
    manifest = {
        "executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "encoder": spec.model_dump(),
        "device": device,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "packages": dict(sorted(
            (d.metadata["Name"], d.version) for d in distributions()
        )),
        "seed": 59,
        "batch_size": batch_size,
        "torch_threads": 4,
        "dtype": "float32",
        "normalization": "sentence-transformers L2; numpy exact dot product",
        "max_seq_length": 512,
        "max_observed_encoder_tokens": max(token_lengths),
        "chunker_version": CHUNKER_VERSION,
        "scope": "persona filter before ranking; same fixed full archive for DEV and TEST",
        "test_policy": "DEV calibration JSON written before TEST query embedding/retrieval",
        "calibration_sha256": digest(output / "calibration.json"),
        "sha256": {
            name: digest(ROOT / name)
            for name in (
                "corpus.json", "corpus.py", "retrieval.py", "run.py", "model-lock.json",
                "requirements.txt",
            )
        },
        "git_head": subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip(),
        "uncommitted_experiment": True,
        "timing_seconds": {
            "load": loaded_seconds,
            "documents": document_seconds,
            "queries": query_times,
            "total": perf_counter() - start,
        },
        "limitations": [
            "Synthetic template-based fixtures; no real-world quality claim.",
            "No generation, semantic judge, service integration, live access control, or Qwen budget test.",
            "Five unanswerable questions per split; threshold estimate has high uncertainty.",
            "No timing warmups or peak RSS measurement; timings are single-run observations.",
        ],
    }
    for name, payload in (
        ("manifest.json", manifest),
        ("metrics.json", metrics),
        ("traces.json", {
            split: [t.model_dump(mode="json") for t in traces]
            for split, traces in all_traces.items()
        }),
        ("chunks.json", [c.model_dump(mode="json") for c in chunks]),
        ("evidence.json", {
            split: [
                {
                    "question_id": t.question.question_id,
                    "persona_id": t.question.persona_id,
                    "persona": next(
                        {"display_name": p.display_name, "provider_address": p.provider_address}
                        for p in corpus.personas if p.persona_id == t.question.persona_id
                    ),
                    "message": t.question.question,
                    "predicted_no_evidence": t.predicted_no_evidence,
                    "evidence": [e.model_dump(mode="json") for e in t.evidence],
                    "topk_evidence": [e.model_dump(mode="json") for e in t.topk_evidence],
                }
                for t in traces
            ]
            for split, traces in all_traces.items()
        }),
    ):
        (output / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"model": model, "threshold": threshold, "metrics": metrics}, indent=2))


if __name__ == "__main__":
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    typer.run(main)
