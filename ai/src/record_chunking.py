"""Bounded complete partition search; token-prefix counts need not be monotone."""
from collections.abc import Callable, Iterator
from dataclasses import replace
import re
from typing import assert_never

from encoding_types import (
    ChunkDraft, EncodingError, EncodingLimits, SourceRecord, SourceSpan,
)


def chunk_record(
    source: SourceRecord,
    count_tokens: Callable[[str], int],
    limits: EncodingLimits = EncodingLimits(),
) -> tuple[ChunkDraft, ...]:
    """Preserve every supplied codepoint, including entirely blank units.

    Empty strings/records are rejected by the input values. Blank nonempty units
    are retained and encoded with their explicit metadata.
    """
    size = sum(len(unit.text) for unit in source.units)
    if size > limits.max_codepoints or len(source.units) > limits.max_units:
        raise EncodingError("input_budget")
    calls = rendered_size = 0

    def fits(draft: ChunkDraft) -> bool:
        nonlocal calls, rendered_size
        rendered = draft.rendered()
        calls += 1
        rendered_size += len(rendered)
        if calls > limits.max_token_calls or rendered_size > limits.max_rendered_codepoints:
            raise EncodingError("token_work_budget")
        count = count_tokens(rendered)
        if type(count) is not int or count < 0:
            raise EncodingError("invalid_token_count")
        return count <= 512

    groups: list[tuple[SourceSpan, ...]] = []
    index = 0
    while index < len(source.units):
        unit = source.units[index]
        match source.kind:
            case "diary":
                groups.append((SourceSpan(index, 0, len(unit.text)),))
                index += 1
            case "chat":
                spans = [SourceSpan(index, 0, len(unit.text))]
                while index + len(spans) < len(source.units):
                    previous = source.units[index + len(spans) - 1]
                    other = source.units[index + len(spans)]
                    if ((unit.speaker, unit.role, unit.date) !=
                            (other.speaker, other.role, other.date)
                            or previous.turn is None or other.turn != previous.turn + 1):
                        break
                    spans.append(SourceSpan(index + len(spans), 0, len(other.text)))
                groups.append(tuple(spans))
                index += len(spans)
            case unreachable:
                assert_never(unreachable)
    drafts: list[ChunkDraft] = []
    for group in groups:
        text = "".join(source.units[span.unit_index].text[span.start:span.end]
                       for span in group)
        unit = source.units[group[0].unit_index]
        unit_ends = []
        offset = 0
        for span in group:
            offset += span.end - span.start
            unit_ends.append(offset)

        def draft_for(bounds: tuple[int, int]) -> ChunkDraft:
            start, end = bounds
            spans = []
            offset = 0
            for span in group:
                length = span.end - span.start
                left, right = max(start, offset), min(end, offset + length)
                if left < right:
                    spans.append(SourceSpan(span.unit_index, span.start + left - offset,
                                            span.start + right - offset))
                offset += length
            turns = tuple(turn for s in spans
                          if (turn := source.units[s.unit_index].turn) is not None)
            return ChunkDraft(source, 0, text[start:end], tuple(spans),
                              unit.speaker, unit.role, unit.date, turns)

        # DFS memoizes dead suffixes. Trying every endpoint allows 2/3 to fit
        # while 1/4 fail, and backtracking avoids stranding an unencodable tail.
        def endpoints(start: int) -> Iterator[int]:
            match source.kind:
                case "diary":
                    maximum = len(text)
                    preferred = next((m.end() for m in re.finditer(r"\n[ \t]*\n", text)
                                      if m.end() > start), maximum)
                case "chat":
                    current = next(i for i, end in enumerate(unit_ends) if end > start)
                    maximum = unit_ends[min(current + 1, len(unit_ends) - 1)]
                    preferred = maximum
                case unreachable:
                    assert_never(unreachable)
            yield preferred
            if maximum != preferred:
                yield maximum
            # A short probe keeps ordinary long inputs cheap; all other endpoints
            # remain available, so this is scheduling, not a token-length bound.
            short = min(start + 256, maximum)
            if short not in (preferred, maximum):
                yield short
            for end in range(maximum - 1, start, -1):
                if end not in (short, preferred):
                    yield end

        choices = {0: endpoints(0)}
        path = [0]
        dead: set[int] = set()
        while path and path[-1] != len(text):
            start = path[-1]
            end = next(choices[start], start)
            if end <= start:
                dead.add(start)
                path.pop()
                continue
            if end in dead:
                continue
            candidate = draft_for((start, end))
            if fits(candidate):
                path.append(end)
                if end != len(text):
                    choices[end] = endpoints(end)
        if not path:
            raise EncodingError("unencodable_source")
        for bounds in zip(path, path[1:]):
            if len(drafts) >= limits.max_chunks:
                raise EncodingError("chunk_budget")
            drafts.append(replace(draft_for(bounds), ordinal=len(drafts)))
    return tuple(drafts)
