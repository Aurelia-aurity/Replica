"""Build shuffled condition-blind judge/human inputs after a complete generation run."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import random
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "source"))
from run_prompt_exp import load_generations, load_items  # noqa: E402

HUMAN_FIELDS = ("naturalness", "instructions", "usefulness", "style_adherence")


def response_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def assemble(rows, seed: int = 58):
    """Keep the same shuffled bid for judge, author and the separate unblinding key."""
    items = {(i.suite, i.case_id): i for i in load_items()}
    eligible = [r for r in rows if r["suite"] in ("probe", "probe_v2", "human")]
    random.Random(seed).shuffle(eligible)
    blind, human, key = [], [], {}
    for n, row in enumerate(eligible, 1):
        bid = f"B{n:04d}"
        source = items[(row["suite"], row["case_id"])]
        case = json.loads(source.metadata)
        text = row.get("text", "")
        common = {"bid": bid, "input": source.text, "response": text,
                  "status": row["status"], "source_response_sha256": response_hash(text)}
        blind.append(dict(common, oracle=case))
        human.append(common)
        key[bid] = {"model": row["model"], "condition": row["condition"],
                    "suite": row["suite"], "case_id": row["case_id"],
                    "source_response_sha256": response_hash(text)}
    return blind, human, key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", type=int, choices=(1, 2, 3, 4, 5, 6, 7), default=1)
    parser.add_argument("--generations", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.generations is None:
        args.generations = HERE.parent / ("generations.jsonl" if args.round == 1
                                         else f"generations-r{args.round}.jsonl")
    if args.output is None:
        args.output = HERE / f"blind-{args.round:03d}"
    seed = 58 if args.round == 1 else 5800 + args.round
    if not args.generations.exists():
        print("PENDING: generate responses before building blind files.")
        return 2
    rows = load_generations(args.generations, args.round)
    assert all(r["status"] == "success" for r in rows), "Resolve failed generation attempts before grading"
    blind, human, key = assemble(rows, seed)
    # Exclusive directory, so reruns cannot silently overwrite completed grades.
    args.output.mkdir()
    for filename, value in (("blind-items.json", blind), ("human-items.json", human),
                            ("blind-key-DO-NOT-OPEN.json", key)):
        (args.output / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
    profile = json.loads((HERE.parent / "source/style_profile.json").read_text(encoding="utf-8"))
    (args.output / "target-style.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
                                                 encoding="utf-8")
    with (args.output / "grading-sheet.csv").open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("bid", "source_response_sha256", *HUMAN_FIELDS, "note"))
        for item in human:
            writer.writerow((item["bid"], item["source_response_sha256"], "", "", "", "", ""))
    print(f"BLIND_DONE judge={len(blind)} human={len(human)} key={len(key)} seed={seed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
