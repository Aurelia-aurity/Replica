"""LLM 호출 클라이언트. LLM_MODE 설정으로 mock과 실제 서버를 교체한다(NFR-05)."""

from typing import Protocol

from app.core.config import get_settings
from app.schemas.chat import ChatTurn


class LLMClient(Protocol):
    name: str

    async def generate(self, message: str, history: list[ChatTurn], persona_id: str | None) -> str: ...


class MockLLMClient:
    name = "mock-llm"

    async def generate(self, message: str, history: list[ChatTurn], persona_id: str | None) -> str:
        return f"(Mock 응답) 받은 메시지: {message}"


def get_llm_client() -> LLMClient:
    mode = get_settings().llm_mode
    if mode == "mock":
        return MockLLMClient()
    # remote: 학교 GPU 서버 호출. AI 서버 API 계약이 정해지면 구현한다.
    raise NotImplementedError("LLM_MODE=remote 는 아직 구현되지 않았습니다. LLM_MODE=mock 으로 실행하세요.")
