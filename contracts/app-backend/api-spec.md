# 레플리카 API 명세서: 앱 ↔ 백엔드

기준일 2026-10-13 · 팀이 올린 이슈·PR·브랜치를 바탕으로 정리한 초안이며 합의된 규격이 아닙니다.

**[구현]** 코드가 있고 동작함   **[제안]** 이슈를 근거로 이 문서가 제안한 규격   **[미정]** 아직 정해지지 않음   (표에서 `필수`는 필수 여부)

- [1. 백엔드가 앱에 제공하는 API](#s1)
- [2. 앱 관련 오류 메시지](#s2)
- [3. AI 서버 오류 메시지(앱 전달)](#s3)

관련 문서: [백엔드 ↔ DB 규격](../backend-db/api-spec.md) (이 문서에서 "DB 규격 문서"로 부름) · 백엔드 ↔ AI 규격의 원본은 `contracts/baseline` (PR #29)

<a id="s1"></a>

## 1. 백엔드가 앱(프론트엔드)에 제공하는 API

### 공통 사항

| 항목 | 내용 |
|---|---|
| 기본 주소 | 앱 빌드 설정 `BACKEND_BASE_URL`. 개발: 에뮬레이터 `http://10.0.2.2:8000`, 로컬 `http://localhost:8000`. staging·prod는 HTTPS 필수 |
| 요청 형식 | JSON은 `Content-Type: application/json`, 파일은 `multipart/form-data` |
| 인증 | `Authorization: Bearer <Supabase JWT>`. 현재 구현된 5개 API는 인증이 없고, 제안 API는 `/health`를 제외하고 모두 필요. 로그인·회원가입은 앱이 Supabase Auth로 직접 하므로 백엔드에 로그인 API는 없음 |
| 식별자·시간 | id는 소문자 UUID 문자열. 시간은 ISO 8601 (시간대 포함) |
| 목록 응답 | `{items, limit, offset}`. `limit` 기본 50 · 최대 100, `offset` 0 이상. (턴 목록은 `after_turn_no` 사용) |
| AI 표시 | AI 답변이 담긴 응답에는 `is_ai_generated: true`를 항상 포함 (FR-08) |
| 앱에 보내지 않는 필드 | `user_id`, `request_hash`, `*_attempt`, `*_started_at`, `bucket_id`, `object_path`, `manifest` |
| 오류 응답 | 2장 참고 |

### API 목록

| 메서드 | 경로 | 용도 | 상태 | 단계·이슈 |
|---|---|---|---|---|
| GET | [`/health`](#a-health) | 서버 상태 확인 | 구현 | baseline · #21 |
| POST | [`/chat/text`](#a-chat-text) | 텍스트 대화 | 구현 | baseline · #6 |
| POST | [`/chat/voice`](#a-chat-voice) | 음성 대화 | 구현 | baseline · #7 |
| POST | [`/stt`](#a-stt) | 음성 → 텍스트 | 구현 | baseline · #7 |
| POST | [`/tts`](#a-tts) | 텍스트 → 음성 | 구현 | baseline · #7 |
| POST | [`/chat/turns/{turn_id}/tts`](#a-turn-tts) | 완료된 답변을 음성으로 합성·재시도 | 제안 | baseline · #7, UC-02 ("원하면 음성 재생") |
| POST | [`/personas`](#a-persona-create) | 대화 상대 생성 | 제안 | baseline~고도화 1 · 전용 이슈 없음 (DB 문서의 대화 상대 함수) |
| GET | [`/personas · /personas/{persona_id}`](#a-persona-get) | 대화 상대 목록·상세 | 제안 | baseline~고도화 1 · 전용 이슈 없음 (DB 문서의 대화 상대 함수) |
| POST | [`/sessions`](#a-session-create) | 대화방 생성 | 제안 | baseline · #5, #6 |
| GET | [`/sessions · /sessions/{session_id}`](#a-session-get) | 대화 이력 목록·상세 | 제안 | 고도화 2 · #12 |
| GET | [`/sessions/{session_id}/turns`](#a-turns) | 대화방의 턴 목록 | 제안 | 고도화 2 · #12 |
| DELETE | [`/sessions/{session_id}`](#a-session-del) | 대화 삭제 | 제안 | 고도화 2 · #12 |
| POST | [`/records`](#a-record-create) | 기록 업로드 예약 | 제안 | 고도화 1 · #9 |
| POST | [`/records/{record_id}/uploaded`](#a-record-uploaded) | 업로드 완료 알림 | 제안 | 고도화 1 · #9 |
| GET | [`/records · /records/{record_id}`](#a-record-get) | 기록 목록·상세 | 제안 | 고도화 1 · #9, #10 |
| GET | [`/records/{record_id}/text-versions`](#a-textver-list) | 전사문 초안·확정본 조회 | 제안 | 고도화 1 · #10 |
| POST | [`/records/{record_id}/text-versions`](#a-textver-create) | 새 텍스트 초안 만들기 | 제안 | 고도화 1 · #10 |
| PATCH | [`/text-versions/{text_version_id}`](#a-textver-edit) | 전사문 수정 | 제안 | 고도화 1 · #10 |
| POST | [`/text-versions/{text_version_id}/confirm`](#a-textver-confirm) | 전사문 확정 | 제안 | 고도화 1·2 · #10 |
| GET | [`/personas/{persona_id}/style-profile`](#a-profile) | 현재 말투 프로필 조회 | 제안 | 고도화 1 · #10 |
| DELETE | [`/records/{record_id}`](#a-record-del) | 기록 삭제 요청 | 제안 | 고도화 2 · #11 |
| GET | [`/deletion-jobs · /deletion-jobs/{job_id}`](#a-jobs) | 삭제 진행·부분 실패 확인 | 제안 | 고도화 2 · #11 |
| DELETE | [`/account`](#a-account-del) | 계정 전체 삭제 | 미정 | 고도화 2 · #11 |

기록 파일을 올리는 방법은 규격이 정해지지 않아 API 목록에서 제외했습니다. 아래 '정해지지 않은 항목'을 참고하세요.

### 응답 객체

여러 API의 응답에 공통으로 쓰이는 객체입니다. API 상세의 각 카드는 객체 이름만 참조합니다. 앱에 보내지 않는 서버 내부 필드는 위 '공통 사항'에 적은 대로 표에 넣지 않았습니다.

#### 대화 상대 객체

| 필드 | 형식 | 설명 |
|---|---|---|
| id | uuid | 대화 상대 id |
| name | string | 이름 |
| status | string | `active` 또는 `deleting` |
| data_revision | integer | 자료 개정 번호. 확정 자료가 바뀔 때 증가, 말투 프로필의 최신 여부 판단에 사용 |
| created_at | datetime | 생성 시각 |
| updated_at | datetime | 수정 시각 |

#### 대화방 객체

| 필드 | 형식 | 설명 |
|---|---|---|
| id | uuid | 대화방 id |
| persona_id | uuid | 대화 상대 id |
| title | string \| null | 제목 (최대 200자) |
| ai_notice_ack_at | datetime | AI 생성 안내를 확인한 시각 (FR-08) |
| created_at | datetime | 생성 시각 |
| updated_at | datetime | 수정 시각 |

#### 턴 객체

| 필드 | 형식 | 설명 |
|---|---|---|
| turn_id | uuid | 대화 턴 id (DB `chat_turns.id`, 앱이 만든 값) |
| turn_no | integer | DB가 매기는 정렬 번호. 중간에 빈 번호가 생길 수 있음 |
| input_mode | string | `text` 또는 `voice` |
| question | string \| null | 질문 텍스트. 음성이면 STT 전사문 |
| question_status | string | `pending` · `done` · `failed` (입력·STT 단계) |
| answer | string \| null | AI 답변 |
| answer_status | string \| null | `pending` · `done` · `failed`, 시작 전이면 `null` (LLM 단계) |
| tts_status | string \| null | `pending` · `ready` · `failed`, 요청 전이면 `null` (TTS 단계) |
| is_ai_generated | boolean | 백엔드가 추가. 답변이 있으면 항상 `true`. 다시 연 대화에서도 AI 표시 유지 (#12) |
| model_name | string \| null | 답변을 만든 모델 이름 |
| stt_ms, llm_ms, tts_ms, total_ms | integer \| null | 단계별·전체 소요 시간(ms) |
| error_message | string \| null | 실패 시 안전한 오류 코드 (예: `llm_timeout`). 질문·답변·예외 원문은 저장하지 않음 |
| created_at | datetime | 생성 시각 |

#### 기록 객체

| 필드 | 형식 | 설명 |
|---|---|---|
| id | uuid | 기록 id |
| persona_id | uuid | 대화 상대 id |
| kind | string | `text` 또는 `audio` |
| original_name | string | 원본 파일 이름 (최대 255자) |
| mime_type | string | 파일 MIME 형식 |
| size_bytes | integer \| null | 파일 크기(바이트) |
| status | string | `uploading` · `uploaded` · `processing` · `ready` · `failed` · `deleting` |
| consent_at | datetime \| null | 자료 사용 동의 시각 |
| consent_version | string \| null | 동의 문구 버전 (최대 100자) |
| consent_scopes | string[] | 동의 범위. `personalization`, `training` 중 선택. 기본 `["personalization"]` |
| error_code | string \| null | 실패 시 오류 코드 (영문·숫자·`.`·`-`·`_`, 1~64자) |
| created_at, updated_at | datetime | 생성·수정 시각 |

#### 텍스트 버전 객체

| 필드 | 형식 | 설명 |
|---|---|---|
| id | uuid | 텍스트 버전 id |
| record_id | uuid | 기록 id |
| revision | integer | 버전 번호 (1 이상). 기록 안에서 중복 불가 |
| content | string | 전사문·텍스트 (비어 있지 않음) |
| status | string | `draft` · `confirmed` · `superseded` |
| confirmed_at | datetime \| null | 확정 시각 (`confirmed`일 때 필수) |
| created_at, updated_at | datetime | 생성·수정 시각 |

#### 말투 프로필 객체

| 필드 | 형식 | 설명 |
|---|---|---|
| id | uuid | 프로필 id |
| persona_id | uuid | 대화 상대 id |
| version | integer | 프로필 버전 (1 이상) |
| source_revision | integer | 만들 때 사용한 자료 개정 번호 |
| content | object | 말투 프로필 내용 (비어 있지 않은 JSON 객체) |
| model_name | string | 프로필을 만든 모델 이름 |
| status | string | `ready` 또는 `stale` (자료가 바뀐 뒤에는 `stale`) |
| created_at, updated_at | datetime | 생성·수정 시각 |

#### 삭제 작업 객체

| 필드 | 형식 | 설명 |
|---|---|---|
| id | uuid | 삭제 작업 id |
| target_type | string | `record` · `session` · `persona` · `account` |
| target_id | uuid | 삭제 대상 id |
| status | string | `pending` · `running` · `failed` · `completed` |
| remaining_steps | string[] | 남은 단계. `storage` · `database` · `auth` · `external_index` · `adapter` 중 일부. 완료되면 빈 배열 |
| attempt_count | integer | 시도 횟수 |
| last_error_code | string \| null | 마지막 실패 코드 |
| created_at, updated_at, finished_at | datetime | 생성·수정·완료 시각 (`finished_at`은 완료 시에만) |

### API 상세

<a id="a-health"></a>

#### `GET /health` (구현) — 서버 상태 확인

| 항목 | 내용 |
|---|---|
| 인증 | 없음 |
| 단계·이슈 | baseline · #21 |

##### 응답 (200)

| 필드 | 형식 | 설명 |
|---|---|---|
| status | string | 항상 `ok` |
| app | string | 앱 이름 (`Replica API`) |
| version | string | 서버 버전 (`0.1.0`) |
| env | string | `local` · `dev` · `prod` |
| llm_mode | string | `mock` · `remote` |
| stt_provider | string | `mock` 또는 STT 공급자 이름 |
| tts_provider | string | `mock` 또는 TTS 공급자 이름 |
| server_time | datetime | 서버 시각 (UTC) |

<a id="a-chat-text"></a>

#### `POST /chat/text` (구현) — 텍스트 대화 (현재 Mock) · DB 연동 후 규격은 아래 '제안'

| 항목 | 내용 |
|---|---|
| 인증 | 현재 없음 / 제안: 필수 |
| 단계·이슈 | baseline · #6 |
| 내부 호출 | 제안: DB `create_chat_turn`·`start_chat_stage`·`finish_chat_stage`·`list_turns`, AI `POST /v1/baseline/generate` (호출 순서는 DB 규격 문서의 '텍스트 턴의 호출 순서') |

같은 `turn_id`로 다시 보내면(제안) 이미 완료된 턴은 저장된 답변을 돌려주고 새로 AI를 호출하지 않으며, 실패한 단계는 새 시도로 다시 실행합니다.

##### 요청 · 현재 구현 (JSON)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| message | body | string | 필수 | 질문. 1~2000자 (앱도 같은 한도) |
| persona_id | body | string | 선택 | 대화 상대 id. 현재 Mock은 사용하지 않음 |
| history | body | array | 선택 | 이전 대화 `[{role: "user" \| "assistant", content: string}]`. 기본 `[]`. 현재는 앱이 보냄 |

##### 응답 · 현재 구현 (200)

| 필드 | 형식 | 설명 |
|---|---|---|
| answer | string | AI 답변 (현재는 `(Mock 응답) 받은 메시지: …`) |
| is_ai_generated | boolean | 항상 `true` (FR-08) |
| model_name | string | 사용 모델 이름 (현재 `mock-llm`) |
| timings | object | `{stt_ms, llm_ms, tts_ms, total_ms}` 구간별 소요 시간(ms). 거치지 않은 구간은 `null`, `total_ms`는 항상 있음 |

##### 요청 · 제안 (DB 연동 후, JSON)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| session_id | body | uuid | 필수 | 대화방 id |
| turn_id | body | uuid | 필수 | 앱이 **전송마다** 만드는 UUID. 같은 질문을 재전송할 때는 같은 값을 유지. 같은 `turn_id`에 다른 내용을 보내면 거부 |
| message | body | string | 필수 | 질문. 1~2000자, 공백만 있는 값 불가 |
| persona_id, history | body | - | 삭제 | 이력은 백엔드가 DB에서 조회하므로 앱이 보내지 않음 |

##### 응답 · 제안 (200)

| 필드 | 형식 | 설명 |
|---|---|---|
| turn_id | uuid | 요청한 턴 id |
| answer | string | AI 답변 |
| is_ai_generated | boolean | 항상 `true` |
| finish_reason | string | `stop`(정상 종료) 또는 `length`(길이 한도로 잘림). 둘 다 성공 |
| model_name | string | 답변을 만든 모델 이름 |
| timings | object | `{stt_ms, llm_ms, tts_ms, total_ms}` 구간별 소요 시간(ms). 거치지 않은 구간은 `null`, `total_ms`는 항상 있음 |

##### 요청 예 (제안)

```json
{
  "session_id": "…uuid…",
  "turn_id": "…uuid…",
  "message": "엄마, 오늘 뭐 했어?"
}
```

##### 응답 예 (제안)

```json
{
  "turn_id": "…uuid…",
  "answer": "…",
  "is_ai_generated": true,
  "finish_reason": "stop",
  "model_name": "…",
  "timings": {
    "stt_ms": null,
    "llm_ms": 1270,
    "tts_ms": null,
    "total_ms": 1310
  }
}
```

##### 발생 가능한 오류

2장(앱 오류), 3장(AI가 원인인 오류), DB 규격 문서(DB가 원인인 오류)

<a id="a-chat-voice"></a>

#### `POST /chat/voice` (구현) — 음성 대화 (현재 Mock) · DB 연동 후 규격은 아래 '제안'

| 항목 | 내용 |
|---|---|
| 인증 | 현재 없음 / 제안: 필수 |
| 단계·이슈 | baseline · #7 |
| 내부 호출 | 제안: 음성 바이트의 SHA-256 계산, DB `create_chat_turn`·`start_chat_stage`·`finish_chat_stage`, STT·AI·TTS 호출 (호출 순서는 DB 규격 문서의 '음성 턴의 호출 순서') |

STT 또는 LLM 단계가 실패하면 200이 아니라 오류로 응답하고, 오류 본문의 `stage`로 실패한 단계를 알립니다 (2장).

##### 요청 · 현재 구현 (multipart/form-data)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| file | form | file | 필수 | 녹음 파일. 확장자 `.wav .mp3 .m4a .webm .ogg .mp4 .aac`, 최대 25MB, 비어 있지 않음 |
| persona_id | form | string | 선택 | 대화 상대 id. 현재 Mock은 사용하지 않음 |

##### 응답 · 현재 구현 (200)

| 필드 | 형식 | 설명 |
|---|---|---|
| transcript | string | STT로 바꾼 질문 텍스트 |
| answer | string | AI 답변 |
| is_ai_generated | boolean | 항상 `true` |
| model_name | string | 사용 모델 이름 |
| audio_base64 | string | 응답 음성(TTS)을 base64로 인코딩한 값 |
| audio_format | string | `audio_base64`의 형식 (현재 `wav`) |
| timings | object | `{stt_ms, llm_ms, tts_ms, total_ms}` 구간별 소요 시간(ms). 거치지 않은 구간은 `null`, `total_ms`는 항상 있음 |

##### 요청 · 제안 (multipart/form-data)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| file | form | file | 필수 | 녹음 파일 (형식·크기 제한은 위와 동일). 음성 형식은 STT·TTS 담당과 확정 필요 |
| session_id | form | uuid | 필수 | 대화방 id |
| turn_id | form | uuid | 필수 | 앱이 전송마다 만드는 UUID. 재전송 시 유지 |

##### 응답 · 제안 (200)

| 필드 | 형식 | 설명 |
|---|---|---|
| turn_id | uuid | 요청한 턴 id |
| transcript | string | 질문 텍스트 (STT 결과) |
| answer | string | AI 답변 |
| is_ai_generated | boolean | 항상 `true` |
| finish_reason | string | `stop` 또는 `length` |
| model_name | string | 모델 이름 |
| tts_status | string | `ready` 또는 `failed`. **TTS가 실패해도 답변 텍스트는 유지**하고 HTTP 200으로 응답 |
| audio_base64 | string \| null | 응답 음성 base64. `tts_status`가 `failed`면 `null` |
| audio_format | string \| null | 음성 형식. `failed`면 `null` |
| tts_error | object \| null | `{code, message}`. TTS 실패 시에만 |
| timings | object | `{stt_ms, llm_ms, tts_ms, total_ms}` 구간별 소요 시간(ms). 거치지 않은 구간은 `null`, `total_ms`는 항상 있음 |

##### 발생 가능한 오류

2장, 3장, DB 규격 문서

<a id="a-stt"></a>

#### `POST /stt` (구현) — 음성 → 텍스트 (STT 단독 확인, 개발 점검용)

| 항목 | 내용 |
|---|---|
| 인증 | 현재 없음 |
| 단계·이슈 | baseline · #7 |

##### 요청 (multipart/form-data)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| file | form | file | 필수 | 녹음 파일. 확장자 `.wav .mp3 .m4a .webm .ogg .mp4 .aac`, 최대 25MB, 비어 있지 않음 |

##### 응답 (200)

| 필드 | 형식 | 설명 |
|---|---|---|
| text | string | 전사 텍스트 (현재는 고정 문장) |
| timings | object | `{stt_ms, llm_ms, tts_ms, total_ms}` 구간별 소요 시간(ms). 거치지 않은 구간은 `null`, `total_ms`는 항상 있음 |

<a id="a-tts"></a>

#### `POST /tts` (구현) — 텍스트 → 음성 (TTS 단독 확인, 개발 점검용)

| 항목 | 내용 |
|---|---|
| 인증 | 현재 없음 |
| 단계·이슈 | baseline · #7 |

##### 요청 (JSON)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| text | body | string | 필수 | 합성할 문장. 1~2000자 |

##### 응답 (200)

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문 전체) | binary | 음성 파일. `Content-Type: audio/wav`(현재) 또는 `audio/mpeg`. 현재 Mock은 0.6초 길이의 440Hz 신호음(wav) |

<a id="a-turn-tts"></a>

#### `POST /chat/turns/{turn_id}/tts` (제안) — 완료된 답변을 음성으로 합성·재시도

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | baseline · #7, UC-02 ("원하면 음성 재생") |
| 내부 호출 | DB `start_chat_stage(turn_id, 'tts')` → TTS → `finish_chat_stage('tts')` |

이미 합성 중이거나 완료된 턴에 다시 요청하면 409를 돌려줍니다. TTS가 실패해도 답변 텍스트는 그대로 남습니다.

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| turn_id | path | uuid | 필수 | 대상 턴. 답변이 완료(`answer_status = done`)되어 있어야 하며 `tts_status`가 없거나 `failed`일 때만 시작 |

##### 응답 (200)

| 필드 | 형식 | 설명 |
|---|---|---|
| turn_id | uuid | 턴 id |
| tts_status | string | `ready` 또는 `failed` |
| audio_base64 | string \| null | 응답 음성 base64 |
| audio_format | string \| null | 음성 형식 |
| timings | object | `{tts_ms, total_ms}` |

##### 발생 가능한 오류

2장, DB 규격 문서

<a id="a-persona-create"></a>

#### `POST /personas` (제안) — 대화 상대 생성

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | baseline~고도화 1 · 전용 이슈 없음 (DB 문서의 대화 상대 함수) |
| 내부 호출 | DB `create_persona(name, persona_id?)` |

##### 요청 (JSON)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| name | body | string | 필수 | 이름. 비어 있지 않음 |
| persona_id | body | uuid | 선택 | 앱이 미리 만든 id. 쓰기 결과가 불확실할 때 같은 id로 재조회하려는 용도 |

##### 응답 (201) · 대화 상대

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 대화 상대 객체 | 이 장의 '응답 객체' 절에 정의한 대화 상대 객체 |

<a id="a-persona-get"></a>

#### `GET /personas · /personas/{persona_id}` (제안) — 대화 상대 목록·상세

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | baseline~고도화 1 · 전용 이슈 없음 (DB 문서의 대화 상대 함수) |
| 내부 호출 | DB `list_personas(limit, offset)` · `get_persona(persona_id)` (`active` 상태만 조회) |

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| persona_id | path | uuid | 상세만 필수 | 대화 상대 id |
| limit | query | integer | 선택 | 기본 50, 최대 100 |
| offset | query | integer | 선택 | 0 이상, 기본 0 |

##### 응답 (200) · 목록

| 필드 | 형식 | 설명 |
|---|---|---|
| items | array | 조회 결과 목록. 정렬은 생성 시각 내림차순, 같으면 UUID 내림차순 |
| limit | integer | 적용된 limit |
| offset | integer | 적용된 offset |

##### 응답 (200) · 상세와 목록의 `items` 항목

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 대화 상대 객체 | 이 장의 '응답 객체' 절에 정의한 대화 상대 객체 |

<a id="a-session-create"></a>

#### `POST /sessions` (제안) — 대화방 생성

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | baseline · #5, #6 |
| 내부 호출 | DB `create_session(persona_id, ai_notice_ack_at, title?, session_id?)` |

##### 요청 (JSON)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| persona_id | body | uuid | 필수 | 같은 사용자의 `active` 대화 상대 |
| ai_notice_ack_at | body | datetime | 필수 | 앱에서 AI 생성 안내를 확인한 시각. 시간대를 포함해야 하며 **현재 시각보다 미래이면 422** |
| title | body | string | 선택 | 제목. 최대 200자 |
| session_id | body | uuid | 선택 | 앱이 미리 만든 id |

##### 응답 (201) · 대화방

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 대화방 객체 | 이 장의 '응답 객체' 절에 정의한 대화방 객체 |

<a id="a-session-get"></a>

#### `GET /sessions · /sessions/{session_id}` (제안) — 대화 이력 목록·상세

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 2 · #12 |
| 내부 호출 | DB `list_sessions(persona_id?, limit, offset)` · `get_session(session_id)` |

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| session_id | path | uuid | 상세만 필수 | 대화방 id |
| persona_id | query | uuid | 선택 | 목록을 특정 대화 상대로 제한 |
| limit | query | integer | 선택 | 기본 50, 최대 100 |
| offset | query | integer | 선택 | 0 이상, 기본 0 |

##### 응답 (200) · 목록

| 필드 | 형식 | 설명 |
|---|---|---|
| items | array | 조회 결과 목록. 정렬은 생성 시각 내림차순, 같으면 UUID 내림차순 |
| limit | integer | 적용된 limit |
| offset | integer | 적용된 offset |

##### 응답 (200) · 상세와 목록의 `items` 항목

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 대화방 객체 | 이 장의 '응답 객체' 절에 정의한 대화방 객체 |

<a id="a-turns"></a>

#### `GET /sessions/{session_id}/turns` (제안) — 대화방의 턴 목록 (이어 보기)

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 2 · #12 |
| 내부 호출 | DB `list_turns(session_id, after_turn_no, limit)` |

앱은 한 턴을 질문·응답 말풍선 두 개로 그립니다. 서버 내부 필드(`user_id`, `request_hash`, `*_attempt`, `*_started_at`)는 앱에 보내지 않습니다.

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| session_id | path | uuid | 필수 | 대화방 id |
| after_turn_no | query | integer | 선택 | 이 번호보다 큰 턴만 조회. 0 이상, 기본 0 |
| limit | query | integer | 선택 | 기본 50, 최대 100 |

##### 응답 (200)

| 필드 | 형식 | 설명 |
|---|---|---|
| items | array | 턴 목록. `turn_no` 오름차순 |
| after_turn_no | integer | 적용된 값 |
| limit | integer | 적용된 값 |

##### `items` 항목 (턴)

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 턴 객체 | 이 장의 '응답 객체' 절에 정의한 턴 객체 |

<a id="a-session-del"></a>

#### `DELETE /sessions/{session_id}` (제안) — 대화 삭제 (DB 행만)

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 2 · #12 |
| 내부 호출 | DB `delete_session_rows(session_id)` |

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| session_id | path | uuid | 필수 | 대화방 id |

##### 응답

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문 없음) | - | 204 No Content. 대화방과 그 턴의 DB 행만 삭제하며 파일·계정 삭제가 아님 |

<a id="a-record-create"></a>

#### `POST /records` (제안) — 기록 업로드 예약

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 1 · #9 |
| 내부 호출 | DB `reserve_record(...)` → 상태 `uploading` 행과 원본 저장 경로 생성. 파일 업로드는 별도 |

파일을 올릴 위치·방법을 알려주는 응답 필드는 아직 정해지지 않았습니다 (아래 '정해지지 않은 항목'의 '기록 파일 업로드').

##### 요청 (JSON)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| persona_id | body | uuid | 필수 | 대화 상대 id |
| kind | body | string | 필수 | `text` 또는 `audio` |
| original_name | body | string | 필수 | 원본 파일 이름. 비어 있지 않음, 최대 255자 |
| mime_type | body | string | 필수 | 파일 MIME 형식 |
| size_bytes | body | integer | 필수 | 파일 크기(바이트). 0 이상 |
| consent_at | body | datetime | 필수 | 자료 사용 동의 시각 (시간대 포함) |
| consent_version | body | string | 필수 | 동의 문구 버전. 최대 100자 |
| record_id | body | uuid | 선택 | 앱이 미리 만든 id |

##### 응답 (201) · 기록

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 기록 객체 | 이 장의 '응답 객체' 절에 정의한 기록 객체 |

<a id="a-record-uploaded"></a>

#### `POST /records/{record_id}/uploaded` (제안) — 업로드 완료 알림

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 1 · #9 |
| 내부 호출 | DB `update_record_status(record_id, expected_status='uploading', status='uploaded')` |

이후 `processing`, `ready`, `failed`로의 상태 변경은 서버가 처리하고 앱은 기록 조회로 확인합니다.

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| record_id | path | uuid | 필수 | 기록 id. 현재 상태가 `uploading`이어야 함 |

##### 응답 (200) · 기록

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 기록 객체 | 이 장의 '응답 객체' 절에 정의한 기록 객체 |

<a id="a-record-get"></a>

#### `GET /records · /records/{record_id}` (제안) — 기록 목록·상세 (처리 상태 확인)

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 1 · #9, #10 |
| 내부 호출 | DB `list_records(persona_id?, limit, offset)` · `get_record(record_id)` (`deleting` 상태 제외) |

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| record_id | path | uuid | 상세만 필수 | 기록 id |
| persona_id | query | uuid | 선택 | 특정 대화 상대의 기록만 |
| limit | query | integer | 선택 | 기본 50, 최대 100 |
| offset | query | integer | 선택 | 0 이상, 기본 0 |

##### 응답 (200) · 목록

| 필드 | 형식 | 설명 |
|---|---|---|
| items | array | 조회 결과 목록. 정렬은 생성 시각 내림차순, 같으면 UUID 내림차순 |
| limit | integer | 적용된 limit |
| offset | integer | 적용된 offset |

##### 응답 (200) · 상세와 목록의 `items` 항목

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 기록 객체 | 이 장의 '응답 객체' 절에 정의한 기록 객체 |

<a id="a-textver-list"></a>

#### `GET /records/{record_id}/text-versions` (제안) — 전사문 초안·확정본 조회

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 1 · #10 |
| 내부 호출 | DB `list_text_versions(record_id)` (revision 오름차순) · `get_text_version(text_version_id)` |

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| record_id | path | uuid | 필수 | 기록 id |

##### 응답 (200)

| 필드 | 형식 | 설명 |
|---|---|---|
| items | array | 텍스트 버전 목록. `revision` 오름차순 |

##### `items` 항목

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 텍스트 버전 객체 | 이 장의 '응답 객체' 절에 정의한 텍스트 버전 객체 |

<a id="a-textver-create"></a>

#### `POST /records/{record_id}/text-versions` (제안) — 새 텍스트 초안 만들기

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 1 · #10 |
| 내부 호출 | DB `create_text_draft(record_id, revision, content, text_version_id?)` |

확정본은 직접 고치지 않고 새 `revision`의 초안을 만들어 수정합니다.

##### 요청 (JSON)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| record_id | path | uuid | 필수 | 기록 id |
| revision | body | integer | 필수 | 1 이상. 이미 있는 번호이면 충돌(409) |
| content | body | string | 필수 | 텍스트. 비어 있지 않음 |
| text_version_id | body | uuid | 선택 | 앱이 미리 만든 id |

##### 응답 (201) · 텍스트 버전

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 텍스트 버전 객체 | 이 장의 '응답 객체' 절에 정의한 텍스트 버전 객체 |

<a id="a-textver-edit"></a>

#### `PATCH /text-versions/{text_version_id}` (제안) — 전사문 수정

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 1 · #10 |
| 내부 호출 | DB `edit_text_draft(text_version_id, content, expected_updated_at)` |

##### 요청 (JSON)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| text_version_id | path | uuid | 필수 | 수정할 텍스트 버전. `draft` 상태만 가능 |
| content | body | string | 필수 | 수정한 텍스트. 비어 있지 않음 |
| expected_updated_at | body | datetime | 필수 | 조회했을 때 받은 `updated_at`. 서버의 값과 다르면 충돌(409) |

##### 응답 (200) · 텍스트 버전

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 텍스트 버전 객체 | 이 장의 '응답 객체' 절에 정의한 텍스트 버전 객체 |

<a id="a-textver-confirm"></a>

#### `POST /text-versions/{text_version_id}/confirm` (제안) — 전사문 확정

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 1·2 · #10 |
| 내부 호출 | DB `confirm_record_text(text_version_id)` → (서버 후속) `save_record_chunks`, `publish_style_profile` |

확정되면 이전 확정본은 `superseded`가 되고, 확정된 텍스트만 개인화에 사용됩니다. 프로필 생성 진행 상태는 말투 프로필 조회로 확인합니다.

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| text_version_id | path | uuid | 필수 | 확정할 초안 |

##### 응답 (200) · 텍스트 버전

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 텍스트 버전 객체 | 이 장의 '응답 객체' 절에 정의한 텍스트 버전 객체 |

<a id="a-profile"></a>

#### `GET /personas/{persona_id}/style-profile` (제안) — 현재 말투 프로필 조회

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 1 · #10 |
| 내부 호출 | DB `get_current_style_profile(persona_id)` (`ready` 프로필만) |

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| persona_id | path | uuid | 필수 | 대화 상대 id |

##### 응답 (200)

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | object \| null | `ready` 프로필이 없으면(생성 중·없음·`stale`) `null` |

##### 프로필 객체

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 말투 프로필 객체 | 이 장의 '응답 객체' 절에 정의한 말투 프로필 객체 |

<a id="a-record-del"></a>

#### `DELETE /records/{record_id}` (제안) — 기록 삭제 요청

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 2 · #11 |
| 내부 호출 | DB `request_record_deletion(record_id)` → 기록이 즉시 `deleting`으로 바뀌어 조회에서 사라지고 삭제 작업 생성 |

삭제 작업을 만든 것이지 파일 삭제가 끝난 것이 아닙니다. 같은 기록에 다시 요청하면 같은 작업을 돌려줍니다. 앱은 `status = completed`이고 `remaining_steps`가 비었을 때만 성공으로 표시합니다.

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| record_id | path | uuid | 필수 | 기록 id |

##### 응답 (202) · 삭제 작업

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 삭제 작업 객체 | 이 장의 '응답 객체' 절에 정의한 삭제 작업 객체 |

<a id="a-jobs"></a>

#### `GET /deletion-jobs · /deletion-jobs/{job_id}` (제안) — 삭제 진행·부분 실패 확인

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 2 · #11 |
| 내부 호출 | DB `list_deletion_jobs(limit, offset)` · `get_deletion_job(job_id)` |

삭제 대상 파일 경로 목록(`manifest`)은 앱에 보내지 않습니다.

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| job_id | path | uuid | 상세만 필수 | 삭제 작업 id |
| limit | query | integer | 선택 | 기본 50, 최대 100 |
| offset | query | integer | 선택 | 0 이상, 기본 0 |

##### 응답 (200) · 목록

| 필드 | 형식 | 설명 |
|---|---|---|
| items | array | 조회 결과 목록. 정렬은 생성 시각 내림차순, 같으면 UUID 내림차순 |
| limit | integer | 적용된 limit |
| offset | integer | 적용된 offset |

##### 응답 (200) · 상세와 목록의 `items` 항목

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 삭제 작업 객체 | 이 장의 '응답 객체' 절에 정의한 삭제 작업 객체 |

<a id="a-account-del"></a>

#### `DELETE /account` (미정) — 계정 전체 삭제

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 2 · #11 |

##### 정해진 것과 정해지지 않은 것

| 항목 | 내용 |
|---|---|
| DB 모듈 | 계정·대상(persona) 전체 삭제는 DB 모듈 범위 밖 (`target_type`에 `persona`, `account` 값만 정의됨) |
| 필요한 작업 | Storage 원본 삭제 → 프로필·외부 색인·학습 산출물 정리 → 기록·파생 자료 삭제 → Auth 계정 제거를 처리하는 삭제 worker |
| 요청·응답 규격 | 미정 |

### 정해지지 않은 항목

<a id="a-record-upload"></a>

#### 기록 파일 업로드 (미정) — 원본 파일을 private Storage에 올리는 방법

| 항목 | 내용 |
|---|---|
| 인증 | 미정 |
| 단계·이슈 | 고도화 1 · #9 |

##### 정해진 것과 정해지지 않은 것

| 항목 | 내용 |
|---|---|
| 저장 위치 | Supabase Storage private 버킷 `replica-records` (아직 생성되지 않음) |
| 저장 경로 | `{user_id}/{record_id}/original`. 서버가 `records` 예약 행을 먼저 만든 뒤에만 업로드 가능, 원본 덮어쓰기 불가 |
| 지원 형식 | `.txt`, `.wav`, `.mp3`, `.m4a` (이슈 #9). 지금 Mock 서버의 음성 허용 목록은 더 넓음 |
| 용량 제한 | 미정 ("서버에서 합의한 용량 제한") |
| 업로드 주체 | 미정: 앱이 Storage로 직접 올림 / 백엔드를 거쳐 올림 |
| 요청·응답 규격 | 미정 |

<a id="s2"></a>

## 2. 앱 관련 오류에 대해 백엔드가 보내는 오류 메시지

### 오류 응답 본문 (제안)

현재 구현은 FastAPI 기본 형식 `{"detail": …}`입니다. 앱이 단계와 재시도 가능 여부를 알 수 있도록 아래 형식을 제안합니다.

```json
{
  "error": {
    "code": "STT_FAILED",
    "message": "음성을 글자로 바꾸지 못했어요. 다시 시도해 주세요.",
    "stage": "stt",
    "turn_id": "…uuid…",
    "retryable": true,
    "field": null
  }
}
```

| 필드 | 형식 | 설명 |
|---|---|---|
| error.code | string | 오류 종류 (아래 표의 `code`) |
| error.message | string | 사용자에게 보여줄 한국어 안내문 |
| error.stage | string \| null | 실패한 단계. `stt` · `llm` · `tts`. 해당 없으면 `null` |
| error.turn_id | uuid \| null | 대화 턴 오류일 때 해당 턴 id (재시도·재조회용) |
| error.retryable | boolean | 사용자가 같은 요청을 다시 시도해도 되는지 |
| error.field | string \| null | 입력 오류일 때 문제가 된 필드 이름 |

### 오류 목록

| 상황 | HTTP | `code` (제안) | `message` (앱에 보내는 문구) | 현재 구현 | 발생 API | `retryable` |
|---|---|---|---|---|---|---|
| 지원하지 않는 음성 형식 | 415 | `UNSUPPORTED_AUDIO_FORMAT` | `지원하지 않는 음성 형식입니다(.확장자). 지원 형식: .aac, .m4a, .mp3, .mp4, .ogg, .wav, .webm`<br>확장자가 없으면 `(확장자 없음)` | 구현됨. 같은 HTTP와 문구를 `{"detail": 문구}`로 반환 (`code` 필드 없음) | `/chat/voice` `/stt` | false |
| 음성 파일이 25MB 초과 | 413 | `FILE_TOO_LARGE` | `파일이 너무 큽니다. 최대 25MB` | 구현됨 (위와 같음) | `/chat/voice` `/stt` | false |
| 빈 음성 파일 | 400 | `EMPTY_FILE` | `빈 파일입니다.` | 구현됨 (위와 같음) | `/chat/voice` `/stt` | false |
| 요청 형식·값 오류 (필수 필드 누락, 형식 오류 등) | 422 | `INVALID_INPUT` | `입력한 내용을 확인해 주세요.` (`field`에 문제 필드) | FastAPI 기본 본문 `{"detail": [{"type", "loc", "msg", "input"}]}`. 일부 오류는 `ctx`도 포함하며 문구는 영문 | 모든 API | false |
| 메시지가 비었음 | 422 | `MESSAGE_EMPTY` | `메시지를 입력해 주세요.` | 빈 문자열은 422(기본 본문). **공백만 있는 메시지는 현재 통과(200)** 하므로 공백만 있는 경우도 이 오류로 막는 것을 제안 | `/chat/text` | false |
| 메시지가 2000자 초과 | 422 | `MESSAGE_TOO_LONG` | `질문이 너무 길어요. 더 짧게 입력해 주세요.` | 422(기본 본문) | `/chat/text` | false |
| 음성 인식 결과가 비어 있음 (무음 녹음 등). AI를 호출하기 전에 차단 | 422 | `EMPTY_TRANSCRIPT` | `음성을 인식하지 못했어요. 다시 말씀해 주세요.` | 미구현 | `/chat/voice` | true |
| STT 단계 실패 (시간 초과 포함) | 502 | `STT_FAILED` (`stage: stt`) | `음성을 글자로 바꾸지 못했어요. 다시 시도해 주세요.` | 미구현 | `/chat/voice` `/stt` | true |
| TTS 단계 실패 (시간 초과 포함). 단독 재시도·단독 호출 | 502 | `TTS_FAILED` (`stage: tts`) | `음성 합성에 실패했어요. 답변 글은 그대로 볼 수 있어요.` | 미구현 | `/chat/turns/{turn_id}/tts` `/tts` | true |
| 로그인하지 않음, 토큰 없음·만료·잘못됨 | 401 | `UNAUTHENTICATED` | `로그인이 필요하거나 만료되었어요. 다시 로그인해 주세요.` | 미구현 (현재 인증 없음) | 인증 필요 API 전부 | false |
| 서버 내부 오류 (원인 상세는 서버 로그에만) | 500 | `INTERNAL_ERROR` | `서버에 문제가 생겼어요. 잠시 후 다시 시도해 주세요.` | FastAPI 기본 `Internal Server Error` (본문은 문자열. 예: `LLM_MODE=remote`인데 미구현) | 모든 API | false |

### 오류가 아닌 부분 실패

아래 경우는 HTTP 200으로 응답하고, 실패한 부분만 응답 본문으로 알립니다.

| 상황 | HTTP | 응답 본문 필드 | `tts_error.message` | 발생 API |
|---|---|---|---|---|
| 답변 생성은 성공했지만 TTS 단계만 실패 (시간 초과 포함) | 200 | `tts_status: "failed"`, `audio_base64: null`, `audio_format: null`, `tts_error: {code: "TTS_FAILED", message}` | `음성 합성에 실패했어요. 답변 글은 그대로 볼 수 있어요.` | `/chat/voice` |

AI 서버가 원인인 오류(`AI_BUSY`, `AI_NOT_READY`, `AI_TIMEOUT`, `AI_FAILED` 등)는 **3장**에 정리했습니다.

DB가 원인인 오류(`NOT_FOUND`, `CONFLICT`, `DB_UNAVAILABLE` 등)는 **DB 규격 문서**에 정리했습니다.

같은 `code`는 한 가지 문구만 씁니다. 2장·3장·DB 규격 문서의 `code`와 문구는 같고, 상황이 다르면 `code`를 나눕니다.

AI·STT·TTS 단계의 시간 초과는 각 단계의 `code`(`AI_TIMEOUT`, `STT_FAILED`, `TTS_FAILED`)로 보냅니다. 전체 시간 초과 전용 코드는 두지 않고, 앱은 자체 30초 타임아웃으로 처리합니다.

오류 본문에는 SQL, 예외 원문, 토큰, 질문·답변 내용, 서버 키를 넣지 않습니다.

앱의 현재 동작: 입력은 2000자 이내, 공백만 있는 메시지는 전송 불가, 응답 대기 중 중복 전송 방지, **30초 타임아웃**, 실패·빈 응답은 오류 안내와 재시도·수정 버튼을 제공합니다.

<a id="s3"></a>

## 3. AI 서버 오류에 대해 백엔드가 보내는 오류 메시지 (앱 전달)

AI 서버의 요청·응답·오류 규격 원본은 `contracts/baseline`(PR #29)입니다. 이 장은 그 오류를 백엔드가 앱에 어떻게 전달하는지만 정리합니다. 앱 오류 본문 형식은 2장과 같고, `code`와 문구도 2장과 DB 규격 문서의 정의와 같습니다. AI 전용 코드(`AI_*`)는 이 장에서 정의합니다.

### AI가 오류 본문을 돌려준 경우

#### ① AI가 보내는 오류와 백엔드 처리

| AI가 주는 HTTP | AI `error.code` | AI `error.message` (계약 예제) | AI의 발생 조건 | 백엔드 처리 | DB 기록 `error_code` (안) |
|---|---|---|---|---|---|
| 400 | `INVALID_REQUEST` | `요청 형식이 올바르지 않습니다.` | JSON 파싱, 필수 필드, 형태·타입·UUID·범위·공백·미정의 필드 오류 | 백엔드의 요청 생성 오류로 보고 로그만 남김. 자동 재시도 없음 | `llm_invalid_request` |
| 401 | `SERVICE_UNAUTHENTICATED` | `서비스 인증이 필요합니다.` | 서비스 인증 실패 (본문 파싱 전에 판정) | 서버 설정·토큰 문제로 보고 경보. 앱에는 연결 오류로 안내 | `llm_service_auth` |
| 403 | `SERVICE_FORBIDDEN` | `서비스 호출 권한이 없습니다.` | 인증된 서비스 호출자에게 권한이 없음 | 위와 같음 | `llm_service_auth` |
| 413 | `PAYLOAD_TOO_LARGE` | `요청 크기가 한도를 초과했습니다.` | 원시 본문 256 KiB, 질문·이력 텍스트 16000자, 이력 20쌍 중 하나 초과 | 앱 입력 한도(2000자)와 이력 20쌍 제한 때문에 정상이면 발생하지 않음. 발생하면 백엔드 오류 | `llm_payload_too_large` |
| 422 | `CONTEXT_LIMIT_EXCEEDED` | `질문이 모델 문맥 한도를 초과했습니다.` | 이력을 모두 빼도 모델 문맥을 초과 | 질문 자체가 너무 긺 | `llm_context_limit` |
| 503 | `MODEL_NOT_READY` | `모델이 아직 준비되지 않았습니다.` | 모델이 준비되지 않음 | 재시도 없이 사용자에게 안내 | `llm_not_ready` |
| 409 | `REQUEST_IN_PROGRESS` | `동일 요청이 처리 중입니다.` | 같은 인스턴스에서 같은 ID가 실행 중 | 같은 요청의 중복 전송. 새로 호출하지 않고 해당 턴 상태 확인을 안내 | `llm_in_progress` |
| 429 | `AI_BUSY` | `AI가 다른 요청을 처리 중입니다.` | 다른 ID가 생성 슬롯을 사용 중 | 자동 재시도 없음. (대기 후 한 번 호출할지 즉시 오류를 줄지는 미정) | `llm_busy` |
| 504 | `INFERENCE_DEADLINE_EXCEEDED` | `추론 마감 시간을 초과했습니다.` | AI가 추론 마감 초과를 알리는 오류를 실제 반환 | 마감 초과 안내일 뿐 AI의 계산 종료나 슬롯 반환을 뜻하지 않음 | `llm_deadline` |
| 500 | `INFERENCE_FAILED` | `답변 생성에 실패했습니다.` | 추론 실패 또는 빈 출력 | - | `llm_failed` |

#### ② 앱에 보내는 오류 (백엔드 → 앱)

| AI `error.code` | 앱에 보내는 HTTP | 앱 `code` | 앱 `message` | `retryable` |
|---|---|---|---|---|
| `INVALID_REQUEST` | 502 | `AI_FAILED` | `답변을 만들지 못했어요. 잠시 후 다시 시도해 주세요.` | false |
| `SERVICE_UNAUTHENTICATED` | 503 | `AI_UNAVAILABLE` | `AI 서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요.` | false |
| `SERVICE_FORBIDDEN` | 503 | `AI_UNAVAILABLE` | `AI 서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요.` | false |
| `PAYLOAD_TOO_LARGE` | 502 | `AI_FAILED` | `답변을 만들지 못했어요. 잠시 후 다시 시도해 주세요.` | false |
| `CONTEXT_LIMIT_EXCEEDED` | 422 | `MESSAGE_TOO_LONG` | `질문이 너무 길어요. 더 짧게 입력해 주세요.` | false |
| `MODEL_NOT_READY` | 503 | `AI_NOT_READY` | `AI를 준비하고 있어요. 잠시 후 다시 시도해 주세요.` | true |
| `REQUEST_IN_PROGRESS` | 409 | `CONFLICT` | `이미 처리 중이거나 상태가 바뀌었어요. 새로고침 후 다시 시도해 주세요.` | false |
| `AI_BUSY` | 429 | `AI_BUSY` | `지금 다른 요청을 처리하고 있어요. 잠시 후 다시 시도해 주세요.` | true |
| `INFERENCE_DEADLINE_EXCEEDED` | 504 | `AI_TIMEOUT` | `답변을 만드는 데 시간이 오래 걸리고 있어요. 잠시 후 다시 시도해 주세요.` | true |
| `INFERENCE_FAILED` | 502 | `AI_FAILED` | `답변을 만들지 못했어요. 잠시 후 다시 시도해 주세요.` | true |

### AI 오류 본문이 없거나 응답이 이상한 경우

| 상황 (AI 오류 본문이 없는 경우) | 백엔드 처리 | 앱에 보내는 HTTP | 앱 `code` | 앱 `message` | `retryable` | DB 기록 `error_code` (안) |
|---|---|---|---|---|---|---|
| 백엔드의 연결·읽기·전체 타임아웃, 연결 끊김 (결과 **UNKNOWN**) | AI 504를 만들어 귀속하지 않음. 자동 재시도·재전송 없음. 답변을 저장하지 않음. AI가 계산을 이어가고 있을 수 있어 직후 요청이 429를 받을 수 있음 | 504 | `AI_TIMEOUT` | `답변을 만드는 데 시간이 오래 걸리고 있어요. 잠시 후 다시 시도해 주세요.` | true | `llm_timeout` |
| AI 서버에 연결 불가 (연결 거부·DNS 실패) | 전송 실패로 기록, 자동 재시도 없음 | 503 | `AI_UNAVAILABLE` | `AI 서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요.` | true | `llm_unreachable` |
| 응답의 `request_id`가 요청과 다름, 또는 허용되지 않은 `null` | 프로토콜 실패, 결과 UNKNOWN. 정상 답변·AI 오류로 저장하지 않음, 자동 재시도 없음 | 502 | `AI_FAILED` | `답변을 만들지 못했어요. 잠시 후 다시 시도해 주세요.` | true | `llm_protocol_error` |
| 응답이 스키마에 맞지 않음 (필드 누락, `finish_reason` 값 오류, `ai_generated`가 `true`가 아님, `context` 합 불일치, 답변이 빈 값) | 응답 폐기 | 502 | `AI_FAILED` | `답변을 만들지 못했어요. 잠시 후 다시 시도해 주세요.` | true | `llm_protocol_error` |
| `finish_reason: length` (답변이 길이 한도로 잘림) | **오류가 아님.** 200으로 처리하고 그대로 보존 | 200 | - | 앱에 `finish_reason: "length"` 전달 (잘림 표시 여부는 앱 담당과 결정) | - | - |

모든 AI 오류는 해당 턴의 LLM 단계 실패로 DB에 기록합니다 (`finish_chat_stage(ok=false, error_code=…)`). 오류 메시지에는 AI 내부 정보, 질문, 이력, 답변을 담지 않습니다.
