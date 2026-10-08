"""STT(음성 → 텍스트) 클라이언트. 실제 공급자는 STT/TTS 담당과 확정 후 추가한다."""

from typing import Protocol

from app.core.config import get_settings


class STTClient(Protocol):
    async def transcribe(self, audio: bytes, filename: str, content_type: str | None) -> str: ...


class MockSTTClient:
    async def transcribe(self, audio: bytes, filename: str, content_type: str | None) -> str:
        return f"(Mock 전사) 테스트 음성입니다. 파일: {filename}, {len(audio)} bytes"


def get_stt_client() -> STTClient:
    provider = get_settings().stt_provider
    if provider == "mock":
        return MockSTTClient()
    raise NotImplementedError(f"STT_PROVIDER={provider} 는 아직 구현되지 않았습니다.")
