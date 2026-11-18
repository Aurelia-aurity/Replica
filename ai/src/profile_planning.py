"""Local analyze planning; supplied snapshot IDs imply no DB freshness or authority.

Inputs are typed immutable values, not a JSON parsing boundary. Offsets are Python
Unicode code points, not UTF-8 bytes or grapheme clusters. No text is normalized.
Only nonblank original utterances are accepted, matching the proposed wire schema.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
import json
from typing import Literal, NewType
from uuid import UUID, uuid4

RecordId = NewType("RecordId", UUID)
TextVersionId = NewType("TextVersionId", UUID)
PersonaId = NewType("PersonaId", UUID)
SnapshotId = NewType("SnapshotId", UUID)
RequestId = NewType("RequestId", UUID)
NodeId = NewType("NodeId", UUID)
ErrorCode = Literal["input", "limits", "search_budget", "infeasible", "counter", "coverage"]


@dataclass(frozen=True, slots=True)
class PlanningError(Exception):
    """Safe failure category; never include source text or callback exceptions."""

    code: ErrorCode

    def __str__(self) -> str:
        return f"profile planning: {self.code}"


@dataclass(frozen=True, slots=True)
class Binding:
    persona_id: PersonaId
    source_revision: int
    source_snapshot_id: SnapshotId
    model_revision: str
    tokenizer_revision: str
    template_revision: str

    def __post_init__(self) -> None:
        if type(self.source_revision) is not int or self.source_revision < 0 or not all(
            value.strip() for value in
            (self.model_revision, self.tokenizer_revision, self.template_revision)
        ):
            raise PlanningError("input")


@dataclass(frozen=True, slots=True)
class Source:
    record_id: RecordId
    text_version_id: TextVersionId
    target_utterances: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Corpus:
    binding: Binding
    sources: tuple[Source, ...]

    def __post_init__(self) -> None:
        if (not self.sources
                or len({s.record_id for s in self.sources}) != len(self.sources)
                or len({s.text_version_id for s in self.sources}) != len(self.sources)):
            raise PlanningError("input")
        for source in self.sources:
            if not source.target_utterances:
                raise PlanningError("input")
            for text in source.target_utterances:
                if not text.strip() or any(0xD800 <= ord(c) <= 0xDFFF for c in text):
                    raise PlanningError("input")


@dataclass(frozen=True, slots=True)
class Limits:
    """Caller-selected work bounds and deployment limits; no operational defaults."""

    input_characters: int
    input_utterances: int
    candidates: int
    leaves: int
    sources_per_request: int
    utterances_per_source: int
    characters_per_utterance: int
    body_bytes: int
    model_context_tokens: int
    server_context_tokens: int
    reserved_output_tokens: int

    def __post_init__(self) -> None:
        if (min(self.input_characters, self.input_utterances, self.candidates,
                self.leaves, self.body_bytes, self.model_context_tokens,
                self.server_context_tokens, self.reserved_output_tokens) < 1
                or not 1 <= self.sources_per_request <= 20
                or not 1 <= self.utterances_per_source <= 100
                or not 1 <= self.characters_per_utterance <= 16000):
            raise PlanningError("limits")


@dataclass(frozen=True, slots=True)
class AnalyzeRequest:
    request_id: RequestId
    node_id: NodeId
    binding: Binding
    sources: tuple[Source, ...]

    def body(self) -> bytes:
        """Exact compact UTF-8 body to transmit; local runtime binding is excluded."""
        return json.dumps({
            "request_id": str(self.request_id), "node_id": str(self.node_id),
            "persona_id": str(self.binding.persona_id),
            "source_revision": self.binding.source_revision,
            "source_snapshot_id": str(self.binding.source_snapshot_id),
            "sources": [
                {"record_id": str(s.record_id), "text_version_id": str(s.text_version_id),
                 "target_utterances": s.target_utterances} for s in self.sources
            ],
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True, slots=True)
class TextRange:
    source_index: int
    utterance_index: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Leaf:
    request: AnalyzeRequest
    ranges: tuple[TextRange, ...]


@dataclass(frozen=True, slots=True)
class Plan:
    binding: Binding
    leaves: tuple[Leaf, ...]


def plan_leaves(
    corpus: Corpus, limits: Limits, count_prompt_tokens: Callable[[AnalyzeRequest], int],
) -> Plan:
    """Find a bounded path of contiguous intervals without token monotonicity.

    The callback must count the COMPLETE fixed rendered prompt (including all
    metadata/template overhead) for the bound runtime, return a nonnegative int,
    and terminate. Output reservation is added here. No callback estimate suffices.
    Search utterance boundaries first, then all code-point boundaries if needed.
    Each pass stores one predecessor per reachable boundary; token failures never
    prune longer candidates. Natural cuts take priority over globally fewest
    leaves. Both passes share the work budget; only full exhaustion proves
    infeasibility. UUIDs are local probes until the returned plan binds each one
    to a single immutable request; no persistence or dispatch happens here.
    """
    positions: list[tuple[int, int, int, int]] = []
    total = 0
    for si, source in enumerate(corpus.sources):
        for ui, text in enumerate(source.target_utterances):
            positions.append((si, ui, total, total + len(text)))
            total += len(text)
    if total > limits.input_characters or len(positions) > limits.input_utterances:
        raise PlanningError("limits")
    pending = deque([(0, 0)])
    previous: dict[int, tuple[int, Leaf]] = {}
    considered = 0
    preferred = len(positions) > 1
    boundaries = tuple(hi for _, _, _, hi in reversed(positions))
    while pending or preferred:
        if not pending:
            preferred = False
            previous.clear()
            pending.append((0, 0))
        start, depth = pending.popleft()
        if depth == limits.leaves:
            continue
        for end in boundaries if preferred else range(total, start, -1):
            if end <= start or end in previous:
                continue
            considered += 1
            if considered > limits.candidates:
                raise PlanningError("search_budget")
            ranges = tuple(
                TextRange(si, ui, max(start, lo) - lo, min(end, hi) - lo)
                for si, ui, lo, hi in positions if lo < end and start < hi
            )
            grouped: dict[int, list[str]] = {}
            for r in ranges:
                text = corpus.sources[r.source_index].target_utterances[r.utterance_index]
                grouped.setdefault(r.source_index, []).append(text[r.start:r.end])
            if (len(grouped) > limits.sources_per_request
                    or any(len(texts) > limits.utterances_per_source
                           for texts in grouped.values())
                    or any(not text.strip() or len(text) > limits.characters_per_utterance
                           for texts in grouped.values() for text in texts)):
                continue
            request = AnalyzeRequest(RequestId(uuid4()), NodeId(uuid4()), corpus.binding, tuple(
                Source(corpus.sources[si].record_id, corpus.sources[si].text_version_id,
                       tuple(texts)) for si, texts in grouped.items()
            ))
            if len(request.body()) > limits.body_bytes:
                continue
            tokens = count_prompt_tokens(request)
            if type(tokens) is not int or tokens < 0:
                raise PlanningError("counter")
            if tokens + limits.reserved_output_tokens > min(
                limits.model_context_tokens, limits.server_context_tokens,
            ):
                continue
            previous[end] = (start, Leaf(request, ranges))
            if end == total:
                leaves: list[Leaf] = []
                cursor = total
                while cursor:
                    cursor, leaf = previous[cursor]
                    leaves.append(leaf)
                return Plan(corpus.binding, tuple(reversed(leaves)))
            pending.append((end, depth + 1))
    raise PlanningError("infeasible")


def validate_coverage(corpus: Corpus, plan: Plan) -> None:
    """Independently walk supplied originals and payloads, never the planner graph.

    Reject gaps, overlaps, reordering, altered text/identity/binding and reused
    node/request IDs. This proves local coverage only, not DB authorization,
    freshness, token fit, model quality, or eventual publication.
    """
    if plan.binding != corpus.binding or not plan.leaves:
        raise PlanningError("coverage")
    source_index = utterance_index = offset = 0
    nodes: set[NodeId] = set()
    requests: set[RequestId] = set()
    for leaf in plan.leaves:
        request = leaf.request
        if (request.binding != corpus.binding or request.node_id in nodes
                or request.request_id in requests or not request.sources):
            raise PlanningError("coverage")
        nodes.add(request.node_id)
        requests.add(request.request_id)
        fragments = [(s, text) for s in request.sources for text in s.target_utterances]
        if (len(fragments) != len(leaf.ranges) or not fragments
                or len({s.text_version_id for s in request.sources}) != len(request.sources)
                or any(not s.target_utterances for s in request.sources)):
            raise PlanningError("coverage")
        for r, (source, text) in zip(leaf.ranges, fragments, strict=True):
            if source_index >= len(corpus.sources):
                raise PlanningError("coverage")
            original = corpus.sources[source_index]
            utterance = original.target_utterances[utterance_index]
            if (r.source_index != source_index or r.utterance_index != utterance_index
                    or r.start != offset or not offset < r.end <= len(utterance)
                    or source.record_id != original.record_id
                    or source.text_version_id != original.text_version_id
                    or text != utterance[offset:r.end] or not text.strip()):
                raise PlanningError("coverage")
            offset = r.end
            if offset == len(utterance):
                offset = 0
                utterance_index += 1
                if utterance_index == len(original.target_utterances):
                    utterance_index = 0
                    source_index += 1
    if source_index != len(corpus.sources) or utterance_index or offset:
        raise PlanningError("coverage")
