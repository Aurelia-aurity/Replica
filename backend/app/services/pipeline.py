"""대화 파이프라인: (음성이면) STT → LLM → (음성이면) TTS. 구간별 시간을 잰다(NFR-01)."""

import base64
import time

from app.clients.llm import get_llm_client
from app.clients.stt import get_stt_client
from app.clients.tts import get_tts_client
from app.schemas.chat import (
    ChatTextRequest,
    ChatTextResponse,
    ChatVoiceResponse,
    SttResponse,
    Timings,
)


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


async def transcribe(audio: bytes, filename: str, content_type: str | None) -> SttResponse:
    t0 = time.perf_counter()
    text = await get_stt_client().transcribe(audio, filename, content_type)
    stt_ms = _ms(t0)
    return SttResponse(text=text, timings=Timings(stt_ms=stt_ms, total_ms=stt_ms))


async def synthesize(text: str) -> tuple[bytes, str]:
    return await get_tts_client().synthesize(text)


async def chat_text(req: ChatTextRequest) -> ChatTextResponse:
    t0 = time.perf_counter()
    llm = get_llm_client()
    answer = await llm.generate(req.message, req.history, req.persona_id)
    llm_ms = _ms(t0)
    return ChatTextResponse(
        answer=answer,
        model_name=llm.name,
        timings=Timings(llm_ms=llm_ms, total_ms=llm_ms),
    )


async def chat_voice(
    audio: bytes, filename: str, content_type: str | None, persona_id: str | None
) -> ChatVoiceResponse:
    start = time.perf_counter()

    t = time.perf_counter()
    transcript = await get_stt_client().transcribe(audio, filename, content_type)
    stt_ms = _ms(t)

    t = time.perf_counter()
    llm = get_llm_client()
    answer = await llm.generate(transcript, [], persona_id)
    llm_ms = _ms(t)

    t = time.perf_counter()
    speech, fmt = await get_tts_client().synthesize(answer)
    tts_ms = _ms(t)

    return ChatVoiceResponse(
        transcript=transcript,
        answer=answer,
        model_name=llm.name,
        audio_base64=base64.b64encode(speech).decode("ascii"),
        audio_format=fmt,
        timings=Timings(stt_ms=stt_ms, llm_ms=llm_ms, tts_ms=tts_ms, total_ms=_ms(start)),
    )
