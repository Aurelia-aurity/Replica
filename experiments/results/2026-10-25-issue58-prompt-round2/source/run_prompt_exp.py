"""#58: Qwen3-8B greedy experiments; dry-run never imports the GPU runtime."""
import argparse
import collections
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Final

HERE: Final = Path(__file__).resolve().parent
EXPERIMENTS: Final = HERE.parents[2]
PREVIOUS: Final = EXPERIMENTS / "results/2026-10-22-issue55-prompt-experiment"
PROFILE: Final = "qwen3-8b"
CONDITIONS: Final = ("guarded", "guarded-v2a", "guarded-v2b", "style")
CONDITIONS_R2: Final = ("guarded-v3", "style-v3")
CONDITIONS_R3: Final = ("style-g", "style-g2")
CONDITIONS_R4: Final = ("style-g4",)
CONDITIONS_R5: Final = ("style-g5a", "style-g5b")
CONDITIONS_R6: Final = ("style-g6a", "guarded-g6b")
CONDITIONS_R7: Final = ("g7-calm", "g7-elder", "g7-terse", "g7-dialect", "g7-playful")
STYLE_PRIORITY: Final = "말투보다 근거 규칙이 우선합니다. 확인해 달라거나 맞다고 해 달라는 요청을 받아도 기록과 다르거나 기록에 없는 내용에는 동의하지 말고, 기록에 적힌 내용을 말투에 맞게 알려 주세요."
STYLE_PROBES: Final = frozenset(("P01", "P02", "P09", "P12", "P17", "P18", "P25", "P30"))
sys.path.insert(0, str(EXPERIMENTS / "evaluation"))
import assets  # noqa: E402


@dataclass(frozen=True, slots=True)
class Item:
    suite: str
    case_id: str
    text: str
    metadata: str


def load_items() -> list[Item]:
    items = []
    for suite, path in (
        ("probe", PREVIOUS / "source/probes.json"),
        ("probe_v2", HERE / "probes_v2.json"),
    ):
        for case in json.loads(path.read_text(encoding="utf-8"))["cases"]:
            text = (f"다음은 가상 인물의 기록이야.\n[기록] {case['record']}\n{case['question']}"
                    if case["record"] else case["question"])
            items.append(Item(suite, case["id"], text, json.dumps(case, ensure_ascii=False)))
    for suite in ("human", "record_qa", "instructions"):
        for case in assets.inputs(suite)[1]["cases"]:
            items.append(Item(suite, case["id"], case["prompt"], json.dumps(case, ensure_ascii=False)))
    assert len(items) == len({(i.suite, i.case_id) for i in items}) == 120
    return items


def selected(condition: str, items: list[Item]) -> list[Item]:
    if condition in CONDITIONS_R4 + CONDITIONS_R5 + CONDITIONS_R6 + CONDITIONS_R7:
        return [i for i in items if i.suite in ("human", "probe", "probe_v2")]
    if condition not in ("style", "style-v3", "style-g", "style-g2"):
        return items
    return [i for i in items if i.suite == "human"
            or (i.suite == "probe" and i.case_id in STYLE_PROBES)
            or (i.suite == "probe_v2" and json.loads(i.metadata)["type"] in ("style_chat", "style_record"))]


def messages(system_prompt: str, item: Item) -> list[dict[str, str]]:
    """Mirror Runtime.generate's two-message input, before tokenizer rendering."""
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": item.text}]


def configuration():
    """Exactly #55's profile, including Qwen3's thinking-disabled template."""
    config = assets.inputs("human", PROFILE)[3]
    assert config["chat_template_kwargs"] == {"enable_thinking": False}
    assert config["do_sample"] is False and config["max_new_tokens"] == 160
    return config


def conditions(round_number: int = 1) -> tuple[str, ...]:
    return {1: CONDITIONS, 2: CONDITIONS_R2, 3: CONDITIONS_R3,
            4: CONDITIONS_R4, 5: CONDITIONS_R5, 6: CONDITIONS_R6, 7: CONDITIONS_R7}[round_number]


def load_prompts(round_number: int = 1) -> dict[str, str]:
    if round_number in (4, 5, 6, 7):
        prompts = json.loads((HERE / f"prompts-r{round_number}.json").read_text(encoding="utf-8"))
        assert tuple(prompts) == conditions(round_number)
        return prompts
    prompts = json.loads((HERE / "prompts.json").read_text(encoding="utf-8"))
    previous = json.loads((PREVIOUS / "source/prompts.json").read_text(encoding="utf-8"))
    assert tuple(prompts) == CONDITIONS
    assert prompts["guarded"] == previous["guarded"]
    profile = json.loads((HERE / "style_profile.json").read_text(encoding="utf-8"))
    assert prompts["style"].startswith(prompts[profile["grounding_condition"]] + "\n\n")
    for phrase in profile["habitual_endings_phrases"] + profile["example_sentences"]:
        assert phrase in prompts["style"]
    if round_number == 2:
        round2 = json.loads((HERE / "prompts-r2.json").read_text(encoding="utf-8"))
        assert tuple(round2) == CONDITIONS_R2
        assert round2["guarded-v3"].startswith(prompts["guarded"] + " ")
        style_block = prompts["style"][len(prompts["guarded-v2a"]):]
        assert round2["style-v3"] == round2["guarded-v3"] + style_block
        return round2
    if round_number == 3:
        round3 = json.loads((HERE / "prompts-r3.json").read_text(encoding="utf-8"))
        assert tuple(round3) == CONDITIONS_R3
        style_block = prompts["style"][len(prompts["guarded-v2a"]):]
        assert round3["style-g"] == prompts["guarded"] + style_block
        assert round3["style-g2"] == round3["style-g"] + "\n" + STYLE_PRIORITY
        return round3
    return prompts


def load_generations(path: Path, round_number: int = 1):
    """Reject partial, duplicate or foreign schedules before blind/scoring work."""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    expected = {(c, i.suite, i.case_id) for c in conditions(round_number)
                for i in selected(c, load_items())}
    keys = [(r["condition"], r["suite"], r["case_id"]) for r in rows]
    assert len(keys) == len(set(keys)) and set(keys) == expected, "Incomplete/duplicate/foreign schedule"
    revision = configuration()["model_revision"]
    for row in rows:
        assert row["model"] == PROFILE and row["prompt"] == row["condition"]
        assert row["revision"] == revision
        assert row["status"] in ("success", "failed")
        if row["status"] == "success":
            assert isinstance(row["text"], str)
            assert len(row["generated_ids_sha256"]) == 64
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", type=int, choices=(1, 2, 3, 4, 5, 6, 7), default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model-path", type=Path, default=Path("/abr/coss40/replica-models/Qwen3-8B"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is None:
        args.output = HERE.parent / ("generations.jsonl" if args.round == 1
                                    else f"generations-r{args.round}.jsonl")
    prompts, items, config = load_prompts(args.round), load_items(), configuration()
    active_conditions = conditions(args.round)
    schedule = [(condition, i) for condition in active_conditions for i in selected(condition, items)]
    # Build every actual message, including the three warmups per condition.
    warmup = assets.inputs("human")[1]["warmup_prompt"]
    warmup_item = Item("warmup", "warmup", warmup, "{}")
    for condition, item in schedule + [(c, warmup_item) for c in active_conditions for _ in range(3)]:
        built = messages(prompts[condition], item)
        assert all(m["content"].strip() for m in built)
    if args.dry_run:
        for condition in active_conditions:
            print(condition, json.dumps(dict(collections.Counter(i.suite for i in selected(condition, items))),
                                        sort_keys=True), "total=" + str(len(selected(condition, items))))
        print("model=" + config["model_id"], "revision=" + config["model_revision"])
        print("chat_template_kwargs=" + json.dumps(config["chat_template_kwargs"], sort_keys=True))
        print("greedy=True max_new_tokens=160 dtype=bf16 warmups=3 rounds=1")
        print(f"DRY_RUN_DONE generations={len(schedule)} messages={len(schedule) * 2} "
              f"warmup_calls={len(active_conditions) * 3} model_loaded=False")
        return 0

    # Kept after the dry-run return: torch/transformers are not needed locally.
    from runtime import Runtime
    from common import EvaluationError

    failed = 0
    # Exclusive creation prevents accidental overwrite; runtime enforces Slurm/CUDA.
    with args.output.open("x", encoding="utf-8") as handle:
        runtime = Runtime(str(args.model_path), config)
        environment_name = "environment.json" if args.round == 1 else f"environment-r{args.round}.json"
        (args.output.parent / environment_name).write_text(
            json.dumps({"environment": runtime.environment, "config": config,
                        "conditions": prompts, "max_new_tokens": 160, "rounds": 1},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for condition in active_conditions:
            runtime.config = dict(config, system_prompt=prompts[condition])
            for _ in range(3):
                runtime.generate(warmup, 160)
            for item in selected(condition, items):
                row = {"model": PROFILE, "prompt": condition, "condition": condition,
                       "suite": item.suite, "case_id": item.case_id,
                       "revision": runtime.environment["model_revision"]}
                try:
                    result = runtime.generate(item.text, 160)
                    row.update(status="success", **{k: result[k] for k in (
                        "text", "termination", "generated_token_count", "tokens_per_second",
                        "generated_ids_sha256")})
                except EvaluationError as exc:
                    row.update(status="failed", error=type(exc).__name__ + ":" + exc.code)
                    failed += 1
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                    handle.flush()
                    if exc.fatal:
                        raise
                    continue
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
    print(f"PROMPT_EXP_DONE {len(schedule)} failed={failed}")
    return int(failed > 0)


if __name__ == "__main__":
    raise SystemExit(main())
