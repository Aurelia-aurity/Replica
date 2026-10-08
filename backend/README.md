# Backend

FastAPI 기반 사용자·기록·대화 API와 외부 STT·TTS 연동을 관리합니다.

외부 음성 API 연동은 이 서비스에서 시작하며, 별도의 음성 서버는 만들지 않습니다.
앱·AI 서버 연동 형식은 `../contracts/`, DB 변경은 `../infra/supabase/`에서 관리합니다.

## 현재 상태

STT·LLM·TTS는 **Mock(고정 응답)** 입니다. 요청·응답 형식, 업로드 검증, 구간별 시간 측정은 실제로 동작하며,
`.env`의 `LLM_MODE`, `STT_PROVIDER`, `TTS_PROVIDER` 값으로 Mock과 실제 구현을 교체하도록 설계했습니다.
인증, DB(Supabase), 실제 STT·TTS·LLM 연동은 아직 구현하지 않았습니다.

## 폴더 구조

```
backend/
├─ app/
│  ├─ main.py          # 앱 생성, CORS, 라우터 등록
│  ├─ core/            # 설정(config.py), 업로드 검증(uploads.py)
│  ├─ routers/         # API 엔드포인트 (health, chat, speech)
│  ├─ schemas/         # 요청·응답 형식 (프론트와의 약속)
│  ├─ services/        # 대화 파이프라인 (STT → LLM → TTS), 구간별 시간 측정
│  └─ clients/         # 외부 호출 (LLM 서버, STT API, TTS API). 현재 Mock
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

## API 목록 (현재 Mock 응답)

Swagger(`/docs`)에서 요청·응답 형식을 확인하고 직접 호출해 볼 수 있습니다.

| 엔드포인트 | 입력 | 출력 |
|---|---|---|
| `GET /health` | - | 서버 상태, 연동 모드 |
| `POST /chat/text` | JSON `{message, persona_id?, history?}` | `{answer, is_ai_generated, model_name, timings}` |
| `POST /chat/voice` | multipart `file`(녹음), `persona_id?` | `{transcript, answer, is_ai_generated, audio_base64, audio_format, timings}` |
| `POST /stt` | multipart `file` | `{text, timings}` |
| `POST /tts` | JSON `{text}` | 음성 파일 (audio/wav) |

- 지원 음성 형식: wav, mp3, m4a, webm, ogg, mp4, aac. 그 외는 415, 25MB 초과는 413, 빈 파일은 400.
- `timings`는 구간별 시간(ms)이며 거치지 않은 구간은 `null`입니다.
- `/chat/voice`의 응답 음성은 base64 문자열입니다. 브라우저에서는 `new Audio("data:audio/wav;base64," + audio_base64).play()`로 재생합니다.
- `is_ai_generated`는 항상 `true`이며, 화면에 "AI 생성" 표시를 붙입니다(FR-08).
- 이 형식은 백엔드 구현 기준의 초안이며, 확정은 `../contracts/`에서 합의합니다.

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
