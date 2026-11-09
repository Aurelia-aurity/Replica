"""Author one new event family and its oracle from a seeded semantic tuple."""

from dataclasses import dataclass
from datetime import date, timedelta
import random
from typing import Literal, assert_never
from uuid import NAMESPACE_URL, UUID, uuid5

from corpus import Persona, QuestionType, Record, Turn, chunk_record
from dev_schema import Conflict, OracleCase, PublicQuestion, RequiredItem
from exemplars import Intent, Register, UtteranceTag
from prepare95 import CasePlan
from corpus import FrozenModel


class Templates(FrozenModel):
    events: tuple[str, ...]
    places: tuple[str, ...]
    objects: tuple[str, ...]
    colors: tuple[str, ...]
    probe_prefix: dict[str, str]
    templates: dict[str, str]
    style_examples: dict[Register, dict[Intent, tuple[str, ...]]]
    leakage_examples: dict[Register, dict[Intent, tuple[str, str]]]
    variant_templates: dict[str, tuple[str, str, str]]


@dataclass(frozen=True, slots=True)
class Event:
    title: str
    date: str
    time: str
    place: str
    item: str

    def fields(self) -> dict[str, str]:
        return dict(event=self.title, date=self.date, time=self.time,
                    place=self.place, item=self.item)


@dataclass(frozen=True, slots=True)
class FamilySpec:
    ordinal: int
    cell_index: int
    kind: QuestionType
    register: Register
    events: tuple[Event, Event]


@dataclass(frozen=True, slots=True)
class Family:
    events: tuple[Event, Event]
    persona: Persona
    records: tuple[Record, ...]
    style_record: Record
    question: PublicQuestion
    plan: CasePlan
    tags: tuple[UtteranceTag, ...]
    oracle: OracleCase


def identifier(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, "replica95-feasibility-dev-20261108:" + name)


def author_family(spec: FamilySpec, bank: Templates) -> Family:
    """No old question/answer files, model output, or retrieval scores are read."""
    family_id = f"s95-new-{spec.ordinal:03d}"
    persona = Persona(
        persona_id=family_id, name=f"Persona {spec.ordinal:03d}",
        display_name=f"Persona {spec.ordinal:03d}", provider_address=None,
        age=30 + spec.ordinal % 40, relationship="synthetic correspondent", register=spec.register,
    )
    first, second = spec.events
    probe = spec.cell_index % 2 == 0
    prefix = bank.probe_prefix["self_honorific" if probe else "plain"]
    t = {**bank.templates, **{
        key: variants[spec.cell_index % 3] for key, variants in bank.variant_templates.items()
    }}
    false_item = bank.objects[-2]
    span1, span2 = (t["item_span"].format(**event.fields()) for event in spec.events)
    fields = {**first.fields(), "probe_prefix": prefix}
    intent: Intent = "recall"
    omissions: tuple[str, ...] = ()
    spans: tuple[str, ...] = ()
    expected: tuple[Event, ...] = (first,)
    cohort: Literal["missing_second_event", "sufficient_but_omission_risk", "control"] = "control"
    match spec.kind:
        case "single":
            question = t["question_single"].format(**fields)
        case "multi":
            question = t["question_multi"].format(
                probe_prefix=prefix, first_span=span1, second_span=span2,
            )
            spans, expected = (span1, span2), spec.events
        case "ambiguity":
            span1 = t["ambiguity_old_diary_span"].format(
                old_date=first.date, old_time=first.time, event=first.title,
            )
            span2 = t["ambiguity_latest_chat_span"].format(
                new_date=second.date, new_time=second.time, event=second.title,
            )
            question = t["ambiguity_question"].format(
                probe_prefix=prefix, old_span=span1, new_span=span2,
            )
            spans, expected = (span1, span2), spec.events
        case "false_premise":
            intent = "correct"
            question = t["false_question"].format(**fields, false_item=false_item, pressure="")
        case "unanswerable":
            intent = "unknown"
            # Vocabulary comes from the agy-authored missing-detail sentence.
            question = t["unknown_question"].format(**fields, detail="색상")
            expected, omissions = (), ("bag color is not recorded; do not infer a color",)
        case "ordinary_chat":
            intent = "chat"
            question = t["chat_question"].format(**fields)
            expected = ()
        case unreachable:
            assert_never(unreachable)
    if len(expected) == 2:
        cohort = ("missing_second_event" if spec.cell_index < 10
                  else "sufficient_but_omission_risk")
        omissions = ("Both dated event place/object pairs are required; item[1] is omission risk.",)

    def record(event: Event, alias: str, text: str) -> Record:
        """Source construction is shared by targets, distractors and style turns."""
        return Record(
            record_id=identifier(f"{family_id}/{alias}"),
            text_version_id=identifier(f"{family_id}/{alias}/v1"),
            alias=f"{family_id}/{alias}", persona_id=family_id, family_id=family_id,
            split="dev", kind="diary", date=event.date,
            speaker=persona.name, text=text, turns=(),
        )

    template = "record"
    match spec.kind:
        case "false_premise":
            template = "correction_record"
        case "unanswerable":
            template = "unknown_record"
        case "single" | "multi" | "ambiguity" | "ordinary_chat":
            pass
        case unreachable:
            assert_never(unreachable)
    a = record(first, "first", t[template].format(**first.fields(), false_item=false_item))
    b = record(second, "second", t["second_record"].format(**second.fields()))
    authored_events = spec.events
    match spec.kind:
        case "ambiguity":
            b = b.model_copy(update={
                "kind": "chat", "speaker": "", "text": "",
                "turns": (Turn(speaker=persona.name, text=b.text),),
            })
        case "false_premise":
            # Both statements concern the SAME dated event. Only their source
            # timestamps differ; the later account explicitly corrects the item.
            old_claim = Event(first.title, first.date, first.time, first.place, false_item)
            authored_events = first, old_claim
            b = record(old_claim, "old-claim", t["record"].format(**old_claim.fields()))
            a = a.model_copy(update={
                "date": (date.fromisoformat(first.date) + timedelta(days=1)).isoformat(),
            })
        case "single" | "multi" | "unanswerable" | "ordinary_chat":
            pass
        case unreachable:
            assert_never(unreachable)
    records = [a, b]
    # The provider uses a different item at the SAME event/time: never persona gold.
    provider_text = t["provider_record"].format(
        **{**first.fields(), "item": bank.objects[-3], "place": bank.places[-3]},
    )
    provider = record(first, "provider", provider_text).model_copy(update={
        "kind": "chat", "speaker": "", "text": "",
        "turns": (Turn(speaker="나", text=provider_text),),
    })
    records.append(provider)
    if len(expected) == 2 and spec.cell_index < 10:
        # Competing near-first events on adjacent days; no score-based resampling.
        for offset in range(1, 5):
            distractor = Event(
                first.title, (date.fromisoformat(first.date) + timedelta(days=offset)).isoformat(),
                first.time, bank.places[offset], bank.objects[offset],
            )
            records.append(record(
                distractor, f"near-first-{offset}", t["record"].format(**distractor.fields()),
            ))
    fact_chunks = [chunk_record(r, persona)[0] for r in records]
    required = tuple(RequiredItem(
        item_index=i, question_span=spans[i] if spans else question,
        gold_support_ids=(fact_chunks[i].chunk_id,), slot_surfaces=(event.place, event.item),
    ) for i, event in enumerate(expected))
    conflicts = [Conflict(
        source_ids=(fact_chunks[0].chunk_id, fact_chunks[2].chunk_id),
        resolution="different_speaker", resolved_support_ids=(fact_chunks[0].chunk_id,),
        rationale="Same event/time, distinct explicitly named speakers; provider is not persona.",
    )]
    if len(expected) == 2:
        conflicts.append(Conflict(
            source_ids=(fact_chunks[0].chunk_id, fact_chunks[1].chunk_id),
            resolution="different_time",
            resolved_support_ids=(fact_chunks[0].chunk_id, fact_chunks[1].chunk_id),
            rationale="Two dated persona accounts; both requested, not a source-source contradiction.",
        ))
    match spec.kind:
        case "false_premise":
            conflicts.append(Conflict(
                source_ids=(fact_chunks[1].chunk_id, fact_chunks[0].chunk_id),
                resolution="latest_persona_correction",
                resolved_support_ids=(fact_chunks[0].chunk_id,),
                rationale="Same event/date/time; later source explicitly corrects the earlier item claim.",
            ))
        case "single" | "multi" | "ambiguity" | "unanswerable" | "ordinary_chat":
            pass
        case unreachable:
            assert_never(unreachable)
    leakage_probe = spec.cell_index % 5 == 1
    leak = dict(leak_place=bank.places[-1], leak_color=bank.colors[spec.ordinal % len(bank.colors)],
                leak_item=bank.objects[-1])
    chooser = random.Random(9508112026 + spec.ordinal)
    utterances = tuple(chooser.sample(bank.style_examples[spec.register][intent], 2))
    if leakage_probe:
        utterances = (
            utterances[0],
            chooser.choice(bank.leakage_examples[spec.register][intent]).format(**leak),
        )
    style = record(first, "style-only", utterances[0]).model_copy(update={
        "kind": "chat", "speaker": "", "text": "",
        "turns": tuple(Turn(speaker=persona.name, text=text) for text in utterances),
    })
    tags = tuple(UtteranceTag(
        record_id=style.record_id, text_version_id=style.text_version_id, turn_index=i,
        register=spec.register, intent=intent,
    ) for i in range(2))
    qid = family_id + "-q"
    return Family(
        authored_events, persona, tuple(records), style,
        PublicQuestion(question_id=qid, persona_id=family_id, family_id=family_id,
                       type=spec.kind, question=question),
        CasePlan(question_id=qid, item_spans=spans, intent=intent, self_honorific_probe=probe),
        tags, OracleCase(
            question_id=qid, family_id=family_id, self_honorific_probe=probe,
            required_items=required, expected_omissions=omissions,
            conflicts=tuple(conflicts), style_only_surfaces=tuple(leak.values()) if leakage_probe else (),
            style_leakage_probe=leakage_probe, cohort=cohort,
        ),
    )
