"""Source-only family-combination freshness; pool reuse is not contamination."""

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import TypedDict

import typer

from dev_schema import Sources
from prepare100 import reject_forbidden
from schema100 import Identities100, Sidecar100
from synthetic100 import ACTIVITIES, OBJECTS, PLACES


@dataclass(frozen=True, slots=True)
class Family:
    persona: str
    provider: str
    facts: tuple[tuple[str, ...], ...]
    provider_pair: tuple[str, str]
    question: str


@dataclass(frozen=True, slots=True)
class Population:
    families: frozenset[Family]
    family_ids: frozenset[str]
    question_ids: frozenset[str]
    names: frozenset[str]
    provider_pairs: frozenset[tuple[str, str]]
    questions: tuple[str, ...]
    templates: frozenset[str]
    distractors: frozenset[str]


def population(data: Path) -> Population:
    """Ignore namespace-specific IDs, quotes and record ordering in fact identity."""
    sources = Sources.model_validate_json((data / "corpus.json").read_text())
    identities = Identities100.model_validate_json((data / "identity-ledger.json").read_text())
    sidecar = Sidecar100.model_validate_json((data / "evaluation-sidecar.json").read_text())
    identities_by_id = {c.family_id: c for c in identities.cases}
    truth = {c.question_id: c for c in sidecar.cases}
    families, pairs, distractors = [], set(), set()
    questions = tuple(q.question for q in sources.questions)
    for q in sources.questions:
        identity, oracle = identities_by_id[q.family_id], truth[q.question_id]
        provider = next(f for f in oracle.facts if f.fact_subject == "provider")
        pair = provider.place, provider.item
        facts = tuple(sorted((f.utterance_owner, f.fact_subject, f.event_date,
                              f.source_date, f.place, f.item, f.activity) for f in oracle.facts))
        families.append(Family(identity.persona_name, identity.provider_name, facts, pair, q.question))
        pairs.add(pair)
        distractors.add(provider.quote)
    # Report literal source templates after lexical/date slots are removed.
    lexical = "|".join(re.escape(v) for v in sorted(set((*ACTIVITIES, *PLACES, *OBJECTS)),
                                                  key=lambda v: (-len(v), v)))
    templates = frozenset(re.sub(lexical, "{slot}", re.sub(r"\d{4}-\d{2}-\d{2}", "{date}", q))
                          for q in questions)
    return Population(
        frozenset(families), frozenset(q.family_id for q in sources.questions),
        frozenset(q.question_id for q in sources.questions),
        frozenset(n for i in identities.cases for n in (i.persona_name, i.provider_name)),
        frozenset(pairs), questions, templates, frozenset(distractors),
    )


class FreshnessReport(TypedDict):
    status: str
    identical_family_combinations: int
    family_id_overlap: int
    question_id_overlap: int
    resampled_families: int
    pool_reuse: dict[str, int | float | str]


def audit_freshness(previous: Population, current: Population) -> FreshnessReport:
    """Enforce only the preregistered namespace and joint-family freshness gates."""
    identical = len(previous.families & current.families)
    family_ids = len(previous.family_ids & current.family_ids)
    question_ids = len(previous.question_ids & current.question_ids)
    if identical or family_ids or question_ids:
        raise ValueError(("Confirmation freshness failed", identical, family_ids, question_ids))
    old_questions = set(previous.questions)
    return {
        "status": "PASS", "identical_family_combinations": identical,
        "family_id_overlap": family_ids, "question_id_overlap": question_ids,
        "resampled_families": 0,
        "pool_reuse": {
            "names": len(previous.names & current.names),
            "name_overlap_expected": 2640,
            "provider_pairs": len(previous.provider_pairs & current.provider_pairs),
            "provider_pair_overlap_expected": 1320 * 1320 / (64 * 64),
            "pair_expectation_assumption": "Independent seeded sets; uniform marginal cell inclusion under balanced Latin sampling, not IID draws",
            "distinct_questions": len(old_questions & set(current.questions)),
            "question_rows": sum(q in old_questions for q in current.questions),
            "templates": len(previous.templates & current.templates),
            "old_templates": len(previous.templates), "new_templates": len(current.templates),
            "distractor_utterances": len(previous.distractors & current.distractors),
            "distractor_expectation_iid_approx": 1320 * 1320 / (4096 * 32 * 2190 * 3),
            "expectation_limits": "All2640 names reused exactly. Six chat strings each appear with near certainty in150 draws/3 per subtype; ~300 chat rows reuse is expected. Date/activity-specific questions and distractors may coincide by chance. No IID or independent family outcomes claim.",
        },
    }


def main(previous: Path, current: Path, output: Path) -> None:
    reject_forbidden(previous, current, output)
    result = audit_freshness(population(previous), population(current))
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    typer.run(main)
