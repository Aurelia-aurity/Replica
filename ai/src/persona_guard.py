"""Narrow, offline self-referral advice guard; not a Korean semantic classifier."""

from dataclasses import dataclass
import re
from typing import Final, Literal, assert_never

Register = Literal["반말", "다정한 반말", "해요체", "하게체"]

# Additive rules for a future persona prompt; not installed into baseline/API.
PERSONA_RULES: Final = """현재 답하는 인물은 persona에 지정된 본인입니다.
본인의 이름이나 관계 호칭을 다른 상담 상대처럼 내세워 사용자에게 본인에게
물어보거나 확인하라고 권하지 마세요. 본인의 역할을 사용자처럼 부르며
기억을 알려 달라고 요청하지 마세요. 생존 인물도 이 규칙을 지키세요.
고인으로 명시된 인물에게 연락하거나 물어보라고 안내하지 마세요.
관계나 나이만 보고 생사를 추측하지 마세요. 다른 가족과 본인을 구분하세요.
기억 범위 밖의 질문은 사용자가 이미 들려준 기억으로 돌아가거나, 지정된 말투로
잘 기억나지 않는다고 짧게 말하세요. 없는 기억을 만들거나 불필요한 질문을
먼저 붙이지 마세요. 회상 인용이나 과거 보고를 현재의 권유로 바꾸지 마세요."""


@dataclass(frozen=True, slots=True)
class PersonaIdentity:
    """Explicit aliases for the speaker, not inferred family or life-status data."""

    relationship_aliases: tuple[str, ...]
    name_aliases: tuple[str, ...]
    register: Register


@dataclass(frozen=True, slots=True)
class GuardResult:
    text: str
    matched: str | None


# Mask quotations before splitting sentences, including punctuation inside quotes.
QUOTES: Final = re.compile(r"""'[^'\n]*'|"[^"\n]*"|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』""")
PREFIX: Final = (
    r"\s*(?:(?:그건|그거는|그 부분은|그 내용은|궁금하면|궁금하다면|"
    r"모르면|필요하면|필요하시면|자세한 건|정확한 건)\s+)*"
)
ADVICE: Final = (
    r"(?:물어\s*|여쭤\s*|여쭈어\s*)"
    r"(?:봐(?:요|라|줘|주세요)?|보(?:게|렴|세요|십시오|자|시겠어요|"
    r"면\s*돼(?:요)?|시면\s*돼요|는\s*게\s*좋겠(?:어|어요))|봐\s*주(?:세요|게)|봐\s*줘)"
    r"|여쭈(?:세요|십시오)|(?:확인|문의)해\s*(?:봐(?:요)?|보세요|보게)"
)
REQUEST: Final = (
    r"(?:[^.!?。！？;\n]{0,160}?(?:에\s*대해|인지|는지|건지|기억하시면|아시면)\s*)?"
    r"(?:말씀해|말해|설명해|알려)\s*"
    r"(?:줘|주세요|주시겠어요|주시면\s*(?:좋겠습니다|좋겠어요|감사하겠습니다))"
    r"\s*[.!?。！？]*\s*$"
)


def check_reply(text: str, persona: PersonaIdentity) -> GuardResult:
    """Replace the whole reply only for locally scoped, positive self-advice.

    Whitelisted clause prefixes deliberately exclude third-party subjects,
    possessives, and fictional framing. Unsupported constructions pass unchanged.
    No question text is searched and no death inference is required.
    """
    names = [
        re.escape(alias.strip()) + r"(?:\s*(?:씨|님|이))?"
        for alias in persona.name_aliases if alias.strip()
    ]
    relations = [
        re.escape(alias.strip()) + r"(?:\s*님)?"
        for alias in persona.relationship_aliases if alias.strip()
    ]
    targets = "|".join((
        *names, *relations,
        *(rf"{name}\s*{relation}" for name in names for relation in relations),
        "나", "저",
    ))
    advice = re.compile(
        PREFIX + rf"(?:{targets})\s*(?:에게|한테|께)\s*"
        r"(?:(?:직접|다시|한번|한\s+번|좀)\s*)*"
        rf"(?:{ADVICE})\s*[.!?。！？]*\s*$"
    )
    request = re.compile(
        PREFIX + rf"(?:{targets})\s*(?:께서는|께서|은|는|이|가)\s*"
        # Do not reinterpret an embedded third-party subject as the addressee.
        r"(?![^.!?。！？;\n]*[가-힣]+(?:이|가|은|는)\s)"
        + REQUEST
    )
    # Keep a non-whitespace boundary: a quoted subject is still clause context.
    masked = QUOTES.sub(lambda match: "\ufffc" + " " * (len(match[0]) - 1), text)
    # A wrapped target/action is one clause. Preserve offsets and other newlines.
    masked = re.sub(
        rf"(?:{targets})\s*(?:에게|한테|께)\s*(?=(?:{ADVICE}))",
        lambda match: match[0].replace("\n", " "),
        masked,
    )
    for clause in re.finditer(r"[^.!?。！？;\n]+[.!?。！？]*", masked):
        if "\ufffc" in clause[0]:
            continue
        if advice.fullmatch(clause[0]) or request.fullmatch(clause[0]):
            match persona.register:
                case "반말" | "다정한 반말":
                    replacement = "그건 잘 기억이 안 나."
                case "해요체":
                    replacement = "그건 잘 기억이 안 나요."
                case "하게체":
                    replacement = "그건 잘 기억이 안 나네."
                case unreachable:
                    assert_never(unreachable)
            return GuardResult(replacement, text[clause.start():clause.end()].strip())
    return GuardResult(text, None)
