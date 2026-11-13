"""Immutable node inputs and profile values; no coordinator or database state."""
from dataclasses import dataclass
from typing import Literal, NewType, TypedDict

RequestId = NewType("RequestId", str)
PersonaId = NewType("PersonaId", str)
SnapshotId = NewType("SnapshotId", str)
NodeId = NewType("NodeId", str)
RecordId = NewType("RecordId", str)
TextVersionId = NewType("TextVersionId", str)
Task = Literal["analyze", "merge"]


@dataclass(frozen=True, slots=True)
class SpeechStyle:
    tone: tuple[str, ...]
    address_terms: tuple[str, ...]
    sentence_endings: tuple[str, ...]
    frequent_expressions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProfileContent:
    speech_style: SpeechStyle
    conversation_habits: tuple[str, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Source:
    record_id: RecordId
    text_version_id: TextVersionId
    target_utterances: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NodeRequest:
    request_id: RequestId
    persona_id: PersonaId
    source_revision: int
    source_snapshot_id: SnapshotId
    node_id: NodeId


@dataclass(frozen=True, slots=True)
class AnalyzeRequest(NodeRequest):
    sources: tuple[Source, ...]


@dataclass(frozen=True, slots=True)
class MergeInput:
    node_id: NodeId
    profile_content: ProfileContent


@dataclass(frozen=True, slots=True)
class MergeRequest(NodeRequest):
    inputs: tuple[MergeInput, ...]


ProfileRequest = AnalyzeRequest | MergeRequest


@dataclass(frozen=True, slots=True)
class ProfileGenerated:
    profile_content: ProfileContent
    model_name: str
    output_tokens: int


class SpeechBody(TypedDict):
    tone: list[str]
    address_terms: list[str]
    sentence_endings: list[str]
    frequent_expressions: list[str]


class ProfileBody(TypedDict):
    schema_version: Literal[1]
    speech_style: SpeechBody
    conversation_habits: list[str]
    limitations: list[str]


class NodeSuccess(TypedDict):
    request_id: str
    persona_id: str
    source_revision: int
    source_snapshot_id: str
    node_id: str
    profile_content: ProfileBody
    model_name: str


class AnalyzeSuccess(NodeSuccess):
    source_text_version_ids: list[str]
