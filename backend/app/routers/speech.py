from fastapi import APIRouter, Depends, File, Header, Response, UploadFile

from app.core.auth import get_user_db
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.uploads import read_audio
from app.schemas.chat import SttResponse, TtsRequest
from app.services import pipeline



async def require_dev_speech(authorization: str | None = Header(default=None)) -> None:
    """개발 점검용 라우트 게이트(#67 N1). 꺼져 있거나 prod이면 없는 경로처럼 404로 응답한다.

    APP_ENV=local에서만 로그인 없이 쓴다. 그 밖의 환경에서 켜면 Bearer 로그인을 요구한다.
    """
    settings = get_settings()
    if settings.app_env == "prod" or not settings.dev_speech_routes:
        raise AppError(404, "NOT_FOUND", "찾을 수 없는 경로예요.", retryable=False)
    if settings.app_env != "local":
        await get_user_db(authorization)


router = APIRouter(tags=["speech"], dependencies=[Depends(require_dev_speech)])


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
