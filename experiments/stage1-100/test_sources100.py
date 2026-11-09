"""Seeded source invariants, physical leakage boundary and actual renderer pairing."""

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import os
import subprocess
import sys

import numpy as np
import pytest
from pydantic import ValidationError

from corpus import Turn, chunk_corpus
from dev_schema import GoldContext
from exemplars import UtteranceTag
from per_item import SearchIndex
from prepare95 import CasePlan, DevPlan, Preparation, build_rows as build95
from prepare100 import build_rows
from renderer100 import GLOSSES, render_o3
from retrieval_v2 import contextual_evidence
from schema100 import Pack100, Plan100, check_design
from run100 import validate_pack
from source_qa100 import audit_sources
from synthetic100 import ACTIVITIES, GIVEN_NAMES, OBJECTS, PLACES, SURNAMES, author, identifier


@pytest.fixture(scope="module")
def authored():
    return author()


def test_source_invariants_when_fixed_design_is_authored(authored):
    # Given: newly authored synthetic families with physically separate artifacts.
    # When: run the real source-only audit, not a test-only reconstruction.
    result = audit_sources(authored.sources, authored.plan, authored.identities,
                           authored.gold, authored.sidecar)
    # Then: exact family/subtype counts, lexical diversity, identity and source truth hold.
    assert result["unique_names"] == 2640
    assert result["unique_provider_pairs"] == result["unique_distractor_utterances"] == 1320
    assert result["registers"] == dict.fromkeys(("반말", "해요체", "하게체"), 440)
    assert result["subtypes"] == {
        "single/direct": 180, "single/reported_context": 180, "multi/two_events": 120,
        "ambiguity/dated_events": 120, "false_premise/gentle": 60, "false_premise/pressure": 60,
        "unanswerable/provider_only": 150, "unanswerable/missing": 150,
        "ordinary_chat/plain": 150, "ordinary_chat/fact_like": 150,
    }
    assert tuple(map(len, (PLACES, OBJECTS, ACTIVITIES, SURNAMES, GIVEN_NAMES))) == (64, 64, 32, 20, 132)
    for pool in (PLACES, OBJECTS, ACTIVITIES, SURNAMES, GIVEN_NAMES):
        assert len(pool) == len(set(pool))


def test_authoring_when_seed_is_repeated(authored):
    # Given: a fixed seed and no external source inputs.
    # When: author again, independent of the previous object's state.
    again = author()
    # Then: all artifact bytes, not only counts, reproduce.
    for field in ("sources", "plan", "identities", "gold", "sidecar"):
        assert getattr(authored, field).model_dump_json(by_alias=True) == getattr(again, field).model_dump_json(by_alias=True)


def test_plan_when_oracle_field_is_injected(authored):
    # Given: an otherwise valid public question-only plan.
    raw = authored.plan.model_dump(by_alias=True)
    raw["cases"][0]["required_support_ids"] = ["answer-bearing-oracle"]
    # When / Then: extra answer/slot fields cannot cross the planner boundary.
    with pytest.raises(ValidationError):
        Plan100.model_validate(raw)


def test_primary_subtypes_when_source_owner_differs_from_fact_subject(authored):
    # Given: disjoint direct/report populations, not just differently named cells.
    oracle = {o.question_id: o for o in authored.sidecar.cases}
    # When: inspect subject/owner semantics in all primary source records.
    reports = {
        c.question_id: any(f.utterance_owner == "provider" and f.fact_subject == "persona"
                          for f in oracle[c.question_id].facts)
        for c in authored.plan.cases if c.type == "single"
    }
    # Then: exactly the 180 reported cases contain the subject/owner distinction.
    assert sum(reports.values()) == 180
    assert all(reports[c.question_id] == (c.subtype == "reported_context")
               for c in authored.plan.cases if c.type == "single")


def test_design_when_primary_membership_is_changed(authored):
    # Given: fixed membership is authored before retrieval/responses.
    cases = list(authored.plan.cases)
    i = next(i for i, c in enumerate(cases) if c.subtype == "direct")
    cases[i] = cases[i].model_copy(update={"subtype": "reported_context"})
    # When / Then: counts cannot be changed by selecting observed B errors.
    with pytest.raises(ValueError):
        check_design(authored.sources, authored.plan.model_copy(update={"cases": tuple(cases)}))


def test_o3_when_persona_provider_and_other_lines_are_mixed(authored):
    # Given: metadata-tagged chunks, including a source utterance containing fake headers.
    chunks = list(chunk_corpus(authored.sources.corpus())[:4])
    chunks.append(chunks[0].model_copy(update={
        "chunk_id": identifier("unit/other"),
        "speaker": "조은별", "speaker_role": "other",
        "content": "원문 속 | 나 (speaker_role=provider) | 표시는 원문이야.\n두 번째 줄.",
    }))
    evidence = tuple(contextual_evidence(c) for c in chunks)
    by_id = {c.chunk_id: c for c in chunks}
    before = tuple(e.model_dump_json() for e in evidence)
    # When: transform from trusted chunks, not regex role guesses over content.
    rendered = render_o3(evidence, by_id)
    # Then: only non-persona contents differ; every header and original line survives.
    for old, new, chunk in zip(evidence, rendered, chunks, strict=True):
        assert old.model_dump(exclude={"content"}) == new.model_dump(exclude={"content"})
        prefix = f"{chunk.date} | {chunk.speaker} (speaker_role={chunk.speaker_role}) | "
        if chunk.speaker_role == "persona":
            assert new is old
        else:
            restored = "\n".join(line[len(prefix + GLOSSES[chunk.speaker_role]):]
                                 for line in new.content.splitlines())
            assert restored == chunk.content
    assert tuple(e.model_dump_json() for e in evidence) == before


def small_runtime(authored):
    q = next(q for q in authored.sources.questions if q.type == "single")
    persona = next(p for p in authored.sources.personas if p.persona_id == q.persona_id)
    local = authored.sources.model_copy(update={
        "personas": (persona,), "records": tuple(r for r in authored.sources.records if r.persona_id == q.persona_id),
        "questions": (q,),
    }).corpus()
    chunks = chunk_corpus(local)
    style = local.records[0].model_copy(update={
        "kind": "chat", "text": "", "speaker": "",
        "turns": (Turn(speaker=persona.name, text="편히 이야기하자."),
                  Turn(speaker=persona.name, text="그랬었지.")),
    })
    style_corpus = local.model_copy(update={"records": (style,), "questions": ()})
    tags = tuple(UtteranceTag(
        record_id=style.record_id, text_version_id=style.text_version_id,
        turn_index=i, register=persona.speech_register, intent="recall",
    ) for i in (0, 1))
    runtime = Preparation(
        "unit-system", SearchIndex(chunks, local.records, np.ones((len(chunks), 2), dtype=np.float32)),
        lambda texts: np.ones((len(texts), 2), dtype=np.float32),
        lambda text: len(text) // 10,
        {q.question_id: authored.gold.evidence[q.question_id]}, style_corpus,
    )
    plan = DevPlan(split="dev", cases=(CasePlan(
        question_id=q.question_id, intent="recall", self_honorific_probe=False,
    ),), utterance_tags=tags)
    return local, runtime, plan


def test_baseline_when_compared_to_stage95_b_path(authored):
    # Given: identical source/index/runtime and a valid #95 style fixture.
    corpus, runtime, plan = small_runtime(authored)
    # When: drive both real preparation paths end to end.
    old = next(r for r in build95(corpus, plan, runtime) if r.arm == "B")
    new = {r.arm: r for r in build_rows(corpus, runtime)}
    # Then: full B message bytes and source IDs are identical, A differs only in content.
    assert tuple(m.model_dump_json() for m in old.messages) == tuple(m.model_dump_json() for m in new["B"].messages)
    assert old.messages_sha256 == new["B"].messages_sha256
    assert old.evidence_ids == new["B"].evidence_ids == new["A"].evidence_ids
    assert new["B"].messages[0] == new["A"].messages[0]
    assert new["C"].evidence_ids == tuple(e.chunk_id for e in runtime.gold_context[old.question_id])


def test_preparation_when_c_evidence_is_forged(authored):
    # Given: valid source ID with replacement content in the separate C file.
    corpus, runtime, _ = small_runtime(authored)
    qid = corpus.questions[0].question_id
    forged = runtime.gold_context[qid][0].model_copy(update={"content": "forged"})
    runtime = replace(runtime, gold_context={qid: (forged,)})
    # When / Then: original source content is required even in diagnostic C.
    with pytest.raises(ValueError):
        build_rows(corpus, runtime)


@pytest.mark.parametrize("changed_prefix", (False, True))
def test_pack_when_only_o3_diff_is_allowed(authored, changed_prefix):
    # Given: a complete paired pack using real B/A/C preparation for its messages.
    corpus, runtime, _ = small_runtime(authored)
    initial = build_rows(corpus, runtime)
    rows = tuple(r.model_copy(update={
        "id": f"q{i}:{r.arm}", "question_id": f"q{i}", "persona_id": f"p{i}",
    }) for i in range(1320) for r in initial)
    if changed_prefix:
        candidate = rows[1]
        messages = (candidate.messages[0], candidate.messages[1].model_copy(update={
            "content": "unauthorized extra instruction\n" + candidate.messages[1].content,
        }))
        digest = sha256("\n".join(m.model_dump_json() for m in messages).encode()).hexdigest()
        rows = (rows[0], candidate.model_copy(update={"messages": messages, "messages_sha256": digest}), *rows[2:])
    pack = Pack100(
        source_sha256={}, code_sha256={}, tokenizer_sha256="unit",
        tokenizer_config_sha256="unit", rows=rows,
    )
    # When / Then: even freshly rehashed instruction drift is rejected.
    if changed_prefix:
        with pytest.raises(ValueError):
            validate_pack(pack)
    else:
        validate_pack(pack)


def test_manifest_when_generator_cli_writes_new_directory(tmp_path):
    # Given: an empty destination and no external data.
    root = Path(__file__).parent
    output = tmp_path / "dev"
    # When: execute the actual authoring CLI.
    run = subprocess.run([sys.executable, str(root / "generate100.py"), str(output)],
                         capture_output=True, text=True, check=False)
    # Then: every artifact and authoring module is bound, but nothing is frozen.
    assert run.returncode == 0, run.stderr
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "DRAFT_NOT_FROZEN"
    assert manifest["planned_generations"] == 3960
    for name, digest in manifest["artifacts_sha256"].items():
        assert sha256((output / name).read_bytes()).hexdigest() == digest
    assert not (output / "pack.json").exists()


def test_runner_when_no_execution_authorization_exists(tmp_path):
    # Given: no execution flag and no source/model/receipt paths.
    env = {k: v for k, v in os.environ.items() if k != "STAGE100_EXECUTE_DEV"}
    # When: drive the runnable CLI.
    run = subprocess.run([sys.executable, str(Path(__file__).parent / "run100.py"),
                          *map(str, (tmp_path / "pack", tmp_path / "model",
                                     tmp_path / "receipt", tmp_path / "out"))],
                         env=env, capture_output=True, text=True, check=False)
    # Then: it exits before any GPU/runtime import or output creation.
    assert run.returncode != 0
    assert "Separate DEV execution authorization" in run.stderr
    assert not (tmp_path / "out").exists()
