"""카카오톡 내보내기(.txt) 분석: 화자 이름과 메시지 수만 센다.

앱의 `mobile/lib/records/text_analysis.dart`와 같은 판정 규칙이다. 앱과 서버의 판정이 다르면 서버 판정이 최종이다
(contracts/app-backend/api-spec.md '업로드 뒤 검증'). 메시지 본문은 보관하거나 로그에 남기지 않는다.
"""

import io
import re
from dataclasses import dataclass, field

# 안드로이드·PC 내보내기: `[이름] [오후 3:12] 내용`
_BRACKET = re.compile(r"^\[([^\]]+)\] \[(?:오전|오후) \d{1,2}:\d{2}\] ")
# iOS·데스크톱 내보내기: `2025. 9. 20. 오후 3:12, 이름 : 내용` 등
_DATED = re.compile(
    r"^\d{4}(?:년 ?|[.\-/] ?)\d{1,2}(?:월 ?|[.\-/] ?)\d{1,2}(?:일|\.)?"
    r"(?: [가-힣]요일)? ?(?:(?:오전|오후) )?\d{1,2}:\d{2}(?::\d{2})?, (.+?) : "
)

# 한 줄짜리 타임스탬프는 대화 내보내기가 아니다(앱과 같은 기준).
_MIN_MESSAGES = 2


# 계약의 화자 이름 한도(1~100자). 이보다 긴 이름은 선택할 수 없으므로 화자로 세지 않는다.
MAX_SPEAKER_NAME = 100
# 서로 다른 화자의 상한. 악성 파일 하나로 메모리와 오류 응답(`speakers`)이 커지는 것을 막는다.
MAX_SPEAKERS = 2000
# 줄 앞에서 화자 이름까지 보는 글자 수. 날짜·시각 접두(약 40자) + 이름(최대 100자) + 구분자를 넉넉히 덮는다.
_HEAD_CHARS = 300


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
    for line in lines:
        # 화자 이름은 줄의 맨 앞에만 있고 최대 100자이므로 앞부분만 본다. 아주 긴 줄을 통째로 복사하지 않는다.
        head = line[:_HEAD_CHARS]
        match = _BRACKET.match(head) or _DATED.match(head)
        name = match.group(1).strip() if match else ""
        if not name or len(name) > MAX_SPEAKER_NAME:
            continue
        if name not in counts and len(counts) >= MAX_SPEAKERS:
            raise TooManySpeakers()
        counts[name] = counts.get(name, 0) + 1
        total += 1
    if total < _MIN_MESSAGES:
        return PLAIN
    speakers = [name for name, _ in sorted(counts.items(), key=lambda item: item[1], reverse=True)]
    return TextAnalysis(kakao=True, speakers=speakers, messages=total)
