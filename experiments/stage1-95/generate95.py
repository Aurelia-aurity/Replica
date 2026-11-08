"""Deterministic fresh feasibility DEV authorship and content-addressed freeze."""

from collections import Counter
from dataclasses import asdict
from datetime import date, timedelta
from hashlib import sha256
import json
from itertools import product
from pathlib import Path
import random
from typing import Final

import typer

from corpus import QuestionType, chunk_corpus, validate
from dev_schema import EvaluationSidecar, GoldContext, Sources
from exemplars import Register
from prepare95 import CRITERIA_SHA256, HERE, DevPlan, check_design
from retrieval_v2 import contextual_evidence
from synthetic95 import Event, Family, FamilySpec, Templates, author_family

SEED: Final = 9508112026
KINDS: Final[tuple[QuestionType, ...]] = (
    "single", "multi", "ambiguity", "false_premise", "unanswerable", "ordinary_chat",
)
REGISTERS: Final[tuple[Register, ...]] = ("반말", "해요체", "하게체")


def families(bank: Templates, seed: int = SEED) -> tuple[Family, ...]:
    """One independent semantic tuple/persona per question; no register replication."""
    rng = random.Random(seed)
    days = rng.sample(range(365 * 3), 360)
    event_tuples = rng.sample(
        list(product(bank.events, bank.places[:-4], bank.objects[:-4])), 720,
    )
    result = []
    for kind in KINDS:
        for register in REGISTERS:
            for member in range(20):
                ordinal = len(result)
                start = date(2022, 1, 1) + timedelta(days=days[ordinal])
                tuples = event_tuples[ordinal * 2:ordinal * 2 + 2]
                events = tuple(Event(
                    f"{start.isoformat()} {tuples[i][0]}", (start + timedelta(days=i * 7)).isoformat(),
                    ("09:00", "16:00")[i], tuples[i][1], tuples[i][2],
                ) for i in range(2))
                result.append(author_family(FamilySpec(
                    ordinal, member, kind, register, (events[0], events[1]),
                ), bank))
    return tuple(result)


def assemble(authored: tuple[Family, ...]) -> tuple[Sources, Sources, DevPlan, GoldContext, EvaluationSidecar]:
    """C gets only evidence content; all slots/conflicts stay in the separate oracle."""
    sources = Sources(
        dataset_id="stage95-new-feasibility-dev-v1",
        personas=tuple(f.persona for f in authored),
        records=tuple(r for f in authored for r in f.records),
        questions=tuple(f.question for f in authored),
    )
    style = Sources(
        dataset_id="stage95-style-only-dev-v1", personas=sources.personas,
        records=tuple(f.style_record for f in authored), questions=(),
    )
    plan = DevPlan(split="dev", cases=tuple(f.plan for f in authored),
                   utterance_tags=tuple(t for f in authored for t in f.tags))
    # Validate sources without inventing dummy answers for source-only questions.
    validate(sources.corpus().model_copy(update={"questions": ()}))
    validate(style.corpus())
    check_design(sources.corpus(), plan)
    chunks = {c.chunk_id: c for c in chunk_corpus(sources.corpus())}
    gold = GoldContext(evidence={
        f.question.question_id: tuple(
            contextual_evidence(chunks[cid])
            for item in f.oracle.required_items for cid in item.gold_support_ids
        ) for f in authored
    })
    return sources, style, plan, gold, EvaluationSidecar(cases=tuple(f.oracle for f in authored))


def main(output: Path) -> None:
    """Write one new freeze directory; deliberately refuses to overwrite artifacts."""
    if sha256((HERE / "CRITERIA.md").read_bytes()).hexdigest() != CRITERIA_SHA256:
        raise RuntimeError("Frozen criteria changed")
    bank = Templates.model_validate_json((HERE / "templates95.json").read_text())
    authored = families(bank)
    source, style, plan, gold, oracle = assemble(authored)
    payloads = dict(zip(
        ("corpus.json", "style-sources.json", "plans.json", "gold-context.json", "evaluation-sidecar.json"),
        (v.model_dump_json(indent=2, by_alias=True) + "\n"
         for v in (source, style, plan, gold, oracle)), strict=True,
    ))
    payloads["event-ledger.json"] = json.dumps({
        "scope": "DEV_ONLY_AUTHORING",
        "families": [{"family_id": f.question.family_id,
                      "events": [asdict(event) for event in f.events]} for f in authored],
    }, ensure_ascii=False, indent=2) + "\n"
    counts = Counter((f.question.type, f.persona.speech_register) for f in authored)
    manifest = {
        "scope": "DEV_ONLY", "synthetic_only": True, "seed": SEED, "families": 360,
        "question_count": 360, "self_honorific_probes": 180,
        "style_leakage_probes": {
            "count": 72, "per_type_register_cell": 4,
            "selection_rule": "zero-based cell_index % 5 == 1 (1,6,11,16); 36 honorific,36 non-honorific",
            "examples": "one generic + one leakage example for probes; two generic examples otherwise",
        },
        "style_selection": "random.Random(9508112026 + ordinal), sample two generic examples without replacement",
        "question_wording": "three variants per type, selected by zero-based cell_index % 3",
        "event_sampling": (
            "720 candidate triples sampled without replacement; 360 unique first triples/dates. "
            "False-premise auxiliary events are replaced by same-event old claims."
        ),
        "pool_sizes": {name: len(getattr(bank, name))
                       for name in ("events", "places", "objects", "colors")},
        "generic_style_templates_per_register_intent": {
            register: {intent: len(pool) for intent, pool in intents.items()}
            for register, intents in bank.style_examples.items()
        },
        "cells": [{"type": k, "register": r, "questions": counts[k, r], "probes": 10}
                  for k in KINDS for r in REGISTERS],
        "criteria_sha256": CRITERIA_SHA256,
        "artifacts_sha256": {name: sha256(text.encode()).hexdigest() for name, text in payloads.items()},
        "authoring_code_sha256": {p.name: sha256(p.read_bytes()).hexdigest()
                                  for p in (HERE / n for n in (
                                      "generate95.py", "synthetic95.py", "dev_schema.py", "templates95.json"))},
        "oracle_access": "evaluation-sidecar.json is never read by preparation or generation",
        "cohort_policy": "predeclared challenges; actual B recall and answer omissions must be measured",
        "model": "Qwen/Qwen3-8B", "model_revision": "b968826d9c46dd6066d109eabc6255188de91218",
        "encoder_revision": "8b418a58414668e75532ed045c22d9ca018ae2b2",
        "prompt": "r1", "output_tokens": 160, "dtype": "bfloat16", "decoding": "greedy_nonthinking",
    }
    output.mkdir(parents=True, exist_ok=False)
    for name, text in payloads.items():
        with (output / name).open("x", encoding="utf-8") as handle:
            handle.write(text)
    with (output / "manifest.json").open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"DEV_AUTHORED families=360 questions=360 probes=180 seed={SEED}")


if __name__ == "__main__":
    typer.run(main)
