"""Proposed node wire boundary and strict raw profile-output protocol."""
from dataclasses import dataclass
from collections.abc import Mapping
import json
import math
from typing import Final, Literal, assert_never

from http_contract import JSON, valid_id
from profile_types import (
    AnalyzeRequest, AnalyzeSuccess, MergeInput, MergeRequest, NodeId, NodeSuccess,
    PersonaId, ProfileBody, ProfileContent, ProfileGenerated, ProfileRequest,
    RecordId, RequestId, SnapshotId, Source, SpeechStyle, Task, TextVersionId,
)

IDENTIFIERS: Final = {"request_id", "persona_id", "source_revision", "source_snapshot_id", "node_id"}


@dataclass(frozen=True, slots=True)
class ProfileError(ValueError):
    code: Literal["shape", "size", "output", "config"]

    def __str__(self) -> str:
        return f"profile: {self.code}"


def decode_json(raw: str) -> JSON:
    """Reject ambiguous keys and non-JSON constants before structural parsing."""
    def unique(pairs: list[tuple[str, JSON]]) -> JSON:
        result: dict[str, JSON] = {}
        for key, value in pairs:
            if key in result:
                raise ProfileError("shape")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ProfileError("shape")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)


def fields(value: JSON, keys: set[str]) -> Mapping[str, JSON]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ProfileError("shape")
    return value


def text(value: JSON) -> str:
    if (not isinstance(value, str) or not value.strip()
            or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ProfileError("shape")
    return value


def identifier(value: JSON) -> str:
    if not valid_id(value):
        raise ProfileError("shape")
    return value


def integer(value: JSON) -> int:
    if (type(value) not in (int, float)
            or isinstance(value, float) and not math.isfinite(value)
            or value < 0 or int(value) != value):
        raise ProfileError("shape")
    return int(value)


def strings(value: JSON) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProfileError("shape")
    items = tuple(text(item) for item in value)
    if len(set(items)) != len(items):
        raise ProfileError("shape")
    return items


def parse_profile(value: JSON) -> ProfileContent:
    data = fields(value, {"schema_version", "speech_style", "conversation_habits", "limitations"})
    if integer(data["schema_version"]) != 1:
        raise ProfileError("shape")
    speech = fields(data["speech_style"],
                    {"tone", "address_terms", "sentence_endings", "frequent_expressions"})
    return ProfileContent(
        SpeechStyle(strings(speech["tone"]), strings(speech["address_terms"]),
                    strings(speech["sentence_endings"]), strings(speech["frequent_expressions"])),
        strings(data["conversation_habits"]), strings(data["limitations"]))


def profile_fits(content: ProfileContent) -> bool:
    speech = content.speech_style
    return all(len(items) <= 16 and all(len(item) <= 512 for item in items)
               for items in (speech.tone, speech.address_terms, speech.sentence_endings,
                             speech.frequent_expressions, content.conversation_habits,
                             content.limitations))


def parse_request(value: JSON, task: Task) -> ProfileRequest:
    """Complete all shape checks before applying parsed-size ceilings."""
    key = "sources" if task == "analyze" else "inputs"
    data = fields(value, IDENTIFIERS | {key})
    identity = (RequestId(identifier(data["request_id"])),
                PersonaId(identifier(data["persona_id"])), integer(data["source_revision"]),
                SnapshotId(identifier(data["source_snapshot_id"])), NodeId(identifier(data["node_id"])))
    values = data[key]
    if not isinstance(values, list):
        raise ProfileError("shape")
    match task:
        case "analyze":
            sources = []
            for item in values:
                source = fields(item, {"record_id", "text_version_id", "target_utterances"})
                utterances = source["target_utterances"]
                if not isinstance(utterances, list) or not utterances:
                    raise ProfileError("shape")
                sources.append(Source(RecordId(identifier(source["record_id"])),
                    TextVersionId(identifier(source["text_version_id"])),
                    tuple(text(utterance) for utterance in utterances)))
            if not sources or len({s.text_version_id for s in sources}) != len(sources):
                raise ProfileError("shape")
            if (len(sources) > 20 or any(len(s.target_utterances) > 100
                    or any(len(t) > 16000 for t in s.target_utterances) for s in sources)):
                raise ProfileError("size")
            return AnalyzeRequest(*identity, tuple(sources))
        case "merge":
            inputs = []
            for item in values:
                child = fields(item, {"node_id", "profile_content"})
                inputs.append(MergeInput(NodeId(identifier(child["node_id"])),
                                         parse_profile(child["profile_content"])))
            if (len(inputs) < 2 or len({i.node_id for i in inputs}) != len(inputs)
                    or any(i.node_id == identity[4] for i in inputs)):
                raise ProfileError("shape")
            if len(inputs) > 8 or any(not profile_fits(i.profile_content) for i in inputs):
                raise ProfileError("size")
            return MergeRequest(*identity, tuple(inputs))
        case unreachable:
            assert_never(unreachable)


def profile_body(content: ProfileContent) -> ProfileBody:
    speech = content.speech_style
    return {"schema_version": 1, "speech_style": {
        "tone": list(speech.tone), "address_terms": list(speech.address_terms),
        "sentence_endings": list(speech.sentence_endings),
        "frequent_expressions": list(speech.frequent_expressions)},
        "conversation_habits": list(content.conversation_habits),
        "limitations": list(content.limitations)}


def parse_output(raw: str) -> ProfileContent:
    content = parse_profile(decode_json(raw))
    if not profile_fits(content):
        raise ProfileError("output")
    return content


def success_body(request: ProfileRequest, generated: ProfileGenerated) -> NodeSuccess | AnalyzeSuccess:
    body: NodeSuccess = {
        "request_id": request.request_id, "persona_id": request.persona_id,
        "source_revision": request.source_revision, "source_snapshot_id": request.source_snapshot_id,
        "node_id": request.node_id, "profile_content": profile_body(generated.profile_content),
        "model_name": generated.model_name}
    match request:
        case AnalyzeRequest():
            return AnalyzeSuccess(**body, source_text_version_ids=[
                s.text_version_id for s in request.sources])
        case MergeRequest():
            return body
        case unreachable:
            assert_never(unreachable)
