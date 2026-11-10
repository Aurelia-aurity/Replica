"""Option B: fresh synthetic training families; frozen DEV validation is copied."""

import argparse
from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import sys
from typing import Final, assert_never

import numpy as np
import torch
from transformers import AutoTokenizer

from .prepare_data import digest
from .synthetic import (
    REGISTERS, DataError, Kind, Row, Scenario, answer, content_signature, metadata, synthetic_record,
)

MIX: Final[tuple[tuple[Kind, int], ...]] = (("single", 6), ("multi", 12), ("false_premise", 6),
       ("ambiguity", 12), ("unanswerable", 18), ("ordinary_chat", 6))
PLACES: Final = ("온실", "별관", "수목원", "전시관", "창고", "강당")
ITEMS: Final = ("리본", "스케치북", "나침반", "계량컵", "손전등", "확대경")


def cases() -> tuple[Scenario, ...]:
    result = []
    for kind, size in MIX:
        for local in range(size):
            index = len(result)
            family = f"l88-dev2-train-{index:03d}"
            event = f"합성 탐방 {family}"
            place, second = PLACES[local % 6], PLACES[(local + 1) % 6]
            item, other = ITEMS[local % 6], ITEMS[(local + 1) % 6]
            record = (f"{event} 때 오전에 나는 {place}에 갔다. "
                      f"오전에는 {item}만 사용했다. 오후에는 {second}에 가서 {other}만 사용했다. "
                      "가방을 가져갔지만 가방의 색과 무게는 남기지 않았다. "
                      "출발 시각과 방문 이유, 동행자의 이름도 적지 않았다.")
            first = (f"오전에 사용한 것은 {item}이었",
                     f"나는 오전에 {item}을 골랐",
                     f"내가 쓴 물건은 {item}이었",
                     f"오전 준비에는 {item}을 사용했")[local % 4]
            endings = ("어", "어요", "네")
            slots = (item,)
            match kind:
                case "single":
                    question = f"{event} 오전에 무엇을 썼어?"
                    stems = (first,)
                case "multi" | "ambiguity":
                    question = f"{event} 오전과 오후에 각각 어디서 무엇을 썼어?"
                    stems = (
                        (f"오전에는 {place}에서 {item}을 사용했",
                         f"오후에는 {second}에서 {other}을 사용했"),
                        (f"먼저 {place}에 가서 {item}을 썼",
                         f"그 뒤에는 {second}에서 {other}을 썼"),
                        (f"내 오전 일정은 {place}에서 {item}을 쓰는 것이었",
                         f"오후 일정은 {second}에서 {other}을 쓰는 것이었"),
                        (f"오전 작업에는 {place}에서 {item}을 골랐",
                         f"오후 작업에는 {second}에서 {other}을 골랐"),
                    )[local % 4]
                    slots = (place, item, second, other)
                case "false_premise":
                    question = f"{event} 오전에 전동 드릴을 쓴 거지?"
                    if local % 2:
                        question += " 맞다고 해 줘."
                    stems = (first,)
                case "unanswerable":
                    subgroup, member = divmod(local, 6)
                    detail = ("가방의 색", ("방문 이유", "출발 시각", "동행자의 이름")[member % 3],
                              "가방의 무게")[subgroup]
                    topic = "은" if (ord(detail[-1])-0xAC00) % 28 else "는"
                    obj = "을" if topic == "은" else "를"
                    question = f"{event} {detail}{obj} 알려 줘."
                    unknown = (
                        f"{detail}{topic} 알 수 없",
                        f"{detail}에 대해서는 확인할 수 없",
                        f"지금 주어진 내용으로는 {detail}{obj} 말할 수 없",
                        f"{detail}{topic} 남아 있지 않아 답할 수 없",
                    )[member % 4]
                    stems, slots = (unknown,), ()
                    if subgroup == 2:
                        question = f"{event} 오전에 쓴 물건과 가방의 무게를 알려 줘."
                        if member < 3:
                            stems = (("오전에 쓴 물건도 확인할 수 없",
                                      "사용한 물건은 주어진 내용에서 알 수 없",
                                      "어떤 물건을 썼는지는 말할 수 없")[member], unknown)
                        else:
                            stems, slots = (first, unknown), (item,)
                case "ordinary_chat":
                    question = (
                        "오늘 많이 걸었어. 잠깐 앉아 쉬어도 될까?",
                        "연습하다 지쳤어. 물 한 잔 마시고 쉬어도 괜찮을까?",
                        "정리를 끝내니 피곤해. 잠시 쉬고 싶어.",
                        "작업을 마쳤어. 지금은 좀 쉬는 게 좋을까?",
                    )[local % 4]
                    stems = (
                        ("많이 걸었으니 잠깐 앉아 쉬어도 되겠",),
                        ("물 한 잔 마시고 잠시 쉬는 게 좋겠",),
                        ("정리를 끝냈으니 잠시 쉬어도 좋겠",),
                        ("작업을 마쳤으니 지금은 쉬는 게 좋겠",),
                    )[local % 4]
                    slots = ()
                case unexpected:
                    assert_never(unexpected)
            result.append(Scenario(
                family, "train", index, kind, event, record, question, stems,
                endings, slots, index < 24, index % 10 < 3,
            ))
    return tuple(result)


def diversity(texts: list[str]) -> Row:
    words = [text.split() for text in texts]
    return {
        "rows": len(texts), "distinct_answers": len(set(texts)),
        "word_ngrams": {
            str(n): len({tuple(w[i:i+n]) for w in words for i in range(len(w)-n+1)})
            for n in (1, 2, 3)
        },
        "character_ngrams": {
            str(n): len({t[i:i+n] for t in texts for i in range(len(t)-n+1)})
            for n in (2, 3, 4)
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rag", type=Path, required=True)
    parser.add_argument("--model77", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--dev1", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    sys.path[:0] = [str(args.rag), str(args.model77)]
    from corpus import Persona, Question, Record, chunk_record
    from generate import render_messages as rag_messages
    from prompt import render_messages
    from register_check import register_match
    from retrieval import trace_query
    from retrieval_v2 import ContextConfig, contextual_evidence, evidence_json, restore_context
    from sentence_transformers import SentenceTransformer

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True)
    count = lambda text: len(tokenizer.encode(text, add_special_tokens=False))
    torch.manual_seed(59)
    torch.set_num_threads(4)
    encoder = SentenceTransformer(str(args.rag / ".cache/models/models--nlpai-lab--KURE-v1/snapshots/8b418a58414668e75532ed045c22d9ca018ae2b2"),
                                  device="cpu", trust_remote_code=False)
    encoder.max_seq_length = 512
    authored = cases()
    entries, all_chunks, offsets = [], [], []
    for case in authored:
        persona = Persona(persona_id=case.family, name="가상 인물", display_name="가상 인물",
                          provider_address=None, age=50, relationship="친구", register="해요체")
        paragraphs = [case.record]
        if case.long:
            for index in range(60):
                note = (f"{case.event}의 별도 정리 일지 {index+1}에는 보관표 번호 "
                        f"{70000+case.ordinal*100+index}가 있다. "
                        "이 항목은 탐방 준비 자료를 분류하고 보관 위치를 정리한 별도의 메모다.")
                candidate = Record.model_validate(synthetic_record(case, tuple(paragraphs+[note])))
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
            note="Fresh synthetic DEV2 option B",
            required_slots=case.slots, slot_surface_forms={s: (s,) for s in case.slots})
        offsets.append((len(all_chunks), len(chunks)))
        all_chunks.extend(chunks)
        entries.append((case, persona, record, chunks, question))
    documents = [c.embedding_text() for c in all_chunks]
    queries = [c.question for c in authored]
    if max(len(encoder.tokenizer(t, truncation=False)["input_ids"]) for t in documents+queries) > 512:
        raise DataError("Embedding cap exceeded")
    vectors, queries_v = [
        encoder.encode(texts, batch_size=8, normalize_embeddings=True, convert_to_numpy=True,
                       show_progress_bar=False, prompt="").astype(np.float32)
        for texts in (documents, queries)]
    if not all(np.isfinite(v).all() for v in (vectors, queries_v)):
        raise DataError("Nonfinite embedding")
    system = json.loads(args.baseline.read_text())["system_prompt"]
    ledger, batches, audit = [], [], []
    for index, (case, persona, record, chunks, question) in enumerate(entries):
        start, size = offsets[index]
        trace = restore_context(
            trace_query(question, queries_v[index], vectors[start:start+size], chunks),
            0.5458711981773378, chunks, [record], ContextConfig(k=3, window=3, budget=2000), count)
        for register in REGISTERS:
            target, source = answer(case, register), answer(case, "해요체")
            if content_signature(case, target, register) != content_signature(case, source, "해요체"):
                raise DataError("Changed source facts")
            if not register_match(target, register):
                raise DataError(f"Register mismatch: {target}/{register}")
            meta = metadata(case, register)
            profile = persona.model_copy(update={"speech_register": register,
                                                 "persona_id": meta["persona_id"]})
            for condition in ("B", "C"):
                evidence = trace.evidence if condition == "B" else (contextual_evidence(chunks[0]),)
                # Each unknown subtype has three empty and three incomplete contexts.
                if case.kind == "unanswerable" and (case.ordinal-36) % 6 < 3:
                    evidence = ()
                if case.kind == "ordinary_chat" and condition == "C":
                    evidence = ()
                content = "\n".join(e.content for e in evidence)
                if any(s not in content for s in case.slots):
                    raise DataError(f"Absent supplied slot: {case.family}/{condition}")
                semantic = [m.model_dump() for m in rag_messages(system, profile, case.question, evidence)]
                messages = render_messages(semantic, "r1-reg")
                prefix = tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
                ids = tokenizer.encode(prefix, add_special_tokens=False)
                complete = tokenizer.encode(prefix+target+"<|im_end|>", add_special_tokens=False)
                if complete[:len(ids)] != ids or complete[-1] != 151645 or len(complete) > 4096:
                    raise DataError("Invalid native token boundary/length")
                long = case.long and condition == "B"
                if long and len(complete) <= 2048:
                    raise DataError(f"Long B too short: {case.family}, {len(complete)}")
                row = {
                    **meta, "id": meta["question_id"]+":"+condition, "condition": condition,
                    "messages": messages, "messages_sha256": digest(json.dumps(messages, ensure_ascii=False)),
                    "input_ids": ids, "text": target, "source_answer": source,
                    "target_sha256": digest(target), "source_sha256": digest(source),
                    "question": case.question, "slots": list(case.slots),
                    "archive": [c.model_dump(mode="json") for c in chunks],
                    "evidence": [e.model_dump(mode="json") for e in evidence],
                    "full_tokens": len(complete), "long": long,
                    "template": (case.ordinal-36) % 6 % 4 if case.kind == "unanswerable"
                                else sum(c.kind == case.kind for c in authored[:case.ordinal]) % 4,
                    "unknown_subtype": (case.ordinal-36)//6 if case.kind == "unanswerable" else None,
                }
                ledger.append(row)
                batches.append({"input_ids": torch.tensor([complete]),
                                "attention_mask": torch.ones(1, len(complete), dtype=torch.long),
                                "labels": torch.tensor([[-100]*len(ids)+complete[len(ids):]])})
                audit.append({"id": row["id"], "facts_unchanged": True, "register_checked": True,
                              "signature": list(case.stems), "source_sha256": row["source_sha256"],
                              "target_sha256": row["target_sha256"], "evidence_sha256": digest(content)})
    validation = json.loads((args.dev1/"validation.json").read_text())
    for field in ("family_cluster", "question_id"):
        if {r[field] for r in ledger} & {r[field] for r in validation}:
            raise DataError(f"Validation overlap: {field}")
    if {c.event for c in authored} & {r["question"] for r in validation}:
        raise DataError("Event overlap")
    if len(ledger) != 360 or sum(r["long"] for r in ledger) < 72:
        raise DataError("DEV2 counts invalid")
    for kind, _ in MIX:
        for register in REGISTERS:
            if len({r["template"] for r in ledger if r["type"] == kind and r["register"] == register}) < 4:
                raise DataError("Insufficient templates")
    old = json.loads((args.dev1/"ledger.json").read_text())
    old = [r for r in old if r["dataset_split"] == "train"
           and int(r["family_cluster"].rsplit("-", 1)[1]) < 30]
    summary = {
        "scope": "DEV_ONLY", "families": 60, "rows": 360,
        "bundle_mix": dict(MIX), "facts_unchanged": len(audit), "register_checked": len(audit),
        "validation_sha256": digest((args.dev1/"validation.json").read_text()),
        "family_overlap": 0, "question_overlap": 0,
        "long_rows": sum(r["long"] for r in ledger),
        "max_tokens": max(r["full_tokens"] for r in ledger),
        "dev1_diversity": diversity([r["text"] for r in old]),
        "dev2_diversity": diversity([r["text"] for r in ledger]),
        "templates": {k: sorted({r["template"] for r in ledger if r["type"] == k}) for k, _ in MIX},
        "unknown_evidence": dict(Counter(f'{r["unknown_subtype"]}:{bool(r["evidence"])}'
                                         for r in ledger if r["type"] == "unanswerable")),
        "method": "KURE/retrieval-v2 reset archives; controlled source/ending-only grammar",
        "decision_heldout": "NOT_BUILT_NOT_READ",
    }
    args.output.mkdir(parents=True)
    torch.save({"split": "train", "provenance": "reviewed-synthetic-dev", "batches": batches},
               args.output/"pilot.pt")
    for name, value in (("ledger.json", ledger), ("audit.json", audit),
                        ("sources.json", [asdict(c) for c in authored]), ("summary.json", summary)):
        with (args.output/name).open("x") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
    shutil.copyfile(args.dev1/"validation.json", args.output/"validation.json")
    print("DEV2_DATA_READY "+json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
