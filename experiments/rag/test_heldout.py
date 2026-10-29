"""Held-out split, selectable B evidence and sequential arm execution contracts."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from corpus import ROOT, load_corpus, validate
from generate import (
    InputError, default_schedule, load_generations,
)
from offline_v2 import export
from retrieval import Hit, Trace, calibrate
from retrieval_v2 import contextual_evidence
from test_generate import fake_generations, without_evidence


@pytest.fixture
def heldout_inputs(tmp_path):
    # Given: synthetic known fixtures relabeled heldout, not real held-out answers.
    source = load_corpus()
    corpus = source.model_copy(update={
        "records": tuple(r.model_copy(update={"split": "heldout"}) for r in source.records),
        "questions": tuple(q.model_copy(update={"split": "heldout"}) for q in source.questions),
    })
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(corpus.model_dump_json(by_alias=True), encoding="utf-8")
    chunks = validate(corpus)
    traces = []
    for question in corpus.questions:
        chunk = next(c for c in chunks if c.persona_id == question.persona_id)
        traces.append(Trace(
            question=question, ranked_ids=(chunk.chunk_id,),
            top5=(Hit(chunk_id=chunk.chunk_id, score=0.9, record_alias=chunk.record_alias),),
            evidence=(chunk.evidence(),), topk_evidence=(chunk.evidence(),),
        ))
    for name, contextual in (("kure_old", False), ("kure_v2", True)):
        directory = tmp_path / name
        directory.mkdir()
        selected = [
            t.model_copy(update={
                "evidence": (contextual_evidence(next(c for c in chunks
                                                   if c.chunk_id == t.ranked_ids[0])),),
                "topk_evidence": (contextual_evidence(next(c for c in chunks
                                                        if c.chunk_id == t.ranked_ids[0])),),
            }) for t in traces
        ] if contextual else traces
        (directory / "evidence.json").write_text(json.dumps({
            "heldout": [row.model_dump(mode="json") for row in export(selected, corpus)],
        }), encoding="utf-8")
        (directory / "manifest.json").write_text(json.dumps({
            "evidence_format": "source-context-v2" if contextual else "canonical",
        }), encoding="utf-8")
        (directory / "calibration.json").write_text("{}", encoding="utf-8")
    return corpus, corpus_path, tmp_path


@pytest.mark.parametrize("version", ["r1", "g1"])
@pytest.mark.parametrize("variant", ["kure_old", "kure_v2"])
def test_heldout_abc_uses_selected_evidence_when_rendered(heldout_inputs, version, variant):
    # Given: both valid evidence formats and a held-out split outside the repository.
    corpus, corpus_path, directory = heldout_inputs
    selected = directory / variant / "evidence.json"
    rows = json.loads(selected.read_text())["heldout"]
    expected = {r["question_id"]: [e["chunk_id"] for e in r["evidence"]] for r in rows}
    # When: select either export without changing the A/B/C condition names.
    schedule = default_schedule(corpus_path, selected, version)
    # Then: B matches precisely the selected export; other model input is identical.
    assert len(schedule) == len(corpus.questions) * 3
    for question in corpus.questions:
        group = [p for p in schedule if p.question_id == question.question_id]
        assert {p.condition for p in group} == {"A", "B", "C"}
        assert all(p.split == "heldout" and p.prompt_version == version for p in group)
        assert len({without_evidence(p) for p in group}) == 1
        assert [str(cid) for cid in group[1].evidence_ids] == expected[question.question_id]


def test_heldout_calibration_is_rejected_when_accidentally_requested(heldout_inputs):
    # Given: held-out traces with both absence and factual labels.
    _, _, directory = heldout_inputs
    from generate import load_retrieved
    rows = load_retrieved(directory / "kure_old/evidence.json")
    corpus = heldout_inputs[0]
    traces = [Trace(question=q, ranked_ids=(), top5=(), topk_evidence=rows[q.question_id].evidence)
              for q in corpus.questions]
    # When / Then: no held-out data can reach the DEV fitter.
    with pytest.raises(ValueError, match="DEV-only"):
        calibrate(traces)


@pytest.mark.parametrize("corruption", ["content", "format"])
def test_contextual_b_is_rejected_when_export_tampered(heldout_inputs, corruption):
    # Given: an export whose content or declared transformation is wrong.
    _, corpus_path, directory = heldout_inputs
    evidence = directory / "kure_v2/evidence.json"
    payload = json.loads(evidence.read_text())
    match corruption:
        case "content":
            payload["heldout"][0]["evidence"][0]["content"] = "tampered"
            evidence.write_text(json.dumps(payload), encoding="utf-8")
        case "format":
            (evidence.parent / "manifest.json").write_text(
                '{"evidence_format":"canonical"}', encoding="utf-8",
            )
    # When / Then: selected B cannot bypass canonical source verification.
    with pytest.raises(InputError):
        default_schedule(corpus_path, evidence, "g1")


def test_generation_arm_mismatch_is_rejected_when_loading(heldout_inputs, tmp_path):
    # Given: one g1 result mislabeled as r1.
    _, corpus_path, directory = heldout_inputs
    schedule = default_schedule(corpus_path, directory / "kure_old/evidence.json", "g1")
    rows = list(fake_generations(schedule))
    rows[0] = rows[0].model_copy(update={"prompt_version": "r1"})
    artifact = tmp_path / "generations.jsonl"
    artifact.write_text("\n".join(row.model_dump_json() for row in rows), encoding="utf-8")
    # When / Then: responses from a different generation arm are not accepted.
    with pytest.raises(InputError):
        load_generations(artifact, schedule)


def test_external_corpus_provenance_when_fake_runtime_used(heldout_inputs, tmp_path, monkeypatch):
    # Given: an actual external held-out corpus path, and a deterministic runtime double.
    from types import SimpleNamespace
    from generate import main

    class FakeRuntime:
        def __init__(self, model_path, config):
            self.config = config
            self.environment = {"test_double": True}

        def generate(self, prompt, cap):
            return {
                "text": "synthetic-response", "generation_ms": 1.0,
                "generated_token_count": 2, "generated_ids_sha256": "a" * 64,
                "termination": "eos", "tokens_per_second": 2000.0,
            }

    class FakeEvaluationError(RuntimeError):
        pass

    monkeypatch.setitem(sys.modules, "runtime", SimpleNamespace(Runtime=FakeRuntime))
    monkeypatch.setitem(sys.modules, "common", SimpleNamespace(EvaluationError=FakeEvaluationError))
    _, corpus_path, directory = heldout_inputs
    output = tmp_path / "generated"
    # When: run the real persistence/provenance path without a model call.
    main(output=output, corpus=corpus_path,
         evidence=directory / "kure_v2/evidence.json", prompt_version="g1")
    # Then: the external file is hashed without assuming it is relative to REPO.
    environment = json.loads((output / "environment.json").read_text())
    assert str(corpus_path) in environment["input_sha256"]
    schedule = default_schedule(corpus_path, directory / "kure_v2/evidence.json", "g1")
    assert len(load_generations(output / "generations.jsonl", schedule)) == len(schedule)


@pytest.mark.parametrize("versions,expected", [
    ("r1 g1", ["r1", "g1"]), (None, ["g1"]),
    ("r1 r1", []), ("r1 unknown", []), ("   ", []),
])
def test_sbatch_arm_forwarding_when_executed_without_slurm(tmp_path, versions, expected):
    # Given: a fake python entry point that records arguments, never loads a model.
    base = tmp_path / "checkout"
    script = base / "experiments/rag/generate.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import json, sys\nprint('CALL ' + json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(sys.executable)
    (venv / "bin/activate").write_text(
        f'export PATH="{venv}/bin:$PATH"\n', encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("REPLICA_")}
    env.update(REPLICA_BASE=str(base), REPLICA_VENV=str(venv),
               REPLICA_CORPUS="/external/corpus.json", REPLICA_EVIDENCE="/selected/evidence.json",
               REPLICA_OUTPUT=str(tmp_path / "outputs"), REPLICA_PROMPT_VERSION="g1")
    if versions is not None:
        env["REPLICA_PROMPT_VERSIONS"] = versions
    # When: execute the actual shell wrapper without sbatch or an allocation.
    result = subprocess.run(["bash", str(ROOT / "run-generate.sbatch")],
                            env=env, capture_output=True, text=True)
    calls = [json.loads(line.removeprefix("CALL ")) for line in result.stdout.splitlines()
             if line.startswith("CALL ")]
    # Then: valid arms run sequentially, bad lists fail before either arm starts.
    assert (result.returncode == 0) == bool(expected)
    assert [args[args.index("--prompt-version") + 1] for args in calls] == expected
    for args, version in zip(calls, expected, strict=True):
        assert args[args.index("--corpus") + 1] == "/external/corpus.json"
        assert args[args.index("--evidence") + 1] == "/selected/evidence.json"
        output = Path(args[args.index("--output") + 1])
        expected_output = tmp_path / "outputs" / version if versions else tmp_path / "outputs"
        assert output == expected_output
