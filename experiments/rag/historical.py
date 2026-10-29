"""Explicit adapters for the immutable pre-slot-annotation r1–r3 corpus."""

import json
from pathlib import Path
from typing import Final

from corpus import ROOT, Corpus, Question

HISTORICAL_CORPUS: Final = ROOT / "history/corpus-r1r3.json"


def load_historical_corpus() -> Corpus:
    """Adapt absent slot annotations, never change historical gold or content."""
    payload = json.loads(HISTORICAL_CORPUS.read_text(encoding="utf-8"))
    for question in payload["questions"]:
        question["required_slots"] = list(dict.fromkeys(
            support["answer"] for support in question["support"]
        ))
    return Corpus.model_validate(payload)


def load_input_corpus(path: Path) -> Corpus:
    """Only the explicitly named historical snapshot accepts the legacy schema."""
    if path.resolve() == HISTORICAL_CORPUS:
        return load_historical_corpus()
    from corpus import load_corpus

    return load_corpus(path)


def historical_question(payload: dict) -> Question:
    """Bridge old trace question metadata without changing recorded labels."""
    return Question.model_validate({
        **payload,
        "required_slots": tuple(dict.fromkeys(s["answer"] for s in payload["support"])),
    })
