"""DEV-only context tuning, then one frozen TEST evaluation; no generation/judge."""

from collections.abc import Callable, Sequence
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from statistics import median
from typing import Final

from pydantic import JsonValue, RootModel
from tokenizers import Tokenizer
import typer

from corpus import ROOT, Corpus, FrozenModel, Split, load_corpus, validate
from generate import PersonaContract, Retrieved
from retrieval import Calibration, Trace, calibrate, select_evidence
from retrieval_v2 import ContextConfig, evidence_json, missing_slots, restore_context

QWEN_REVISION: Final = "b968826d9c46dd6066d109eabc6255188de91218"
TOKENIZER_DIR: Final = ROOT / ".cache/qwen3-8b-tokenizer"


class OfflineMetrics(FrozenModel):
    questions: int
    factual: int
    all_slots_covered: int
    slot_hits: int
    required_slots: int
    unanswerable: int
    unanswerable_empty: int
    ordinary: int
    ordinary_empty: int
    factual_empty: int
    median_evidence_tokens: float
    max_evidence_tokens: int
    per_question: dict[str, dict[str, int | bool | tuple[str, ...]]]


def load_tokenizer() -> Tokenizer:
    """Download tokenizer files only at the same revision as the GPU baseline."""
    if not (TOKENIZER_DIR / "tokenizer.json").exists():
        from huggingface_hub import snapshot_download

        snapshot_download(
            "Qwen/Qwen3-8B", revision=QWEN_REVISION,
            local_dir=TOKENIZER_DIR,
            allow_patterns=["tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt"],
        )
    tokenizer = Tokenizer.from_file(str(TOKENIZER_DIR / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


def metrics(traces: Sequence[Trace], count_tokens: Callable[[str], int]) -> OfflineMetrics:
    """Count surfaces in evidence content, and tokens in the full rendered JSON."""
    factual = [t for t in traces if t.question.required_slots]
    absent = [t for t in traces if t.question.type == "unanswerable"]
    ordinary = [t for t in traces if t.question.type == "ordinary_chat"]
    per_question = {
        t.question.question_id: {
            "missing_slots": missing_slots(t.question, t.evidence),
            "tokens": count_tokens(evidence_json(t.evidence)),
            "empty": not t.evidence,
        }
        for t in traces
    }
    tokens = [count_tokens(evidence_json(t.evidence)) for t in traces]
    return OfflineMetrics(
        questions=len(traces), factual=len(factual),
        all_slots_covered=sum(not missing_slots(t.question, t.evidence) for t in factual),
        slot_hits=sum(
            len(t.question.required_slots) - len(missing_slots(t.question, t.evidence))
            for t in factual
        ),
        required_slots=sum(len(t.question.required_slots) for t in factual),
        unanswerable=len(absent), unanswerable_empty=sum(not t.evidence for t in absent),
        ordinary=len(ordinary), ordinary_empty=sum(not t.evidence for t in ordinary),
        factual_empty=sum(not t.evidence for t in factual),
        median_evidence_tokens=float(median(tokens)), max_evidence_tokens=max(tokens),
        per_question=per_question,
    )


def export(traces: Sequence[Trace], corpus: Corpus) -> list[Retrieved]:
    """Use the original export contract so the GPU renderer consumes both arms."""
    personas = {p.persona_id: p for p in corpus.personas}
    return [Retrieved(
        question_id=t.question.question_id,
        persona_id=t.question.persona_id,
        persona=PersonaContract(
            display_name=personas[t.question.persona_id].display_name,
            provider_address=personas[t.question.persona_id].provider_address,
        ),
        message=t.question.question,
        predicted_no_evidence=t.predicted_no_evidence,
        evidence=t.evidence,
        topk_evidence=t.topk_evidence,
    ) for t in traces]


def write_json(path: Path, payload: JsonValue) -> None:
    """Never replace a result, even when a previous invocation was interrupted."""
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main(
    source: Path = ROOT / "results/retrieval-v2/kure_old",
    output: Path = ROOT / "results/retrieval-v2",
) -> None:
    corpus = load_corpus()
    chunks = validate(corpus)
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest["sha256"]["corpus.json"] != sha256((ROOT / "corpus.json").read_bytes()).hexdigest():
        raise ValueError("source run does not match corrected corpus")
    lock = json.loads((ROOT / "model-lock.json").read_text(encoding="utf-8"))["kure"]
    if manifest["encoder"] != lock:
        raise ValueError("source must use the pinned KURE revision")
    tokenizer = load_tokenizer()

    @lru_cache(maxsize=10000)
    def count_tokens(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    raw = RootModel[dict[Split, list[Trace]]].model_validate_json(
        (source / "traces.json").read_text(encoding="utf-8")
    ).root
    calibration = calibrate(raw["dev"])
    frozen = Calibration.model_validate_json((source / "calibration.json").read_text())
    if calibration != frozen:
        raise ValueError("DEV calibration replay mismatch")
    threshold = calibration.threshold
    # All candidates and tie-breaks fixed in code before looking at TEST.
    candidates = []
    for k in (3, 4, 5):
        for window in (1, 2, 3):
            for budget in (1500, 1750, 2000):
                config = ContextConfig(k=k, window=window, budget=budget)
                traces = [
                    restore_context(t, threshold, chunks, corpus.records, config, count_tokens)
                    for t in raw["dev"]
                ]
                score = metrics(traces, count_tokens)
                candidates.append((config, score))
    eligible = [(c, m) for c, m in candidates if m.median_evidence_tokens <= 1500]
    chosen, dev_metrics = min(eligible, key=lambda pair: (
        -pair[1].all_slots_covered, -pair[1].slot_hits,
        pair[1].median_evidence_tokens, pair[1].max_evidence_tokens,
        pair[0].k, pair[0].window, pair[0].budget,
    ))
    destination = output / "kure_v2"
    destination.mkdir(parents=True, exist_ok=False)
    write_json(destination / "dev-selection.json", {
        "split": "dev",
        "policy": "median<=1500; max complete questions; max slots; min median/max tokens; min k/window/budget",
        "chosen": chosen.model_dump(),
        "candidates": [{"config": c.model_dump(), "metrics": m.model_dump(exclude={"per_question"})}
                       for c, m in candidates],
    })
    write_json(destination / "calibration.json", calibration.model_dump(mode="json"))
    print(f"FROZEN DEV config={chosen.model_dump()} coverage={dev_metrics.all_slots_covered}/30", flush=True)
    # TEST restoration and its annotations are accessed only after DEV choice is persisted.
    old = {split: [select_evidence(t, threshold) for t in traces] for split, traces in raw.items()}
    restored = {
        split: [restore_context(t, threshold, chunks, corpus.records, chosen, count_tokens)
                for t in traces]
        for split, traces in raw.items()
    }
    write_json(destination / "evidence.json", {
        split: [row.model_dump(mode="json") for row in export(traces, corpus)]
        for split, traces in restored.items()
    })
    write_json(destination / "traces.json", {
        split: [t.model_dump(mode="json") for t in traces] for split, traces in restored.items()
    })
    summaries = {
        name: {
            split: metrics(traces, count_tokens).model_dump(mode="json")
            for split, traces in (
                ("dev", values["dev"]), ("test", values["test"]),
                ("all", [*values["dev"], *values["test"]]),
            )
        }
        for name, values in (("B_old", old), ("B_v2", restored))
    }
    b_old, b_v2 = summaries["B_old"], summaries["B_v2"]
    criteria = {
        "R1": {"actual": b_v2["all"]["all_slots_covered"], "minimum": 54,
               "pass": b_v2["all"]["all_slots_covered"] >= 54},
        "R2": {"actual": b_v2["test"]["all_slots_covered"], "minimum": 27,
               "pass": b_v2["test"]["all_slots_covered"] >= 27},
        "R3": {"actual": b_v2["test"]["unanswerable_empty"],
               "minimum": b_old["test"]["unanswerable_empty"],
               "pass": b_v2["test"]["unanswerable_empty"] >= b_old["test"]["unanswerable_empty"]},
        "R4": {"actual": b_v2["all"]["ordinary_empty"], "minimum": b_old["all"]["ordinary_empty"],
               "pass": b_v2["all"]["ordinary_empty"] >= b_old["all"]["ordinary_empty"]},
        "R5": {"actual": b_v2["all"]["median_evidence_tokens"], "maximum": 1500,
               "pass": b_v2["all"]["median_evidence_tokens"] <= 1500},
    }
    provenance = {
        "corpus_sha256": manifest["sha256"]["corpus.json"],
        "encoder": lock, "source_manifest_sha256": sha256((source / "manifest.json").read_bytes()).hexdigest(),
        "threshold": threshold, "config": chosen.model_dump(), "tokenizer": {
            "repository": "Qwen/Qwen3-8B", "revision": QWEN_REVISION,
            "sha256": sha256((TOKENIZER_DIR / "tokenizer.json").read_bytes()).hexdigest(),
            "counting": "full pretty-printed evidence JSON, including IDs/metadata; no special tokens",
        },
        "code_sha256": {name: sha256((ROOT / name).read_bytes()).hexdigest()
                        for name in ("offline_v2.py", "retrieval_v2.py", "generate.py", "corpus.py")},
        "prompt_change": "r1 instructions/system/persona unchanged; evidence items expanded and content lines prefixed with date | speaker (speaker_role=role) |; same five JSON fields",
        "gpu_or_judge_run": False,
    }
    write_json(destination / "manifest.json", provenance)
    write_json(output / "offline-metrics.json", {
        **provenance, "conditions": summaries, "criteria": criteria,
        "offline_pass": all(row["pass"] for row in criteria.values()),
        "generation_criteria": "G1–G5 unmeasured; no adoption decision",
    })
    print(json.dumps({"criteria": criteria, "config": chosen.model_dump()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    typer.run(main)
