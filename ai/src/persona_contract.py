"""Section 3 draft + PR #75 wire boundary, isolated from generation policy.

Source: PR #75 at 104a241. These fields are not an agreed guard identity.
No model-output JSON protocol or caller/database DTO is defined here.
"""
from dataclasses import dataclass
import math
import re
from typing import Literal, TypeAlias, TypedDict

from inference import Prepared
from rag.types import Evidence

JSON: TypeAlias = None | bool | int | float | str | list["JSON"] | dict[str, "JSON"]
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class PayloadTooLarge(ValueError):
    """A structurally valid request exceeds a parsed contract size limit."""


def fields(value: JSON, keys: set[str]) -> dict[str, JSON]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError
    return value


def text(value: JSON, *, nonblank: bool = True) -> str:
    if not isinstance(value, str) or not value or (nonblank and not value.strip()):
        raise ValueError
    return value


def uuid(value: JSON) -> str:
    if not isinstance(value, str) or UUID_RE.fullmatch(value) is None:
        raise ValueError
    return value


def integer(value: JSON, minimum: int, maximum: int | None = None) -> int:
    if type(value) not in (int, float):
        raise ValueError
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError
    if int(value) != value or value < minimum or (maximum is not None and value > maximum):
        raise ValueError
    return int(value)


@dataclass(frozen=True, slots=True)
class PersonaNames:
    display_name: str
    provider_address: str | None


@dataclass(frozen=True, slots=True)
class ProfileContent:
    tone: tuple[str, ...]
    address_terms: tuple[str, ...]
    sentence_endings: tuple[str, ...]
    frequent_expressions: tuple[str, ...]
    conversation_habits: tuple[str, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StyleProfile:
    id: str
    version: int
    source_revision: int
    source_snapshot_id: str
    content: ProfileContent


@dataclass(frozen=True, slots=True)
class PersonaRequest:
    request_id: str
    persona_id: str
    persona: PersonaNames
    source_revision: int
    message: str
    history: tuple[tuple[str, str], ...]
    max_new_tokens: int
    style_profile: StyleProfile
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True, slots=True)
class PersonaGenerated:
    """Typed generator seam, not a decision about the model's output encoding."""
    message: str
    finish_reason: Literal["stop", "length"]
    output_tokens: int
    evidence_used: tuple[str, ...]
    model_name: str


class Context(TypedDict):
    used_history_turns: int
    dropped_history_turns: int


class AppliedProfile(TypedDict):
    id: str
    version: int
    source_revision: int


class PersonaSuccess(TypedDict):
    request_id: str
    message: str
    finish_reason: Literal["stop", "length"]
    ai_generated: Literal[True]
    context: Context
    applied_profile: AppliedProfile
    evidence_used: list[str]
    model_name: str


def parse_request(value: JSON) -> PersonaRequest:
    """Check all structural constraints before any parsed-size rejection."""
    data = fields(value, {"request_id", "persona_id", "persona", "source_revision",
                          "message", "history", "max_new_tokens", "style_profile", "evidence"})
    request_id, persona_id = uuid(data["request_id"]), uuid(data["persona_id"])
    names = fields(data["persona"], {"display_name", "provider_address"})
    display = text(names["display_name"])
    address_value = names["provider_address"]
    address = None if address_value is None else text(address_value, nonblank=False)
    revision = integer(data["source_revision"], 0)
    message = text(data["message"])
    budget = integer(data["max_new_tokens"], 1, 1024)
    history_value = data["history"]
    if not isinstance(history_value, list):
        raise ValueError
    history = []
    for value in history_value:
        pair = fields(value, {"user", "assistant"})
        history.append((text(pair["user"]), text(pair["assistant"])))
    profile = fields(data["style_profile"],
                     {"id", "version", "source_revision", "source_snapshot_id", "content"})
    profile_id, snapshot = uuid(profile["id"]), uuid(profile["source_snapshot_id"])
    version, profile_revision = integer(profile["version"], 1), integer(profile["source_revision"], 0)
    if revision != profile_revision:
        raise ValueError
    content = fields(profile["content"],
                     {"schema_version", "speech_style", "conversation_habits", "limitations"})
    if integer(content["schema_version"], 1, 1) != 1:
        raise ValueError
    speech = fields(content["speech_style"],
                    {"tone", "address_terms", "sentence_endings", "frequent_expressions"})
    arrays = {}
    for key, value in {
            **speech, "conversation_habits": content["conversation_habits"],
            "limitations": content["limitations"]}.items():
        if not isinstance(value, list):
            raise ValueError
        items = tuple(text(item) for item in value)
        if len(set(items)) != len(items):
            raise ValueError
        arrays[key] = items
    profile_content = ProfileContent(
        arrays["tone"], arrays["address_terms"], arrays["sentence_endings"],
        arrays["frequent_expressions"], arrays["conversation_habits"], arrays["limitations"])
    evidence_value = data["evidence"]
    if not isinstance(evidence_value, list):
        raise ValueError
    evidence = []
    for value in evidence_value:
        item = fields(value, {"chunk_id", "record_id", "text_version_id", "speaker_role", "content"})
        role = item["speaker_role"]
        if role not in ("persona", "provider", "other"):
            raise ValueError
        evidence.append(Evidence(uuid(item["chunk_id"]), uuid(item["record_id"]),
                                 uuid(item["text_version_id"]), text(item["content"]), role))
    if len({item.chunk_id for item in evidence}) != len(evidence):
        raise ValueError
    if (len(display) > 50 or (address is not None and len(address) > 50)
            or len(message) > 16000 or len(history) > 20 or len(evidence) > 10
            or any(len(t) > 16000 for pair in history for t in pair)
            or any(len(a) > 16 or any(len(t) > 512 for t in a) for a in arrays.values())
            or any(len(item.content) > 16000 for item in evidence)):
        raise PayloadTooLarge
    return PersonaRequest(
        request_id, persona_id, PersonaNames(display, address), revision, message, tuple(history),
        budget, StyleProfile(profile_id, version, profile_revision, snapshot, profile_content),
        tuple(evidence))


def success_body(request: PersonaRequest, prepared: Prepared,
                 result: PersonaGenerated) -> PersonaSuccess:
    """Validate the external success contract, without parsing model output."""
    text(result.message)
    text(result.model_name)
    integer(result.output_tokens, 0)
    used = integer(prepared.used_history_turns, 0, 20)
    dropped = integer(prepared.dropped_history_turns, 0, 20)
    if (len(result.message) > 16000 or len(result.model_name) > 512
            or result.finish_reason not in ("stop", "length")
            or used + dropped != len(request.history)
            or len(set(result.evidence_used)) != len(result.evidence_used)
            or not set(result.evidence_used) <= {item.chunk_id for item in request.evidence}):
        raise ValueError
    return {
        "request_id": request.request_id, "message": result.message,
        "finish_reason": result.finish_reason, "ai_generated": True,
        "context": {"used_history_turns": used, "dropped_history_turns": dropped},
        "applied_profile": {"id": request.style_profile.id,
                            "version": request.style_profile.version,
                            "source_revision": request.style_profile.source_revision},
        "evidence_used": list(result.evidence_used), "model_name": result.model_name,
    }
