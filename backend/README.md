# Backend

FastAPI를 계획 기준으로 사용자·기록·대화 API와 외부 STT·TTS 연동을 관리합니다.

- `app/`: 서비스 코드
  - `app/db/`: DB 연결과 대화방·턴 저장·조회 함수
- `tests/`: 백엔드 테스트
- `.env.example`: 비밀 값이 없는 설정 예시

외부 음성 API 연동은 이 서비스에서 시작하며, 별도의 음성 서버는 만들지 않습니다.
앱 및 AI 서버 연동 형식은 `../contracts/`, DB 변경은 `../infra/supabase/`에서 관리합니다.

## 준비

Python 3.12 계열 기준입니다.

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # macOS·Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
copy .env.example .env          # macOS·Linux: cp .env.example .env
```

## DB 함수 사용

엔드포인트는 SQL을 직접 쓰지 않고 `app.db.chat`의 함수를 호출합니다.

```python
from app.db import chat, close_pool, init_pool

init_pool()          # 앱 시작 시 (DATABASE_URL 사용)
...
turn = chat.create_turn(user_id, session_id, turn_id, "text", "오늘 뭐 했어?")
...
close_pool()         # 앱 종료 시
```

- 모든 함수의 첫 인자는 `user_id`입니다. baseline에서는 `.env`의 `BASELINE_USER_ID`를 넘기고, 고도화 1부터는 로그인 토큰에서 꺼낸 값을 넘깁니다.
- 대상이 없거나, 다른 사용자 소유이거나, 지금 상태에서 할 수 없는 변경이면 `None`을 돌려줍니다. 엔드포인트에서 404나 409로 바꿔 응답합니다.
- `turn_id`는 앱이 전송마다 만든 uuid입니다. 재시도 때 같은 값을 보내면 `create_turn`이 기존 턴을 돌려주므로, 상태를 보고 실패한 단계부터 다시 실행합니다.

| 함수 | 호출 시점 |
| --- | --- |
| `create_session(user_id, persona_id)` | 대화 화면 진입 |
| `ack_ai_notice(user_id, session_id)` | AI 생성 안내 확인 (FR-08) |
| `create_turn(user_id, session_id, turn_id, input_mode, question=None)` | 질문 수신 |
| `get_turn(user_id, turn_id)` | 턴 상태 조회 |
| `set_question(user_id, turn_id, text, stt_ms=None)` | STT 성공 |
| `start_answer(user_id, turn_id, model_name, with_audio=False)` | LLM 호출 직전, LLM 재시도 |
| `complete_answer(user_id, turn_id, text, llm_ms=None)` | LLM 성공 |
| `start_tts(user_id, turn_id)` | TTS 재시도, 텍스트 대화 후 음성 요청 |
| `set_tts_result(user_id, turn_id, ok, tts_ms=None, error=None)` | TTS 끝 |
| `fail_turn(user_id, turn_id, stage, error)` | STT·LLM 실패 (`stage`: `"stt"`, `"llm"`) |
| `record_total_ms(user_id, turn_id, total_ms)` | 턴 종료 |
| `get_context(user_id, session_id, limit=10)` | LLM 프롬프트 조립 |
| `list_turns(user_id, session_id)` | 대화 화면 표시 |
| `get_turn_results(user_id, session_id)` | 대표 시나리오 측정 (2.7절) |

## 테스트

DB 테스트는 로컬 Supabase가 필요합니다(`../infra/supabase/README.md`). `TEST_DATABASE_URL`이 없으면 건너뜁니다.

```bash
cd backend
pytest
```
