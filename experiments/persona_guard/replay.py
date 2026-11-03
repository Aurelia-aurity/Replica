"""Replay saved synthetic replies; stdout JSON only, no model/runtime imports."""

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ai/src"))
from persona_guard import PersonaIdentity, check_reply


def main(argv: list[str] | None = None) -> int:
    """Check frozen expectations and optionally replay an existing ABC dataset."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, nargs="+", default=[
        Path(__file__).with_name("cases.json"),
        Path(__file__).with_name("request_cases.json"),
    ])
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--generations", type=Path)
    args = parser.parse_args(argv)
    if bool(args.corpus) != bool(args.generations):
        parser.error("--corpus and --generations must be supplied together")
    synthetic = []
    cases = []
    for path in args.cases:
        fixture = json.loads(path.read_text(encoding="utf-8"))
        if fixture["synthetic_only"] is not True:
            parser.error("only explicitly synthetic corpora are permitted")
        cases.extend((case, fixture["identities"][case["identity"]])
                     for case in fixture["cases"])
    for case, identity in cases:
        persona = PersonaIdentity(
            tuple(identity["relationship_aliases"]),
            tuple(identity["name_aliases"]), identity["register"],
        )
        result = check_reply(case["output"], persona)
        action = "replace" if result.matched is not None else "pass"
        # Independent oracle: frozen case outcome, not a second matcher pass.
        # Replaced output must contain neither configured identity nor questions
        # and must have the configured register ending.
        ending = {"반말": "나.", "다정한 반말": "나.",
                  "해요체": "나요.", "하게체": "나네."}[persona.register]
        postcheck_ok = (
            result.text != case["output"]
            and result.text.endswith(ending)
            and not any(alias in result.text for alias in
                        (*persona.relationship_aliases, *persona.name_aliases))
            and "?" not in result.text
        ) if case["expected"] == "replace" else (
            result.text.encode("utf-8") == case["output"].encode("utf-8")
        )
        synthetic.append({
            **case, "persona": identity, "guarded_output": result.text,
            "matched": result.matched, "action": action,
            "outcome_ok": action == case["expected"], "postcheck_ok": postcheck_ok,
        })
    sources = [*args.cases, Path(__file__), REPO / "ai/src/persona_guard.py"]
    heldout = []
    if args.corpus:
        corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
        if corpus["synthetic_only"] is not True:
            parser.error("only explicitly synthetic corpora are permitted")
        personas = {p["persona_id"]: p for p in corpus["personas"]}
        questions = {q["question_id"]: q for q in corpus["questions"]}
        seen = set()
        for line in args.generations.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            key = (row["question_id"], row["condition"])
            if key in seen or row["condition"] not in ("A", "B", "C"):
                parser.error("duplicate or non-ABC generation row")
            seen.add(key)
            question = questions[row["question_id"]]
            if row["persona_id"] != question["persona_id"] or row["status"] != "success":
                parser.error("generation identity mismatch or unsuccessful row")
            identity = personas[row["persona_id"]]
            persona = PersonaIdentity(
                tuple(dict.fromkeys((identity["relationship"], identity["display_name"]))),
                (identity["name"],), identity["register"],
            )
            result = check_reply(row["text"], persona)
            heldout.append({
                "question_id": row["question_id"], "condition": row["condition"],
                "persona_id": row["persona_id"], "persona": identity,
                "input": question["question"], "output": row["text"],
                "guarded_output": result.text, "matched": result.matched,
                "unchanged_bytes": result.text.encode("utf-8") == row["text"].encode("utf-8"),
            })
        if seen != {(qid, c) for qid in questions for c in ("A", "B", "C")}:
            parser.error("generations must cover every corpus question once per A/B/C")
        sources.extend((args.corpus, args.generations))
    failures = sum(not r["outcome_ok"] for r in synthetic)
    residuals = sum(not r["postcheck_ok"] for r in synthetic)
    print(json.dumps({
        "evaluation_mode": "self_referral_only_both_living_and_deceased",
        "missing_life_status": "unknown; never inferred from age or relationship",
        "model_calls": 0,
        "sources": [{"path": str(p.resolve()), "sha256": sha256(p.read_bytes()).hexdigest()}
                    for p in sources],
        "summary": {
            "synthetic_rows": len(synthetic),
            "synthetic_expected": dict(Counter(r["expected"] for r in synthetic)),
            "synthetic_expectation_failures": failures,
            "synthetic_independent_postcheck_violations": residuals,
            "heldout_rows": len(heldout),
            "heldout_detections": sum(r["matched"] is not None for r in heldout),
            "heldout_unchanged_bytes": sum(r["unchanged_bytes"] for r in heldout),
        },
        "synthetic": synthetic, "heldout": heldout,
    }, ensure_ascii=False, indent=2))
    return int(bool(failures or residuals))


if __name__ == "__main__":
    raise SystemExit(main())
