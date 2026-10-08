from fastapi import APIRouter, File, Form, UploadFile

from app.core.uploads import read_audio
from app.schemas.chat import ChatTextRequest, ChatTextResponse, ChatVoiceResponse
from app.services import pipeline

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/text", response_model=ChatTextResponse, summary="텍스트 대화")
async def chat_text(req: ChatTextRequest) -> ChatTextResponse:
    """질문 텍스트를 보내면 AI 응답을 돌려준다. 모든 응답은 AI 생성(`is_ai_generated: true`)이다."""
    return await pipeline.chat_text(req)


@router.post("/voice", response_model=ChatVoiceResponse, summary="음성 대화")
async def chat_voice(
    file: UploadFile = File(..., description="녹음 파일 (wav, mp3, m4a, webm, ogg, mp4, aac)"),
    persona_id: str | None = Form(default=None),
) -> ChatVoiceResponse:
    """녹음 파일을 보내면 STT → LLM → TTS를 거쳐 전사문, 답변, 응답 음성(base64)을 돌려준다."""
    audio = await read_audio(file)
    return await pipeline.chat_voice(audio, file.filename or "audio", file.content_type, persona_id)
