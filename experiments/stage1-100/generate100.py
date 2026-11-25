"""Deterministic CPU-only dataset output with draft provenance, never a freeze."""

from hashlib import sha256
import json
from pathlib import Path
from typing import Final

import typer

from synthetic100 import ACTIVITIES, GIVEN_NAMES, OBJECTS, PLACES, SEED, SURNAMES, author

HERE: Final = Path(__file__).resolve().parent
ROOT: Final = HERE.parents[1]


def main(output: Path, seed: int = SEED, namespace: str = "s100-v3") -> None:
    """Write a new directory; all answer/oracle data stays physically separate."""
    authored = author(seed, namespace)
    payloads = {
        name: value.model_dump_json(indent=2, by_alias=True) + "\n"
        for name, value in (
            ("corpus.json", authored.sources), ("plans.json", authored.plan),
            ("identity-ledger.json", authored.identities), ("gold-context.json", authored.gold),
            ("evaluation-sidecar.json", authored.sidecar),
        )
    }
    manifest = {
        "scope": "DEV_ONLY", "status": "DRAFT_NOT_FROZEN", "seed": seed,
        "families": 1320, "questions": 1320, "planned_generations": 3960,
        "pools": {"places": PLACES, "objects": OBJECTS, "activities": ACTIVITIES,
                  "surnames": SURNAMES, "given_names": GIVEN_NAMES},
        "artifacts_sha256": {n: sha256(t.encode()).hexdigest() for n, t in payloads.items()},
        "authoring_code_sha256": {n: sha256((HERE / n).read_bytes()).hexdigest()
                                  for n in ("generate100.py", "synthetic100.py", "schema100.py")},
        "model": "Qwen/Qwen3-8B",
        "model_revision": "b968826d9c46dd6066d109eabc6255188de91218",
        "encoder_revision": "8b418a58414668e75532ed045c22d9ca018ae2b2",
        "prompt": "r1", "output_tokens": 160, "dtype": "bfloat16",
        "decoding": "greedy_nonthinking",
        "provider_normalization": "Selected provider header is canonical 나 for ALL arms; real names in source ledger",
        "sampling": "20 Latin diagonals plus 40 cells, shuffled; activities 41/42; unique shuffled names; one family per question",
        "limitations": "Shared templates/vocabulary do not prove empirical IID; synthetic name coincidence is possible",
    }
    output.mkdir(parents=True, exist_ok=False)
    for name, text in payloads.items():
        with (output / name).open("x", encoding="utf-8") as handle:
            handle.write(text)
    with (output / "manifest.json").open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print("DEV_DRAFT_AUTHORED families=1320 questions=1320 generations=0 judges=0")


if __name__ == "__main__":
    typer.run(main)
