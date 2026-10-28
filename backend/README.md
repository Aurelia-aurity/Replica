# Backend

FastAPI 기반 사용자·기록·대화 API와 외부 STT·TTS 연동을 관리합니다.

외부 음성 API 연동은 이 서비스에서 시작하며, 별도의 음성 서버는 만들지 않습니다.
앱·AI 서버 연동 형식은 `../contracts/`, DB 변경은 `../infra/supabase/`에서 관리합니다.

## 현재 상태

- **텍스트 대화 흐름(`/chat/text`)** 은 명세(`../contracts/app-backend`)의 로그인·DB 연동 형식으로 구현했습니다. 로그인한 사용자의
  대화방에 턴을 저장하고, AI를 한 번 호출하고, 결과를 저장합니다. 같은 `turn_id`로 다시 보내면 끝난 답변은 저장된 결과를 돌려줍니다.
- 대화 상대(`/personas`)와 대화방 생성(`/sessions`)도 같은 방식으로 동작합니다.
- 가짜 DB·가짜 AI로 시험했습니다. **실제 Supabase와 실제 AI 서버에는 아직 연결해 보지 않았습니다.**
- **음성 대화 흐름(`/chat/voice`, `/chat/turns/{id}/tts`)** 도 같은 방식으로 구현했습니다(음성 → STT → LLM → TTS, 단계마다 DB 저장).
  STT·TTS는 아직 **Mock**입니다. 공급자와 호출 규격이 정해지면 `app/clients/stt.py`, `tts.py`에 구현을 추가합니다.
- `/stt`, `/tts`는 개발 점검용이며 로그인 없이 Mock으로 동작합니다.
- LLM은 `LLM_MODE=mock`(고정 응답) 또는 `remote`(AI 서버 호출 계약 `../contracts/baseline`, 담당자 합의 전)입니다.
- `app/db/`는 PR #61(`jaesung_41-db-integration`, #45 위에 쌓인 후속 PR)의 DB 모듈 사본입니다. 두 PR이 병합되면 같은 파일이므로 그쪽을 기준으로 합니다.
  AI 요청 ID를 DB에 먼저 기록해 결과 저장 때 확인하고, `finish_reason`·전체 소요시간(`total_ms`)을 저장하며, 이력은 DB의 최근 20쌍 조회를 씁니다.

## 폴더 구조

```
backend/
├─ app/
│  ├─ main.py          # 앱 생성, CORS, 라우터 등록
│  ├─ core/            # 설정(config.py), 로그인 확인(auth.py), 오류 형식(errors.py), 업로드 검증(uploads.py)
│  ├─ routers/         # API 엔드포인트 (health, personas, sessions, chat, speech)
│  ├─ schemas/         # 요청·응답 형식 (프론트와의 약속)
│  ├─ services/        # 텍스트(chat_text.py)·음성(chat_voice.py) 대화 한 턴, 단계 공통 처리(turn_stages.py), /stt·/tts 점검(pipeline.py)
│  ├─ clients/         # 외부 호출 (LLM 서버, STT API, TTS API). STT·TTS는 현재 Mock
│  └─ db/              # Supabase 로그인 확인·저장·조회 모듈 (#45, #61)
├─ tests/              # pytest
├─ Dockerfile          # 컨테이너 이미지 (build context: backend/)
├─ requests.http       # VS Code REST Client용 요청 모음
├─ .env.example        # 환경변수 목록 (복사해서 .env로 사용)
└─ requirements*.txt   # 버전 고정
```

## 실행 (Windows, VS Code)

VS Code에서 `backend` 폴더를 연 뒤 터미널(Ctrl+`)에서 실행합니다.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env
uvicorn app.main:app --reload
```

- PowerShell에서 `Activate.ps1`이 막히면 `Set-ExecutionPolicy -Scope Process RemoteSigned`를 실행합니다.
- 실행 후 http://localhost:8000/health 와 http://localhost:8000/docs (Swagger)를 확인합니다.
- VS Code 디버그 실행: F5 → "FastAPI (uvicorn --reload)"
- 테스트: `pytest`
- `python app/main.py`로는 실행되지 않습니다. 반드시 uvicorn으로 실행합니다.

## API 목록

Swagger(`/docs`)에서 요청·응답 형식을 확인하고 직접 호출해 볼 수 있습니다.

| 엔드포인트 | 입력 | 출력 |
|---|---|---|
| `GET /health` | - | 서버 상태, 연동 모드 |
| `POST /personas` | JSON `{name, persona_id?}` (로그인 필요) | 대화 상대 객체 (201) |
| `GET /personas`, `GET /personas/{id}` | `limit`, `offset` (로그인 필요) | 목록 `{items, limit, offset}` / 대화 상대 객체 |
| `POST /sessions` | JSON `{persona_id, ai_notice_ack_at, title?, session_id?}` (로그인 필요) | 대화방 객체 (201) |
| `POST /chat/text` | JSON `{session_id, turn_id, message}` (로그인 필요) | `{turn_id, answer, is_ai_generated, finish_reason, model_name, timings}` |
| `POST /chat/voice` | multipart `file`(녹음), `session_id`, `turn_id` (로그인 필요) | `{turn_id, transcript, answer, is_ai_generated, finish_reason, model_name, tts_status, audio_base64, audio_format, tts_error, timings}` |
| `POST /chat/turns/{turn_id}/tts` | (로그인 필요) 답변이 끝난 턴 | `{turn_id, tts_status, audio_base64, audio_format, timings}` / 합성 중·완료된 턴은 409 |
| `POST /stt` | multipart `file` | `{text, timings}` |
| `POST /tts` | JSON `{text}` | 음성 파일 (audio/wav) |

- 지원 음성 형식: wav, mp3, m4a, webm, ogg, mp4, aac. 그 외는 415 `UNSUPPORTED_AUDIO_FORMAT`, 25MB 초과는 413 `FILE_TOO_LARGE`, 빈 파일은 400 `EMPTY_FILE`.
- 음성 대화: STT·LLM이 실패하면 `stage`(`stt`/`llm`)가 표시된 오류로, **TTS만 실패하면 전사문·답변을 유지한 채 200**으로 `tts_status: failed`를 돌려줍니다.
  이때 `POST /chat/turns/{turn_id}/tts`로 음성만 다시 만들 수 있습니다.
- 응답 음성은 저장하지 않습니다. TTS까지 끝난 턴을 같은 `turn_id`로 다시 보내면 저장된 전사문·답변을 쓰고 음성만 다시 합성합니다.
- `timings`는 구간별 시간(ms)이며 거치지 않은 구간은 `null`입니다.
- `/chat/voice`의 응답 음성은 base64 문자열입니다. 브라우저에서는 `new Audio("data:audio/wav;base64," + audio_base64).play()`로 재생합니다.
- `is_ai_generated`는 항상 `true`이며, 화면에 "AI 생성" 표시를 붙입니다(FR-08).
- 로그인이 필요한 API는 `Authorization: Bearer <Supabase JWT>` 헤더를 보냅니다. 없거나 잘못되면 401 `UNAUTHENTICATED`입니다.
- 오류는 `{"error": {code, message, stage, turn_id, retryable, field}}` 형식입니다(명세 2·3장). 음성 API의 오류는 아직 기본 형식입니다.
- 형식의 기준은 `../contracts/app-backend/api-spec.md`입니다.

## Docker

```powershell
docker build -t replica-api .
docker run -p 8000:8000 --env-file .env replica-api
```

AWS EC2 배포 시에는 외부 노출을 막기 위해 `-p 127.0.0.1:8000:8000`으로 실행하고 Cloudflare Tunnel로 연결합니다.

## 환경변수

| 이름 | 기본값 | 설명 |
|---|---|---|
| `APP_ENV` | local | local / dev / prod |
| `LOG_LEVEL` | INFO | 로그 레벨 |
| `CORS_ORIGINS` | `["*"]` | 허용 출처 (JSON 배열). 배포 시 웹앱 도메인으로 제한 |
| `LLM_MODE` | mock | mock: 고정 응답, remote: 학교 GPU 서버 호출(미구현) |
| `STT_PROVIDER` | mock | mock 또는 STT 공급자 |
| `TTS_PROVIDER` | mock | mock 또는 TTS 공급자 |
| `MAX_UPLOAD_MB` | 25 | 음성 업로드 최대 크기(MB) |

API 키 등 비밀 값은 `.env`에만 기록하고 커밋하지 않습니다.
