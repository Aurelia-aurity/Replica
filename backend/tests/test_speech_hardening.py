"""#67 하드닝 시험: 개발용 음성 라우트 게이트(N1), 업로드·본문 크기 검증(N2), 로그·오류 봉투(N3).

가짜 데이터만 쓰고 외부 호출은 없다.
"""

import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app.clients.llm import RemoteLLMClient
from app.core.config import get_settings
from app.main import app

M4A = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A mp42isom"
VALID = {
    ".wav": b"RIFF\x24\x00\x00\x00WAVEfmt ",
    ".mp3": b"ID3\x03\x00\x00\x00\x00\x00\x00",
    ".m4a": M4A,
    ".mp4": M4A,
    ".webm": b"\x1a\x45\xdf\xa3\x01\x00\x00\x00",
    ".ogg": b"OggS\x00\x02\x00\x00\x00\x00\x00\x00",
    ".aac": b"\xff\xf1\x50\x80\x00\x1f\xfc\x00",
}


def use_settings(monkeypatch, **env):
    for key, value in {"APP_ENV": "local", "DEV_SPEECH_ROUTES": "true", **env}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def error_of(res):
    return res.json()["error"]


# --- N1: 개발용 라우트 게이트 ---


@pytest.mark.parametrize("env", [{"DEV_SPEECH_ROUTES": "false"}, {"APP_ENV": "prod"}, {"APP_ENV": "prod", "DEV_SPEECH_ROUTES": "false"}])
def test_dev_routes_are_closed_by_default_and_in_prod(monkeypatch, client, env):
    use_settings(monkeypatch, **env)
    for res in (client.post("/stt", files={"file": ("a.m4a", M4A, "audio/mp4")}), client.post("/tts", json={"text": "안녕"})):
        assert res.status_code == 404 and error_of(res)["code"] == "NOT_FOUND"


def test_dev_routes_default_is_off_without_any_setting(monkeypatch, client):
    monkeypatch.delenv("DEV_SPEECH_ROUTES", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    get_settings.cache_clear()
    assert get_settings().dev_speech_routes is False
    assert client.post("/tts", json={"text": "안녕"}).status_code == 404


def test_dev_routes_open_without_login_only_in_local(monkeypatch, client):
    use_settings(monkeypatch, APP_ENV="local")
    assert client.post("/tts", json={"text": "안녕"}).status_code == 200


def test_dev_env_requires_login_even_when_enabled(monkeypatch, client):
    use_settings(monkeypatch, APP_ENV="dev")
    res = client.post("/tts", json={"text": "안녕"})
    assert res.status_code == 401 and error_of(res)["code"] == "UNAUTHENTICATED"

    async def accepted(authorization):
        assert authorization == "Bearer synthetic-token"

    monkeypatch.setattr("app.routers.speech.get_user_db", accepted)
    assert client.post("/tts", json={"text": "안녕"}, headers={"Authorization": "Bearer synthetic-token"}).status_code == 200


def test_app_routes_do_not_depend_on_the_dev_gate(monkeypatch):
    use_settings(monkeypatch, DEV_SPEECH_ROUTES="false", APP_ENV="prod")
    paths = set(app.openapi()["paths"])
    assert {"/chat/voice", "/chat/turns/{turn_id}/tts"} <= paths  # 앱 경로는 게이트와 무관하게 등록되어 있다


# --- N2: 시그니처·MIME ---


@pytest.mark.parametrize("ext", sorted(VALID))
def test_valid_signature_is_accepted_for_every_extension(monkeypatch, client, ext):
    use_settings(monkeypatch)
    res = client.post("/stt", files={"file": (f"rec{ext}", VALID[ext] + b"\x00" * 16, "application/octet-stream")})
    assert res.status_code == 200, res.text


@pytest.mark.parametrize(
    "name, data",
    [
        ("fake.wav", b"this is not audio at all"),
        ("fake.wav", b"RIFF\x24\x00\x00\x00AVI LIST"),  # RIFF이지만 WAVE가 아님
        ("fake.mp3", b"plain text pretending to be mp3"),
        ("fake.m4a", b"fake-audio-bytes"),
        ("fake.webm", b"OggS\x00\x02"),  # 다른 형식의 시그니처
        ("fake.ogg", b"\x1a\x45\xdf\xa3"),
        ("fake.aac", b"\x00\x01\x02\x03\x04"),
    ],
)
def test_fake_audio_content_is_rejected(monkeypatch, client, name, data):
    use_settings(monkeypatch)
    res = client.post("/stt", files={"file": (name, data, "audio/wav")})
    assert res.status_code == 415
    assert error_of(res)["code"] == "UNSUPPORTED_AUDIO_FORMAT" and error_of(res)["retryable"] is False


@pytest.mark.parametrize("content_type", ["text/plain", "application/json", "image/png", "text/html; charset=utf-8"])
def test_non_audio_mime_is_rejected_even_with_audio_extension(monkeypatch, client, content_type):
    use_settings(monkeypatch)
    res = client.post("/stt", files={"file": ("a.wav", VALID[".wav"], content_type)})
    assert res.status_code == 415 and error_of(res)["code"] == "UNSUPPORTED_AUDIO_FORMAT"


@pytest.mark.parametrize("content_type", ["audio/wav", "audio/x-m4a", "audio/webm;codecs=opus", "video/mp4", "video/webm", "application/octet-stream"])
def test_audio_like_mime_types_are_accepted(monkeypatch, client, content_type):
    use_settings(monkeypatch)
    res = client.post("/stt", files={"file": ("a.m4a", M4A, content_type)})
    assert res.status_code == 200


def test_existing_extension_empty_and_size_checks_still_apply(monkeypatch, client):
    use_settings(monkeypatch, MAX_UPLOAD_MB="1")
    assert client.post("/stt", files={"file": ("a.txt", b"x", "text/plain")}).status_code == 415
    assert error_of(client.post("/stt", files={"file": ("a.wav", b"", "audio/wav")}))["code"] == "EMPTY_FILE"
    over = VALID[".wav"] + b"\x00" * (1024 * 1024)  # 1MB를 1바이트 이상 넘는다
    res = client.post("/stt", files={"file": ("a.wav", over, "audio/wav")})
    assert res.status_code == 413 and error_of(res)["code"] == "FILE_TOO_LARGE"


# --- N2: 본문 크기 제한(파서 이전) ---


def test_declared_content_length_over_limit_is_rejected_before_parsing(monkeypatch, client):
    use_settings(monkeypatch, MAX_UPLOAD_MB="1")
    res = client.post("/stt", content=b"x" * (3 * 1024 * 1024), headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert res.status_code == 413 and error_of(res)["code"] == "FILE_TOO_LARGE"
    assert error_of(res)["message"] == "파일이 너무 큽니다. 최대 1MB" and error_of(res)["field"] == "file"


def test_upload_without_content_length_is_cut_when_the_stream_exceeds_the_limit(monkeypatch, client):
    use_settings(monkeypatch, MAX_UPLOAD_MB="1")
    chunk = b"x" * 65536

    def body():
        for _ in range(48):  # 3MiB, Content-Length 없이 청크 전송
            yield chunk

    res = client.post("/stt", content=body(), headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert res.status_code == 413 and error_of(res)["code"] == "FILE_TOO_LARGE"


def test_voice_route_has_the_same_limit(monkeypatch, client):
    use_settings(monkeypatch, MAX_UPLOAD_MB="1")
    res = client.post("/chat/voice", content=b"x" * (3 * 1024 * 1024), headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert res.status_code == 413 and error_of(res)["code"] == "FILE_TOO_LARGE"


def test_json_body_over_one_mib_is_rejected(monkeypatch, client):
    use_settings(monkeypatch)
    res = client.post("/chat/text", content=b'{"message":"' + b"a" * (1024 * 1024) + b'"}', headers={"Content-Type": "application/json"})
    assert res.status_code == 413 and error_of(res)["code"] == "REQUEST_TOO_LARGE" and error_of(res)["retryable"] is False


def test_get_requests_are_not_limited(client):
    assert client.get("/health").status_code == 200


# --- N3: 오류 봉투 ---

ENVELOPE_KEYS = {"code", "message", "stage", "turn_id", "retryable", "field"}


def test_unknown_path_uses_the_error_envelope(client):
    res = client.get("/no-such-path")
    assert res.status_code == 404
    assert set(error_of(res)) == ENVELOPE_KEYS and error_of(res)["code"] == "NOT_FOUND" and error_of(res)["retryable"] is False


def test_wrong_method_uses_the_error_envelope_and_keeps_allow_header(client):
    res = client.get("/chat/text")
    assert res.status_code == 405 and error_of(res)["code"] == "METHOD_NOT_ALLOWED"
    assert "POST" in res.headers["allow"]


def test_broken_multipart_uses_the_error_envelope(monkeypatch, client):
    use_settings(monkeypatch)
    res = client.post("/stt", content=b"--x\r\nbroken", headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert res.status_code in (400, 422)
    assert set(error_of(res)) == ENVELOPE_KEYS and error_of(res)["code"] == "INVALID_INPUT"


def test_missing_multipart_boundary_uses_the_error_envelope(monkeypatch, client):
    use_settings(monkeypatch)
    res = client.post("/stt", content=b"abc", headers={"Content-Type": "multipart/form-data"})
    assert res.status_code in (400, 422) and set(error_of(res)) == ENVELOPE_KEYS


def test_framework_error_body_contains_no_input_or_internal_detail(monkeypatch, client):
    use_settings(monkeypatch)
    res = client.post("/stt", content=b"--x\r\nsecret-marker-123", headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert "secret-marker-123" not in res.text and "detail" not in res.json()


# --- N3: 알 수 없는 AI 오류 code는 로그에 넣지 않는다 ---


def test_unknown_upstream_error_code_is_not_logged_verbatim(caplog):
    raw = "RAW_UPSTREAM_CODE_secret-marker-9"

    def handler(request: httpx.Request) -> httpx.Response:
        rid = json.loads(request.content)["request_id"]
        return httpx.Response(429, json={"request_id": rid, "error": {"code": raw, "message": "x"}})

    client = RemoteLLMClient("http://ai.test", 5, transport=httpx.MockTransport(handler))
    import asyncio

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as info:
            asyncio.run(client.generate("안녕", [], None))
    assert info.value.code == "AI_FAILED"
    assert raw not in caplog.text and "AI 응답이 계약과 다릅니다" in caplog.text
