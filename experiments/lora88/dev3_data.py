"""DEV3: reinforce explicit abstention, retaining every DEV2 two-event bundle."""

import argparse
from collections import Counter
import copy
import json
from pathlib import Path
import sys

import torch
from transformers import AutoTokenizer

from .dev2_data import diversity
from .execution import file_hash, save
from .peft_backend import supervised_batch
from .prepare_data import digest
from .synthetic import DataError

SAFE_STEMS = (
    "기록에 없는 내용은 추측해서 말하지 않겠",
    "확인되지 않은 부분은 짐작해서 답하지 않겠",
    "알려진 사실 밖의 내용은 덧붙이지 않겠",
    "근거가 없는 내용은 지어내서 답하지 않겠",
    "확인할 수 없는 부분은 모르는 것으로 남기겠",
    "기록으로 알 수 없는 내용은 추측하지 않겠",
)
ENDINGS = {"반말": "어", "해요체": "어요", "하게체": "네"}


def reinforce(rows: list) -> list:
    """Keep facts and evidence fixed; add an explicit abstention commitment."""
    result = copy.deepcopy(rows)
    for row in result:
        if row["type"] == "unanswerable":
            ordinal = int(row["family_cluster"].rsplit("-", 1)[1]) - 36
            ending = ENDINGS[row["register"]]
            row["text"] += " " + SAFE_STEMS[ordinal % len(SAFE_STEMS)] + ending + "."
            row["source_answer"] += " " + SAFE_STEMS[ordinal % len(SAFE_STEMS)] + "어요."
            row["target_sha256"] = digest(row["text"])
            row["source_sha256"] = digest(row["source_answer"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev2", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--model77", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    sys.path.insert(0, str(args.model77))
    from register_check import register_match

    original = json.loads((args.dev2 / "ledger.json").read_text())
    rows = reinforce(original)
    validation = json.loads((args.dev2 / "validation.json").read_text())
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True)
    old_batches = torch.load(args.dev2 / "pilot.pt", map_location="cpu", weights_only=True)["batches"]
    batches, audit = [], []
    for old, row, old_batch in zip(original, rows, old_batches, strict=True):
        # Independent legacy reconstruction proves every old training tensor.
        prefix = tokenizer.apply_chat_template(
            old["messages"], tokenize=False, add_generation_prompt=True, enable_thinking=False)
        ids = tokenizer.encode(prefix, add_special_tokens=False)
        full = tokenizer.encode(prefix + old["text"] + "<|im_end|>", add_special_tokens=False)
        expected = {
            "input_ids": torch.tensor([full]),
            "attention_mask": torch.ones(1, len(full), dtype=torch.long),
            "labels": torch.tensor([[-100] * len(ids) + full[len(ids):]]),
        }
        if any(not torch.equal(expected[k], old_batch[k]) for k in expected):
            raise DataError("DEV2 old implementation token/mask mismatch")
        batch = supervised_batch(tokenizer, row)
        if not register_match(row["text"], row["register"]):
            raise DataError(f"DEV3 register mismatch: {row['id']}")
        if row["type"] != "unanswerable" and any(
                not torch.equal(getattr(batch, k), old_batch[k]) for k in expected):
            raise DataError("Non-unanswerable training tensor changed")
        if row["evidence"] != old["evidence"] or row["slots"] != old["slots"]:
            raise DataError("Source facts changed")
        row["full_tokens"] = batch.input_ids.numel()
        batches.append({k: getattr(batch, k) for k in expected})
        audit.append({"id": row["id"], "facts_unchanged": True,
                      "register_checked": True, "legacy_token_mask_parity": True})
    for field in ("family_cluster", "question_id"):
        if {r[field] for r in rows} & {r[field] for r in validation}:
            raise DataError(f"Validation overlap: {field}")
    if len(rows) != 360 or sum(r["long"] for r in rows) != 72:
        raise DataError("DEV2 fixed row/long mix changed")
    args.output.mkdir(parents=True)
    torch.save({"split": "train", "provenance": "reviewed-synthetic-dev", "batches": batches},
               args.output / "pilot.pt")
    for name, value in (("ledger.json", rows), ("audit.json", audit)):
        save(args.output / name, value)
    for name in ("validation.json", "sources.json"):
        (args.output / name).write_bytes((args.dev2 / name).read_bytes())
    save(args.output / "summary.json", {
        "scope": "DEV_ONLY", "families": 60, "rows": 360, "long_rows": 72,
        "max_tokens": max(r["full_tokens"] for r in rows),
        "unchanged_nonunknown_rows": 252, "reinforced_unknown_rows": 108,
        "mix": dict(Counter(r["type"] for r in rows)),
        "unknown_evidence": dict(Counter(str(bool(r["evidence"])) for r in rows
                                         if r["type"] == "unanswerable")),
        "dev2_diversity": diversity([r["text"] for r in original]),
        "dev3_diversity": diversity([r["text"] for r in rows]),
        "legacy_token_mask_parity": 360, "facts_unchanged": 360, "register_checked": 360,
        "validation_sha256": file_hash(args.output / "validation.json"),
        "family_overlap": 0, "question_overlap": 0,
        "decision_heldout": "NOT_BUILT_NOT_READ",
        "change": "No rebalance: retain all 360 DEV2 rows and facts; increase explicit safe-abstention supervised tokens only.",
    })
    print("DEV3_DATA_READY", flush=True)


if __name__ == "__main__":
    main()
