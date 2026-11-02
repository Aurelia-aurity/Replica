"""Run the agent grounding judge over blind items in fixed batches.

Usage: python3 judge/run_judge.py --blind judge/blind-001 [--model sonnet] [--batch 12] [--jobs 4]
Writes judge/blind-001/judge-raw/*.txt and judge/blind-001/judged.json.
"""
import argparse
import concurrent.futures as cf
import json
import re
import subprocess
from pathlib import Path

FIELDS = ("grounding_1to5", "contradiction", "premise_accepted", "fabricated",
          "core_correct", "over_refusal", "note")


def parse(text, bids):
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("no JSON object")
    data = json.loads(match.group(0))
    if set(data) != set(bids):
        raise ValueError(f"bid mismatch {sorted(set(bids) ^ set(data))}")
    for bid, row in data.items():
        if set(FIELDS) - set(row):
            raise ValueError(f"{bid} missing fields")
        if row["grounding_1to5"] not in (1, 2, 3, 4, 5):
            raise ValueError(f"{bid} grounding out of range")
    return data


def run_batch(model, rubric, batch, raw_dir, index):
    bids = [item["bid"] for item in batch]
    prompt = rubric + "\n\n# 판정할 항목\n" + json.dumps(batch, ensure_ascii=False, indent=1)
    last = None
    for attempt in range(2):
        proc = subprocess.run(["claude", "-p", "--model", model], input=prompt,
                              capture_output=True, text=True, timeout=900)
        (raw_dir / f"batch-{index:03d}-try{attempt}.txt").write_text(proc.stdout, encoding="utf-8")
        try:
            return index, parse(proc.stdout, bids)
        except (ValueError, json.JSONDecodeError) as exc:
            last = exc
    raise RuntimeError(f"batch {index} failed: {last}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blind", type=Path, required=True)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--batch", type=int, default=12)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    rubric = (here / "rubric_grounding.txt").read_text(encoding="utf-8")
    items = json.loads((args.blind / "blind-items.json").read_text(encoding="utf-8"))
    raw_dir = args.blind / "judge-raw"
    raw_dir.mkdir(exist_ok=True)
    batches = [items[i:i + args.batch] for i in range(0, len(items), args.batch)]
    judged = {}
    with cf.ThreadPoolExecutor(args.jobs) as pool:
        for index, result in pool.map(lambda pair: run_batch(args.model, rubric, pair[1], raw_dir, pair[0]),
                                      enumerate(batches)):
            judged.update(result)
            print(f"batch {index} ok ({len(result)})", flush=True)
    if len(judged) != len(items):
        raise SystemExit(f"judged {len(judged)} of {len(items)}")
    (args.blind / "judged.json").write_text(json.dumps(judged, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"JUDGE_DONE {len(judged)} model={args.model}")


if __name__ == "__main__":
    main()
