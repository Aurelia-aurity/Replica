"""Freeze #77 retrieval and r1 messages without loading a generator."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import sys


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def content_hash(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def save(path: Path, value) -> None:
    """Artifacts are immutable; a second execution cannot replace them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def prepare(args) -> None:
    """Use the unchanged #59 encoder, chunker, restoration and r1 renderer."""
    sys.path.insert(0, str(args.rag))
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer
    from tokenizers import Tokenizer
    from corpus import load_corpus, validate
    from generate import render_messages
    from offline_v2 import export, metrics
    from retrieval import trace_query
    from retrieval_v2 import ContextConfig, contextual_evidence, restore_context

    source = load_corpus(args.corpus)
    chunks = validate(source)
    splits = {q.split for q in source.questions}
    if len(splits) != 1 or not splits <= {"dev", "heldout"}:
        raise ValueError("Only a single DEV or heldout split is allowed")
    split = next(iter(splits))
    if {r.split for r in source.records} != {split}:
        raise ValueError("Record and question splits must match")
    if args.output.exists():
        raise FileExistsError(args.output)
    revision = "8b418a58414668e75532ed045c22d9ca018ae2b2"
    cache = args.rag / ".cache/models/models--nlpai-lab--KURE-v1/snapshots" / revision
    if not cache.is_dir():
        raise FileNotFoundError(cache)
    torch.manual_seed(59)
    torch.set_num_threads(4)
    encoder = SentenceTransformer(str(cache), device="cpu", trust_remote_code=False)
    encoder.max_seq_length = 512
    docs = [c.embedding_text() for c in chunks]
    queries = [q.question for q in source.questions]
    lengths = [len(encoder.tokenizer(t, truncation=False)["input_ids"]) for t in docs + queries]
    if max(lengths) > 512:
        raise ValueError("Embedding input exceeds 512; never truncate")
    vectors, query_vectors = [
        encoder.encode(texts, batch_size=8, normalize_embeddings=True,
                       convert_to_numpy=True, show_progress_bar=False, prompt="").astype(np.float32)
        for texts in (docs, queries)
    ]
    if not all(np.isfinite(v).all() for v in (vectors, query_vectors)):
        raise ValueError("Nonfinite embedding")
    tokenizer_path = args.rag / ".cache/qwen3-8b-tokenizer/tokenizer.json"
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.no_truncation()
    tokenizer.no_padding()

    def count(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    traces = [
        restore_context(trace_query(q, v, vectors, chunks), 0.5458711981773378,
                        chunks, source.records, ContextConfig(k=3, window=3, budget=2000), count)
        for q, v in zip(source.questions, query_vectors, strict=True)
    ]
    destination = args.output
    save(destination / "evidence.json", {split: [
        r.model_dump(mode="json") for r in export(traces, source)
    ]})
    save(destination / "coverage.json", metrics(traces, count).model_dump(
        mode="json", exclude={"per_question"}))
    saved = [json.loads(line) for line in args.r1.read_text().splitlines() if line]
    systems = {r["messages"][0]["content"].rsplit("\n\n", 1)[0] for r in saved}
    if len(systems) != 1:
        raise ValueError("Saved r1 prompts disagree on system message")
    system = systems.pop()
    personas = {p.persona_id: p for p in source.personas}
    by_chunk = {c.chunk_id: c for c in chunks}
    by_question = {q.question_id: q for q in source.questions}
    evidence = {t.question.question_id: t.evidence for t in traces}
    ids = list(by_question)
    random.Random(77).shuffle(ids)
    rows = []
    for qid in ids:
        q = by_question[qid]
        for condition in ("B", "C"):
            received = evidence[qid] if condition == "B" else tuple(
                contextual_evidence(by_chunk[cid]) for cid in q.gold_chunk_ids)
            messages = [m.model_dump(mode="json") for m in render_messages(
                system, personas[q.persona_id], q.question, received, prompt_version="r1")]
            rows.append({
                "id": f"{qid}:{condition}", "question_id": qid,
                "condition": condition, "persona_id": q.persona_id,
                "messages": messages, "messages_sha256": content_hash(messages),
                "evidence_sha256": content_hash([e.model_dump(mode="json") for e in received]),
            })
    profiles_path = Path(__file__).with_name("profiles.json")
    bundle = {
        "schema_version": 1, "split": split, "schedule_seed": 77,
        "model_order": list(json.loads(profiles_path.read_text())),
        "corpus_sha256": digest(args.corpus),
        "retrieval_sha256": digest(destination / "evidence.json"),
        "protocol_sha256": digest(args.protocol), "r1_sha256": digest(args.r1),
        "profiles_sha256": digest(profiles_path), "rows": rows,
        "warmups": [], "stress": [],
    }
    if split == "dev":
        spec = json.loads(args.dev_spec.read_text())
        by_id = {r["id"]: r for r in rows}
        bundle["dev_spec_sha256"] = digest(args.dev_spec)
        bundle["warmups"] = [
            {**by_id[f"{qid}:{condition}"], "id": f"warmup:{qid}:{condition}"}
            for qid in spec["warmup_question_ids"] for condition in spec["conditions"]
        ]
        for probe in spec["stress_calls"]:
            row = by_id[f"{probe['question_id']}:{probe['condition']}"]
            records = [r for r in source.records if r.persona_id == probe["persona_id"]]
            cycle = "\n".join(
                f"{r.date} | {r.speaker} | {r.text}" if r.kind == "diary" else
                "\n".join(f"{r.date} | {t.speaker} | {t.text}" for t in r.turns)
                for r in records
            )
            bundle["stress"].append({
                **row, "id": probe["call_id"], "context_cycle": cycle,
                "target_input_tokens": probe["target_input_tokens"],
                "min_new_tokens": probe["min_new_tokens"],
            })
    if args.prompt_version != "r1":
        from prompt import variant_bundle
        bundle = variant_bundle(bundle, args.prompt_version)
    save(destination / "bundle.json", bundle)
    save(destination / "manifest.json", {
        "corpus_sha256": digest(args.corpus), "protocol_sha256": digest(args.protocol),
        "encoder_revision": revision, "encoder_device": "cpu", "seed": 59,
        "batch_size": 8, "threads": 4, "max_encoder_tokens": max(lengths),
        "threshold": 0.5458711981773378, "k": 3, "window": 3, "budget": 2000,
        "tokenizer_sha256": digest(tokenizer_path), "r1_sha256": digest(args.r1),
        "source_sha256": {p: digest(args.rag / p) for p in
                          ("corpus.py", "retrieval.py", "retrieval_v2.py", "generate.py")},
        "artifacts": {p: digest(destination / p) for p in
                      ("evidence.json", "coverage.json", "bundle.json")},
        "generation_performed": False,
        **({"prompt_version": args.prompt_version,
            "register_template_sha256": bundle["register_template_sha256"]}
           if args.prompt_version != "r1" else {}),
    })
    print(json.dumps({"split": split, "bundle_sha256": digest(destination / "bundle.json"),
                      "coverage": json.loads((destination / "coverage.json").read_text())}), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("rag", "corpus", "r1", "protocol", "dev-spec", "output"):
        parser.add_argument("--" + flag, required=True, type=Path)
    parser.add_argument("--prompt-version", choices=("r1", "r1-reg", "r1-reg-14b"), default="r1")
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    prepare(args)


if __name__ == "__main__":
    main()
