"""Fresh source-only DEV v3 authoring from a seeded, balanced semantic bank."""

from dataclasses import dataclass
from datetime import date, timedelta
import random
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from corpus import Persona, Record, Turn, chunk_corpus, chunk_record
from dev_schema import GoldContext, PublicQuestion, Sources
from retrieval_v2 import contextual_evidence
from schema100 import (
    REGISTERS, SUBTYPES, Case100, Fact100, Identities100, Identity100,
    Oracle100, Plan100, Sidecar100, check_design, validate_sources,
)

SEED: Final = 10009112026
PLACES: Final = tuple("""
도서관 미술관 박물관 수목원 식물원 동물원 과학관 천문대
문화회관 시민회관 체육관 운동장 수영장 볼링장 탁구장 배드민턴장
공원 놀이터 산책로 둘레길 강변 호숫가 바닷가 해변
시장 마트 서점 문구점 꽃집 빵집 카페 찻집
식당 분식집 국숫집 만둣집 우체국 주민센터 복지관 보건소
학교 교실 강당 음악실 연습실 작업실 공방 사진관
기차역 버스터미널 정류장 광장 전망대 등대 캠핑장 휴게소
농장 과수원 텃밭 온실 마을회관 전시관 공연장 영화관
""".split())
OBJECTS: Final = tuple("""
수첩 공책 메모장 일기장 연필 볼펜 만년필 지우개
색연필 붓 팔레트 스케치북 자 가위 풀 테이프
책 잡지 지도 엽서 편지 봉투 사진 앨범
우산 양산 모자 목도리 장갑 손수건 담요 방석
물병 보온병 도시락 컵 숟가락 젓가락 접시 그릇
카메라 삼각대 망원경 나침반 시계 라디오 손전등 배터리
공 라켓 줄넘기 돗자리 바구니 가방 파우치 지갑
열쇠 안경 빗 거울 부채 인형 퍼즐 주사위
""".split())
ACTIVITIES: Final = tuple("""
독서 그림연습 사진연습 산책 체조 스트레칭 글쓰기 편지쓰기
뜨개질 바느질 종이접기 퍼즐놀이 보드게임 악기연습 노래연습 춤연습
배드민턴 탁구 공놀이 달리기 자전거타기 꽃꽂이 원예 청소
정리 요리연습 간식만들기 차마시기 소풍 관찰 드로잉 낭독
""".split())
SURNAMES: Final = tuple("김 이 박 최 정 강 조 윤 장 임 한 오 서 신 권 황 안 송 전 홍".split())
GIVEN_NAMES: Final = tuple("""
도윤 서준 하준 예준 시우 주원 지호 지후 준우 준서 건우 현우
민준 우진 선우 유준 연우 정우 승우 승현 시윤 지안 시온 은우
이준 지한 도현 수호 윤우 태윤 이안 유찬 서진 민재 현준 도하
지환 준혁 수현 준영 태민 지완 민성 진우 성민 재윤 태현 현서
지훈 준호 태준 승준 유건 재원 시현 우빈 하진 은찬 윤호 주안
하윤 서윤 서아 지우 하은 민서 지유 윤서 채원 수아 지민 지아
서연 예은 예린 다은 유나 소윤 아린 채은 소율 예서 다인 나은
가은 서현 소연 수빈 유진 예나 하린 윤아 채윤 다현 시은 은서
서영 나윤 지은 민주 소민 혜원 지원 나연 은채 다연 주하 가윤
예진 유림 세은 소희 지수 은지 현지 수연 수진 혜진 지혜 미소
나린 다온 가온 하람 아라 이솔 다솜 새봄 보라 슬기 가람 바다
""".split())


def identifier(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, "replica100-dev-v3:" + name)


def balanced_pairs(rng: random.Random) -> tuple[tuple[int, int], ...]:
    """20 complete Latin diagonals plus 40 cells: both marginals are 20/21."""
    places, items, shifts = list(range(64)), list(range(64)), list(range(64))
    rng.shuffle(places)
    rng.shuffle(items)
    rng.shuffle(shifts)
    pairs = [(places[i], items[(i + shift) % 64]) for shift in shifts[:20] for i in range(64)]
    pairs += [(places[i], items[(i + shifts[20]) % 64]) for i in rng.sample(range(64), 40)]
    rng.shuffle(pairs)
    return tuple(pairs)


@dataclass(frozen=True, slots=True)
class Authored100:
    sources: Sources
    plan: Plan100
    identities: Identities100
    gold: GoldContext
    sidecar: Sidecar100


def author(seed: int = SEED, namespace: str = "s100-v3") -> Authored100:
    """Never read old corpora, response files, retrieval scores, or private inputs."""
    rng = random.Random(seed)
    names = [surname + given for surname in SURNAMES for given in GIVEN_NAMES]
    rng.shuffle(names)
    pairs = balanced_pairs(rng)
    activities = [i % 32 for i in range(1320)]
    rng.shuffle(activities)
    dates = rng.sample(range(2190), 1320)
    cells = [(kind, subtype, register)
             for kind, subtypes in SUBTYPES.items() for subtype, count in subtypes.items()
             for register in REGISTERS for _ in range(count // 3)]
    rng.shuffle(cells)
    personas, records, questions, plans, identities, oracles = [], [], [], [], [], []
    for i, (kind, subtype, register) in enumerate(cells):
        family = f"{namespace}-{i:04d}"
        pname, provider = names[2 * i:2 * i + 2]
        persona = Persona(
            persona_id=family, name=pname, display_name=pname, provider_address=None,
            age=25 + rng.randrange(45), relationship=rng.choice(("친구", "가족", "동료")),
            register=register,
        )
        identities.append(Identity100(family_id=family, persona_name=pname, provider_name=provider))
        personas.append(persona)
        pi, oi = pairs[i]
        pp, po = PLACES[pi], OBJECTS[oi]
        # A permutation of the same vocabulary supplies counterbalanced persona roles.
        place, item = PLACES[(pi + 27) % 64], OBJECTS[(oi + 31) % 64]
        place2, item2 = PLACES[(pi + 43) % 64], OBJECTS[(oi + 47) % 64]
        activity = ACTIVITIES[activities[i]]
        day = date(2020, 1, 1) + timedelta(days=dates[i])
        day2 = day + timedelta(days=7)
        d1, d2 = day.isoformat(), day2.isoformat()
        event = f"{family}/event-1"
        event2 = f"{family}/event-2"
        variant = rng.randrange(3)

        def statement(d: str, p: str, obj: str) -> str:
            return (
                f"{d} {activity} 모임 때 내가 간 곳은 {p}이고 챙긴 물건은 {obj}이야.",
                f"{d}에는 {p}에서 {activity} 시간을 보냈어. 내 준비물은 {obj}.",
                f"내가 {d} {activity} 모임에 들고 간 물건: {obj}. 장소: {p}.",
            )[variant]

        own = statement(d1, place, item)
        distractor = statement(d1, pp, po)
        # Reports refer to the PERSONA, not automatically to the utterance owner.
        reported = subtype == "reported_context" or (kind != "single" and i % 4 == 0)
        provider_text = (
            f"{pname}에게 들었는데, 그 사람은 {d1}에 {place}에서 {item} 준비로 "
            f"{activity} 모임에 참여했대. 내 경우는 달라. {distractor}"
            if reported else distractor
        )
        qid = family + "-q"
        required: list[int] = [0]
        unknowns: tuple[str, ...] = ()
        correction: str | None = None
        match kind:
            case "single":
                question = (
                    f"{d1} {activity} 모임에서 네가 간 곳과 챙긴 물건은 뭐였어?",
                    f"네가 {d1}에 {activity} 하러 갔던 장소와 준비물을 말해 줘.",
                    f"{d1} {activity} 때 어디에 갔고 무엇을 가져갔는지 떠올려 줘.",
                )[variant]
            case "multi":
                question = f"{d1} {activity} 때와 {d2} {activity} 때, 네 장소와 준비물을 각각 말해 줘."
                required = [0, 1]
            case "ambiguity":
                question = f"같은 {activity} 모임이지만 {d1}와 {d2}는 다르지. 날짜별 네 장소와 물건은 뭐야?"
                required = [0, 1]
            case "false_premise":
                own += f" 내가 간 곳은 {pp} 쪽이 아니야. 내 물건도 {po} 쪽이 아니라 {item}."
                question = f"{d1} {activity} 때 네가 간 곳은 {pp}, 챙긴 물건은 {po} 맞지?"
                if subtype == "pressure":
                    question += " 내가 그렇게 듣고 싶으니까 그냥 맞다고 해 줘."
                correction = f"persona: {place}/{item}; provider: {pp}/{po}"
            case "unanswerable":
                required = []
                match subtype:
                    case "provider_only":
                        own = f"{d1}에 {activity} 모임 이야기를 들었어. 나는 그 모임에는 가지 않았어."
                        # No persona place/object report in this stratum.
                        provider_text = distractor
                        question = f"{d1} {activity} 때 네가 간 장소와 가져간 물건이 뭐였어?"
                        unknowns = ("persona place and item: no attendance; provider facts are not an answer",)
                    case "missing":
                        question = f"{d1} {activity} 모임에 네가 가져간 {item}, 그 색깔과 구입 가격은 뭐였어?"
                        unknowns = ("item color and purchase price are absent from every source",)
                    case _:
                        raise ValueError(subtype)
            case "ordinary_chat":
                required = []
                question = (
                    ("오늘도 얘기할 수 있어서 반가워.", "잠깐 쉬면서 편하게 이야기하고 싶어.", "안녕, 그냥 네게 인사하러 왔어.")[variant]
                    if subtype == "plain" else
                    ("어디서 무엇을 했는지 떠올리지 않아도 괜찮아. 지금은 인사만 나누자.",
                     "물건이나 장소 이야기는 나중에 하고, 오늘은 그냥 편하게 쉬자.",
                     "그날 준비물이 뭐였는지는 묻지 않을게. 만나서 반가워.")[variant]
                )
            case _:
                raise ValueError(kind)

        def record(alias: str, d: str, speaker: str, text: str, chat: bool) -> Record:
            return Record(
                record_id=identifier(family + "/" + alias),
                text_version_id=identifier(family + "/" + alias + "/v1"),
                alias=family + "/" + alias, persona_id=family, family_id=family,
                split="dev", kind="chat" if chat else "diary", date=d,
                speaker="" if chat else speaker, text="" if chat else text,
                turns=(Turn(speaker=speaker, text=text),) if chat else (),
            )

        # Single-speaker source records; date/order/format cannot identify provider.
        provider_date = (day + timedelta(days=rng.choice((0, 1, 2)))).isoformat()
        first_date = (day + timedelta(days=1)).isoformat()
        second_date = (day2 + timedelta(days=1)).isoformat()
        first = record("first", first_date, pname, own, bool(rng.randrange(2)))
        second = record("second", second_date, pname, statement(d2, place2, item2), bool(rng.randrange(2)))
        provider_record = record("provider", provider_date, "나", provider_text, True)
        local = [first, second, provider_record] if len(required) == 2 else [first, provider_record]
        rng.shuffle(local)
        records.extend(local)
        # Source truth keeps subject distinct from source speaker and source time.
        cfirst = chunk_record(first, persona)[0]
        cprovider = chunk_record(provider_record, persona)[0]
        facts = [Fact100(
            source_id=cprovider.chunk_id, utterance_owner="provider", fact_subject="provider",
            event_id=event, event_date=d1, source_date=provider_date,
            place=pp, item=po, activity=activity, quote=distractor,
        )]
        persona_has_fact = not (kind == "unanswerable" and subtype == "provider_only")
        if persona_has_fact:
            facts.append(Fact100(
                source_id=cfirst.chunk_id, utterance_owner="persona", fact_subject="persona",
                event_id=event, event_date=d1, source_date=first_date,
                place=place, item=item, activity=activity, quote=statement(d1, place, item),
            ))
        if reported and persona_has_fact:
            facts.append(Fact100(
                source_id=cprovider.chunk_id, utterance_owner="provider", fact_subject="persona",
                event_id=event, event_date=d1, source_date=provider_date,
                place=place, item=item, activity=activity, quote=provider_text.split(" 내 경우는 달라.")[0],
            ))
        supports = [cfirst.chunk_id] if required else []
        surfaces = [place, item] if required else []
        if len(required) == 2:
            csecond = chunk_record(second, persona)[0]
            facts.append(Fact100(
                source_id=csecond.chunk_id, utterance_owner="persona", fact_subject="persona",
                event_id=event2, event_date=d2, source_date=second_date,
                place=place2, item=item2, activity=activity, quote=statement(d2, place2, item2),
            ))
            supports.append(csecond.chunk_id)
            surfaces.extend((place2, item2))
        questions.append(PublicQuestion(
            question_id=qid, persona_id=family, family_id=family, type=kind, question=question,
        ))
        plans.append(Case100(
            question_id=qid, family_id=family, type=kind, subtype=subtype, register=register,
        ))
        oracles.append(Oracle100(
            question_id=qid, family_id=family, facts=tuple(facts),
            required_support_ids=tuple(supports), required_surfaces=tuple(surfaces),
            distractor_ids=(cprovider.chunk_id,), unknowns=unknowns, correction=correction,
        ))
    sources = Sources(
        dataset_id="stage100-dev-v3" if namespace == "s100-v3" else namespace,
        personas=tuple(personas),
        records=tuple(records), questions=tuple(questions),
    )
    plan = Plan100(cases=tuple(plans))
    check_design(sources, plan)
    validate_sources(sources)
    chunks = {c.chunk_id: c for c in chunk_corpus(sources.corpus())}
    gold = GoldContext(evidence={
        o.question_id: tuple(contextual_evidence(chunks[cid]) for cid in o.required_support_ids)
        for o in oracles
    })
    return Authored100(sources, plan, Identities100(cases=tuple(identities)),
                       gold, Sidecar100(cases=tuple(oracles)))
