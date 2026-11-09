"""Build and tokenizer-audit one ROUND 3 arm, without generation or retrieval."""

import argparse
import json
from pathlib import Path

from arms import ARMS, schedule_hash
from audit import pack_stress, serialize, token_ids
from decision import validate_bundle
from prepare import content_hash, digest, save
from prompt import template_path, variant_bundle
from smoke import load_profile


def build(args) -> None:
    """Apply an explicitly frozen template once; audit both native tokenizers."""
    from transformers import AutoTokenizer

    model, version = ARMS[args.arm]
    if digest(template_path(version)) != args.template_sha256:
        raise ValueError("Supplied final template SHA does not match source")
    sources = [json.loads(p.read_text()) for p in (args.bundle, args.dev_bundle)]
    if sources[0]["split"] != "heldout" or sources[1]["split"] != "dev":
        raise ValueError("Expected sealed heldout and fixed DEV r1 bundles")
    if len(sources[0]["rows"]) != 480 or sources[0]["schedule_seed"] != 77:
        raise ValueError("Expected the sealed 480-row seed-77 schedule")
    if args.output.exists():
        raise FileExistsError(args.output)
    bundles = []
    for original in sources:
        bundle = variant_bundle(original, version)
        bundle.update(arm=args.arm, run_id="decision-v3", arm_order=list(ARMS),
                      model_order=[entry[0] for entry in ARMS.values()])
        for phase in ("rows", "warmups", "stress"):
            for row in bundle[phase]:
                row.update(arm=args.arm, run_id="decision-v3")
        validate_bundle(bundle)
        if bundle["split"] == "heldout" and schedule_hash(bundle) != schedule_hash(original):
            raise ValueError("Prompt transformation changed schedule/evidence")
        bundles.append(bundle)
    heldout, dev = bundles
    save(args.output / "bundle.json", heldout)
    save(args.output / "dev/bundle.json", dev)
    report = {"arm": args.arm, "run_id": "decision-v3", "ceiling": 4096,
              "generation_performed": False, "models": {}}
    for name in ("Qwen3-8B", "Qwen3-14B"):
        profile = load_profile(name)
        directory = args.tokenizers / name
        tokenizer = AutoTokenizer.from_pretrained(
            directory, local_files_only=True, trust_remote_code=False)
        result = {"splits": {}}
        for bundle, relative in ((heldout, "bundle.json"), (dev, "dev/bundle.json")):
            rows = [{
                "arm": args.arm, "id": row["id"],
                "tokens": len(token_ids(tokenizer, serialize(tokenizer, row["messages"], profile))),
                "messages_sha256": row["messages_sha256"],
                "rendered_sha256": content_hash(serialize(tokenizer, row["messages"], profile)),
            } for row in bundle["rows"]]
            warmups = [
                len(token_ids(tokenizer, serialize(tokenizer, row["messages"], profile)))
                for row in bundle["warmups"]
            ]
            violations = [r["id"] for r in rows if r["tokens"] > 4096]
            if any(count > 4096 for count in warmups):
                violations.append("warmup")
            result["splits"][bundle["split"]] = {
                "bundle_sha256": digest(args.output / relative), "rows": rows,
                "max_tokens": max(r["tokens"] for r in rows),
                "warmup_tokens": warmups, "violations": violations,
            }
            if violations:
                raise ValueError(f"4096-token ceiling violation: {name}")
        stress = [dict(pack_stress(tokenizer, row, profile), arm=args.arm,
                       run_id="decision-v3") for row in dev["stress"]]
        stress_path = args.output / f"audit/stress-{name}.json"
        save(stress_path, {"arm": args.arm, "run_id": "decision-v3",
                           "dev_bundle_sha256": digest(args.output / "dev/bundle.json"),
                           "rows": stress})
        result["stress_sha256"] = digest(stress_path)
        result["tokenizer_files"] = {
            p.name: digest(p) for p in sorted(directory.iterdir()) if p.is_file()
        }
        result["template_sha256"] = content_hash(tokenizer.chat_template)
        report["models"][name] = result
        print(f"AUDIT_DONE {args.arm} {name} " + json.dumps({
            key: value["max_tokens"] for key, value in result["splits"].items()}), flush=True)
    save(args.output / "audit/audit.json", report)
    save(args.output / "manifest.json", {
        "arm": args.arm, "run_id": "decision-v3", "model": model,
        "prompt_version": version, "register_template_sha256": args.template_sha256,
        "source_bundle_sha256": digest(args.bundle),
        "source_dev_bundle_sha256": digest(args.dev_bundle),
        "bundle_sha256": digest(args.output / "bundle.json"),
        "dev_bundle_sha256": digest(args.output / "dev/bundle.json"),
        "audit_sha256": digest(args.output / "audit/audit.json"),
        "schedule_sha256": schedule_hash(heldout),
        "schedule_seed": 77, "arm_order": list(ARMS),
        "messages_sha256": {row["id"]: row["messages_sha256"] for row in heldout["rows"]},
        "generation_performed": False,
    })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", required=True, choices=tuple(ARMS))
    parser.add_argument("--template-sha256", required=True)
    for flag in ("bundle", "dev-bundle", "tokenizers", "output"):
        parser.add_argument("--" + flag, type=Path, required=True)
    build(parser.parse_args())


if __name__ == "__main__":
    main()
