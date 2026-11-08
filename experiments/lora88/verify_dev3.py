"""CPU preflight in the authorized new environment before any GPU model load."""

import argparse
import json
from pathlib import Path

import torch
from transformers import AutoTokenizer

from .execution import save, verify_seal
from .peft_backend import supervised_batch
from .test_dev3 import peft_cpu_parity


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    verify_seal(args.root)
    rows = json.loads((args.root / "data/ledger.json").read_text())
    batches = torch.load(args.root / "data/pilot.pt", map_location="cpu", weights_only=True)["batches"]
    tokenizer = AutoTokenizer.from_pretrained(
        "/abr/coss40/replica-models/Qwen3-8B", local_files_only=True)
    for row, old in zip(rows, batches, strict=True):
        actual = supervised_batch(tokenizer, row)
        for name in ("input_ids", "attention_mask", "labels"):
            torch.testing.assert_close(getattr(actual, name), old[name], atol=0, rtol=0)
    peft_cpu_parity(args.root / "cpu-parity-adapter")
    save(args.root / "CPU_PARITY.json", {
        "rows": len(rows), "tokenization_label_masks_exact": True,
        "peft_old_initialization_training_reload_merge": "PASS",
        "device": "cpu", "decision_heldout": "NOT_BUILT_NOT_READ",
    })
    print("DEV3_CPU_PARITY_PASS", flush=True)


if __name__ == "__main__":
    main()
