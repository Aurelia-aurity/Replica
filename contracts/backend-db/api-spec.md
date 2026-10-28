# 레플리카 API 명세서: 백엔드 ↔ DB

기준일 2026-10-13 · DB 담당이 올린 이슈 #25·#26을 바탕으로 정리한 초안이며 합의된 규격이 아닙니다.

**[구현]** 코드가 있고 동작함   **[제안]** 이슈를 근거로 이 문서가 제안한 규격   **[미정]** 아직 정해지지 않음   (표에서 `필수`는 필수 여부)

- [1. 백엔드 ↔ DB API](#s1)
- [2. DB 오류 메시지](#s2)

관련 문서: [앱 ↔ 백엔드 규격](../app-backend/api-spec.md) (이 문서에서 "앱↔백엔드 규격 문서"로 부름)

<a id="s1"></a>

## 1. 백엔드 ↔ DB API (백엔드가 호출하는 DB 함수)

&lt;span class="badge draft"&gt;초안&lt;/span&gt; DB 담당의 이슈 #25(스키마·SQL 함수)와 #26(Python 함수 명세)입니다. 브랜치 `jaesung_26-db-access`, 문서 `docs/architecture/db-python-api.md`. PR은 아직 없고, 정상 로그인 사용자의 실제 읽기·쓰기 검증은 남아 있습니다.

### 연결과 사용 방식

| 항목 | 내용 |
|---|---|
| 초기화 | `db = Database(Settings.from_env())` |
| 요청별 사용자 확인 | `user_db = await db.for_user(access_token)` — Supabase Auth 서버로 토큰을 확인하고 검증한 사용자 id를 내부에서 주입. **호출자가 `user_id`를 넘기는 인자는 없음** |
| 호출 방식 | 모든 메서드는 `await`로 호출 (비동기). UUID 인자는 UUID 객체 또는 문자열, 시간은 timezone-aware `datetime` |
| 권한 모델 | 읽기: 공개 키 + 사용자 JWT(RLS 적용). 쓰기·RPC: 서버 비밀 키를 쓰되 모든 소유자 조건은 검증한 사용자 id에서만 생성. 일반 로그인 사용자는 자기 데이터 읽기만 가능 |
| 반환 | `Row`: DB 행의 JSON dict (UUID·시간은 문자열). `list[Row]`: 행 목록. 읽을 수 없거나 다른 사용자 소유인 단건은 동일하게 `NotFoundError` |
| 목록 | 기본 `limit` 50, 최대 100, `offset` 0 이상. 순서는 생성 시각 내림차순 + UUID 내림차순 (턴만 `turn_no` 오름차순). 조회 중 새 행이 생기면 중복·이동 가능 |
| 쓰기 재시도 | 네트워크 실패 시 쓰기를 자동 재시도하지 않음. 불확실한 쓰기는 호출 전에 ID를 만들어 두고 같은 ID로 조회해 확인 |
| 비밀 보호 | 사용자 토큰·서버 키를 로그·예외 문자열·반환값에 포함하지 않음 |

#### 환경변수

| 이름 | 필수 | 설명 |
|---|---|---|
| `SUPABASE_URL` | 필수 | Supabase 프로젝트 주소 |
| `SUPABASE_PUBLISHABLE_KEY` | 필수 | 공개용 키 (읽기에 사용, 사용자 JWT와 함께 RLS 적용). 기존 이름 `SUPABASE_ANON_KEY`도 지원 |
| `SUPABASE_SECRET_KEY` | 필수 | 서버 비밀 키 (쓰기·RPC에 사용). 기존 이름 `SUPABASE_SERVICE_ROLE_KEY`도 지원. 앱·저장소에 넣지 않음 |
| `SUPABASE_DB_TIMEOUT_SECONDS` | 선택 | DB 호출 타임아웃. 기본 10초 |

### 대화 함수 (이슈 #6 · #7 · #12)

| 함수 | 인자 (`*` 뒤는 이름을 지정해 호출) | 반환 | 동작·제약 |
|---|---|---|---|
| `create_persona` | `name: str`, `*`, `persona_id: UUID \| None = None` | `Row` | 대화 상대 생성 |
| `get_persona` | `persona_id: UUID` | `Row` | `active` 상태만 조회 |
| `list_personas` | `*`, `limit: int = 50`, `offset: int = 0` | `list[Row]` |  |
| `create_session` | `persona_id: UUID`, `*`, `ai_notice_ack_at: datetime`, `title: str \| None = None`, `session_id: UUID \| None = None` | `Row` | 같은 사용자의 `active` 대화 상대에만 생성하며, 대화 상대가 없거나 남의 것이면 `NotFoundError`. `ai_notice_ack_at`은 시간대를 포함해야 하고 **현재 시각보다 미래이면 `ValidationError`**. `title` 최대 200자 |
| `get_session` | `session_id: UUID` | `Row` |  |
| `list_sessions` | `*`, `persona_id: UUID \| None = None`, `limit: int = 50`, `offset: int = 0` | `list[Row]` |  |
| `delete_session_rows` | `session_id: UUID` | `None` | DB의 대화방과 턴만 삭제. 파일·계정 삭제 아님. 대화방이 없거나 남의 것이면 `NotFoundError` |
| `create_chat_turn` | `session_id: UUID`, `turn_id: UUID`, `*`, `input_mode: "text" \| "voice"`, `question: str \| None = None`, `audio_sha256: str \| None = None` | `Row` | 텍스트는 `question` 필수(공백만 불가)이고 `audio_sha256`을 함께 주면 `ValidationError`. **음성은 업로드한 원본 바이트의 SHA-256(소문자 64자리 hex)이 필수**이고 `question`을 함께 주면 `ValidationError`. 대화방이 없거나 남의 것이면 `NotFoundError`. 같은 ID·같은 입력의 재전송은 기존 턴을 재사용하고, **같은 ID에 다른 입력은 `ConflictError`** |
| `start_chat_stage` | `turn_id: UUID`, `stage: "stt" \| "llm" \| "tts"`, `*`, `model_name: str \| None = None`, `with_audio: bool = False` | `Row` | 단계 실행권 확보. 해당 `*_attempt`(시도 번호)를 반환하며 **확보에 성공한 요청만 외부 호출**. 재시작은 새 시도 번호. **`stage`가 `llm`이면 `model_name`이 필수**(비어 있지 않은 문자열)이고 `stt`·`tts`에서 `model_name`을 넘기면 `ValidationError`. `with_audio`는 음성도 요청할 때 |
| `finish_chat_stage` | `turn_id: UUID`, `stage: "stt" \| "llm" \| "tts"`, `*`, `attempt: int`, `ok: bool`, `text: str \| None = None`, `elapsed_ms: int \| None = None`, `error_code: str \| None = None` | `Row` | 해당 시도의 `pending` 결과만 저장. 이전 시도의 늦은 결과는 `ConflictError`. `attempt`는 1 이상, `elapsed_ms`는 0 이상, **`ok`가 true인 `stt`·`llm` 단계는 `text`가 비어 있지 않아야 함**. TTS 실패는 완료된 답변 텍스트를 지우지 않음 |
| `expire_chat_stages` | `*`, `started_before: datetime` | `int` | 기준 시각보다 오래 실행 중인 단계를 실패 처리하고 처리한 단계 수를 반환 (예: `tts_timeout`) |
| `get_turn` | `turn_id: UUID` | `Row` |  |
| `list_turns` | `session_id: UUID`, `*`, `after_turn_no: int = 0`, `limit: int = 50` | `list[Row]` | `turn_no` 오름차순 커서 조회. 대화방이 없거나 남의 것이면 `NotFoundError` |

### 기록·전사문 함수 (이슈 #9 · #10)

| 함수 | 인자 (`*` 뒤는 이름을 지정해 호출) | 반환 | 동작·제약 |
|---|---|---|---|
| `reserve_record` | `persona_id: UUID`, `*`, `kind: "text" \| "audio"`, `original_name: str`, `mime_type: str`, `size_bytes: int`, `consent_at: datetime`, `consent_version: str`, `record_id: UUID \| None = None` | `Row` | `uploading` 상태로 예약하고 `user_id`·`record_id`·원본 경로 생성. **파일 자체는 저장하지 않음**. 원본 이름 최대 255자, 동의 버전 최대 100자 |
| `get_record` | `record_id: UUID` | `Row` | `deleting` 상태는 제외 |
| `list_records` | `*`, `persona_id: UUID \| None = None`, `limit: int = 50`, `offset: int = 0` | `list[Row]` |  |
| `update_record_status` | `record_id: UUID`, `*`, `expected_status: str`, `status: str`, `error_code: str \| None = None` | `Row` | 예상 상태와 일치할 때만 변경. 허용되지 않는 전이(아래 '기록 상태 전이')나 잘못된 상태 이름은 `ValidationError`, 현재 상태가 `expected_status`와 다르면 `ConflictError`. `deleting`으로의 변경은 삭제 요청 함수만 가능. `error_code`는 영문·숫자·`.`·`-`·`_` 1~64자 |
| `create_text_draft` | `record_id: UUID`, `*`, `revision: int`, `content: str`, `text_version_id: UUID \| None = None` | `Row` | 양의 `revision`의 초안 생성. 같은 번호가 있으면 `ConflictError` |
| `get_text_version` | `text_version_id: UUID` | `Row` |  |
| `list_text_versions` | `record_id: UUID` | `list[Row]` | `revision` 오름차순 |
| `edit_text_draft` | `text_version_id: UUID`, `*`, `content: str`, `expected_updated_at: datetime` | `Row` | `draft`이며 수정 시각이 예상과 일치할 때만 편집. `draft`가 아니거나 수정 시각이 다르면 `ConflictError`. 확정본은 덮어쓰지 않고 새 `revision`을 만들어야 함 |
| `confirm_record_text` | `text_version_id: UUID` | `Row` | 초안을 확정하고 이전 확정본을 `superseded`로 전환 |

### 말투 프로필·검색 함수 (이슈 #10, 검색은 전용 이슈 없음)

| 함수 | 인자 (`*` 뒤는 이름을 지정해 호출) | 반환 | 동작·제약 |
|---|---|---|---|
| `get_personalization_input` | `persona_id: UUID` | `dict` | `{source_revision: int, sources: list[Row]}`. 현재 확정·`ready` 자료만 포함 |
| `get_current_style_profile` | `persona_id: UUID` | `Row \| None` | `ready` 프로필만 |
| `publish_style_profile` | `persona_id: UUID`, `*`, `source_revision: int`, `content: dict`, `model_name: str`, `source_ids: Sequence[UUID]` | `Row` | 자료 수집 때 얻은 `source_revision`과 출처를 함께 검증. 입력이 바뀌었으면 거부하고 다시 생성. 자료 변경 시 이전 프로필은 `stale` |
| `save_record_chunks` | `text_version_id: UUID`, `*`, `embedding_model: str`, `chunks: Sequence[ChunkInput]` | `list[Row]` | 확정 자료의 조각을 일괄 INSERT. 1회 요청으로 원자적 저장. 미확정 자료는 거부 |
| `search_record_chunks` | `persona_id: UUID`, `*`, `embedding_model: str`, `query_embedding: Sequence[float]`, `limit: int = 10`, `min_similarity: float = 0.5` | `list[Row]` | 사용자·대화 상대·모델·차원을 먼저 제한한 뒤 유사도 검색. 반환 행: `chunk_id`, `record_id`, `text_version_id`, `content`, `similarity`. 이전 확정본·삭제 중 기록은 제외 |

#### `ChunkInput(chunk_no, content, embedding)`

| 필드 | 형식 | 규칙 |
|---|---|---|
| `chunk_no` | int | 0 이상. 한 배치 안에서 중복 불가 |
| `content` | str | 공백이 아닌 문자열 |
| `embedding` | Sequence[float] | 유한한 숫자로 된 비영 벡터. 차원이 섞이면 거부 |

### 삭제 함수 (이슈 #11)

| 함수 | 인자 (`*` 뒤는 이름을 지정해 호출) | 반환 | 동작·제약 |
|---|---|---|---|
| `request_record_deletion` | `record_id: UUID` | `Row` | 삭제 작업을 만들거나 재사용하고 기록을 즉시 `deleting`으로 전환. **실제 파일 삭제 완료가 아님** |
| `get_deletion_job` | `job_id: UUID` | `Row` |  |
| `list_deletion_jobs` | `*`, `limit: int = 50`, `offset: int = 0` | `list[Row]` |  |

SQL 함수 8개(`create_chat_turn`, `start_chat_stage`, `finish_chat_stage`, `expire_chat_stages`, `confirm_record_text`, `publish_style_profile`, `search_record_chunks`, `request_record_deletion`)는 위 표의 같은 이름의 Python 함수가 호출하고, 나머지 함수는 테이블을 직접 읽고 씁니다.

### 호출 규칙

#### 대화 단계 규칙

| 항목 | 규칙 |
|---|---|
| STT 시작 | 질문이 `failed`이거나, `pending`이면서 아직 시도한 적이 없을 때 |
| LLM 시작 | 질문이 `done`이고 답변이 시작 전이거나 `failed`일 때. 시작하면 `tts_status`와 TTS 시간을 비움 |
| TTS 시작 | 답변이 `done`이고 `tts_status`가 없거나 `failed`일 때 |
| 결과 저장 | 해당 단계가 `pending`이고 **현재 시도 번호와 같을 때만**. 아니면 늦은 결과로 거부 |
| 턴 중복 | 같은 `turn_id`·같은 입력은 재사용, 같은 `turn_id`에 다른 입력은 `ConflictError`, 다른 사용자의 대화방은 `NotFoundError` |

#### 텍스트 턴의 호출 순서

```
create_chat_turn(session_id, turn_id, input_mode="text", question=…)
→ start_chat_stage(turn_id, "llm", model_name=…)      # 실행권 확보에 성공한 경우에만 AI 호출
→ (AI 호출)
→ finish_chat_stage(turn_id, "llm", attempt=claimed["llm_attempt"], ok=True, text=…, elapsed_ms=…)
```

#### 음성 턴의 호출 순서

```
create_chat_turn(session_id, turn_id, input_mode="voice", audio_sha256=…)
→ start_chat_stage(turn_id, "stt") → (STT) → finish_chat_stage(turn_id, "stt", attempt, ok, text=전사문, elapsed_ms)
→ start_chat_stage(turn_id, "llm", model_name=…, with_audio=True) → (AI 호출) → finish_chat_stage(…, "llm", …)
→ start_chat_stage(turn_id, "tts") → (TTS) → finish_chat_stage(…, "tts", …)    # 실패해도 답변 텍스트 유지
```

#### 기록 상태 전이 (`update_record_status`)

| 현재 상태 | 바꿀 수 있는 상태 |
|---|---|
| `uploading` | `uploaded`, `failed` |
| `uploaded` | `processing`, `ready`, `failed` |
| `processing` | `ready`, `failed` |
| `failed` | `uploading`, `processing` |
| `ready` | 일반 상태 함수로 수정 불가 |
| `deleting` | 일반 상태 함수로 전환 불가 (삭제 요청 함수만) |

#### 입력 한도

| 대상 | 한도 |
|---|---|
| `title` | 최대 200자 |
| 원본 파일 이름 | 최대 255자 |
| 동의 버전 | 최대 100자 |
| `error_code` | 영문·숫자·`.`·`-`·`_`, 1~64자. 사용자 질문·답변·예외 원문을 넣지 않음 (예: `llm_timeout`, `tts_provider_error`) |
| 벡터 차원 | 1~16000 |
| `save_record_chunks` 한 번의 조각 수 | 1~100개 |
| 목록 `limit` | 기본 50, 최대 100 |
| `audio_sha256` | 소문자 hex 64자리 |

### 반환 행(Row)의 필드

DB 함수가 돌려주는 JSON dict의 키입니다. 앱에 보내는 필드는 앱↔백엔드 규격 문서의 '응답 객체'에 정리했고, 설명이 같은 칸은 그 문서를 참조하도록 적었습니다.

#### `personas`

| 필드 | 형식 | 설명 |
|---|---|---|
| id, user_id | uuid | 대화 상대 id, 소유자 |
| name | string | 이름 |
| status | string | 앱 응답 객체와 같음 (앱↔백엔드 문서의 '응답 객체') |
| data_revision | integer | 앱 응답 객체와 같음 (앱↔백엔드 문서의 '응답 객체') |
| created_at | datetime | 생성 시각 |
| updated_at | datetime | 수정 시각 |

#### `chat_sessions`

| 필드 | 형식 | 설명 |
|---|---|---|
| id, user_id | uuid | 대화방 id, 소유자 |
| persona_id | uuid | 대화 상대 id |
| title | string \| null | 제목 (최대 200자) |
| ai_notice_ack_at | datetime | 앱 응답 객체와 같음 (앱↔백엔드 문서의 '응답 객체') |
| created_at | datetime | 생성 시각 |
| updated_at | datetime | 수정 시각 |

#### `chat_turns`

| 필드 | 형식 | 설명 |
|---|---|---|
| id | uuid | 턴 id (앱이 만든 값) |
| session_id, user_id | uuid | 대화방·소유자 |
| turn_no | bigint | 정렬 번호 (자동 증가, 빈 번호 가능) |
| input_mode | string | `text` · `voice` |
| question | string \| null | 질문 또는 STT 전사문 |
| question_status | string | `pending` · `done` · `failed` |
| answer | string \| null | AI 답변 |
| answer_status | string \| null | `pending` · `done` · `failed` |
| tts_status | string \| null | `pending` · `ready` · `failed` |
| request_hash | string \| null | 입력(텍스트 질문 또는 음성 SHA-256)의 지문. 같은 ID에 다른 입력이 들어오는지 검사 |
| audio_requested | boolean | 음성 응답을 요청했는지 |
| stt_attempt, llm_attempt, tts_attempt | integer | 단계별 시도 번호 |
| stt_started_at, llm_started_at, tts_started_at | datetime \| null | 단계 시작 시각 |
| model_name | string \| null | 모델 이름 |
| stt_ms, llm_ms, tts_ms, total_ms | integer \| null | 소요 시간(ms). `stt_ms`는 음성 턴에서만 |
| error_message | string \| null | 실패 코드 |
| created_at, updated_at | datetime | 생성·수정 시각 |

#### `records`

| 필드 | 형식 | 설명 |
|---|---|---|
| id, user_id, persona_id | uuid | 기록·소유자·대화 상대 |
| kind | string | `text` · `audio` |
| original_name | string | 원본 파일 이름 |
| bucket_id | string | 항상 `replica-records` |
| object_path | string | `{user_id}/{record_id}/original` |
| mime_type | string | MIME 형식 |
| size_bytes | bigint \| null | 파일 크기 |
| source_description, target_speaker | string \| null | 자료 설명, 대상 화자 |
| status | string | 앱 응답 객체와 같음 (앱↔백엔드 문서의 '응답 객체') |
| consent_at, consent_version, consent_scopes | datetime · string · string[] | 동의 시각·버전·범위 (`ready`가 되려면 동의 시각·버전과 `personalization` 범위 필요) |
| error_code | string \| null | 실패 코드 |
| created_at, updated_at | datetime | 생성·수정 시각 |

#### `record_text_versions`

| 필드 | 형식 | 설명 |
|---|---|---|
| id, user_id, persona_id, record_id | uuid | 식별자 |
| revision | integer | 앱 응답 객체와 같음 (앱↔백엔드 문서의 '응답 객체') |
| content | string | 앱 응답 객체와 같음 (앱↔백엔드 문서의 '응답 객체') |
| status | string | 앱 응답 객체와 같음 (앱↔백엔드 문서의 '응답 객체') |
| confirmed_at | datetime \| null | 앱 응답 객체와 같음 (앱↔백엔드 문서의 '응답 객체') |
| created_at, updated_at | datetime | 생성·수정 시각 |

#### `style_profiles`

| 필드 | 형식 | 설명 |
|---|---|---|
| id, user_id, persona_id | uuid | 식별자 |
| version | integer | 프로필 버전 (1 이상) |
| source_revision | integer | 앱 응답 객체와 같음 (앱↔백엔드 문서의 '응답 객체') |
| content | object | 앱 응답 객체와 같음 (앱↔백엔드 문서의 '응답 객체') |
| model_name | string | 프로필을 만든 모델 이름 |
| status | string | 앱 응답 객체와 같음 (앱↔백엔드 문서의 '응답 객체') |
| created_at, updated_at | datetime | 생성·수정 시각 |

#### `record_chunks`

| 필드 | 형식 | 설명 |
|---|---|---|
| id, user_id, persona_id, text_version_id | uuid | 식별자 |
| chunk_no | integer | 조각 번호 (0 이상) |
| content | string | 조각 텍스트 |
| embedding | vector | 임베딩 벡터 |
| embedding_model, embedding_dimensions | string · integer | 임베딩 모델 이름과 차원 |
| created_at | datetime | 생성 시각 |

#### `deletion_jobs`

| 필드 | 형식 | 설명 |
|---|---|---|
| id | uuid | 삭제 작업 id |
| user_id | uuid \| null | 소유자 (계정이 삭제돼도 작업 내역은 유지되어 `null`이 될 수 있음) |
| target_type | string | 앱 응답 객체와 같음 (앱↔백엔드 문서의 '응답 객체') |
| target_id | uuid | 삭제 대상 id |
| status | string | 앱 응답 객체와 같음 (앱↔백엔드 문서의 '응답 객체') |
| remaining_steps | string[] | 앱 응답 객체와 같음 (앱↔백엔드 문서의 '응답 객체') |
| attempt_count | integer | 시도 횟수 |
| manifest | object | 삭제 대상 파일 경로 목록 (앱에는 보내지 않음) |
| last_error_code | string \| null | 마지막 실패 코드 |
| created_at, updated_at, finished_at | datetime | 앱 응답 객체와 같음 (앱↔백엔드 문서의 '응답 객체') |

#### `profile_sources`

| 필드 | 형식 | 설명 |
|---|---|---|
| profile_id, text_version_id | uuid | 프로필과 그 프로필이 사용한 확정 텍스트 버전의 연결 (복합 기본키) |
| user_id, persona_id | uuid | 소유자·대화 상대 |

<a id="s2"></a>

## 2. DB 오류에 대해 백엔드가 보내는 오류 메시지

DB 모듈이 던지는 예외를 앱용 오류로 변환합니다. 오류 본문 형식은 앱↔백엔드 규격 문서 2장과 같고, SQL 원문·개인 데이터·비밀 값은 담지 않습니다.

### DB 모듈의 예외 변환 규칙

DB 모듈이 어떤 조건에서 어떤 예외를 던지는지 코드 기준으로 정리한 표입니다. 아래 '앱에 보내는 오류'는 이 예외를 앱용 오류로 바꾼 것입니다.

| DB 예외 | 변환 조건 (모듈 코드 기준) |
|---|---|
| `ValidationError` | 모듈의 인자 검사 실패(코드 없음), DB 코드 `23514`, `23502`, `22`로 시작하는 모든 코드. 단 `create_chat_turn`의 `22023`은 `ConflictError`로 바뀜 |
| `AuthenticationError` | HTTP 401, 사용자 확인(`/auth/v1/user`) 실패, 토큰이 비었거나 공백 포함·비ASCII·16384자 초과·`sb_`로 시작 |
| `NotFoundError` | DB 코드 `P0002`, 단건 조회 결과가 비어 있음 |
| `ConflictError` | DB 코드 `55000`, `23505`, `23503`, `40001`, `40P01`, `create_chat_turn`의 `22023`, 수정·삭제 대상 행이 비어 있음(이미 상태가 바뀜), `edit_text_draft`에서 `draft`가 아니거나 수정 시각이 다름, `update_record_status`에서 현재 상태가 `expected_status`와 다름 |
| `PermissionDeniedError` | HTTP 403 또는 DB 코드 `42501` |
| `DatabaseUnavailableError` | HTTP 429·502·503·504·5xx, 연결 실패, 타임아웃(기본 10초). 쓰기에서는 `outcome_unknown`이 참이 되어 성공 여부가 불명확함을 알림 |
| `DatabaseError` | 그 밖의 응답. 모듈 자체 코드 `owner_mismatch`, `invalid_response` 포함 |

### 앱에 보내는 오류

| DB 예외 | DB 코드 | 발생 상황 (발생 함수) | 앱에 보내는 HTTP | 앱 `code` | 앱 `message` | `retryable` | 앱 동작 |
|---|---|---|---|---|---|---|---|
| `ValidationError` | 위 규칙 | **앱이 보낸 값**이 잘못됨: UUID·시간 형식, 비어 있는 문자열, 길이·개수 한도 초과, 미래 시각 등 (모든 함수) | 422 | `INVALID_INPUT` | `입력한 내용을 확인해 주세요.` (`field`에 문제 필드) | false | 입력 수정 후 다시 전송 |
| `ValidationError` | 위 규칙 | **백엔드가 만든 인자**가 잘못됨: 허용되지 않는 상태 전이, 잘못된 단계 이름 등 호출 오류 | 500 | `INTERNAL_ERROR` | `서버에 문제가 생겼어요. 잠시 후 다시 시도해 주세요.` | false | 원인은 서버 로그에만 기록 |
| `ConflictError` | `22023` (`create_chat_turn`만) | **같은 `turn_id`에 다른 입력**을 보냄 (요청 ID 재사용) | 409 | `TURN_ID_CONFLICT` | `같은 요청 번호로 다른 내용을 보낼 수 없어요. 새로 전송해 주세요.` | false | 새 `turn_id`를 만들어 다시 전송 |
| `AuthenticationError` | - | 토큰이 없거나 잘못됨·만료 (`for_user`) | 401 | `UNAUTHENTICATED` | `로그인이 필요하거나 만료되었어요. 다시 로그인해 주세요.` | false | 로그인 화면으로 이동 |
| `NotFoundError` | `P0002` | 대상이 없거나, 다른 사용자 소유이거나, 삭제 중이라 보이지 않음. `get_*`, `create_session`의 대화 상대, `create_chat_turn`·`list_turns`·`delete_session_rows`의 대화방, 삭제 중인 기록(`get_record`). **없음과 남의 것을 구분하지 않음** | 404 | `NOT_FOUND` | `찾을 수 없는 항목이에요. 목록을 새로고침해 주세요.` | false | 목록 새로고침 |
| `ConflictError` | `55000` | 이미 실행 중이거나 끝난 단계를 다시 시작 (`start_chat_stage`), 이전 시도의 늦은 결과 저장 (`finish_chat_stage`) | 409 | `CONFLICT` | `이미 처리 중이거나 상태가 바뀌었어요. 새로고침 후 다시 시도해 주세요.` | false | 해당 턴 상태 다시 조회 |
| `ConflictError` | 코드 없음 (수정 대상 행이 비어 있음) | 기록의 현재 상태가 `expected_status`와 다름 (`update_record_status`), 삭제하려던 대화방이 이미 사라짐 (`delete_session_rows`) | 409 | `CONFLICT` | `이미 처리 중이거나 상태가 바뀌었어요. 새로고침 후 다시 시도해 주세요.` | false | 기록·대화 상태 다시 조회 |
| `ConflictError` | 코드 없음 | 전사문 수정 시 `draft`가 아니거나 수정 시각이 다름 (`edit_text_draft`) | 409 | `EDIT_CONFLICT` | `다른 곳에서 먼저 수정되었거나 이미 확정된 내용이에요. 최신 내용을 불러와 확인해 주세요.` | false | 최신 내용을 불러오고 입력한 내용은 유지 |
| `ConflictError` | `23505` (`23503`) | 이미 있는 `revision` 또는 ID로 생성 (`create_text_draft`, 일반 생성 함수의 중복 INSERT) | 409 | `CONFLICT` | `이미 처리 중이거나 상태가 바뀌었어요. 새로고침 후 다시 시도해 주세요.` | false | 해당 ID로 먼저 조회 |
| `ConflictError` | `55000` | 입력이 바뀐 뒤 프로필 발행 (`publish_style_profile`의 `source_revision` 불일치) | - | - | 앱에 전달하지 않음 | - | 서버가 프로필을 다시 생성 |
| `PermissionDeniedError` | HTTP 403, `42501` | DB 권한 설정과 서버 설정 불일치 | 500 | `INTERNAL_ERROR` | `서버에 문제가 생겼어요. 잠시 후 다시 시도해 주세요.` | false | 원인은 서버 로그에만 기록, 경보 |
| `DatabaseUnavailableError` | HTTP 429·5xx 등 | 연결 실패, 타임아웃(기본 10초), 일시적 서비스 오류. **쓰기 성공 여부가 불명확할 수 있음** (`outcome_unknown`) | 503 | `DB_UNAVAILABLE` | `일시적으로 저장소에 연결할 수 없어요. 잠시 후 다시 시도해 주세요.` | true | 같은 ID(`turn_id` 등)로 재조회해 저장 여부를 확인한 뒤 판단 |
| `DatabaseError` | 그 밖 | 그 밖의 DB·응답 오류 (SQL 원문·개인 데이터·비밀 값 대신 안전한 메시지와 코드만 제공) | 500 | `INTERNAL_ERROR` | `서버에 문제가 생겼어요. 잠시 후 다시 시도해 주세요.` | false | 원인은 서버 로그에만 기록 |

`ValidationError`는 DB 문서에서 "400 또는 422 매핑 후보"로 적혀 있어, 앱 입력이 원인이면 422, 백엔드가 만든 인자가 원인이면 500으로 정리했습니다. `NotFoundError`는 404, `ConflictError`는 409로 문서에 명시되어 있습니다.

같은 `turn_id`에 다른 입력은 DB가 `22023`으로 알리지만 모듈이 `ConflictError`로 바꿔 던집니다. 같은 `ConflictError`라도 코드가 `22023`이면 앱에는 `TURN_ID_CONFLICT`로 구분해 보냅니다 (제안).

`INVALID_INPUT`, `INTERNAL_ERROR`, `UNAUTHENTICATED`는 앱↔백엔드 규격 문서 2장의 정의와 같고, 나머지 `code`는 이 장에서 정의합니다. AI 오류를 앱에 전달할 때 쓰는 `CONFLICT`도 이 장의 정의를 따릅니다.

삭제 요청의 중복(같은 기록에 다시 요청)은 오류가 아니라 **같은 삭제 작업을 돌려줍니다.**

`finish_chat_stage`의 `error_code`로 DB에 기록하는 AI·STT·TTS 실패 코드는 앱↔백엔드 규격 문서 3장의 'DB 기록 `error_code`' 열과 같은 규칙(영문 소문자·밑줄)을 따릅니다 (예: `llm_timeout`, `tts_provider_error`).
