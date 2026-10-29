"""Recompute saved metrics and verify source hashes, scope and evidence exports."""

from hashlib import sha256
from typing import Literal

from pydantic import ConfigDict, RootModel
import typer

from corpus import Evidence, FrozenModel, ROOT, Split, load_corpus, validate
from retrieval import Calibration, SplitMetrics, Trace, calibrate, evaluate, select_evidence


class Manifest(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    calibration_sha256: str
    sha256: dict[str, str]


class Export(FrozenModel):
    question_id: str
    persona_id: str
    persona: dict[str, str | None]
    message: str
    predicted_no_evidence: bool
    evidence: tuple[Evidence, ...]
    topk_evidence: tuple[Evidence, ...]


def main(model: Literal["kure", "e5"]) -> None:
    corpus = load_corpus()
    chunks = validate(corpus)
    by_id = {c.chunk_id: c for c in chunks}
    questions = {q.question_id: q for q in corpus.questions}
    personas = {p.persona_id: p for p in corpus.personas}
    output = ROOT / "results" / model
    manifest = Manifest.model_validate_json(
        (output / "manifest.json").read_text(encoding="utf-8")
    )
    for name, expected in manifest.sha256.items():
        assert sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    assert sha256((output / "calibration.json").read_bytes()).hexdigest() == (
        manifest.calibration_sha256
    )
    calibration = Calibration.model_validate_json(
        (output / "calibration.json").read_text(encoding="utf-8")
    )
    traces = RootModel[dict[Split, list[Trace]]].model_validate_json(
        (output / "traces.json").read_text(encoding="utf-8")
    ).root
    metrics = RootModel[dict[Split, SplitMetrics]].model_validate_json(
        (output / "metrics.json").read_text(encoding="utf-8")
    ).root
    exported = RootModel[dict[Split, list[Export]]].model_validate_json(
        (output / "evidence.json").read_text(encoding="utf-8")
    ).root
    assert calibrate(traces["dev"]) == calibration
    for split in ("dev", "test"):
        assert len(traces[split]) == 40
        assert {t.question.question_id for t in traces[split]} == {
            q.question_id for q in corpus.questions if q.split == split
        }
        assert evaluate(traces[split], calibration.threshold) == metrics[split]
        for trace, row in zip(traces[split], exported[split], strict=True):
            question = questions[trace.question.question_id]
            assert question == trace.question
            assert question.split == split
            assert len(trace.ranked_ids) == len(set(trace.ranked_ids))
            assert set(trace.ranked_ids) == {
                c.chunk_id for c in chunks if c.persona_id == question.persona_id
            }
            assert tuple(h.chunk_id for h in trace.top5) == trace.ranked_ids[:5]
            assert [(-h.score, str(h.chunk_id)) for h in trace.top5] == sorted(
                (-h.score, str(h.chunk_id)) for h in trace.top5
            )
            assert trace.topk_evidence == tuple(
                by_id[h.chunk_id].evidence() for h in trace.top5
            )
            assert select_evidence(trace, calibration.threshold) == trace
            assert row.question_id == question.question_id
            assert row.persona_id == question.persona_id
            assert row.message == question.question
            persona = personas[question.persona_id]
            assert row.persona == {
                "display_name": persona.display_name,
                "provider_address": persona.provider_address,
            }
            assert row.predicted_no_evidence == trace.predicted_no_evidence
            assert row.evidence == trace.evidence
            assert row.topk_evidence == trace.topk_evidence
        failures = [
            t for t in traces[split]
            if (
                t.question.gold_chunk_ids
                and not set(t.question.gold_chunk_ids) <= set(t.ranked_ids[:5])
            ) or (
                t.question.type != "ordinary_chat"
                and (t.question.type == "unanswerable") != t.predicted_no_evidence
            )
        ]
        for trace in failures:
            gold_ranks = [
                trace.ranked_ids.index(chunk_id) + 1
                for chunk_id in trace.question.gold_chunk_ids
            ]
            print(
                f"FAILURE {model}/{split}/{trace.question.question_id}: "
                f"gold_ranks={gold_ranks}; empty={trace.predicted_no_evidence}; "
                f"top5={[(h.record_alias, round(h.score, 6)) for h in trace.top5]}"
            )
    print(
        f"AUDIT PASS {model}: 80 traces; current source hashes; DEV-only threshold; "
        "recomputed metrics; zero persona-scope leaks; exact five-field evidence"
    )


if __name__ == "__main__":
    typer.run(main)
