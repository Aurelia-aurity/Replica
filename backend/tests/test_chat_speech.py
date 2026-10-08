import base64
import io
import wave

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

AUDIO = ("test.m4a", b"fake-audio-bytes", "audio/mp4")


def test_chat_text_ok():
    res = client.post("/chat/text", json={"message": "엄마, 오늘 뭐 했어?"})
    assert res.status_code == 200
    body = res.json()
    assert "엄마, 오늘 뭐 했어?" in body["answer"]
    assert body["is_ai_generated"] is True
    assert body["timings"]["llm_ms"] is not None
    assert body["timings"]["stt_ms"] is None


def test_chat_text_rejects_empty_message():
    assert client.post("/chat/text", json={"message": ""}).status_code == 422


def test_chat_voice_ok_and_audio_is_playable_wav():
    res = client.post("/chat/voice", files={"file": AUDIO}, data={"persona_id": "p1"})
    assert res.status_code == 200
    body = res.json()
    assert body["transcript"] and body["answer"]
    assert body["is_ai_generated"] is True
    t = body["timings"]
    assert t["stt_ms"] is not None and t["llm_ms"] is not None and t["tts_ms"] is not None
    assert body["audio_format"] == "wav"
    with wave.open(io.BytesIO(base64.b64decode(body["audio_base64"]))) as w:
        assert w.getnframes() > 0


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


def test_upload_rejects_empty_file():
    res = client.post("/stt", files={"file": ("a.wav", b"", "audio/wav")})
    assert res.status_code == 400
