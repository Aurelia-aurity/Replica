"""#65: 코드·`.env.example`·README·`requests.http`가 서로 어긋나지 않는지 확인한다.

`backend/` 폴더만 복사한 환경에서도 실행되도록 backend 안의 파일만 읽는다.
"""

import re
from pathlib import Path

import inspect

from app.clients.llm import RemoteLLMClient
from app.core.config import Settings
from app.schemas.chat import ChatTextRequest

BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_LABEL = "Qwen/Qwen3-8B"


def read(name: str) -> str:
    return (BACKEND / name).read_text(encoding="utf-8")


def test_remote_model_label_default_is_the_same_everywhere():
    config_default = Settings.model_fields["llm_model_name"].default
    client_default = inspect.signature(RemoteLLMClient.__init__).parameters["model_name"].default
    assert config_default == client_default == DEFAULT_LABEL
    assert f"# LLM_MODEL_NAME={DEFAULT_LABEL}" in read(".env.example")
    assert re.search(rf"\| `LLM_MODEL_NAME` \| `{re.escape(DEFAULT_LABEL)}` \|", read("README.md"))


def test_model_label_is_not_described_as_confirmed():
    readme = read("README.md")
    row = next(line for line in readme.splitlines() if line.startswith("| `LLM_MODEL_NAME`"))
    assert "미확정" in row and "확정 모델" not in row


def test_readme_lists_every_llm_and_supabase_setting():
    readme = read("README.md")
    for name in (
        "LLM_MODE", "LLM_SERVER_URL", "LLM_SERVER_TOKEN", "LLM_TIMEOUT_SECONDS", "LLM_MODEL_NAME",
        "SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_SECRET_KEY", "SUPABASE_DB_TIMEOUT_SECONDS",
        "DEV_SPEECH_ROUTES", "MAX_UPLOAD_MB",
    ):
        assert f"| `{name}` |" in readme, name


def test_readme_documents_python_312_for_windows_and_posix():
    readme = read("README.md")
    assert "Python 3.12" in readme
    assert "## 실행 (Windows, VS Code)" in readme and "## 실행 (macOS / Linux)" in readme
    assert "python3.12 -m venv .venv" in readme and "source .venv/bin/activate" in readme
    assert "Activate.ps1" in readme


def test_readme_separates_verified_and_unverified_scope():
    readme = read("README.md")
    assert "## 검증 범위" in readme
    scope = readme.split("## 검증 범위", 1)[1].split("\n## ", 1)[0]
    assert "미검증" in scope and "Storage" in scope and "GPU" in scope and "STT" in scope


def request_block(name_fragment: str) -> str:
    blocks = read("requests.http").split("\n### ")
    return next(block for block in blocks if name_fragment in block.splitlines()[0])


def test_requests_http_chat_text_matches_the_current_schema():
    block = request_block("텍스트 대화")
    assert "Authorization: Bearer {{token}}" in block
    body = block.split("\n\n", 1)[1]
    for field in ChatTextRequest.model_fields:
        assert f'"{field}"' in body, field
    assert '"persona_id"' not in body and '"history"' not in body  # 이전 형식의 필드


def test_requests_http_login_required_calls_send_a_bearer_token():
    text = read("requests.http")
    for block in text.split("\n### ")[1:]:
        title = block.splitlines()[0]
        request_line = next((line for line in block.splitlines() if re.match(r"^(GET|POST) ", line)), None)
        if request_line is None or "/health" in request_line:  # 머리말 블록, 로그인 불필요한 요청
            continue
        assert "Authorization: Bearer {{token}}" in block, title


def test_requests_http_voice_call_sends_session_and_turn_ids():
    block = request_block("음성 대화")
    assert 'name="session_id"' in block and 'name="turn_id"' in block and 'name="file"' in block
