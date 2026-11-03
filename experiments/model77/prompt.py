"""Apply an opt-in register block to frozen r1 messages, without retrieval."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
from typing import Literal, TypedDict

from prepare import content_hash, digest, save

PromptVersion = Literal["r1", "r1-reg"]
TEMPLATE_PATH = Path(__file__).with_name("register-template.json")


class Message(TypedDict):
    role: str
    content: str


def render_messages(messages: list[Message], prompt_version: PromptVersion = "r1") -> list[Message]:
    """Preserve r1 exactly; select the same register rule for every model."""
    match prompt_version:
        case "r1":
            return deepcopy(messages)
        case "r1-reg":
            template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
            result = deepcopy(messages)
            system = result[0]["content"]
            persona = json.loads(system.split("<persona>\n", 1)[1].split("\n</persona>", 1)[0])
            register = persona["register"]
            key = template["aliases"].get(register, register)
            rule = template["registers"][key]
            result[0]["content"] += "\n\n" + template["template"].format(
                register=register, register_rule=rule)
            return result
        case _:
            raise ValueError(f"Unknown prompt version: {prompt_version}")


def variant_bundle(bundle, prompt_version: PromptVersion = "r1"):
    """Retain evidence, schedule and warmup identities; hash only changed messages."""
    result = deepcopy(bundle)
    if prompt_version == "r1":
        return result
    if bundle.get("prompt_version", "r1") != "r1":
        raise ValueError("Variant input must be an unmodified r1 bundle")
    for phase in ("rows", "warmups", "stress"):
        for row in result[phase]:
            row["messages"] = render_messages(row["messages"], prompt_version)
            row["messages_sha256"] = content_hash(row["messages"])
    result["prompt_version"] = prompt_version
    result["register_template_sha256"] = digest(TEMPLATE_PATH)
    result["r1_bundle_content_sha256"] = content_hash(bundle)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--prompt-version", choices=("r1", "r1-reg"), default="r1")
    args = parser.parse_args()
    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    save(args.output, variant_bundle(bundle, args.prompt_version))
    print(json.dumps({"bundle_sha256": digest(args.output),
                      "prompt_version": args.prompt_version}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
