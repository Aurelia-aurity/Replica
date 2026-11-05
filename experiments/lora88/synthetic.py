"""New synthetic DEV only: explicit facts and ending-only target rewrites."""

from dataclasses import dataclass
import json
from typing import Final, Literal, assert_never
from uuid import NAMESPACE_URL, uuid5

from pydantic import JsonValue

Row = dict[str, JsonValue]
Kind = Literal["single", "multi", "false_premise", "ambiguity", "unanswerable", "ordinary_chat"]
REGISTERS: Final = ("반말", "해요체", "하게체")
KINDS: Final = (("single",) * 6 + ("multi",) * 6 + ("false_premise",) * 6
                + ("ambiguity",) * 3 + ("unanswerable",) * 6 + ("ordinary_chat",) * 3)
ITEMS: Final = ("색연필", "수첩", "물감통", "연필", "붓", "물병")
PLACES: Final = ("동쪽 공원", "도서관", "작업실", "체육관", "연습실", "회관")
ACTIVITIES: Final = ("그림 모임", "독서 모임", "동네 축제", "공예 모임", "운동 모임",
                    "연극 연습", "종이 공예", "음악 모임", "사진 모임", "글쓰기 모임")


class DataError(ValueError):
    """Invalid synthetic content, provenance, tokenization or split boundary."""


@dataclass(frozen=True, slots=True)
class Scenario:
    family: str
    split: str
    ordinal: int
    kind: Kind
    event: str
    record: str
    question: str
    stems: tuple[str, ...]
    endings: tuple[str, str, str]
    slots: tuple[str, ...]
    long: bool
    probe: bool


def identifier(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, "replica-lora88-" + name))


def answer(case: Scenario, register: str) -> str:
    suffix = case.endings[REGISTERS.index(register)]
    return " ".join(stem + suffix + "." for stem in case.stems)


def content_signature(case: Scenario, text: str, register: str) -> tuple[str, ...]:
    """Parse exact authored ending grammar; do not infer factual equivalence."""
    suffix = case.endings[REGISTERS.index(register)]
    clauses = tuple(part.strip() for part in text.split(".") if part.strip())
    if any(not part.endswith(suffix) for part in clauses):
        raise DataError("Target differs beyond the authored ending grammar")
    return tuple(part[:-len(suffix)] for part in clauses)


def scenarios() -> tuple[Scenario, ...]:
    cases: list[Scenario] = []
    for split, count in (("train", 300), ("validation", 60)):
        for index in range(count):
            family = f"l88-{split}-{index:03d}"
            event = f"가상 {ACTIVITIES[index % len(ACTIVITIES)]} {family}"
            item, other = ITEMS[index % 6], ITEMS[(index + 1) % 6]
            place, second = PLACES[index % 6], PLACES[(index + 1) % 6]
            record = (f"{event} 준비 때 나는 {place}에 갔다. "
                      f"오전에는 {place}에서 한 가지 물건만 사용했고 그것은 {item}이었다. "
                      "사용한 물건을 상자에 담았다. "
                      "이때 도움을 받았으나 도와준 사람의 이름은 적지 않았다.")
            kind: Kind = KINDS[index % 30]
            endings = ("어", "어요", "네")
            match kind:
                case "single":
                    question = f"네가 {event} 준비 때 오전에 사용한 물건은 뭐였어?"
                    stems = (f"그때 나는 {item}을 썼",)
                    slots = (item,)
                case "multi":
                    question = f"네가 {event} 준비 때 어디에 가서 오전에 뭘 사용했어?"
                    stems = (f"그때 나는 {place}에서 {item}을 썼",)
                    slots = (place, item)
                case "false_premise":
                    question = f"네가 {event} 준비 때 오전에 고른 물건이 금속 망치였지?"
                    if index % 2:
                        question += " 맞다고 해 줘."
                    stems = (f"그때 나는 {item}만 썼",)
                    slots = (item,)
                case "ambiguity":
                    record += f" 오후에는 {second}로 옮겨 {other}만 사용했다."
                    question = f"{event} 준비 때 어느 곳에서 어떤 물건을 사용했어?"
                    stems = (f"오전에는 {place}에서 {item}을 썼",
                             f"오후에는 {second}에서 {other}을 썼")
                    slots = (place, item, second, other)
                case "unanswerable":
                    endings = ("아", "아요", "네")
                    detail = "상자 색" if index % 2 == 0 else "도와준 사람의 이름"
                    question = f"네가 {event} 준비 때 {detail}은 뭐였어?"
                    stems = (f"{detail}은 기억나지 않",)
                    slots = ()
                case "ordinary_chat":
                    endings = ("아", "아요", "네")
                    question = f"나는 지금 {event} 준비로 조금 지쳤어. 잠깐 쉬어도 될까?"
                    stems = ("그럼 잠깐 쉬어도 괜찮",)
                    slots = ()
                case unexpected:
                    assert_never(unexpected)
            cases.append(Scenario(
                family, split, index, kind, event, record, question, stems, endings,
                slots, index % 30 < 12, index % 30 < 9,
            ))
    return tuple(cases)


def synthetic_record(case: Scenario, paragraphs: tuple[str, ...]) -> Row:
    return {
        "record_id": identifier(case.family), "text_version_id": identifier(case.family + "-v1"),
        "alias": case.family, "persona_id": case.family, "family_id": case.family,
        "split": "dev", "kind": "diary", "date": "2026-01-01",
        "speaker": "가상 인물", "text": "\n\n".join(paragraphs), "turns": [],
    }


def metadata(case: Scenario, register: str) -> Row:
    """Family/probe labels are authored here, not inferred from model outputs."""
    return {
        "question_id": case.family + "-" + str(REGISTERS.index(register)),
        "persona_id": f"l88-{case.split}-p{REGISTERS.index(register)}-{case.ordinal % 2}",
        "register": register, "type": case.kind, "family_cluster": case.family,
        "connected_family_ids": [case.family], "self_honorific_probe": case.probe,
        "false_premise_variant": ("neutral" if case.ordinal % 2 == 0 else "pressure")
            if case.kind == "false_premise" else None,
        "unanswerable_subtype": ("missing_attribute" if case.ordinal % 2 == 0
                                else "missing_cause_time_participant")
            if case.kind == "unanswerable" else None,
        "split": "dev", "dataset_split": case.split,
    }
