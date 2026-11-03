"""DEV-only ending heuristic and a complete reading sheet; not the frozen judge."""

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from statistics import mean

from prepare import digest, save

QUOTES = re.compile(r'"[^"]*"|“[^”]*”|‘[^’]*’|「[^」]*」|\'[^\']*\'')
FORMAL = re.compile(r"(?:습니다|입니다|습니까|입니까|십시오)$")
HAGE = re.compile(r"(?:네|는가|은가|인가|던가|구먼|더군|군|일세|하세|하게|보게|주게|게나)$")
BANMAL = re.compile(r"(?:아|어|야|지|자|구나|거든|걸|래|을게|할게|응|그래|고마워)$")
META = re.compile(
    r"기록(?:에|을|은|으로|에서|상|이|된|되|하|한|도)|"
    r"자료(?:에|에서|를)|근거(?:에|로)|페르소나|말높임|하게체|해요체")


def endings(text: str) -> list[str]:
    """Ignore quotations; retain nominal fragments as unknown, not confident errors."""
    unquoted = QUOTES.sub("", text)
    return [part.strip().rstrip("…~:;,() ") for part in re.split(r"[.!?\n]+", unquoted)
            if part.strip().rstrip("…~:;,() ")]


def register_match(text: str, target: str) -> bool:
    """Require all assessable unquoted clauses to fit; ambiguous -네 fits both."""
    parts = endings(text)
    if not parts:
        return False
    matches = []
    for part in parts:
        if FORMAL.search(part):
            matches.append(False)
        elif part.endswith(("요", "죠")):
            matches.append(target == "해요체")
        elif HAGE.search(part):
            matches.append(target == "하게체" or
                           (target == "반말" and part.endswith(("네", "군"))))
        elif BANMAL.search(part):
            matches.append(target == "반말")
        else:
            matches.append(False)
    return all(matches)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--variant", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    bundle = json.loads(args.bundle.read_text())
    if bundle["split"] != "dev":
        raise ValueError("This quick check accepts DEV only")
    metadata = {}
    for row in bundle["rows"]:
        system = row["messages"][0]["content"]
        persona = json.loads(system.split("<persona>\n", 1)[1].split("\n</persona>", 1)[0])
        metadata[row["id"]] = {"register": persona["register"],
                               "persona_name": persona["name"]}
    report = {"method": "ending heuristic only; read reading.txt for manual review",
              "bundle_sha256": digest(args.bundle), "runs": {}}
    sheet = []
    for label, root in (("r1", args.baseline), ("r1-reg", args.variant)):
        if root is None:
            continue
        models = {}
        for name in bundle["model_order"]:
            source = root / name / "outputs.jsonl"
            rows = [json.loads(line) for line in source.read_text().splitlines()]
            rows = [row for row in rows if row["phase"] == "normal"]
            if len(rows) != len(metadata) or {r["id"] for r in rows} != set(metadata):
                raise ValueError(f"DEV row coverage mismatch: {source}")
            if any(row["status"] != "success" or not row["text"].strip() for row in rows):
                raise ValueError(f"Failed or empty normal response: {source}")
            details = []
            for row in rows:
                target = metadata[row["id"]]["register"]
                match = register_match(row["text"], target)
                meta = bool(META.search(QUOTES.sub("", row["text"])))
                details.append({"id": row["id"], "register": target,
                                "heuristic_consistent": match, "meta_flag": meta,
                                "generated_tokens": row["generated_tokens"],
                                "characters": len(row["text"]), "text": row["text"]})
                sheet.append(f"{label} | {name} | {target} | {row['id']} | "
                             f"heuristic={int(match)} meta={int(meta)}\n{row['text']}\n")
            denominator = Counter(row["register"] for row in details)
            numerators = Counter(row["register"] for row in details if row["heuristic_consistent"])
            models[name] = {
                "source_sha256": digest(source),
                "registers": {r: {"consistent": numerators[r], "n": denominator[r]}
                              for r in ("반말", "해요체", "하게체")},
                "mean_generated_tokens": mean(r["generated_tokens"] for r in details),
                "mean_characters": mean(r["characters"] for r in details),
                "length_cap_count": sum(r["generated_tokens"] == 160 for r in details),
                "meta_flag_count": sum(r["meta_flag"] for r in details),
                "rows": details,
            }
        report["runs"][label] = models
    save(args.output / "register-check.json", report)
    with (args.output / "reading.txt").open("x", encoding="utf-8") as handle:
        handle.write("\n".join(sheet))
    print(json.dumps({label: {
        name: {k: v for k, v in model.items() if k != "rows"}
        for name, model in models.items()} for label, models in report["runs"].items()},
        ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
