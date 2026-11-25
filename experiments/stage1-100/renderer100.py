"""O3 changes model-facing content only, from trusted original chunk metadata."""

from collections.abc import Mapping, Sequence
from typing import Final, assert_never
from uuid import UUID

from corpus import Chunk, Evidence
from retrieval_v2 import contextual_evidence

GLOSSES: Final = {
    "provider": "정보 제공자의 발화(답하는 인물의 발화 아님): ",
    "other": "제삼자의 발화(답하는 인물의 발화 아님): ",
}


def render_o3(evidence: Sequence[Evidence], chunks: Mapping[UUID, Chunk]) -> tuple[Evidence, ...]:
    """Preserve IDs, role, header, ordering and original utterance bytes."""
    rendered = []
    for item in evidence:
        chunk = chunks[item.chunk_id]
        if item != contextual_evidence(chunk):
            raise ValueError("O3 requires exact contextual source evidence")
        match chunk.speaker_role:
            case "persona":
                rendered.append(item)
            case "provider" | "other":
                prefix = f"{chunk.date} | {chunk.speaker} (speaker_role={chunk.speaker_role}) | "
                content = "\n".join(prefix + GLOSSES[chunk.speaker_role] + line
                                    for line in chunk.content.splitlines())
                rendered.append(item.model_copy(update={"content": content}))
            case unreachable:
                assert_never(unreachable)
    return tuple(rendered)
