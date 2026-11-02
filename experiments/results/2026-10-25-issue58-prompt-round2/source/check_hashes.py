"""Compare all 92 shared guarded token hashes with #55, never decoded text."""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PREVIOUS = HERE.parents[2] / "results/2026-10-22-issue55-prompt-experiment/generations.jsonl"


def compare(current: Path, previous: Path = PREVIOUS):
    def guarded_rows(path):
        result = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["model"] == "qwen3-8b" and row["prompt"] == "guarded" and row["suite"] != "probe_v2":
                key = (row["suite"], row["case_id"])
                assert key not in result, f"Duplicate guarded item: {key}"
                result[key] = row
        return result

    old, new = guarded_rows(previous), guarded_rows(current)
    assert len(old) == 92
    missing, extra = sorted(old.keys() - new.keys()), sorted(new.keys() - old.keys())
    mismatches = []
    matched = 0
    for key in sorted(old.keys() & new.keys()):
        a, b = old[key], new[key]
        same = (a["status"] == b["status"] == "success"
                and a["revision"] == b["revision"]
                and a["generated_ids_sha256"] == b.get("generated_ids_sha256"))
        matched += same
        if not same:
            mismatches.append({"suite": key[0], "case_id": key[1],
                               "previous_hash": a.get("generated_ids_sha256"),
                               "current_hash": b.get("generated_ids_sha256"),
                               "previous_revision": a["revision"], "current_revision": b["revision"],
                               "current_status": b["status"]})
    return {"expected": 92, "matched": matched, "missing": missing, "extra": extra,
            "mismatches": mismatches, "complete_match": matched == 92 and not extra}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generations", type=Path, default=HERE.parent / "generations.jsonl")
    args = parser.parse_args()
    if not args.generations.exists():
        print("PENDING: generations.jsonl does not exist; no reproduction claim.")
        return 2
    result = compare(args.generations)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["complete_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
