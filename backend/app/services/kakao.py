"""카카오톡 내보내기(.txt) 분석: 화자 이름과 메시지 수만 센다.

앱의 `mobile/lib/records/text_analysis.dart`와 기본 규칙(줄 형식, 2개 이상이면 대화)은 같지만, 이름에 `]`가 들어간
경우와 내보내기 머리글이 있는 1메시지 파일, 100자 초과 이름·화자 2000명 초과의 거절은 서버만 처리한다. 앱과 서버의
판정이 다르면 서버 판정이 최종이다(contracts/app-backend/api-spec.md '업로드 뒤 검증'). 메시지 본문은 보관하거나
로그에 남기지 않는다.
"""

import io
import re
from dataclasses import dataclass, field

# 안드로이드·PC 내보내기: `[이름] [오후 3:12] 내용`. 이름에 `]`가 들어갈 수 있으므로(`[엄마 [회사]] [오후 3:12] ...`)
# 이름을 `]`로 막지 않고, 뒤따르는 `] [오전|오후 시:분] ` 경계가 처음 나오는 곳까지를 이름으로 본다.
_BRACKET = re.compile(r"^\[(.+?)\] \[(?:오전|오후) \d{1,2}:\d{2}\] ")
# iOS·데스크톱 내보내기: `2025. 9. 20. 오후 3:12, 이름 : 내용` 등
_DATED = re.compile(
    r"^\d{4}(?:년 ?|[.\-/] ?)\d{1,2}(?:월 ?|[.\-/] ?)\d{1,2}(?:일|\.)?"
    r"(?: [가-힣]요일)? ?(?:(?:오전|오후) )?\d{1,2}:\d{2}(?::\d{2})?, (.+?) : "
)

# 내보내기 머리글: 첫 줄이 `카카오톡 대화`로 끝나고(`… 님과 카카오톡 대화`) 이어서 `저장한 날짜 : …` 줄이 있다.
_HEADER_TITLE = re.compile(r"^.*카카오톡 대화\s*$")
_HEADER_SAVED = re.compile(r"^저장한 날짜 : ")
_HEADER_LINES = 5  # 머리글을 찾는 앞쪽 줄 수

# 머리글이 없으면 메시지로 인식한 줄이 2개 이상이어야 대화 내보내기로 본다(일기 속 시각 한 줄을 대화로 보지 않으려고).
# 머리글이 있으면 메시지 1개짜리 내보내기도 대화로 본다.
_MIN_MESSAGES = 2


# 계약의 화자 이름 한도(1~100자). 이보다 긴 이름이 화자 줄에 나오면 선택할 수 없으므로 파일을 거절한다.
MAX_SPEAKER_NAME = 100
# 서로 다른 화자의 상한. 악성 파일 하나로 메모리와 오류 응답(`speakers`)이 커지는 것을 막는다.
MAX_SPEAKERS = 2000


class SpeakerNameTooLong(Exception):
    """화자 줄 구조가 인식됐는데 이름이 `MAX_SPEAKER_NAME`를 넘는다. 일반 텍스트로 조용히 바꾸지 않고 거절한다."""


class NoChatMessages(Exception):
    """내보내기 머리글은 있는데 메시지로 인식한 줄이 하나도 없다. 화자를 알 수 없어 받을 수 없다."""


class TooManySpeakers(Exception):
    """서로 다른 화자가 `MAX_SPEAKERS`를 넘었다. 대화 내보내기로 볼 수 없는 파일이다."""


@dataclass(frozen=True)
class TextAnalysis:
    kakao: bool
    speakers: list[str] = field(default_factory=list)  # 메시지 수가 많은 순
    messages: int = 0


PLAIN = TextAnalysis(kakao=False)


def analyze_text(data: bytes) -> TextAnalysis:
    counts: dict[str, int] = {}
    total = 0
    # 한 줄씩 읽어 최대 25MiB 본문의 복사본을 만들지 않는다. BOM은 벗기고, 줄 나눔은 \n·\r\n·\r만 쓴다(앱과 같음).
    # UTF-8이 아니면 UnicodeDecodeError를 그대로 올린다(호출하는 쪽이 "읽을 수 없는 파일"로 처리).
    lines = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig", errors="strict")
    header_title = header_saved = False
    for index, line in enumerate(lines):
        if index < _HEADER_LINES:
            header_title = header_title or bool(_HEADER_TITLE.match(line))
            header_saved = header_saved or bool(_HEADER_SAVED.match(line))
        match = _BRACKET.match(line) or _DATED.match(line)  # 줄을 복사하지 않고 그 자리에서 맞춘다
        name = match.group(1).strip() if match else ""
        if not name:
            continue
        if len(name) > MAX_SPEAKER_NAME:
            # 이름을 자르면 다른 화자와 충돌할 수 있고, 건너뛰면 대화가 일반 텍스트로 바뀌어 화자 확인을 우회한다.
            raise SpeakerNameTooLong()
        if name not in counts and len(counts) >= MAX_SPEAKERS:
            raise TooManySpeakers()
        counts[name] = counts.get(name, 0) + 1
        total += 1
    if header_title and header_saved:
        if total == 0:
            raise NoChatMessages()
    elif total < _MIN_MESSAGES:
        return PLAIN
    speakers = [name for name, _ in sorted(counts.items(), key=lambda item: item[1], reverse=True)]
    return TextAnalysis(kakao=True, speakers=speakers, messages=total)
