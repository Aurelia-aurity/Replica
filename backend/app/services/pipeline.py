"""STT·TTS 단독 확인용 호출(개발 점검용 /stt, /tts). 구간별 시간을 잰다(NFR-01).

대화 흐름(텍스트·음성)은 chat_text.py, chat_voice.py, turn_stages.py에 있다.
"""

import time

from app.clients.stt import get_stt_client
from app.clients.tts import get_tts_client
from app.schemas.chat import SttResponse, Timings


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


async def transcribe(audio: bytes, filename: str, content_type: str | None) -> SttResponse:
    t0 = time.perf_counter()
    text = await get_stt_client().transcribe(audio, filename, content_type)
    stt_ms = _ms(t0)
    return SttResponse(text=text, timings=Timings(stt_ms=stt_ms, total_ms=stt_ms))


async def synthesize(text: str) -> tuple[bytes, str]:
    return await get_tts_client().synthesize(text)
