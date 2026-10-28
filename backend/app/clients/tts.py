"""TTS(텍스트 → 음성) 클라이언트. 실제 공급자는 STT/TTS 담당과 확정 후 추가한다."""

import io
import math
import struct
import wave
from typing import Protocol

from app.core.config import get_settings


class TTSClient(Protocol):
    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """(음성 바이트, 형식)을 반환한다. 형식 예: 'wav', 'mp3'."""
        ...


class MockTTSClient:
    """브라우저에서 바로 재생되는 짧은 '삐' 소리(wav)를 만든다. 파일 없이 메모리에서 생성."""

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        rate, seconds, freq = 16000, 0.6, 440.0
        frames = bytearray()
        for i in range(int(rate * seconds)):
            fade = min(1.0, i / 800, (rate * seconds - i) / 800)  # 시작·끝 클릭음 방지
            sample = int(9000 * fade * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", sample)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(frames))
        return buf.getvalue(), "wav"


def get_tts_client() -> TTSClient:
    provider = get_settings().tts_provider
    if provider == "mock":
        return MockTTSClient()
    raise NotImplementedError(f"TTS_PROVIDER={provider} 는 아직 구현되지 않았습니다.")
