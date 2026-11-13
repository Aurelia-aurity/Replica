"""Pre-persistence source values; identities are supplied, never issued here."""
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal, assert_never
from uuid import UUID

Role = Literal["persona", "provider", "other"]
Kind = Literal["diary", "chat"]
CHUNKER_VERSION: Final = "lossless-unit-spans-v1"


@dataclass(frozen=True, slots=True)
class EncodingError(ValueError):
    code: str

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class SourceUnit:
    text: str
    speaker: str
    role: Role
    date: str
    turn: int | None = None

    def __post_init__(self) -> None:
        if not self.text or not self.speaker.strip():
            raise EncodingError("invalid_source_unit")
        if self.role not in ("persona", "provider", "other"):
            raise EncodingError("invalid_role")
        try:
            date.fromisoformat(self.date)
        except ValueError:
            raise EncodingError("invalid_date") from None
        if self.turn is not None and (type(self.turn) is not int or self.turn < 0):
            raise EncodingError("invalid_turn")


@dataclass(frozen=True, slots=True)
class SourceRecord:
    record_id: UUID
    text_version_id: UUID
    persona_id: UUID
    revision: int
    source_revision: int
    kind: Kind
    units: tuple[SourceUnit, ...]

    def __post_init__(self) -> None:
        if any(not isinstance(value, UUID) for value in
               (self.record_id, self.text_version_id, self.persona_id)):
            raise EncodingError("invalid_source_identity")
        if (type(self.revision) is not int or self.revision < 1
                or type(self.source_revision) is not int or self.source_revision < 0):
            raise EncodingError("invalid_source_revision")
        if not isinstance(self.units, tuple) or not self.units:
            raise EncodingError("empty_source")
        if self.kind not in ("diary", "chat"):
            raise EncodingError("invalid_kind")
        match self.kind:
            case "diary":
                if any(unit.turn is not None for unit in self.units):
                    raise EncodingError("diary_turn")
            case "chat":
                turns = tuple(unit.turn for unit in self.units)
                if any(turn is None for turn in turns):
                    raise EncodingError("missing_turn")
                if len(set(turns)) != len(turns) or list(turns) != sorted(turns):
                    raise EncodingError("unordered_turns")
            case unreachable:
                assert_never(unreachable)


@dataclass(frozen=True, slots=True)
class EncodingLimits:
    max_codepoints: int = 16384
    max_units: int = 256
    max_token_calls: int = 20000
    max_rendered_codepoints: int = 2000000
    max_chunks: int = 4096

    def __post_init__(self) -> None:
        if any(type(v) is not int or v < 1 for v in (
                self.max_codepoints, self.max_units, self.max_token_calls,
                self.max_rendered_codepoints, self.max_chunks)):
            raise EncodingError("invalid_limits")


@dataclass(frozen=True, slots=True)
class SourceSpan:
    unit_index: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    source: SourceRecord
    ordinal: int
    content: str
    spans: tuple[SourceSpan, ...]
    speaker: str
    role: Role
    date: str
    source_turns: tuple[int, ...]
    chunker_version: str = CHUNKER_VERSION

    def rendered(self) -> str:
        body = "\n".join(
            self.source.units[span.unit_index].text[span.start:span.end]
            for span in self.spans)
        return f"{self.date} | {self.speaker} ({self.role})\n{body}"


@dataclass(frozen=True, slots=True)
class EncodingProvenance:
    intended_model: str
    intended_revision: str
    actual_revision: None = None
    identity_status: str = "unverified_local_directory"
    representation: str = "immutable-float32-rows-l2"
    purpose: str = "offline-local-candidate"
    dimensions: int = 1024
    max_tokens: int = 512


@dataclass(frozen=True, slots=True)
class EncodedRows:
    rows: tuple[tuple[float, ...], ...]
    provenance: EncodingProvenance
    drafts: tuple[ChunkDraft, ...] = ()
