import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings

from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def dev_speech_routes(monkeypatch):
    """개발용 /stt, /tts는 기본이 꺼져 있다(#67). 이 파일의 시험은 로컬에서 켠 상태를 쓴다."""
    monkeypatch.setenv("DEV_SPEECH_ROUTES", "true")
    monkeypatch.setenv("APP_ENV", "local")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

AUDIO = ("test.m4a", bytes.fromhex("0000001866747970" "4d344120" "00000000" "4d344120" "6d703432"), "audio/mp4")


# 텍스트 대화는 test_chat_text.py, 음성 대화(/chat/voice)는 test_chat_voice.py에서 시험한다.
# 여기서는 개발 점검용 /stt, /tts와 업로드 검증만 시험한다.


def test_stt_ok():
    res = client.post("/stt", files={"file": AUDIO})
    assert res.status_code == 200
    assert res.json()["text"]


def test_tts_returns_audio():
    res = client.post("/tts", json={"text": "안녕"})
    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/wav"
    assert res.content[:4] == b"RIFF"


def test_upload_rejects_unsupported_type():
    res = client.post("/stt", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert res.status_code == 415
    assert res.json()["error"]["code"] == "UNSUPPORTED_AUDIO_FORMAT"


def test_upload_rejects_empty_file():
    res = client.post("/stt", files={"file": ("a.wav", b"", "audio/wav")})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "EMPTY_FILE"
