"""Source-only invariant and exhaustive possible-context token checks on CPU."""

from collections import Counter
from hashlib import sha256
from itertools import combinations
import json
from pathlib import Path
import sys
from typing import NotRequired, TypedDict

import typer

from corpus import chunk_corpus
from dev_schema import GoldContext, Sources
from generate import render_messages
from prepare100 import HERE, ROOT, reject_forbidden
from renderer100 import render_o3
from retrieval_v2 import contextual_evidence, evidence_json
from schema100 import Identities100, Plan100, Sidecar100, check_design, validate_sources
from synthetic100 import ACTIVITIES, GIVEN_NAMES, OBJECTS, PLACES, SURNAMES


class TokenReport(TypedDict):
    status: str
    reason: NotRequired[str]
    method: NotRequired[str]
    contexts_checked: NotRequired[int]
    evidence_max: NotRequired[dict[str, int]]
    native_prompt_max: NotRequired[dict[str, int]]
    evidence_cap: NotRequired[int]
    context_cap: NotRequired[int]
    output_reserve: NotRequired[int]
    tokenizer_sha256: NotRequired[str]
    tokenizer_config_sha256: NotRequired[str]
    actual_kure_retrieval_executed: NotRequired[bool]


class SourceAuditReport(TypedDict):
    families: int
    questions: int
    records: int
    chunks: int
    unique_names: int
    unique_provider_pairs: int
    unique_distractor_utterances: int
    pool_sizes: dict[str, int]
    pool_frequency_ranges: dict[str, list[int]]
    subtypes: dict[str, int]
    registers: dict[str, int]
    source_owner_subject_time_checks: str
    question_only_plan_leakage: str
    c_exact_persona_support: str
    tokens: NotRequired[TokenReport]
    status: NotRequired[str]
    generations: NotRequired[int]
    judges: NotRequired[int]
    remote_commands: NotRequired[int]
    manifest_sha256: NotRequired[str]


def audit_sources(sources: Sources, plan: Plan100, identities: Identities100,
                  gold: GoldContext, sidecar: Sidecar100) -> SourceAuditReport:
    """Check authored semantics/IDs without any generated response or judge."""
    check_design(sources, plan)
    chunks = validate_sources(sources)
    by_id = {c.chunk_id: c for c in chunks}
    qmap = {q.question_id: q for q in sources.questions}
    imap = {c.family_id: c for c in identities.cases}
    if len(imap) != 1320 or set(imap) != {q.family_id for q in qmap.values()}:
        raise ValueError("Identity map incomplete")
    names = [n for i in identities.cases for n in (i.persona_name, i.provider_name)]
    if len(names) != 2640 or len(set(names)) != 2640:
        raise ValueError("Names must be globally unique")
    natural_names = {s + g for s in SURNAMES for g in GIVEN_NAMES}
    if not set(names) <= natural_names:
        raise ValueError("Name outside authored natural pool")
    for persona in sources.personas:
        if persona.name != imap[persona.persona_id].persona_name or persona.display_name != persona.name:
            raise ValueError("Persona name/identity mismatch")
    oracles = {o.question_id: o for o in sidecar.cases}
    membership = {c.question_id: c for c in plan.cases}
    if len(oracles) != 1320 or len(sidecar.cases) != 1320 or set(oracles) != set(qmap) or set(gold.evidence) != set(qmap):
        raise ValueError("Incomplete evaluation/C map")
    pairs, utterances, activities = [], [], []
    for qid, oracle in oracles.items():
        q = qmap[qid]
        if oracle.family_id != q.family_id:
            raise ValueError("Oracle family mismatch")
        own_facts = [f for f in oracle.facts if f.fact_subject == "provider"]
        if len(own_facts) != 1:
            raise ValueError("Expected exactly one provider distractor")
        provider_fact = own_facts[0]
        reports = [f for f in oracle.facts
                   if f.utterance_owner == "provider" and f.fact_subject == "persona"]
        if q.type == "single" and bool(reports) != (membership[qid].subtype == "reported_context"):
            raise ValueError("Direct/reported-context source semantics do not match membership")
        pairs.append((provider_fact.place, provider_fact.item))
        utterances.append(by_id[provider_fact.source_id].content)
        activities.append(provider_fact.activity)
        for fact in oracle.facts:
            chunk = by_id[fact.source_id]
            if chunk.family_id != q.family_id or chunk.speaker_role != fact.utterance_owner:
                raise ValueError("Foreign source or owner")
            if fact.quote not in chunk.content or fact.place not in fact.quote or fact.item not in fact.quote:
                raise ValueError("Unsupported source quote/surfaces")
            if fact.source_date != chunk.date or fact.event_date > fact.source_date:
                raise ValueError("Invalid source/event dates")
            if fact.fact_subject == "persona" and (fact.place == provider_fact.place or fact.item == provider_fact.item):
                raise ValueError("Provider/persona values must differ")
        expected = tuple(contextual_evidence(by_id[cid]) for cid in oracle.required_support_ids)
        if gold.evidence[qid] != expected or any(e.speaker_role != "persona" for e in expected):
            raise ValueError("C must be exact required original persona evidence")
        if q.type in ("unanswerable", "ordinary_chat") and expected:
            raise ValueError("Safe/chat C is not empty")
        if q.type == "unanswerable" and not oracle.unknowns:
            raise ValueError("Missing unknown rationale")
        if q.type == "false_premise" and not oracle.correction:
            raise ValueError("Missing correction condition")
        if any(surface not in "\n".join(e.content for e in expected) for surface in oracle.required_surfaces):
            raise ValueError("Gold surface lacks support")
    if len(set(pairs)) != 1320 or len(set(utterances)) != 1320:
        raise ValueError("Repeated provider pair or distractor utterance")
    place_counts = Counter(p for p, _ in pairs)
    object_counts = Counter(o for _, o in pairs)
    activity_counts = Counter(activities)
    if set(place_counts) != set(PLACES) or set(place_counts.values()) != {20, 21}:
        raise ValueError("Place bank imbalance")
    if set(object_counts) != set(OBJECTS) or set(object_counts.values()) != {20, 21}:
        raise ValueError("Object bank imbalance")
    if set(activity_counts) != set(ACTIVITIES) or set(activity_counts.values()) != {41, 42}:
        raise ValueError("Activity bank imbalance")
    if set(Plan100.model_fields) != {"split", "cases"} or set(type(plan.cases[0]).model_fields) != {
        "question_id", "family_id", "type", "subtype", "speech_register",
    }:
        raise ValueError("Question-only plan schema gained fields")
    if any(q.gold_chunk_ids or q.support or q.required_slots or q.expected_answer
           for q in sources.corpus().questions):
        raise ValueError("Oracle leaked through source adapter")
    subtype_counts = Counter((c.type, c.subtype) for c in plan.cases)
    return {
        "families": 1320, "questions": 1320, "records": len(sources.records), "chunks": len(chunks),
        "unique_names": len(set(names)), "unique_provider_pairs": len(set(pairs)),
        "unique_distractor_utterances": len(set(utterances)),
        "pool_sizes": {"places": len(place_counts), "objects": len(object_counts), "activities": len(activity_counts)},
        "pool_frequency_ranges": {"places": [min(place_counts.values()), max(place_counts.values())],
                                  "objects": [min(object_counts.values()), max(object_counts.values())],
                                  "activities": [min(activity_counts.values()), max(activity_counts.values())]},
        "subtypes": {f"{k}/{s}": n for (k, s), n in sorted(subtype_counts.items())},
        "registers": dict(Counter(c.speech_register for c in plan.cases)),
        "source_owner_subject_time_checks": "PASS", "question_only_plan_leakage": "PASS",
        "c_exact_persona_support": "PASS",
    }


def token_audit(sources: Sources, gold: GoldContext, tokenizer_path: Path) -> TokenReport:
    """Enumerate every possible retained subset, not a retrieval-selected sample."""
    from transformers import AutoTokenizer
    sys.path.append(str(ROOT / "experiments/model77"))
    from audit import serialize, token_ids
    from smoke import load_profile

    native = AutoTokenizer.from_pretrained(tokenizer_path, local_files_only=True, trust_remote_code=False)
    profile = load_profile("Qwen3-8B")
    system = json.loads((ROOT / "ai/configs/baseline.json").read_text())["system_prompt"]
    chunks = chunk_corpus(sources.corpus())
    by_id = {c.chunk_id: c for c in chunks}
    by_persona = {p.persona_id: [] for p in sources.personas}
    for chunk in chunks:
        by_persona[chunk.persona_id].append(chunk)
    personas = {p.persona_id: p for p in sources.personas}
    max_evidence = dict.fromkeys(("B", "A", "C"), 0)
    max_prompt = dict.fromkeys(("B", "A", "C"), 0)
    contexts = 0
    for q in sources.questions:
        local = sorted(by_persona[q.persona_id], key=lambda c: (c.date, str(c.record_id), c.ordinal))
        for count in range(len(local) + 1):
            for subset in combinations(local, count):
                b = tuple(contextual_evidence(c) for c in subset)
                a = render_o3(b, by_id)
                for arm, evidence in (("B", b), ("A", a)):
                    n = len(native.encode(evidence_json(evidence), add_special_tokens=False))
                    messages = render_messages(system, personas[q.persona_id], q.question, evidence)
                    prompt = len(token_ids(native, serialize(native, [m.model_dump() for m in messages], profile)))
                    max_evidence[arm] = max(max_evidence[arm], n)
                    max_prompt[arm] = max(max_prompt[arm], prompt)
                    contexts += 1
        evidence = gold.evidence[q.question_id]
        max_evidence["C"] = max(max_evidence["C"], len(native.encode(evidence_json(evidence), add_special_tokens=False)))
        messages = render_messages(system, personas[q.persona_id], q.question, evidence)
        max_prompt["C"] = max(max_prompt["C"], len(token_ids(native, serialize(
            native, [m.model_dump() for m in messages], profile,
        ))))
        contexts += 1
    if max(max_evidence.values()) > 2000 or max(max_prompt.values()) + 160 > 4096:
        raise ValueError("Token cap exceeded in an attainable retained source subset")
    return {
        "status": "PASS", "method": "every possible family-local retained subset, including empty/full archive",
        "contexts_checked": contexts, "evidence_max": max_evidence, "native_prompt_max": max_prompt,
        "evidence_cap": 2000, "context_cap": 4096, "output_reserve": 160,
        "tokenizer_sha256": sha256((tokenizer_path / "tokenizer.json").read_bytes()).hexdigest(),
        "tokenizer_config_sha256": sha256((tokenizer_path / "tokenizer_config.json").read_bytes()).hexdigest(),
        "actual_kure_retrieval_executed": False,
    }


def main(data: Path, output: Path, tokenizer_path: Path | None = None) -> None:
    reject_forbidden(data, output, *((tokenizer_path,) if tokenizer_path else ()))
    manifest = json.loads((data / "manifest.json").read_text())
    for name, digest in manifest["artifacts_sha256"].items():
        if sha256((data / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Artifact hash mismatch")
    for name, digest in manifest["authoring_code_sha256"].items():
        if sha256((HERE / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Authoring code hash mismatch")
    sources = Sources.model_validate_json((data / "corpus.json").read_text())
    plan = Plan100.model_validate_json((data / "plans.json").read_text())
    identities = Identities100.model_validate_json((data / "identity-ledger.json").read_text())
    gold = GoldContext.model_validate_json((data / "gold-context.json").read_text())
    sidecar = Sidecar100.model_validate_json((data / "evaluation-sidecar.json").read_text())
    result = audit_sources(sources, plan, identities, gold, sidecar)
    result["tokens"] = token_audit(sources, gold, tokenizer_path) if tokenizer_path else {
        "status": "UNVERIFIED", "reason": "No local tokenizer supplied",
    }
    result.update(status="DRAFT_NOT_FROZEN", generations=0, judges=0, remote_commands=0,
                  manifest_sha256=sha256((data / "manifest.json").read_bytes()).hexdigest())
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print("SOURCE_QA " + json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    typer.run(main)
