from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.clients.llm import LLMClient, get_llm_client
from app.clients.stt import STTClient, get_stt_client
from app.clients.tts import TTSClient, get_tts_client
from app.core.auth import get_user_db
from app.core.uploads import read_audio
from app.db import UserDatabase
from app.schemas.chat import ChatTextRequest, ChatTextResponse, ChatVoiceResponse, TurnTtsResponse
from app.services import chat_text as chat_text_service
from app.services import chat_voice as chat_voice_service

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/text", response_model=ChatTextResponse, summary="텍스트 대화")
async def chat_text(
    req: ChatTextRequest,
    user_db: UserDatabase = Depends(get_user_db),
    llm: LLMClient = Depends(get_llm_client),
) -> ChatTextResponse:
    """질문 텍스트를 보내면 AI 응답을 돌려준다. 모든 응답은 AI 생성(`is_ai_generated: true`)이다.

    같은 `turn_id`로 다시 보내면 끝난 답변은 저장된 결과를 돌려주고 AI를 다시 부르지 않는다.
    """
    return await chat_text_service.handle_text_turn(user_db, llm, req)


@router.post("/voice", response_model=ChatVoiceResponse, summary="음성 대화")
async def chat_voice(
    file: UploadFile = File(..., description="녹음 파일 (wav, mp3, m4a, webm, ogg, mp4, aac), 최대 25MB"),
    session_id: UUID = Form(description="대화방 id"),
    turn_id: UUID = Form(description="앱이 전송마다 만드는 id. 같은 녹음을 재전송할 때는 같은 값을 유지"),
    user_db: UserDatabase = Depends(get_user_db),
    stt: STTClient = Depends(get_stt_client),
    llm: LLMClient = Depends(get_llm_client),
    tts: TTSClient = Depends(get_tts_client),
) -> ChatVoiceResponse:
    """녹음 파일을 보내면 STT → LLM → TTS를 거쳐 전사문, 답변, 응답 음성(base64)을 돌려준다.

    STT·LLM이 실패하면 오류(`stage`로 실패 단계 표시)로, TTS만 실패하면 `tts_status: failed`와 함께 200으로 응답한다.
    """
    audio = await read_audio(file)
    return await chat_voice_service.handle_voice_turn(
        user_db, stt, llm, tts,
        session_id=session_id, turn_id=turn_id, audio=audio,
        filename=file.filename or "audio", content_type=file.content_type,
    )


@router.post("/turns/{turn_id}/tts", response_model=TurnTtsResponse, summary="완료된 답변을 음성으로 합성·재시도")
async def turn_tts(
    turn_id: UUID,
    user_db: UserDatabase = Depends(get_user_db),
    tts: TTSClient = Depends(get_tts_client),
) -> TurnTtsResponse:
    """답변이 끝난 턴을 음성으로 합성한다. 합성 중이거나 이미 완료된 턴, 답변이 없는 턴은 409."""
    return await chat_voice_service.handle_turn_tts(user_db, tts, turn_id)
