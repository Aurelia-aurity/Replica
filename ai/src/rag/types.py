"""Immutable service inputs. Callers supply authorized, current archive chunks."""
from dataclasses import dataclass
from typing import Literal

Role = Literal["persona", "provider", "other"]


@dataclass(frozen=True, slots=True)
class Evidence:
    chunk_id: str
    record_id: str
    text_version_id: str
    content: str
    speaker_role: Role


@dataclass(frozen=True, slots=True)
class Chunk(Evidence):
    persona_id: str
    speaker: str
    date: str
    ordinal: int
    source_turns: tuple[int, ...]

    def evidence(self) -> Evidence:
        return Evidence(self.chunk_id, self.record_id, self.text_version_id,
                        self.content, self.speaker_role)


@dataclass(frozen=True, slots=True)
class Record:
    record_id: str
    text_version_id: str
    persona_id: str
    kind: Literal["diary", "chat"]
    turn_count: int


@dataclass(frozen=True, slots=True)
class Hit:
    chunk_id: str
    score: float


@dataclass(frozen=True, slots=True)
class Retrieval:
    persona_id: str
    top5: tuple[Hit, ...]
    evidence: tuple[Evidence, ...] = ()
    topk_evidence: tuple[Evidence, ...] = ()
    predicted_no_evidence: bool = False
