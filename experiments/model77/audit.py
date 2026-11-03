"""Tokenizer-only audit and DEV-only 4096-token memory-probe packing."""

import argparse
from copy import deepcopy
import json
from pathlib import Path

from prepare import content_hash, digest, save
from smoke import empty_think_suffix, load_profile


def serialize(tokenizer, messages, profile) -> str:
    rendered = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True, **profile.chat_template_kwargs)
    if profile.empty_think_required and not empty_think_suffix(rendered):
        raise ValueError("Non-thinking template mismatch")
    return rendered


def token_ids(tokenizer, rendered: str) -> list[int]:
    return tokenizer.encode(rendered, add_special_tokens=False, truncation=False)


def pack_stress(tokenizer, row, profile):
    """Select a prefix of repeated original DEV text, never pad decision prompts."""
    cycle = row["context_cycle"] + "\n"
    target = row["target_input_tokens"]
    repeated = cycle * (target * 5 // len(cycle) + 10)
    while len(token_ids(tokenizer, repeated)) < target * 2:
        repeated += repeated

    def candidate(size, separator):
        messages = deepcopy(row["messages"])
        messages[1]["content"] += (
            "\n<dev_memory_context>\n" + repeated[:size] + separator + "</dev_memory_context>")
        rendered = serialize(tokenizer, messages, profile)
        return messages, rendered, token_ids(tokenizer, rendered)

    # Native BPE merges can skip the target with short space-only delimiters.
    # Keep the original candidates first so existing packs remain identical.
    for separator in ("\n", "\n \n", "\n  \n", "\n\t\n", "\n\n", "\n \t\n", "\n\t \n"):
        low, high = 0, len(repeated)
        while low < high:
            middle = (low + high) // 2
            if len(candidate(middle, separator)[2]) < target:
                low = middle + 1
            else:
                high = middle
        # A character can add two BPE tokens; vary only delimiter whitespace.
        for size in range(max(0, low - 16), min(len(repeated), low + 32)):
            messages, rendered, ids = candidate(size, separator)
            if len(ids) == target:
                return {
                    "id": row["id"], "condition": row["condition"], "persona_id": row["persona_id"],
                    "messages": messages, "messages_sha256": content_hash(messages),
                    "input_ids_sha256": content_hash(ids), "input_tokens": len(ids),
                    "rendered_sha256": content_hash(rendered), "min_new_tokens": 160,
                    "context_cycle_sha256": content_hash(cycle),
                }
    raise ValueError(f"No exact 4096-token DEV packing: {row['id']}")


def main() -> None:
    from transformers import AutoTokenizer

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundles", nargs="+", type=Path, required=True)
    parser.add_argument("--tokenizers", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--normal-only", action="store_true",
                        help="Audit normal prompts without packing DEV memory probes")
    args = parser.parse_args()
    report = {"ceiling": 4096, "generation_performed": False, "models": {}}
    profiles = json.loads(Path(__file__).with_name("profiles.json").read_text())
    for name in profiles:
        profile = load_profile(name)
        tokenizer = AutoTokenizer.from_pretrained(
            args.tokenizers / name, local_files_only=True, trust_remote_code=False)
        result = {"splits": {}}
        for path in args.bundles:
            bundle = json.loads(path.read_text())
            rows = [{
                "id": row["id"],
                "tokens": len(token_ids(tokenizer, serialize(tokenizer, row["messages"], profile))),
                "messages_sha256": content_hash(row["messages"]),
            } for row in bundle["rows"]]
            result["splits"][bundle["split"]] = {
                "bundle_sha256": digest(path), "rows": rows,
                "max_tokens": max(r["tokens"] for r in rows),
                "violations": [r for r in rows if r["tokens"] > 4096],
            }
            if bundle["split"] == "dev" and not args.normal_only:
                stress = [pack_stress(tokenizer, row, profile) for row in bundle["stress"]]
                stress_path = args.output / f"stress-{name}.json"
                payload = {"dev_bundle_sha256": digest(path), "rows": stress}
                if stress_path.exists():
                    if json.loads(stress_path.read_text()) != payload:
                        raise ValueError("Previously packed stress file changed")
                else:
                    save(stress_path, payload)
                result["stress_sha256"] = digest(stress_path)
        result["tokenizer_files"] = {
            p.name: digest(p) for p in sorted((args.tokenizers / name).iterdir()) if p.is_file()
        }
        result["template_sha256"] = content_hash(tokenizer.chat_template)
        report["models"][name] = result
        print(f"AUDIT_DONE {name} " + json.dumps({
            split: value["max_tokens"] for split, value in result["splits"].items()}), flush=True)
    save(args.output / "audit.json", report)
    dev_report = deepcopy(report)
    for model in dev_report["models"].values():
        model["splits"] = {"dev": model["splits"]["dev"]}
    save(args.output / "audit-dev.json", dev_report)
    if any(s["violations"] for m in report["models"].values() for s in m["splits"].values()):
        raise SystemExit("Prompt ceiling violations: see audit.json; no generation permitted")


if __name__ == "__main__":
    main()
