"""Build newly authored synthetic DEV with offline KURE/retrieval-v2; no held-out."""

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import torch
from transformers import AutoTokenizer

from .synthetic import (
    REGISTERS, DataError, answer, content_signature, metadata, scenarios, synthetic_record,
)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rag", type=Path, required=True)
    parser.add_argument("--model77", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    sys.path.insert(0, str(args.rag))
    sys.path.insert(0, str(args.model77))
    from corpus import Persona, Question, Record, chunk_record
    from generate import render_messages as rag_messages
    from prompt import render_messages
    from register_check import register_match
    from retrieval import trace_query
    from retrieval_v2 import ContextConfig, contextual_evidence, evidence_json, restore_context
    from sentence_transformers import SentenceTransformer

    tokenizer = AutoTokenizer.from_pretrained(
        args.tokenizer, local_files_only=True, trust_remote_code=False)
    count = lambda text: len(tokenizer.encode(text, add_special_tokens=False))
    model_path = args.rag / ".cache/models/models--nlpai-lab--KURE-v1/snapshots/8b418a58414668e75532ed045c22d9ca018ae2b2"
    torch.manual_seed(59)
    torch.set_num_threads(4)
    encoder = SentenceTransformer(str(model_path), device="cpu", trust_remote_code=False)
    encoder.max_seq_length = 512
    synthetic = scenarios()
    entries = []
    all_chunks = []
    chunk_offsets = []
    for case in synthetic:
        persona = Persona(
            persona_id=case.family, name="가상 인물", display_name="가상 인물",
            provider_address=None, age=50, relationship="친구", register="해요체")
        paragraphs = [case.record]
        # Full additional source paragraphs, never textual padding/truncation.
        if case.long:
            for index in range(60):
                note = (f"{case.event} 준비 일지의 부록 {index + 1}에는 정리표 번호 "
                        f"{1000 + case.ordinal * 100 + index}가 남아 있다. "
                        "이 항목은 오후 준비물을 정리한 순서를 적은 별도의 메모다.")
                candidate = Record.model_validate(synthetic_record(case, tuple(paragraphs + [note])))
                chunks = chunk_record(candidate, persona)
                if count(evidence_json(tuple(contextual_evidence(c) for c in chunks))) > 1950:
                    break
                paragraphs.append(note)
        record = Record.model_validate(synthetic_record(case, tuple(paragraphs)))
        chunks = chunk_record(record, persona)
        question = Question(
            question_id=case.family, persona_id=case.family, family_id=case.family,
            split="dev", type=case.kind, question=case.question,
            gold_chunk_ids=(chunks[0].chunk_id,), support=(), expected_answer=answer(case, "해요체"),
            note="New synthetic scenario; author fact slots, no historical answers",
            required_slots=case.slots, slot_surface_forms={slot: (slot,) for slot in case.slots},
        )
        chunk_offsets.append((len(all_chunks), len(chunks)))
        all_chunks.extend(chunks)
        entries.append((case, persona, record, chunks, question))
    documents = [c.embedding_text() for c in all_chunks]
    queries = [case.question for case in synthetic]
    if max(len(encoder.tokenizer(text, truncation=False)["input_ids"]) for text in documents + queries) > 512:
        raise DataError("Embedding inputs exceed frozen512 cap")
    vectors, query_vectors = [
        encoder.encode(texts, batch_size=8, normalize_embeddings=True,
                       convert_to_numpy=True, show_progress_bar=False, prompt="").astype(np.float32)
        for texts in (documents, queries)
    ]
    if not all(np.isfinite(array).all() for array in (vectors, query_vectors)):
        raise DataError("Nonfinite embeddings")
    system = json.loads(args.baseline.read_text())["system_prompt"]
    training, pilot, validation, audit, ledger = [], [], [], [], []
    for index, (case, persona, record, chunks, question) in enumerate(entries):
        start, size = chunk_offsets[index]
        trace = restore_context(
            trace_query(question, query_vectors[index], vectors[start:start + size], chunks),
            0.5458711981773378, chunks, [record],
            ContextConfig(k=3, window=3, budget=2000), count)
        b_evidence = trace.evidence
        c_evidence = () if case.kind in ("ordinary_chat", "unanswerable") else (
            contextual_evidence(chunks[0]),)
        for register in REGISTERS:
            target = answer(case, register)
            source = answer(case, "해요체")
            if (content_signature(case, source, "해요체") != content_signature(case, target, register)
                    or not register_match(target, register)):
                raise DataError("Meaning-preserving rewrite/register check failed")
            meta = metadata(case, register)
            profile = persona.model_copy(update={
                "speech_register": register,
                "persona_id": meta["persona_id"],
            })
            for condition, evidence in (("B", b_evidence), ("C", c_evidence)):
                content = "\n".join(e.content for e in evidence)
                if any(slot not in content for slot in case.slots):
                    raise DataError(f"Retrieved target facts absent: {case.family}/{condition}")
                semantic = [m.model_dump() for m in rag_messages(system, profile, case.question, evidence)]
                messages = render_messages(semantic, "r1-reg")
                prefix = tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
                prefix_ids = tokenizer.encode(prefix, add_special_tokens=False)
                complete = tokenizer.encode(prefix + target + "<|im_end|>", add_special_tokens=False)
                if complete[:len(prefix_ids)] != prefix_ids or complete[-1] != 151645:
                    raise DataError("Native prefix/EOS boundary mismatch")
                if len(complete) > 4096:
                    raise DataError("Full training example exceeds4096, no truncation allowed")
                if case.long and condition == "B" and len(complete) <= 2048:
                    raise DataError(f"Declared long example too short: {case.family}")
                labels = [-100] * len(prefix_ids) + complete[len(prefix_ids):]
                row = {
                    **meta, "condition": condition, "id": meta["question_id"] + ":" + condition,
                    "messages": messages, "messages_sha256": digest(json.dumps(messages, ensure_ascii=False)),
                    "input_ids": prefix_ids, "text": target, "source_answer": source,
                    "target_sha256": digest(target), "source_sha256": digest(source),
                    "question": case.question, "slots": list(case.slots),
                    "archive": [c.model_dump(mode="json") for c in chunks],
                    "evidence": [e.model_dump(mode="json") for e in evidence],
                    "full_tokens": len(complete), "long": case.long and condition == "B",
                }
                ledger.append(row)
                audit.append({"id": row["id"], "family": case.family, "split": case.split,
                              "fact_signature": list(case.stems), "facts_unchanged": True,
                              "register_checked": True, "input_tokens": len(prefix_ids),
                              "total_tokens": len(complete), "long": row["long"],
                              "source_sha256": row["source_sha256"], "target_sha256": row["target_sha256"]})
                if case.split == "train":
                    batch = {"input_ids": torch.tensor([complete]),
                             "attention_mask": torch.ones(1, len(complete), dtype=torch.long),
                             "labels": torch.tensor([labels])}
                    training.append(batch)
                    if case.ordinal < 30:
                        pilot.append(batch)
                else:
                    validation.append(row)
    if (len(training), len(pilot), len(validation)) != (1800, 180, 360):
        raise DataError("Frozen row counts differ")
    train_families = {r["family"] for r in audit if r["split"] == "train"}
    val_families = {r["family"] for r in audit if r["split"] == "validation"}
    if train_families & val_families:
        raise DataError("Family split leak")
    args.output.mkdir(parents=True)
    for name, data in (("train.pt", training), ("pilot.pt", pilot)):
        torch.save({"split": "train", "provenance": "reviewed-synthetic-dev", "batches": data},
                   args.output / name)
    for name, data in (("validation.json", validation), ("ledger.json", ledger),
                       ("audit.json", audit), ("sources.json", [asdict(c) for c in synthetic])):
        with (args.output / name).open("x") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    report = {
        "train": len(training), "pilot": len(pilot), "validation": len(validation),
        "long_rows": Counter(r["split"] for r in audit if r["long"]),
        "long_range": [min(r["total_tokens"] for r in audit if r["long"]),
                       max(r["total_tokens"] for r in audit if r["long"])],
        "max_tokens": max(r["total_tokens"] for r in audit),
        "facts_unchanged": len(audit), "register_checked": len(audit), "family_overlap": 0,
        "method": "new synthetic stem templates; ending-only rewrites; actual KURE/retrieval-v2 on reset scenario archives",
        "source": "no personal/AIHub/NIKL/historical responses",
        "decision_data": "NOT_BUILT_NOT_READ",
    }
    with (args.output / "summary.json").open("x") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print("DATA_READY " + json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
