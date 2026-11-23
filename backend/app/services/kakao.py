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
    lines = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig", errors="replace")
    for line in lines:
        line = line.rstrip("\n")
        match = _BRACKET.match(line) or _DATED.match(line)
        name = match.group(1).strip() if match else ""
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
        total += 1
    if total < _MIN_MESSAGES:
        return PLAIN
    speakers = [name for name, _ in sorted(counts.items(), key=lambda item: item[1], reverse=True)]
    return TextAnalysis(kakao=True, speakers=speakers, messages=total)
