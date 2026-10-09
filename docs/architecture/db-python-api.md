# Python DB 함수 명세 및 사용법

관리 원본: 이 문서. 최초 등록: [이슈 #26](https://github.com/Aurelia-aurity/Replica/issues/26). HTTP API 계약은 별도로 `contracts/`에서 합의한다.

### 관련 파트

백엔드 (backend), 인프라·DB (infra), API 계약 (contracts), AI (ai)

### 목적

#25에서 적용한 DB 스키마와 SQL 함수 위에, 백엔드가 import하여 사용할 Python 저장·조회 모듈을 구현한다.
함수의 인자·반환값·호출 순서·오류를 명세하여 API 담당자가 DB 내부 구조나 서버 비밀키를 직접 다루지 않도록 한다.

### 작업 범위

- `backend/app/db/`에 비동기 저장·조회 모듈과 인증·설정·오류·전송 계층 구현.
- Python 3.11 이상, 표준 라이브러리 기반 Supabase Auth/REST/RPC 호출. 추가 패키지 의존성 없이 FastAPI의 async 함수에서 사용.
- 요청마다 `Database.for_user(access_token)`으로 Supabase Auth 서버에 토큰을 확인하고, 검증한 사용자 ID를 내부적으로 주입.
- 조회는 publishable/anon 키 + 사용자 JWT로 RLS를 적용. 쓰기/RPC는 서버 secret/service-role 키를 사용하되 모든 소유자 조건은 검증한 사용자 ID에서만 생성.
- 사용자 토큰이나 서버 키를 로그·예외 문자열·반환값에 포함하지 않음. 네트워크 실패 시 쓰기를 무조건 자동 재시도하지 않음.
- 기존 DB SQL 함수 8개 호출과 persona/session/record/text/profile/chunk/deletion-job의 저장·조회.
- 함수 명세·호출 예시·반환 구조·테스트·환경변수 예시 문서화.

범위 제외: HTTP 라우트, 회원가입/로그인 화면, 실제 AI/STT/TTS 실행, 파일 업로드/삭제, private 버킷 생성, 삭제 worker, persona/계정 전체 삭제.

### 함수 사용 명세

모든 메서드는 `await`로 호출하며, 호출자가 `user_id`를 전달하는 인자는 없다.
`UUID` 인자는 UUID 객체 또는 UUID 문자열, 시간은 timezone-aware `datetime`을 받는다.
`Row`는 DB 행의 JSON dict(UUID·시간은 문자열), `list[Row]`는 행 목록이다. 읽을 수 없거나 다른 사용자 소유인 단건은 동일한 `NotFoundError`로 처리한다.

#### 초기화·인증

```python
from app.db import Database, Settings
db = Database(Settings.from_env())
user_db = await db.for_user(access_token)
```

필수 환경변수: `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY`.
기존 anon/service-role 키는 `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` 대체 이름으로 지원한다.
`SUPABASE_DB_TIMEOUT_SECONDS` 기본 10초. 실제 키는 환경변수로만 전달하고 .env를 커밋하지 않는다.

#### 기본 저장·조회

아래 서명에서 `*` 뒤 인자는 이름을 지정해 호출한다. 기본 목록 limit은 50, 최대 100이며 offset은 0 이상이다.

| 함수 | 인자 | 반환·동작 |
| --- | --- | --- |
| create_persona | name: str, *, persona_id: UUID 또는 None=None | Row, 대상 생성 |
| get_persona | persona_id: UUID | Row, active 대상만 조회 |
| list_personas | *, limit: int=50, offset: int=0 | list[Row] |
| create_session | persona_id: UUID, *, ai_notice_ack_at: datetime, title: str 또는 None=None, session_id: UUID 또는 None=None | Row, 같은 사용자 active 대상의 대화방 생성 |
| get_session | session_id: UUID | Row |
| list_sessions | *, persona_id: UUID 또는 None=None, limit: int=50, offset: int=0 | list[Row] |
| delete_session_rows | session_id: UUID | None, DB 대화방과 turn만 삭제. 파일·계정 삭제 아님 |
| get_turn | turn_id: UUID | Row |
| list_turns | session_id: UUID, *, after_turn_no: int=0, limit: int=50 | list[Row], turn_no 오름차순 커서 조회 |
| reserve_record | persona_id: UUID, *, kind: text/audio, original_name: str, mime_type: str, size_bytes: int, consent_at: datetime, consent_version: str, record_id: UUID 또는 None=None | Row, uploading 예약 및 user_id/record_id/original 경로 생성. 파일 업로드는 별도 |
| get_record | record_id: UUID | Row, deleting 제외 |
| list_records | *, persona_id: UUID 또는 None=None, limit: int=50, offset: int=0 | list[Row] |
| update_record_status | record_id: UUID, *, expected_status: str, status: str, error_code: str 또는 None=None | Row, 예상 상태와 일치할 때만 변경. deleting은 삭제 요청 함수만 사용 |
| create_text_draft | record_id: UUID, *, revision: int, content: str, text_version_id: UUID 또는 None=None | Row, 양의 revision 초안 생성. 충돌 시 ConflictError |
| get_text_version | text_version_id: UUID | Row |
| list_text_versions | record_id: UUID | list[Row], revision 오름차순 |
| edit_text_draft | text_version_id: UUID, *, content: str, expected_updated_at: datetime | Row, draft 및 예상 수정시각과 일치할 때만 편집 |
| get_personalization_input | persona_id: UUID | dict의 source_revision: int, sources: list[Row]. 현재 확정·ready 자료만 포함 |
| get_current_style_profile | persona_id: UUID | Row 또는 None, ready 프로필만 |
| save_record_chunks | text_version_id: UUID, *, embedding_model: str, chunks: Sequence[ChunkInput] | list[Row], 확정 자료의 chunk 일괄 INSERT. 1회 요청으로 원자적 저장 |
| get_deletion_job | job_id: UUID | Row |
| list_deletion_jobs | *, limit: int=50, offset: int=0 | list[Row] |

`ChunkInput(chunk_no: int, content: str, embedding: Sequence[float])`: 번호는 0 이상, 내용은 공백이 아닌 문자열, 벡터는 유한한 숫자로 구성된 비영 벡터. 배치 내부 번호 중복 및 차원 혼합을 거부한다.

기록 상태 전이: uploading→uploaded/failed, uploaded→processing/ready/failed, processing→ready/failed, failed→uploading/processing. ready 수정·deleting 전환은 일반 상태 함수로 허용하지 않는다.
초안 편집 시 확정 버전을 덮어쓰지 않고 새 revision을 생성한다.

#### 기존 SQL 함수 호출

| Python 함수 | 인자 | 반환 |
| --- | --- | --- |
| create_chat_turn | session_id: UUID, turn_id: UUID, *, input_mode: text/voice, question: str 또는 None=None, audio_sha256: str 또는 None=None | Row, 동일 ID/입력 재사용 |
| start_chat_stage | turn_id: UUID, stage: stt/llm/tts, *, model_name: str 또는 None=None, with_audio: bool=False | Row, 해당 *_attempt 반환 |
| finish_chat_stage | turn_id: UUID, stage: stt/llm/tts, *, attempt: int, ok: bool, text: str 또는 None=None, elapsed_ms: int 또는 None=None, error_code: str 또는 None=None | Row, 해당 시도의 pending 결과만 저장 |
| expire_chat_stages | *, started_before: datetime | int, 만료 처리한 단계 수 |
| confirm_record_text | text_version_id: UUID | Row |
| publish_style_profile | persona_id: UUID, *, source_revision: int, content: dict, model_name: str, source_ids: Sequence[UUID] | Row, 생성 당시 자료 개정 번호와 확정 출처 검증 |
| search_record_chunks | persona_id: UUID, *, embedding_model: str, query_embedding: Sequence[float], limit: int=10, min_similarity: float=0.5 | list[Row], chunk_id/record_id/text_version_id/content/similarity |
| request_record_deletion | record_id: UUID | Row, 삭제 job. 실제 파일 삭제 완료가 아님 |

호출 흐름:

```python
from uuid import uuid4
user_db = await db.for_user(access_token)
turn = await user_db.create_chat_turn(
    session_id, uuid4(), input_mode="text", question="안녕"
)
claimed = await user_db.start_chat_stage(turn["id"], "llm", model_name="agreed-model")
# 실행권 확보 성공 후에만 외부 LLM을 호출한다.
answer_text = "합성 예시 응답"  # 실제 AI 호출은 별도 서비스 담당
saved = await user_db.finish_chat_stage(
    turn["id"], "llm", attempt=claimed["llm_attempt"],
    ok=True, text=answer_text, elapsed_ms=120
)
```

재전송 시 같은 turn_id를 유지한다. 실패 후 재시작은 새 *_attempt를 받아야 하며, 예전 attempt 결과를 다시 저장하면 ConflictError이다.
TTS 실패는 이미 완료된 답변 텍스트를 제거하지 않는다.
프로필은 자료 수집 시 얻은 source_revision과 source_ids를 발행 시 전달하며 입력 변경 시 다시 생성한다.

#### 오류 계약

- ValidationError: 잘못된 인자, 22023/23514 등 → API 계층의 400 또는 422 매핑 후보
- AuthenticationError: 잘못되거나 만료된 토큰 → 401
- NotFoundError: 없거나 현재 사용자에게 안 보이는 자원 → 404
- ConflictError: 상태/수정시각/요청 ID/버전 충돌, 55000/23505 → 409
- PermissionDeniedError: DB 권한 설정 불일치 → 내부 권한 오류로 구분
- DatabaseUnavailableError: 연결·타임아웃·일시 서비스 오류. 쓰기 성공 여부가 불명확할 수 있으므로 같은 요청 ID로 재조회 후 판단
- DatabaseError: 그 밖의 DB/응답 오류. 원문 SQL·개인 데이터·비밀값 대신 안전한 메시지와 code만 제공

### 완료 조건

- [x] 명세에 있는 함수의 서명·반환값·호출 예시를 저장소 문서에 유지한다.
- [x] Python 비동기 모듈을 구현하고 8개 RPC의 SQL 인자명을 정확히 연결한다.
- [x] Auth 토큰 검증과 요청별 사용자/서버 헤더 분리를 검사한다.
- [x] 모든 읽기·쓰기/RPC에서 사용자 소유권 및 삭제 중 부모 상태를 검사한다.
- [x] 잘못된 UUID·시간·상태·벡터·호출 순서를 검사하고 안전한 오류로 변환한다.
- [x] 목록 순서/페이지, 낙관적 충돌, 입력 변경, 반복 요청·재시도와 비밀정보 비노출을 시험한다.
- [x] 외부 연결 없는 자동 테스트와 실제 Supabase 연결 검증 여부·미검증 이유를 기록한다.
- [x] 환경변수 예시·실행/테스트 방법·백엔드 담당자의 연동 방법을 정리한다.

### 관련 이슈·선행 작업

- #25: 스키마 및 SQL 함수 적용·DB 검증
- #6, #7: 대화 API에서 단계별 저장·조회 모듈 사용
- #8: JWT 전달 계약 및 요청마다 for_user 호출
- #9, #10: 업로드 예약·상태 및 초안 확정
- #11: 실제 파일/프로필/계정 삭제 worker는 별도 연동
- #12: 대화 이력 저장·조회·DB 행 삭제
- #15, #21: AI 호출/배포 환경과 저장 모듈 연결

### 참고 자료

- #25의 DB 설계·검증 결과
- 산출물: backend/app/db/, backend/tests/test_db.py, backend/tests/test_db_live.py, docs/architecture/db-python-api.md
- Supabase Auth: https://supabase.com/docs/reference/python/auth-getuser
- API 키: https://supabase.com/docs/guides/getting-started/api-keys
- DB 함수: https://supabase.com/docs/guides/database/functions

### AI 활용·검증

- AI 활용 범위: Codex로 명세·Python 모듈·테스트·사용 문서를 작성한다.
- 검토·확인: 자동 검증 결과와 실제 연결의 확인/미확인 범위를 후속 기록하고, 팀의 API 계약 확인·코드 리뷰는 별도로 받는다.


## 추가 사용 예시 및 제한

```python
from datetime import datetime, timezone
from app.db import ChunkInput

# 파일 업로드 예약: 파일 자체를 저장하는 함수가 아니다.
record = await user_db.reserve_record(
    persona_id, kind="text", original_name="sample.txt", mime_type="text/plain",
    size_bytes=100, consent_at=datetime.now(timezone.utc), consent_version="v1",
)
# 여기서 별도 Storage API로 실제 업로드 및 무결성/크기 검증을 한다.
await user_db.update_record_status(record["id"], expected_status="uploading", status="uploaded")
await user_db.update_record_status(record["id"], expected_status="uploaded", status="ready")
draft = await user_db.create_text_draft(record["id"], revision=1, content="검토할 합성 텍스트")
edited = await user_db.edit_text_draft(
    draft["id"], content="사용자가 수정한 합성 텍스트",
    expected_updated_at=datetime.fromisoformat(draft["updated_at"].replace("Z", "+00:00")),
)
# 사용자 확인 이후에만 확정한다.
confirmed = await user_db.confirm_record_text(edited["id"])
await user_db.save_record_chunks(
    confirmed["id"], embedding_model="example-model",  # 팀의 실제 모델로 교체
    chunks=[ChunkInput(0, confirmed["content"], [1.0, 0.0])],  # 합성 시험 벡터
)
snapshot = await user_db.get_personalization_input(persona_id)
profile = await user_db.publish_style_profile(
    persona_id, source_revision=snapshot["source_revision"],
    content={"tone": "합성 프로필 예시"}, model_name="agreed-model",
    source_ids=[source["id"] for source in snapshot["sources"]],
)
job = await user_db.request_record_deletion(record["id"])
# job 생성은 실제 파일 삭제 완료가 아니다. 별도 worker가 나머지 단계를 처리한다.
```

- 목록 순서는 생성 시각 내림차순 + UUID 내림차순이며, turn만 turn_no 오름차순이다. offset 페이지는 조회 중 새 행이 추가되면 중복/이동할 수 있다. 고정 snapshot을 보장하는 페이지는 아니다.
- 전체 텍스트 버전 및 개인화 출처는 100건씩 페이지를 수집한다. Data API의 최대 반환 행 설정은 최소 100 이상이어야 한다. 수집 도중 원본이 바뀌면 publish_style_profile의 개정 번호 검증에서 충돌로 거부한다.
- title 최대 200자, 원본 이름 255자, 동의 버전 100자, error_code는 영문/숫자/점/하이픈/밑줄 1~64자. 벡터 차원 1~16000, chunk 한 배치는 1~100개. 모델에 맞는 차원 합의와 파일 용량 검증은 별도다.
- error_code에 사용자 질문·답변·원문 예외를 넣지 않는다. 예: llm_timeout, tts_provider_error.
- create 계열의 선택 ID를 생략하면 새 UUID를 생성한다. 불확실한 쓰기 결과를 안전하게 확인하려면 호출 전에 ID를 만들고 재사용한다. 일반 생성 함수는 중복 INSERT 시 ConflictError를 반환하므로 먼저 해당 ID로 조회한다. create_chat_turn만 동일 입력 재전송을 자동 재사용한다.
- 공개된 저장 함수는 임의 컬럼 PATCH/upsert를 받지 않는다. DB 트리거와 RPC의 잠금/제약이 최종 무결성을 판단한다. 별도 HTTP 요청 사이의 조회+INSERT 전체를 단일 트랜잭션으로 묶지는 않는다.
- for_user는 Supabase Auth 서버로 토큰과 현재 계정을 확인하지만 모든 연산에서 auth.sessions를 재검증하지는 않는다. 엄격한 세션 폐기와 persona/계정 삭제를 구현할 때 별도 세션 검사 및 정책이 필요하다.
- 동기 HTTP 요청은 asyncio의 기본 스레드 실행기로 넘긴다. 외부 I/O는 이벤트 루프를 막지 않지만, 취소 시 이미 시작된 스레드/쓰기를 되돌리지는 않는다. timeout과 outcome_unknown을 기준으로 조회 후 처리한다. 기본 urllib 전송은 연결 풀을 유지하지 않는다. 높은 부하는 별도 측정 후 Transport 구현 교체로 대응한다.
- HTTP 인증·업로드 검증·세션 폐기·worker 구현·HTTP 상태 코드 매핑은 API 담당의 책임이다. 이 모듈의 예외를 사용자에게 SQL/원문 메시지와 함께 반환하지 않는다.

## 확인 결과

- 오프라인 자동 테스트 34개 통과. Auth/REST는 가짜 응답이며 JSON·헤더·쿼리 인코딩과 리다이렉트 차단은 실제 로컬 HTTP 서버에서 확인했다. 공개된 사용자 함수 모두가 명세 표에 있는지도 검사한다.
- 원격 Supabase의 함수명/인자/반환 타입을 조회해 8개 SQL 함수와 대조했다. 오프라인 테스트에서도 SQL 원본의 인자명과 RPC JSON 키를 비교한다.
- 2026-10-09: 사용자가 설정한 로컬 `.env`를 검사 프로세스에만 주입해 실제 연결 검사 5개가 통과했다. 공개 키의 Auth 접근, 서버 키의 9개 테이블 조회, 읽기 전용 검색 RPC, 비로그인 공개 키의 테이블/RPC 접근 거부, 잘못된 사용자 토큰 거부를 확인했다. 키 값과 조회 데이터는 출력하지 않았다. 서비스의 `.env` 자동 로딩은 추가하지 않았다.
- 총 40개 검사 중 39개 통과, 실제 로그인 사용자 검사 1개 skip. 실제 사용자 JWT가 없어 `Database.for_user()`의 정상 로그인 및 해당 사용자의 읽기·쓰기 흐름은 아직 확인하지 않았다. `REPLICA_DB_TEST_ACCESS_TOKEN`을 환경변수로 제공하면 선택적 읽기 전용 검사를 추가 실행할 수 있다. 실제 쓰기 및 사용자 간 격리의 원격 종단간 검증은 별도다.
- 이번 Python 작업은 원격 스키마 및 기존 사용자/데이터를 변경하지 않았다. #25의 SQL 롤백 검증과 이번 Python 전송 계층 테스트는 서로 다른 검증이다.
- 이번 저장 모듈과 별개인 기존 협업 스크립트 테스트 100개도 Python UTF-8 모드에서 확인했다. Windows 기본 cp949에서는 기존 테스트 한 개가 UTF-8 문서를 읽지 못하므로 `python -X utf8 -m unittest discover -s scripts/tests`로 실행한다.
- 구현 공유 브랜치는 `jaesung_26-db-access`이다. PR/팀 리뷰, 정상 로그인 사용자의 통합 검증과 배포 환경 연결은 후속 확인 항목이다.
