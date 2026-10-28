from fastapi import APIRouter, File, Response, UploadFile

from app.core.uploads import read_audio
from app.schemas.chat import SttResponse, TtsRequest
from app.services import pipeline

router = APIRouter(tags=["speech"])


@router.post("/stt", response_model=SttResponse, summary="음성 → 텍스트 (STT 단독 확인)")
async def stt(file: UploadFile = File(..., description="녹음 파일 (wav, mp3, m4a, webm, ogg, mp4, aac)")) -> SttResponse:
    audio = await read_audio(file)
    return await pipeline.transcribe(audio, file.filename or "audio", file.content_type)


@router.post(
    "/tts",
    summary="텍스트 → 음성 (TTS 단독 확인)",
    response_class=Response,
    responses={200: {"content": {"audio/wav": {}, "audio/mpeg": {}}, "description": "음성 파일 본문"}},
)
async def tts(req: TtsRequest) -> Response:
    audio, fmt = await pipeline.synthesize(req.text)
    media = {"wav": "audio/wav", "mp3": "audio/mpeg"}.get(fmt, "application/octet-stream")
    return Response(content=audio, media_type=media)
