"""Apply main-DEV-frozen KURE old/v2 retrieval on a separate held-out corpus."""

from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from typing import Final

import numpy as np
import torch
import typer

from corpus import ROOT, CorpusIntegrityError, load_corpus, validate
from offline_v2 import (
    QWEN_REVISION, TOKENIZER_DIR, export, load_tokenizer, metrics, write_json,
)
from retrieval import Calibration, select_evidence, trace_query
from retrieval_v2 import ContextConfig, restore_context
from run import Encoder

THRESHOLD: Final = 0.5458711981773378
CONFIG: Final = ContextConfig(k=3, window=3, budget=2000)
MAIN_RESULTS: Final = ROOT / "results/retrieval-v2"


def main(
    corpus: Path = typer.Option(...),
    output: Path = ROOT / "results/heldout",
) -> None:
    """Encode on CPU, never fit or choose settings using held-out annotations."""
    from sentence_transformers import SentenceTransformer

    corpus = corpus.resolve()
    source = load_corpus(corpus)
    if {q.split for q in source.questions} != {"heldout"} or {
        r.split for r in source.records
    } != {"heldout"}:
        raise CorpusIntegrityError(["held-out runner requires only heldout splits"])
    chunks = validate(source)
    calibration_path = MAIN_RESULTS / "kure_old/calibration.json"
    calibration = Calibration.model_validate_json(calibration_path.read_text(encoding="utf-8"))
    selection_path = MAIN_RESULTS / "kure_v2/dev-selection.json"
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    if (
        calibration.split != "dev" or calibration.threshold != THRESHOLD
        or selection["split"] != "dev" or selection["chosen"] != CONFIG.model_dump()
    ):
        raise CorpusIntegrityError(["main DEV frozen settings differ from held-out contract"])
    lock_path = ROOT / "model-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))["kure"]
    spec = Encoder.model_validate(lock)
    output = output.resolve()
    if not output.is_relative_to(ROOT):
        raise CorpusIntegrityError(["retrieval output must stay within experiments/rag"])
    destinations = [output / name for name in ("kure_old", "kure_v2")]
    if any(path.exists() for path in destinations):
        raise FileExistsError("held-out retrieval output already exists")
    for path in destinations:
        path.mkdir(parents=True, exist_ok=False)
    torch.manual_seed(59)
    torch.set_num_threads(4)
    encoder = SentenceTransformer(
        spec.repository, revision=spec.revision, device="cpu",
        cache_folder=str(ROOT / ".cache/models"), trust_remote_code=False,
    )
    encoder.max_seq_length = 512
    documents = [spec.passage_prefix + c.embedding_text() for c in chunks]
    queries = [spec.query_prefix + q.question for q in source.questions]
    lengths = [len(encoder.tokenizer(text, truncation=False)["input_ids"])
               for text in (*documents, *queries)]
    if max(lengths) > 512:
        raise CorpusIntegrityError(["held-out encoder input would truncate"])
    vectors = encoder.encode(
        documents, batch_size=8, normalize_embeddings=True,
        convert_to_numpy=True, show_progress_bar=False, prompt="",
    ).astype(np.float32)
    query_vectors = encoder.encode(
        queries, batch_size=8, normalize_embeddings=True,
        convert_to_numpy=True, show_progress_bar=False, prompt="",
    ).astype(np.float32)
    if (
        vectors.shape != (len(chunks), spec.dimensions)
        or query_vectors.shape != (len(queries), spec.dimensions)
        or not np.isfinite(vectors).all() or not np.isfinite(query_vectors).all()
    ):
        raise CorpusIntegrityError(["invalid held-out embedding matrix"])
    raw = [trace_query(q, v, vectors, chunks)
           for q, v in zip(source.questions, query_vectors, strict=True)]
    tokenizer = load_tokenizer()

    @lru_cache(maxsize=10000)
    def count_tokens(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    old = [select_evidence(trace, THRESHOLD) for trace in raw]
    restored = [
        restore_context(trace, THRESHOLD, chunks, source.records, CONFIG, count_tokens)
        for trace in raw
    ]
    provenance = {
        "corpus_path": str(corpus), "corpus_sha256": sha256(corpus.read_bytes()).hexdigest(),
        "encoder": spec.model_dump(), "device": "cpu", "seed": 59, "batch_size": 8,
        "torch_threads": 4, "dtype": "float32", "max_seq_length": 512,
        "max_observed_encoder_tokens": max(lengths),
        "threshold": THRESHOLD, "config": CONFIG.model_dump(),
        "calibration_source_sha256": sha256(calibration_path.read_bytes()).hexdigest(),
        "dev_selection_sha256": sha256(selection_path.read_bytes()).hexdigest(),
        "settings_policy": "fixed main DEV threshold/config; no held-out calibration or tuning",
        "tokenizer": {
            "repository": "Qwen/Qwen3-8B", "revision": QWEN_REVISION,
            "sha256": sha256((TOKENIZER_DIR / "tokenizer.json").read_bytes()).hexdigest(),
            "counting": "full pretty-printed evidence JSON; no special tokens",
        },
        "code_sha256": {name: sha256((ROOT / name).read_bytes()).hexdigest()
                        for name in ("run_heldout.py", "corpus.py", "retrieval.py",
                                     "retrieval_v2.py", "offline_v2.py", "model-lock.json")},
        "gpu_or_judge_run": False,
    }
    for name, traces, evidence_format in (
        ("kure_old", old, "canonical"), ("kure_v2", restored, "source-context-v2"),
    ):
        destination = output / name
        write_json(destination / "calibration.json", calibration.model_dump(mode="json"))
        write_json(destination / "manifest.json", {
            **provenance, "variant": name, "evidence_format": evidence_format,
        })
        write_json(destination / "evidence.json", {
            "heldout": [row.model_dump(mode="json") for row in export(traces, source)],
        })
        write_json(destination / "traces.json", {
            "heldout": [trace.model_dump(mode="json") for trace in traces],
        })
        # Aggregate coverage only: do not expose held-out answer/slot strings.
        summary = metrics(traces, count_tokens).model_dump(
            mode="json", exclude={"per_question"},
        )
        write_json(destination / "metrics.json", summary)
        print(json.dumps({"variant": name, **summary}), flush=True)
    print("HELDOUT_RETRIEVAL_DONE settings=fixed model_device=cpu", flush=True)


if __name__ == "__main__":
    typer.run(main)
