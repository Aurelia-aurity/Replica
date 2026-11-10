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
- `/stt`, `/tts`는 개발 점검용이며 **기본이 꺼져 있습니다**(404). `DEV_SPEECH_ROUTES=true`로 켜도 `APP_ENV=prod`에서는 열리지 않고, `APP_ENV=dev`에서는 로그인(Bearer)이 필요하며, `APP_ENV=local`에서만 로그인 없이 Mock으로 동작합니다.
- LLM은 `LLM_MODE=mock`(고정 응답) 또는 `remote`(AI 서버 호출 계약 `../contracts/baseline`, 담당자 합의 전)입니다.
- `app/db/`는 PR #45·#61의 Supabase DB 모듈입니다. #63의 API와 함께 검증하며, #61의 요청 ID 검증 보완을 유지합니다.
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

Python 3.12를 사용합니다. VS Code에서 `backend` 폴더를 연 뒤 터미널(Ctrl+`)에서 실행합니다.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env
uvicorn app.main:app --reload
```

- Supabase 값(`SUPABASE_URL` 등)을 채우지 않으면 로그인이 필요한 API가 500 `INTERNAL_ERROR`를 돌려줍니다(서버 로그에 "Supabase 환경변수 설정을 확인하세요"). `/health`는 영향이 없습니다.
- PowerShell에서 `Activate.ps1`이 막히면 `Set-ExecutionPolicy -Scope Process RemoteSigned`를 실행합니다.
- 실행 후 http://localhost:8000/health 와 http://localhost:8000/docs (Swagger)를 확인합니다.
- VS Code 디버그 실행: F5 → "FastAPI (uvicorn --reload)"
- 테스트: `pytest`
- `python app/main.py`로는 실행되지 않습니다. 반드시 uvicorn으로 실행합니다.

## 실행 (macOS / Linux)

Python 3.12를 사용합니다. `backend` 폴더에서 실행합니다.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
```

- 이후 확인 방법(`/health`, `/docs`, `pytest`)과 Supabase 값 주의는 위 Windows 절차와 같습니다.
- `requests.http`는 VS Code의 REST Client 확장으로 보냅니다. 맨 위 `@token`에 앱(또는 Supabase Auth)에서 받은 access token을 넣습니다. 토큰은 저장소에 올리지 않습니다.

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
- 오류는 `{"error": {code, message, stage, turn_id, retryable, field}}` 형식입니다(명세 2·3장). 없는 경로(404)·허용하지 않는 메서드(405)·본문 크기 초과(413)·깨진 요청 본문(400)도 같은 형식입니다.
- 형식의 기준은 `../contracts/app-backend/api-spec.md`입니다.

## 업로드·요청 검증

- 음성 파일은 확장자, content-type, 파일 머리 시그니처, 크기, 빈 파일 순으로 확인합니다. 확장자만 맞추고 내용이 다른 파일(content-type이 음성이 아니거나 시그니처가 확장자와 다름)은 415 `UNSUPPORTED_AUDIO_FORMAT`입니다. 디코딩까지는 확인하지 않습니다.
- 본문 크기는 파서가 읽기 전에 확인합니다. 음성 경로(`/chat/voice`, `/stt`)는 `MAX_UPLOAD_MB` + 1MiB, 그 밖의 JSON 요청은 1MiB를 넘으면 413으로 끊습니다.
- 개발용 `/stt`, `/tts`는 `DEV_SPEECH_ROUTES`로만 열립니다(위 환경변수 표 참고).

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
| `LLM_MODE` | mock | mock: 고정 응답, remote: 학교 GPU 서버 호출(실제 서버 통합 미검증) |
| `LLM_SERVER_URL` | (없음) | `LLM_MODE=remote`일 때 필수. AI 서버 주소 |
| `LLM_SERVER_TOKEN` | (없음) | AI 서버로 보내는 Bearer 토큰. 서비스 인증 방식은 #14 확인 후 확정 |
| `LLM_TIMEOUT_SECONDS` | (없음) | `LLM_MODE=remote`일 때 필수(0보다 큼). GPU 측정과 합의 전이라 기본값을 두지 않음 |
| `LLM_MODEL_NAME` | `Qwen/Qwen3-8B` | DB에 기록하는 답변 모델 라벨. **서비스 모델은 미확정**이며 이 값은 #77 결과의 조건부 기준선입니다. 배포 라벨이 다르면 바꿉니다 |
| `STT_PROVIDER` | mock | mock 또는 STT 공급자 |
| `TTS_PROVIDER` | mock | mock 또는 TTS 공급자 |
| `MAX_UPLOAD_MB` | 25 | 음성 업로드 최대 크기(MB) |
| `DEV_SPEECH_ROUTES` | false | 개발용 `/stt`, `/tts` 라우트 사용 여부. prod에서는 켜도 열리지 않음 |
| `SUPABASE_URL` | (없음) | 앱이 쓰는 프로젝트와 같은 Supabase 주소(`https://<id>.supabase.co`, 경로 없이). 로그인이 필요한 API에 필수 |
| `SUPABASE_PUBLISHABLE_KEY` | (없음) | 사용자 토큰으로 읽을 때 쓰는 공개 키. 필수 |
| `SUPABASE_SECRET_KEY` | (없음) | 서버 전용 비밀 키(RLS 우회). `.env`에만 두고 앱·저장소에 넣지 않음. 필수 |
| `SUPABASE_DB_TIMEOUT_SECONDS` | 10 | DB 요청 제한 시간(초) |

API 키 등 비밀 값은 `.env`에만 기록하고 커밋하지 않습니다.

## 검증 범위

| 구분 | 상태 | 근거·비고 |
|---|---|---|
| 오프라인 시험(가짜 DB·가짜 AI, Mock STT·TTS) | 통과 | `pytest` 전체 |
| DB 모듈과 로그인 확인 흐름 | 합성 Auth·PostgREST 응답으로 시험 | `tests/test_auth_boundaries.py`, `tests/test_db*.py`. 외부 연결 없음 |
| 실제 Supabase 대상 시험 | 기본 비활성, 선택 실행 | `REPLICA_DB_LIVE_TEST=1`일 때 읽기, `REPLICA_DB_MUTATING_TEST=1`일 때 합성 계정 시험. 결과는 [DB 구조 및 검증 결과](../infra/supabase/README.md) |
| 로컬 서버 ↔ 실제 Supabase 수동 확인 | 개발자 수동 확인(자동 시험 아님) | 로컬에서 서버를 띄워 실제 토큰으로 `/personas` 조회까지 확인 |
| **미검증** | - | 운영 환경, Supabase Storage 정책, 실제 GPU AI 서버(원격 LLM은 가짜 서버로만 시험), 외부 STT·TTS 제공자(Mock만), 앱 실연동 |

## DB 모듈과 초기화

- [함수 인자·반환값·호출 예시](../docs/architecture/db-python-api.md), [통합 인계와 SQL 적용 순서](../docs/architecture/db-weekend-integration.md), [스키마·권한·검증 SQL](../infra/supabase/README.md).
- **DB 전제**: 연결할 Supabase 프로젝트에 적용 이력 `baseline_chat_schema` → `replica_db_redesign` → `weekend_db_integration`이 있어야 대화·대화방·대화 상대 API가 동작합니다. 이미 적용한 SQL은 재실행하지 않으며, 새 환경의 적용 순서는 위 두 문서를 따릅니다.
- **선행 계약 문서**: 앱 ↔ 백엔드 `../contracts/app-backend/api-spec.md`, 백엔드 ↔ DB `../contracts/backend-db/api-spec.md`, 백엔드 ↔ AI `../contracts/baseline/`.
- DB 모듈은 표준 라이브러리를 사용합니다. FastAPI 서버·전체 테스트는 Python 3.12와 `requirements-dev.txt`가 필요합니다.
- 서버 시작 시 설정 팩토리를 만들고, 각 요청의 JWT를 `await database.for_user(access_token)`으로 확인합니다. 사용자 객체를 다른 요청과 공유하지 않습니다.
- DB 모듈 자체는 `.env`를 자동 탐색하지 않습니다. API의 `load_db_settings()`가 실행 폴더의 `.env`를 명시적으로 읽고 환경변수를 우선 적용합니다. DB만 사용할 때는 `Settings.from_env()` 또는 명시적 `Settings.from_env_file(path)`를 사용합니다.
- `.env.example`의 Supabase 항목은 기본적으로 주석입니다. 실제 프로젝트 값으로 채우고 주석을 해제해야 인증·DB API를 사용할 수 있습니다. 미설정 상태에서는 DB로 요청하지 않습니다.
- LLM은 `start_chat_stage(..., request_id=...)`에 쓴 ID를 성공·실패의 `finish_chat_stage`에도 전달합니다. 누락 시 충돌로 거부하며, ID 없이 시작한 기존 호출은 호환됩니다.
- PR 병합은 원격 DB를 초기화하지 않습니다. 기존 적용 이력을 확인하고 새 환경은 스키마 순서에 맞춰 초기화합니다. 이미 적용한 SQL은 재실행하지 않습니다. `guard_llm_request_id.sql`은 #63 실패 호출부 수정 반영 후 적용합니다.
- DB 테스트만 실행: `python -m unittest discover -s tests -p "test_db*.py" -v`. 전체 백엔드 테스트는 `pytest`이며 `PYTHONPATH=.:tests`는 필요하지 않습니다.
- 테스트는 저장소의 `contracts/`, `docs/`, `infra/supabase/`도 읽어 함수 계약·사용법·SQL 인자를 대조합니다. `backend/`만 복사한 환경에서는 전체 검사가 실행되지 않으며, 이 문서나 명세·SQL을 수정할 때도 백엔드 테스트를 실행해야 합니다.
- 실제 읽기 검사는 `REPLICA_DB_LIVE_TEST=1`일 때만, 합성 계정 생성·정리는 `REPLICA_DB_MUTATING_TEST=1`일 때만 실행합니다. 기본 테스트와 CI는 둘 다 비활성입니다. 쓰기 시험은 통합 인계 문서의 동의·정리 절차를 따릅니다.
