# 레플리카 API 명세서: 앱 ↔ 백엔드

기준일 2026-11-01 · 팀이 올린 이슈·PR과 `main`(`35962b5`, 챗 API #63 병합)의 구현을 바탕으로 정리했습니다. 팀 검토를 거쳐 병합하기 전까지는 합의된 규격이 아닙니다.

**[구현]** `main`에 코드가 있고 동작함   **[제안]** 이슈를 근거로 이 문서가 제안한 규격   **[미정]** 아직 정해지지 않음   (표에서 `필수`는 필수 여부)

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
| 인증 | `Authorization: Bearer <Supabase JWT>`. `/health`를 제외한 모든 API에 필요(개발 점검용 `/stt`·`/tts`는 기본이 꺼져 있고, 켜는 환경의 규칙은 각 API 참고). **로그인·회원가입은 앱이 Supabase Auth와 직접 하며 백엔드에는 로그인 API가 없음.** 백엔드는 받은 토큰을 Supabase Auth로 확인해 사용자 id를 얻고, 앱은 DB에 직접 접근하지 않음 |
| 식별자·시간 | id는 소문자 UUID 문자열. 시간은 ISO 8601 (시간대 포함) |
| 목록 응답 | `{items, limit, offset}`. `limit` 기본 50 · 최대 100, `offset` 0 이상. (턴 목록은 `after_turn_no` 사용) |
| AI 표시 | AI 답변이 담긴 응답에는 `is_ai_generated: true`를 항상 포함 (FR-08) |
| 앱에 보내지 않는 필드 | `user_id`, `request_hash`, `llm_request_id`, `*_attempt`, `*_started_at`, `bucket_id`, `object_path`, `manifest` |
| 오류 응답 | 2장 참고 |

### API 목록

| 메서드 | 경로 | 용도 | 상태 | 단계·이슈 |
|---|---|---|---|---|
| GET | [`/health`](#a-health) | 서버 상태 확인 | 구현 | baseline · #21 |
| POST | [`/chat/text`](#a-chat-text) | 텍스트 대화 | 구현 | baseline · #6 |
| POST | [`/chat/voice`](#a-chat-voice) | 음성 대화 | 구현 | baseline · #7 |
| POST | [`/stt`](#a-stt) | 음성 → 텍스트 (개발 점검용) | 구현 | baseline · #7 |
| POST | [`/tts`](#a-tts) | 텍스트 → 음성 (개발 점검용) | 구현 | baseline · #7 |
| POST | [`/chat/turns/{turn_id}/tts`](#a-turn-tts) | 완료된 답변을 음성으로 합성·재시도 | 구현 | baseline · #7, UC-02 ("원하면 음성 재생") |
| POST | [`/personas`](#a-persona-create) | 대화 상대 생성 | 구현 | baseline~고도화 1 · 전용 이슈 없음 (DB 문서의 대화 상대 함수) |
| GET | [`/personas · /personas/{persona_id}`](#a-persona-get) | 대화 상대 목록·상세 | 구현 | baseline~고도화 1 · 전용 이슈 없음 (DB 문서의 대화 상대 함수) |
| PATCH | [`/personas/{persona_id}`](#a-persona-patch) | 대화 상대 이름·호칭 수정 | 제안 | 고도화 1 · #74 |
| POST | [`/sessions`](#a-session-create) | 대화방 생성 | 구현 | baseline · #5, #6 |
| GET | [`/sessions · /sessions/{session_id}`](#a-session-get) | 대화 이력 목록·상세 | 구현 | 고도화 2 · #12 |
| GET | [`/sessions/{session_id}/turns`](#a-turns) | 대화방의 턴 목록 | 구현 | 고도화 2 · #12 |
| DELETE | [`/sessions/{session_id}`](#a-session-del) | 대화 삭제 | 구현 | 고도화 2 · #12 |
| POST | [`/records`](#a-record-create) | 기록 업로드 예약 | 제안 | 고도화 1 · #9 |
| POST | [`/records/{record_id}/uploaded`](#a-record-uploaded) | 업로드 완료 알림 | 제안 | 고도화 1 · #9 |
| GET | [`/records · /records/{record_id}`](#a-record-get) | 기록 목록·상세 | 제안 | 고도화 1 · #9, #10 |
| GET | [`/records/{record_id}/text-versions`](#a-textver-list) | 전사문 초안·확정본 조회 | 제안 | 고도화 1 · #10 |
| POST | [`/records/{record_id}/text-versions`](#a-textver-create) | 새 텍스트 초안 만들기 | 제안 | 고도화 1 · #10 |
| PATCH | [`/text-versions/{text_version_id}`](#a-textver-edit) | 전사문 수정 | 제안 | 고도화 1 · #10 |
| POST | [`/text-versions/{text_version_id}/confirm`](#a-textver-confirm) | 전사문 확정 | 제안 | 고도화 1·2 · #10 |
| GET | [`/personas/{persona_id}/style-profile`](#a-profile) | 현재 말투 프로필·분석 상태 조회 | 제안 | 고도화 1 · #10, #113 |
| POST | [`/personas/{persona_id}/style-profile/analysis`](#a-analysis) | 말투 프로필 분석 시작·재요청 | 제안 | 고도화 1 · #113 |
| DELETE | [`/records/{record_id}`](#a-record-del) | 기록 삭제 요청 | 제안 | 고도화 2 · #11, #123 |
| DELETE | [`/personas/{persona_id}`](#a-persona-del) | 대화 상대 삭제 요청 | 제안 | 고도화 2 · #123 |
| GET | [`/deletion-jobs · /deletion-jobs/{job_id}`](#a-jobs) | 삭제 진행·부분 실패 확인 | 제안 | 고도화 2 · #11, #123 |
| POST | [`/deletion-jobs/{job_id}/retry`](#a-job-retry) | 실패한 삭제 작업 다시 시작 | 제안 | 고도화 2 · #123 |
| DELETE | [`/account`](#a-account-del) | 계정 전체 삭제 요청 | 제안 | 고도화 2 · #123 |

기록 파일은 `POST /records` 응답의 서명 업로드 정보로 Storage에 직접 올립니다(API 목록에는 넣지 않음). 아래 '기록 파일 업로드'를 참고하세요.

### 응답 객체

여러 API의 응답에 공통으로 쓰이는 객체입니다. API 상세의 각 카드는 객체 이름만 참조합니다. 앱에 보내지 않는 서버 내부 필드는 위 '공통 사항'에 적은 대로 표에 넣지 않았습니다.

#### 대화 상대 객체

| 필드 | 형식 | 설명 |
|---|---|---|
| id | uuid | 대화 상대 id |
| name | string | 이름 |
| display_name | string \| null | (제안) 앱에 표시하고 AI가 부르는 이름·호칭(예: "엄마"). 사용자가 편집하며 미설정이면 `null`이고 이때 앱은 `name`을 표시. 한 번 설정하면 `null`로 되돌릴 수 없음(수정 API 규칙 참고). 현재 `main`의 응답에는 아직 없음 |
| provider_address | string \| null | (제안) 대화 상대가 사용자를 부르는 호칭. 사용자가 확인·수정한 값이며 미설정이면 `null`. 프로필의 자동 추출 후보와 별개. 현재 `main`의 응답에는 아직 없음 |
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
| finish_reason | string \| null | (제안) 답변 종료 사유. `stop` 또는 `length`(길이 한도로 잘림, 성공). 답변이 완료된 턴에만 있고 그 밖에는 `null`. 컬럼이 생기기 전에 만든 턴도 `null`이며, 이때 앱은 `stop`으로 봄 (`/chat/text` 재전송 응답과 같은 규칙) |
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
| status | string | 원본 파일의 처리 단계. `uploading`(예약만 함. 원본 확인 전이며 카카오톡 기록은 화자 확인 전 포함) · `uploaded`(원본 존재·크기와 화자 검증을 통과) · `processing`(서버가 텍스트 처리·전사 초안 생성 중) · `ready`(처리 끝. 텍스트 기록은 `revision 1`이 자동 확정된 상태, 음성 기록은 전사 초안이 만들어져 사용자 확인을 기다리는 상태) · `failed`(원본 처리 실패, `error_code`) · `deleting`. 색인·말투 프로필 반영은 `reflection_status`로 따로 봄(`ready`여도 `reflected`가 아닐 수 있음) |
| consent_at | datetime \| null | 자료 사용 동의 시각 |
| consent_version | string \| null | 동의 문구 버전 (최대 100자) |
| consent_scopes | string[] | 동의 범위. `personalization`, `training` 중 선택. 기본 `["personalization"]` |
| error_code | string \| null | 실패 시 오류 코드 (영문·숫자·`.`·`-`·`_`, 1~64자). `status`가 `uploading`일 때는 `/uploaded`가 화자 오류로 거절한 사유 `speaker_required` · `speaker_not_found` · `speaker_not_allowed`가 들어 있을 수 있으며, 앱은 이 값이 있으면 '확인 필요'로 표시하고 화자 선택을 이어감('업로드 뒤 검증'의 '화자 선택 이어가기'). 화자 검증을 통과해 `uploaded`가 되면 `null` |
| target_speaker | string \| null | (제안) 카카오톡 기록에서 말투를 반영하기로 고른 화자. 화자가 1명뿐인 기록에서는 사용자가 "이 화자가 대화 상대입니다"라고 확인한 그 화자. 파일에 표시된 문자열(앞뒤 공백 제거 후)이며 이 기록 안에서만 의미가 있음. 이 값은 이 기록의 최초 업로드와 `revision 1`, 기록 안 화자에 연결된 확인이며 이후 `revision`에는 '화자 확인의 적용 범위' 조건으로 승계됨 |
| provider_speaker | string \| null | (제안) 화자가 2명 이상인 카카오톡 기록에서 제공자로 고른 화자(동의 근거). 화자가 1명뿐인 기록은 `null`이며 파일에 없는 제공자는 추정하지 않음 |
| reflection_status | string | (제안) `none`(확정 전, 카카오톡 기록의 화자 확인 전 포함) · `pending`(확정됨, 색인·프로필 반영 중) · `reflected`(같은 확정본·자료 개정이 색인과 프로필 모두에 반영됨) · `failed`(반영 실패. 사용자가 말투 프로필 분석 API로 다시 요청하면 서버가 색인부터 다시 시작). 색인과 프로필의 개별 상태로 계산하며 둘 중 하나만 반영됐으면 `reflected`가 아님. 하위 상태와 재개 방식은 #112·#113·#121에서 정함 |
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
| next_attempt_at | datetime \| null | (제안) 자동 재시도를 기다리는 `pending`일 때만 값이 있는 다음 시도 시각. 작업을 집어 `running`이 될 때, 사용자 재시도로 `pending`이 될 때, `failed`·`completed`일 때는 항상 `null`입니다. 앱은 `pending`이면서 이 값이 있을 때만 '자동으로 다시 시도하는 중'으로 보여 주고, `pending`인데 `null`이면 접수되어 곧 처리를 기다리는 중입니다 |
| parent_job_id | uuid \| null | (제안) 이 작업을 흡수한 상위 작업(`persona`·`account`)의 id. 값이 있으면 이 작업은 상위 작업이 처리하므로 이 작업의 `status`·`last_error_code`와 다시 시도 버튼 대신 상위 작업을 따릅니다. 하위 작업은 상위 작업의 `database` 단계가 성공할 때 같은 트랜잭션에서 `completed`로 닫힙니다 |
| created_at, updated_at, finished_at | datetime | 생성·수정·완료 시각 (`finished_at`은 완료 시에만) |

#### 삭제 작업의 상태와 처리 규칙

삭제 요청은 작업을 만들 뿐 삭제가 끝난 것이 아닙니다. 삭제 worker(백엔드 프로세스의 백그라운드 작업)가 남은 단계를 처리하고, 앱은 작업 객체로 진행을 확인합니다. 아래 규칙은 모두 제안입니다(#123). 기록·대화 상대·계정 삭제와 앱·DB가 같은 상태 모델을 쓰도록 이 절을 기준으로 삼습니다.

| 항목 | 내용 |
|---|---|
| 상태 | `pending`(접수되었거나 자동 재시도를 기다림. 기다리는 중이면 `next_attempt_at`과 직전 `last_error_code`가 있음) · `running`(worker가 처리 중) · `failed`(자동 재시도를 다 쓰고 멈춤. 사용자 재시도를 기다리며 남은 단계는 그대로 남음) · `completed`(모든 단계가 끝남). **자동 재시도를 기다리는 동안은 `failed`가 아닙니다** |
| 완료 조건 | `status = completed`이고 `remaining_steps`가 비어 있을 때만 완료입니다(앱의 현재 판정과 같음). `finished_at`은 DB 제약상 `completed`일 때 항상 있으므로 앱이 따로 확인하지 않아도 됩니다. 한 단계라도 남거나 실패하면 완료가 아니며 부분 실패를 성공으로 표시하지 않습니다. 계정 삭제는 로그인 계정이 사라져 이 객체를 조회할 수 없으므로 'DELETE /account'의 완료 판정을 따릅니다 |
| 단계(`remaining_steps`) | `storage`(Storage 원본 객체) · `external_index`(외부 색인·검색 서비스의 자료. 지금은 대상 없음) · `adapter`(학습 산출물. 지금은 대상 없음) · `database`(DB 행: 전사문·조각·프로필 연결·기록 등) · `auth`(Supabase 로그인 계정. 계정 삭제에만). 대상이 없는 단계는 처음부터 목록에 넣지 않습니다 |
| 처리 순서와 표시 순서 | 서버는 `storage` → `external_index` → `adapter` → `database` → `auth`(남은 것만) 순서로 처리하며, Storage 원본을 먼저 지운 뒤에만 DB 행을 지웁니다(DB가 이 순서를 강제). 단계는 끝날 때마다 바로 `remaining_steps`에서 빠지고 같은 단계를 다시 처리해도 결과가 같습니다(멱등). 앱은 화면의 단계 목록 순서를 스스로 정하고, 단계가 끝났는지는 `remaining_steps`에 남아 있는지로만 판단하며 처리 순서에 의존하지 않습니다 |
| 소유권 | worker가 작업을 집을 때 소유 토큰(`lease_token`)을 받고, 진행을 알리거나 단계를 끝낼 때마다 이 토큰을 함께 보냅니다. 토큰이 다르면(예: 5분 넘게 갱신이 없어 다른 시도가 이어받은 뒤에 도착한 이전 시도의 늦은 완료·실패) 서버가 거절하고 아무것도 바꾸지 않습니다. 한 작업에는 한 번에 하나의 소유 토큰만 유효합니다 |
| 진행 신호와 이어받기 | 처리 중인 worker는 일정 간격(예: 30초)마다 `updated_at`을 갱신해 진행 중임을 알립니다. `running`이 5분(임시 값) 동안 갱신되지 않을 때만 멈춘 것으로 보고 다른 시도가 새 소유 토큰으로 `remaining_steps`부터 이어받습니다. 개별 Storage·DB 호출에는 이보다 훨씬 짧은 시간 제한을 둡니다 |
| 시도와 자동 재시도 | worker가 작업을 집을 때마다 `attempt_count`가 1 올라갑니다(누적). 단계가 실패하면 `last_error_code`를 남기고, 자동 재시도 횟수가 남아 있으면 작업을 `pending`으로 돌리고 `next_attempt_at`(1분·5분·30분 뒤, 임시 값)을 정합니다. 3번을 다 쓰고도 실패하면 `failed`로 멈춥니다. 작업을 집는 순간 `next_attempt_at`은 `null`이 됩니다. 계정 삭제에서 로그인 계정이 이미 지워진 뒤의 마무리는 이 횟수 제한 없이 계속 다시 시도합니다('DELETE /account'의 'Auth 삭제 뒤 정리와 복구') |
| 사용자 재시도 | `POST /deletion-jobs/{job_id}/retry`는 `failed`인 작업을 같은 작업 id로 `pending`(바로 시도)으로 되돌리며 `next_attempt_at`은 `null`로 둡니다(자동 재시도 대기가 아니므로 앱이 '자동으로 다시 시도하는 중'으로 보여 주지 않음). 자동 재시도 횟수는 0으로 초기화합니다(재시도 요청마다 자동 재시도 3번의 새 예산). `attempt_count`는 이어서 늘어납니다. 새 작업을 만들어 부분 실패를 감추지 않습니다 |
| 앱의 조회와 복원 | 앱은 `pending`·`running`인 작업을 시트가 열려 있는 동안 3초 간격으로 최대 20번 확인하고, `failed`는 자동으로 다시 묻지 않습니다. 그 뒤에는 기록 목록의 삭제 요청 카드로 남기고, 목록을 새로고침하면 `GET /deletion-jobs`로 복원해 다시 확인합니다. `pending`이면서 `next_attempt_at`이 있으면 "자동으로 다시 시도하는 중"으로, `failed`면 부분 실패와 다시 시도 버튼으로 보여 줍니다. `parent_job_id`가 있는 작업은 상위 작업이 처리하므로 실패 표시와 다시 시도 버튼을 숨기고 '계정 삭제에 포함됨'(대화 상대 삭제면 '대화 상대 삭제에 포함됨')으로 보여 주며 상위 작업을 확인합니다 |
| 실패 코드 | `last_error_code`는 안전한 코드만 씁니다: `storage_unavailable` · `storage_error` · `database_error` · `auth_error` · `external_index_error` · `adapter_error` · `worker_interrupted`. 내부 경로·예외 원문은 넣지 않습니다 |
| 앱에 보이지 않는 것 | 삭제 대상 파일 경로 목록(`manifest`)과 소유 토큰(`lease_token`)은 앱에 보내지 않습니다 |
| 보관 | 완료된 작업의 보관 기간은 정하지 않았습니다. 앱은 `GET /deletion-jobs`로 이전 작업을 복원합니다 |

##### 삭제 요청을 다시 보낼 때

| 상황 | 응답 |
|---|---|
| 진행 중이거나 `failed`인 작업이 있는 대상에 `DELETE`를 다시 보냄 | 같은 작업을 그대로 202로 돌려줍니다. 상태를 바꾸지 않고 실패한 작업을 다시 시작하지도 않습니다. 응답을 놓쳤을 때 작업 id를 되찾는 용도이며, 삭제 중인 대상의 다른 API는 404이지만 `DELETE`는 예외입니다 |
| `failed`인 작업을 다시 시작 | `POST /deletion-jobs/{job_id}/retry`만 가능합니다 |
| 삭제가 이미 끝나 행이 사라진 대상에 `DELETE` | 404 |
| `completed`인 작업에 retry | 200과 현재 작업 객체입니다(409가 아님. 앱이 응답을 그대로 완료로 처리) |
| 흡수된 작업(`parent_job_id`가 있음)에 retry | 200과 현재 작업 객체입니다(아무것도 바꾸지 않음. 상위 작업이 처리하며 단독으로 집지 않습니다) |
| 계정 삭제를 요청한 뒤 | 그 계정 삭제 작업이 `completed`가 되기 전에는(`pending`·`running`·`failed` 모두) 삭제 작업 조회, retry, 같은 `DELETE /account`(진행 중이거나 실패한 같은 작업을 202)만 허용하고 그 밖의 쓰기는 409 `ACCOUNT_DELETING`입니다. 삭제를 취소하는 API가 없고 데이터는 이미 숨겨졌으므로 `failed`여도 쓰기를 열지 않습니다 |

##### 합의가 남은 범위 (제안)

| 항목 | 내용 |
|---|---|
| 대화 상대 삭제와 이력 삭제 범위 | #123에서 확정한 정책(기록 삭제는 과거 대화 보존, 계정 삭제는 전부 삭제)에는 없는 제안입니다. 영향 파트의 합의 뒤 #123·#11에 반영합니다 |
| worker 배치와 외부 파생물 | 삭제 worker를 백엔드 프로세스의 백그라운드 작업으로 두는 것과 외부 색인·학습 산출물 정리의 책임은 제안이며 합의가 필요합니다 |
| AI 연계(#112·#113) | 삭제와 검색 스냅샷이 겹칠 때, 삭제된 입력으로 시작한 분석이 늦게 끝날 때, 마지막 유효 기록을 지운 뒤의 조회·대체 처리와 정리 순서는 백엔드·DB·AI가 함께 정해야 합니다. 과거 답변 보존은 기존 응답 본문을 지운다는 뜻이 아닙니다 |

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

#### `POST /chat/text` (구현) — 텍스트 대화

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | baseline · #6 |
| 내부 호출 | DB `create_chat_turn`·`start_chat_stage`·`get_recent_history`·`finish_chat_stage`·`record_turn_total_ms`, AI `POST /v1/baseline/generate` (호출 순서는 DB 규격 문서의 '텍스트 턴의 호출 순서') |

대화 이력은 앱이 보내지 않습니다. 백엔드가 같은 대화방의 최근 완결 질문·답변 20쌍을 DB에서 조회해 AI에 전달합니다. 같은 `turn_id`로 다시 보내면 이미 완료된 턴은 저장된 답변을 돌려주고 새로 AI를 호출하지 않으며, 실패한 단계는 새 시도로 다시 실행합니다. 제한 시간 안에 처리 중인 턴을 다시 보내면 409입니다.

##### 요청 (JSON)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| session_id | body | uuid | 필수 | 대화방 id |
| turn_id | body | uuid | 필수 | 앱이 **전송마다** 만드는 UUID. 같은 질문을 재전송할 때는 같은 값을 유지. 같은 `turn_id`에 다른 내용을 보내면 거부 |
| message | body | string | 필수 | 질문. 1~2000자, 공백만 있는 값 불가 |
| (그 밖의 필드) | body | - | 보내지 않음 | `persona_id`, `history` 등 정의되지 않은 필드를 보내면 422 `INVALID_INPUT` |

##### 응답 (200)

| 필드 | 형식 | 설명 |
|---|---|---|
| turn_id | uuid | 요청한 턴 id |
| answer | string | AI 답변 |
| is_ai_generated | boolean | 항상 `true` (FR-08) |
| finish_reason | string | `stop`(정상 종료) 또는 `length`(길이 한도로 잘림). 둘 다 성공 |
| model_name | string | 답변을 만든 모델 이름 |
| timings | object | `{stt_ms, llm_ms, tts_ms, total_ms}` 구간별 소요 시간(ms). 거치지 않은 구간은 `null`, `total_ms`는 항상 있음 |

##### 요청 예

```json
{
  "session_id": "…uuid…",
  "turn_id": "…uuid…",
  "message": "엄마, 오늘 뭐 했어?"
}
```

##### 응답 예

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

#### `POST /chat/voice` (구현) — 음성 대화

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | baseline · #7 |
| 내부 호출 | 음성 바이트의 SHA-256 계산, DB `create_chat_turn`·`start_chat_stage`·`get_recent_history`·`finish_chat_stage`·`record_turn_total_ms`, STT·AI·TTS 호출 (호출 순서는 DB 규격 문서의 '음성 턴의 호출 순서') |

STT 또는 LLM 단계가 실패하면 200이 아니라 오류로 응답하고, 오류 본문의 `stage`로 실패한 단계를 알립니다 (2장).

같은 `turn_id`와 같은 음성으로 다시 보내면 끝난 단계는 건너뛰고, 실패했거나 시작 전인 단계부터 새 시도로 이어서 실행합니다. 같은 `turn_id`에 다른 음성을 보내면 409 `TURN_ID_CONFLICT`입니다. 응답 음성은 저장하지 않으므로, TTS까지 끝난 턴을 다시 보내면 저장된 답변 텍스트로 음성만 다시 합성해 돌려줍니다 (DB 상태는 바꾸지 않음). 이때 재합성이 실패하면 응답은 `tts_status: "failed"`이지만 DB의 `tts_status`는 `ready`로, 답변과 함께 그대로 남습니다. 같은 요청을 다시 보내거나 `POST /chat/turns/{turn_id}/tts`로 다시 받을 수 있습니다. TTS 호출은 실패 시 백엔드가 자동으로 다시 시도합니다 (자세한 규칙은 `/chat/turns/{turn_id}/tts`). STT 전사문이 16,000자를 넘으면 AI·TTS를 호출하지 않고 422 `MESSAGE_TOO_LONG`(`stage: llm`, `retryable: false`)으로 응답하며, 전사문은 자르지 않고 보관합니다.

##### 요청 (multipart/form-data)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| file | form | file | 필수 | 녹음 파일. 확장자 `.wav .mp3 .m4a .webm .ogg .mp4 .aac`, 최대 25MB, 비어 있지 않음. 음성 형식은 STT·TTS 담당과 확정 필요 |
| session_id | form | uuid | 필수 | 대화방 id |
| turn_id | form | uuid | 필수 | 앱이 전송마다 만드는 UUID. 재전송 시 유지 |

##### 응답 (200)

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
| audio_format | string \| null | 음성 형식(현재 Mock은 `wav`). `failed`면 `null` |
| tts_error | object \| null | `{code, message}`. TTS 실패 시에만 |
| timings | object | `{stt_ms, llm_ms, tts_ms, total_ms}` 구간별 소요 시간(ms). 거치지 않은 구간은 `null`, `total_ms`는 항상 있음 |

##### 발생 가능한 오류

2장, 3장, DB 규격 문서

<a id="a-stt"></a>

#### `POST /stt` (구현) — 음성 → 텍스트 (STT 단독 확인, 개발 점검용)

| 항목 | 내용 |
|---|---|
| 인증 | 개발 점검 경로이며 **기본이 꺼져 있음**(`DEV_SPEECH_ROUTES=false` → 404 `NOT_FOUND`). 켜도 `APP_ENV=prod`에서는 열리지 않고, `APP_ENV=dev`에서는 로그인(Bearer)이 필요하며, `APP_ENV=local`에서만 로그인 없이 쓴다 (#67). 앱 기능에서는 사용하지 않음 |
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
| 인증 | `/stt`와 같음(기본 꺼짐, prod에서는 열리지 않음, dev는 로그인 필요, local만 무인증) (#67). 앱 기능에서는 사용하지 않음 |
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

#### `POST /chat/turns/{turn_id}/tts` (구현) — 완료된 답변을 음성으로 합성·재시도

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | baseline · #7, UC-02 ("원하면 음성 재생") |
| 내부 호출 | 새 단계 실행(`tts_status`가 없거나 `failed`): DB `start_chat_stage(turn_id, 'tts')` → TTS → `finish_chat_stage('tts')` → `record_turn_total_ms`. 재합성(`tts_status = ready`): DB 호출 없이 TTS만 호출하며 상태·시도 번호·시간 기록을 바꾸지 않음 |

텍스트 대화의 "음성 재생"과, 음성 대화에서 TTS만 실패한 뒤의 재시도에 씁니다. 합성 중인 턴, 답변이 아직 없는 턴에 요청하면 409를 돌려줍니다. 합성이 실패하면 200이 아니라 502 `TTS_FAILED`를 돌려주고, 답변 텍스트는 그대로 남습니다.

응답 음성은 저장하지 않으므로, 이미 완료(`tts_status = ready`)된 턴에 요청하면 DB 상태를 바꾸지 않고 저장된 답변으로 음성만 다시 합성해 돌려줍니다. 이때 합성이 실패해도 502 `TTS_FAILED`만 돌려주고 `ready` 상태와 답변은 그대로 남으므로, 앱은 같은 요청을 다시 보낼 수 있습니다 (`retryable: true`).

TTS 호출이 실패하면 백엔드가 한 요청 안에서 자동으로 다시 시도합니다 (첫 시도 + 재시도 2번, 팀 회의 결정). 앱이 응답을 기다리는 최대 시간 3분(요청 시작 기준, 응답 여유 10초 제외) 안에서만 다시 시도하고, 남은 시간이 직전 시도에 걸린 시간보다 짧으면 다음 시도를 건너뜁니다. 첫 시도는 남은 시간과 관계없이 항상 하며, 남은 시간이 10초보다 적으면 첫 시도에 한해 최대 10초까지 기다립니다(요청 시작 후 170초 이후에 시작한 TTS는 최대 10초 더 걸릴 수 있습니다). 진행 중인 TTS 단계는 요청 시작 후 180초(앱 대기 시간)가 지나기 전에는 다른 요청이 실패로 닫지 않습니다(STT·LLM 단계는 `LLM_TIMEOUT_SECONDS`, 없으면 30초 기준 그대로). 합성 중인 같은 턴을 다시 요청하면 409입니다. 모두 실패하면 위의 502 `TTS_FAILED`(`/chat/voice`는 200 + `tts_status: failed`)로 응답하므로, 앱은 이 응답을 받으면 사용자에게 다시 요청해 달라고 안내합니다. 자동 재시도는 DB 시도 번호(`tts_attempt`)를 늘리지 않습니다.

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| turn_id | path | uuid | 필수 | 대상 턴. 답변이 완료(`answer_status = done`)되어 있어야 함. `tts_status`가 없거나 `failed`이면 새 TTS 단계를 시작하고, `ready`이면 DB 변경 없이 다시 합성하며, `pending`(합성 중)이면 409 |

##### 응답 (200)

| 필드 | 형식 | 설명 |
|---|---|---|
| turn_id | uuid | 턴 id |
| tts_status | string | 항상 `ready` (실패하면 502 오류) |
| audio_base64 | string | 응답 음성 base64 |
| audio_format | string | 음성 형식 |
| timings | object | `{tts_ms, total_ms}` |

##### 발생 가능한 오류

2장, DB 규격 문서

<a id="a-persona-create"></a>

#### `POST /personas` (구현) — 대화 상대 생성

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

#### `GET /personas · /personas/{persona_id}` (구현) — 대화 상대 목록·상세

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

<a id="a-persona-patch"></a>

#### `PATCH /personas/{persona_id}` (제안) — 대화 상대 이름·호칭 수정

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 1 · #74 |
| 내부 호출 | DB 대화 상대 호칭 수정 함수 (`display_name`·`provider_address` 컬럼과 함께 DB 담당이 확정. 현재 원격 스키마에는 없음) |

SC-13(이름 입력·수정)은 `display_name`을 바꾸고, `provider_address`는 SC-07에서 바꿉니다. 기존 `name`은 이 API로 바꾸지 않습니다.

##### 요청 (JSON)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| persona_id | path | uuid | 필수 | `active` 상태의 대화 상대 id |
| display_name | body | string | 선택 | 앞뒤 공백을 제거한 뒤 1~50자. 공백만 있거나 `null`이면 422이며 해제할 수 없음(미설정이면 `name`을 표시). DB의 호칭 수정 함수(#124)는 JSON `null`로 해제할 수 있지만 백엔드가 호출 전에 거부함 |
| provider_address | body | string \| null | 선택 | 앞뒤 공백을 제거한 뒤 1~50자. 빈 문자열이나 `null`이면 미설정(`null`)으로 저장. 백엔드가 빈 문자열을 DB 호출 전에 JSON `null`로 바꿔 전달함 |

두 필드 중 하나 이상을 보내야 하며, 정의하지 않은 필드는 422입니다.

##### 규칙

| 항목 | 내용 |
|---|---|
| 표시 | `display_name`이 `null`이면 앱은 `name`을 표시합니다 |
| 개인화 호출 | 서버는 `display_name`을 쓰고, 없으면 `name`(1~50자일 때)을 씁니다. `name`이 범위를 벗어나면 개인화 호출 전에 422(`INVALID_INPUT`, `field: display_name`)로 이름을 입력하게 합니다 |
| 자료 개정 | 호칭은 기록(자료)이 아니므로 `data_revision`을 올리지 않고 말투 프로필을 다시 분석하지 않습니다. 호칭 편집은 카카오톡 기록의 화자 확인을 대신하지 않습니다 |
| 편집값 보호 | 분석 결과가 이 값을 덮어쓰지 않습니다. 자동 추출 후보는 말투 프로필의 `speech_style.address_terms`로만 제공합니다 |
| 입력 한도 | 앱의 이름·호칭 입력 한도도 서버와 같은 50자에 맞춥니다(#94 초안의 20자는 50자로 바꾸도록 요청) |

##### 응답 (200) · 대화 상대

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 대화 상대 객체 | 이 장의 '응답 객체' 절에 정의한 대화 상대 객체 |

<a id="a-session-create"></a>

#### `POST /sessions` (구현) — 대화방 생성

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

#### `GET /sessions · /sessions/{session_id}` (구현) — 대화 이력 목록·상세

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

#### `GET /sessions/{session_id}/turns` (구현) — 대화방의 턴 목록 (이어 보기)

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 2 · #12 |
| 내부 호출 | DB `list_turns(session_id, after_turn_no, limit)` |

앱은 한 턴을 질문·응답 말풍선 두 개로 그립니다. 서버 내부 필드(`user_id`, `request_hash`, `*_attempt`, `*_started_at`)는 앱에 보내지 않습니다.

조회할 때 진행 중(`pending`)인 단계가 있으면, 제한 시간을 넘긴 단계를 먼저 `failed`로 정리한 뒤 돌려줍니다(기준은 대화·TTS 요청이 쓰는 만료 기준과 같음). 서버가 요청 도중 멈춰 `pending`으로 남은 턴을 앱이 이 조회만으로 풀 수 있게 하기 위해서입니다. 정리에 실패해도 조회는 실패하지 않고 정리 전 상태를 돌려줍니다.

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

#### `DELETE /sessions/{session_id}` (구현) — 대화 삭제 (DB 행만)

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

같은 대화방을 다시 삭제하거나 없는·남의 대화방이면 404 `NOT_FOUND`입니다.

<a id="a-record-create"></a>

#### `POST /records` (제안) — 기록 업로드 예약

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 1 · #9 |
| 내부 호출 | DB `reserve_record(...)` → 상태 `uploading` 행과 원본 저장 경로 생성. 파일 업로드는 별도 |

파일은 이 응답의 `upload`(서명 업로드 정보)로 Storage에 직접 올립니다(아래 '기록 파일 업로드').

##### 요청 (JSON)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| persona_id | body | uuid | 필수 | 대화 상대 id |
| kind | body | string | 필수 | `text` 또는 `audio` |
| original_name | body | string | 필수 | 원본 파일 이름. 비어 있지 않음, 최대 255자 |
| mime_type | body | string | 필수 | 파일 MIME 형식 |
| size_bytes | body | integer | 필수 | 파일 크기(바이트). 1 이상(0이면 422 `INVALID_INPUT`, `field: size_bytes`) |
| consent_at | body | datetime | 필수 | 자료 사용 동의 시각 (시간대 포함). 서버 시각보다 미래이면 422 `INVALID_INPUT`(`field: consent_at`). 현재 구현은 시계 오차 허용 범위가 없어 기기 시계가 앞서면 거절될 수 있으므로 앱은 동의한 시각을 그대로 보냄 |
| consent_version | body | string | 필수 | 동의 문구 버전. 최대 100자 |
| target_speaker | body | string | 조건부 | (제안) 카카오톡 내보내기에서 말투를 반영할 화자. 화자가 2명 이상이면 `provider_speaker`와 함께 보내고, 1명뿐이면 그 화자를 대화 상대로 확인한 값으로 이것만 보냄. 카카오톡이 아니거나 음성이면 보내지 않음. 앞뒤 공백 제거 후 1~100자 |
| provider_speaker | body | string | 조건부 | (제안) 화자가 2명 이상인 카카오톡 내보내기에서 기록을 제공한 화자. `target_speaker`와 함께 보내며 화자가 1명뿐이면 보내지 않음. 앞뒤 공백 제거 후 1~100자 |
| record_id | body | uuid | 선택 | 앱이 미리 만든 id. 저장소 장애로 503을 받으면 같은 id로 다시 예약하므로(아래 '오류 응답') 보내는 것을 권장 |

##### 검증 규칙

| 항목 | 내용 |
|---|---|
| 용량 | `size_bytes`가 25 MiB(26,214,400바이트)를 넘으면 413 `FILE_TOO_LARGE`(`field: size_bytes`). 앱의 한도와 같음. 0이면 빈 파일이므로 422 `INVALID_INPUT`(`field: size_bytes`)입니다. 공백뿐인 텍스트처럼 내용이 비어 있는 파일은 업로드 뒤 검증의 '내용 형식'에서 422입니다 |
| 형식 | `mime_type`은 `text/plain`, `audio/wav`, `audio/x-wav`, `audio/mpeg`, `audio/mp4`, `audio/x-m4a` 중 하나이고 `kind`와 맞아야 함. 아니면 422 |
| 화자 선택 | `target_speaker`는 카카오톡 내보내기에서 보냅니다. 화자가 2명 이상이면 `provider_speaker`와 함께 보내고, 화자가 1명뿐이면 `target_speaker`(그 화자를 대화 상대로 확인한 값)만 보냅니다. 예약 단계에는 파일이 없으므로 **형식만** 검사합니다. 앞뒤 공백을 제거한 뒤 1~100자여야 하고, 아래는 422(`INVALID_INPUT`)이며 `field`는 다음과 같습니다. `provider_speaker`만 옴 → `target_speaker`, 두 값이 같음 → `provider_speaker`, 1~100자 위반 → 그 값의 필드, 음성 기록에 화자를 보냄 → `target_speaker`. 값이 파일의 화자 목록에 있는지, 카카오톡 판정, 화자 수는 파일을 올린 뒤 `/uploaded`에서 검사합니다(아래 '업로드 뒤 검증'). 앱의 비활성 처리는 우회될 수 있으므로 서버도 거부합니다 |
| 화자 구분이 없는 기록 | 음성 기록은 대상 인물(persona)의 목소리만 담긴 파일이며 전사 전체를 `persona`의 발화로 봅니다. 카카오톡 형식이 아닌 텍스트(일기 등)도 전체를 `persona`의 기록으로 봅니다(제안). 이 경우 화자 필드를 보내면 업로드 뒤 검증에서 422입니다. 카카오톡 내보내기에서 화자가 **1명뿐이면** 제공자인지 대화 상대인지 파일만으로 알 수 없습니다. 앱은 업로드 전에 파일을 파싱해 화자를 알고 있으므로 **예약 전에** 그 화자를 미리 보여 주고 사용자가 "이 화자가 대화 상대입니다"라고 **한 번 명시 확인**하게 하며, 확인한 값을 예약 요청의 `target_speaker`로 함께 보냅니다(`provider_speaker`는 보내지 않음). 파일에 없는 제공자를 추정하지 않으며 자동으로 확인된 것으로 처리하지도 않습니다. 사용자가 "대화 상대가 아니다"라고 하면 앱은 **예약과 업로드를 하지 않으므로** 서버에 남는 기록이 없고 기록 삭제도 필요하지 않습니다. 서버도 파일을 올린 뒤 `/uploaded`에서 다시 검사하며, 확인값이 없으면 422이고 기록이 `uploading`에 머물러 자동 확정·색인·프로필 분석이 시작되지 않습니다. 이 `/uploaded`의 확인 요구는 앱과 서버의 판정이 다를 때만 쓰는 경로입니다. 파일 선택과 처리 동의는 사용 허락이고, 이 확인은 발화의 주인을 정하는 절차라 서로 대신하지 않습니다. 확인이 적용되는 업로드·텍스트 버전과 승계 조건은 아래 '화자 확인의 적용 범위'에서 정합니다 |
| 재예약 | 같은 `record_id`로 다시 보내고 기록이 `uploading`이며 화자 값을 뺀 필드(`persona_id`, `kind`, `original_name`, `mime_type`, `size_bytes`, `consent_at`, `consent_version`)가 같으면 기록은 그대로 두고 200과 새 `upload`를 돌려줍니다. 화자 값은 이 재예약 요청의 값이 새 값이 되어 `uploading`인 동안 바뀔 수 있습니다(생략하면 없음. `/uploaded`의 "생략은 유지"와 다름). 화자 값 외 필드가 다르거나 이미 `uploading`이 아니면 409 `CONFLICT`입니다 |
| 화자 식별 | 앱이 보내는 값은 파일에 표시된 화자 문자열이며 그 기록 안에서만 의미가 있습니다. 서버는 이를 기록별 화자 식별자로 바꿔 저장하고, 표시 문자열을 사람·계정을 가리키는 전역 ID처럼 쓰지 않습니다. 발화·청크의 `speaker_role` 저장 구조는 백엔드·DB가 정합니다(#74) |

##### 응답 (201, 재예약은 200) · 기록과 업로드 정보

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 기록 객체 | 이 장의 '응답 객체' 절에 정의한 기록 객체 |
| upload | object | 원본 파일을 올릴 서명 업로드 정보. 이 응답에만 있고 기록 객체에는 없음 |
| upload.url | string | 서명 업로드 주소 (private Storage). `expires_at`까지 유효하며, 한 번 저장된 원본은 덮어쓸 수 없음. 같은 주소로 다시 `PUT`할 수 있는지는 실제 Storage로 확인하기 전이라 정하지 않았고, 거절되면 재예약함 |
| upload.method | string | `PUT` |
| upload.headers | object | 앱이 그대로 붙여 보낼 헤더. `Content-Type`은 예약 요청의 `mime_type`과 같고 버킷이 허용하는 MIME 목록과 일치해야 함 |
| upload.expires_at | datetime | 서명 만료 시각. 지나면 같은 `record_id`로 다시 예약해 새 정보를 받음. 서명이 만료·거절되어 Storage가 돌려주는 오류는 백엔드의 401이 아니므로 앱은 로그아웃하지 않고 재예약으로 처리함 |

##### 오류 응답

| 상태 | `code` | 상황 | 복구 |
|---|---|---|---|
| 401 | `UNAUTHENTICATED` | 로그인하지 않음·토큰 만료 | 다시 로그인 |
| 404 | `NOT_FOUND` | 없거나 남의 대화 상대 | 대화 상대 목록을 새로고침 |
| 409 | `CONFLICT` | 같은 `record_id`인데 화자 값 외 필드가 다르거나 이미 `uploading`이 아님 | 새 `record_id`로 예약 |
| 413 | `FILE_TOO_LARGE`(`field: size_bytes`) | 25 MiB 초과 | 더 작은 파일 |
| 422 | `INVALID_INPUT`(`field`) | 형식·MIME·`size_bytes` 0·`consent_at` 미래·화자 형식 오류 | 값을 고쳐 다시 예약 |
| 503 | `DB_UNAVAILABLE`(`retryable: true`) | Storage가 서명 주소를 발급하지 못함. 기록 행은 이미 만들어져 있음 | 같은 `record_id`로 다시 예약(화자 값 외 필드는 같게. 200과 새 `upload`) |

<a id="a-record-uploaded"></a>

#### `POST /records/{record_id}/uploaded` (제안) — 업로드 완료 알림

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 1 · #9 |
| 내부 호출 | DB `update_record_status(record_id, expected_status='uploading', status='uploaded')` |

이후 `processing`, `ready`, `failed`로의 상태 변경은 서버가 처리하고 앱은 기록 조회로 확인합니다. 텍스트 기록은 서버가 파일 전체를 읽어 파싱하므로 이 호출에 시간이 걸릴 수 있습니다. 앱의 대기 시간은 #96 측정 뒤 정하며 이 계약에서는 정하지 않습니다.

서버는 Storage에 원본이 있고 `size_bytes`와 같은지 확인한 뒤 `uploaded`로 바꿉니다. 원본이 없으면 409 `UPLOAD_MISSING`, 원본은 있지만 크기가 `size_bytes`와 다르면 409 `UPLOAD_SIZE_MISMATCH`입니다(제안). 복구 절차는 두 경우가 다릅니다(아래 '기록 파일 업로드'의 '끊긴 전송·응답 유실'). 이미 `uploaded` 이후 상태인 기록에 다시 호출하면(응답을 받지 못해 재호출한 경우) 200으로 현재 기록을 그대로 돌려줍니다.

처리 흐름은 다음과 같습니다. **텍스트 기록**은 서버가 본문을 처리해 `revision 1`을 만들고 **자동으로 확정**한 뒤 색인과 말투 프로필 분석을 이어서 수행합니다. 자동 확정은 텍스트 기록의 최초 `revision 1`에만 적용하며 화자 확인을 대신하지 않습니다. 카카오톡 기록은 화자 확인이 끝나 `uploaded`가 된 뒤에만 자동 확정·색인·분석을 시작하고, 확인 전 기록은 `reflected`로 표시되지 않습니다. 이후 텍스트 초안(`revision 2` 이상)은 앱이 확정 API로 명시 확정하며, 카카오톡 기록이면 화자 확인이 승계되어야 확정되고 승계되지 않으면 색인·분석을 시작하지 않습니다(아래 '화자 확인의 적용 범위'). **음성 기록**은 STT로 `revision 1` 초안을 만들고, 사용자가 앱에서 확인·수정해 확정(`confirm`)하면 같은 후속을 수행합니다. 반영 진행은 기록 객체의 `reflection_status`로 확인합니다. 텍스트 자동 확정·색인·분석도 저장·임베딩 전에 마스킹(`contracts/backend-ai/api-spec.md`의 '기록 민감정보 마스킹')을 거쳐야 합니다. 마스킹 구현 담당과 진행 조건은 #74에서 정하며, 정해지기 전에는 합성 자료나 공유 권한이 확인된 자료로만 검증합니다.

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| record_id | path | uuid | 필수 | 기록 id. 현재 상태가 `uploading`이어야 함(이미 `uploaded` 이후이면 위 멱등 규칙, `failed`이면 409 `CONFLICT`, 삭제 중(`deleting`)이면 목록·상세와 같이 없는 기록으로 보아 404 `NOT_FOUND`) |
| target_speaker | body | string \| null | 선택 | (제안) 예약 때 화자를 보내지 않았거나 업로드 뒤 검증에서 거절됐을 때 선택값(화자가 1명뿐이면 대화 상대로 확인한 값)을 다시 보냄. 앞뒤 공백 제거 후 1~100자. **키를 생략하면 예약 때 값을 유지**하고, 값을 보내면 예약 때 값을 덮어쓰며(재전송값 우선), **JSON `null`을 명시하면 예약 때 값을 지움**(#124의 "생략은 유지, 명시적 null은 해제"와 같은 규칙). 검증은 이렇게 합친 **최종 값**에 적용함('업로드 뒤 검증'의 '최종 선택값') |
| provider_speaker | body | string \| null | 선택 | (제안) 위와 같음(생략은 유지, 값은 덮어씀, `null`은 해제). 화자가 2명 이상일 때만 값을 보내며 `target_speaker`와 함께 보냄. 화자가 1명뿐이거나 카카오톡이 아닌데 예약 때 보냈다면 `null`로 지움 |

##### 응답 (200) · 기록

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 기록 객체 | 이 장의 '응답 객체' 절에 정의한 기록 객체 |

##### 업로드 뒤 검증

| 항목 | 내용 |
|---|---|
| 내용 형식 | 실제 내용이 `kind`·`mime_type`과 맞는지 확인합니다(텍스트 디코딩, 음성 파일 시그니처). 맞지 않으면 422(`INVALID_INPUT`)이고 기록을 삭제한 뒤 새로 예약합니다 |
| 카카오톡 판정 | 서버가 파일을 파싱해 카카오톡 내보내기 여부와 화자 목록을 판정합니다. 서버 판정이 최종이며 앱의 판정과 다를 수 있습니다 |
| 최종 선택값 | `/uploaded`는 예약 때 값과 이번 본문을 위 규칙(생략은 유지, 값은 교체, `null`은 해제)으로 합친 **최종 값**에 아래 검증을 모두 적용합니다. 순서는 ① 앞뒤 공백 제거·1~100자 ② `provider_speaker`만 있으면 안 됨 ③ 두 값은 서로 달라야 함(동일인을 두 역할로 선택할 수 없음) ④ 서버 판정에 따른 필요 여부와 파일의 화자 목록 포함입니다. ①~③ 위반은 422 `INVALID_INPUT`이며 `field`는 `provider_speaker`만 있음 → `target_speaker`, 두 값이 같음 → `provider_speaker`, 길이 위반 → 그 값의 필드입니다(`speakers`는 없음). ④ 위반은 아래 `SPEAKER_*`입니다. 예: 예약에서 `target=A`, `provider=B`였는데 `provider`를 `A`로 바꾸면 최종 값이 같은 사람이므로 422 `INVALID_INPUT`(`field: provider_speaker`)입니다 |
| 화자 값이 필요한데 없음 | 서버가 카카오톡이라고 판정하면 화자가 1명뿐이어도 `target_speaker`(화자 확인)가 필요합니다. 없으면 422 `SPEAKER_REQUIRED`(`field: target_speaker`)입니다. 화자가 2명 이상이면 `provider_speaker`도 필요하며 없으면 422 `SPEAKER_REQUIRED`(`field: provider_speaker`)입니다. 응답에는 서버가 판정한 화자 목록 `speakers`가 들어 있으며, 앱은 그 목록으로 화자 선택(2명 이상) 또는 화자 확인(1명) 화면을 띄우고 값을 `/uploaded`에 담아 다시 호출합니다 |
| 화자가 파일에 없음 | 두 값(앞뒤 공백 제거 후)이 파일의 화자 목록에 없으면 422 `SPEAKER_NOT_FOUND`(`field: target_speaker` 또는 `provider_speaker`)입니다. 응답의 `speakers`로 다시 선택하게 합니다 |
| 화자 값이 필요 없는데 있음 | 카카오톡이 아니면 화자 값을 보내면 422 `SPEAKER_NOT_ALLOWED`(`field: target_speaker`)입니다(`speakers`는 빈 배열). 화자가 1명뿐인 카카오톡에서 `provider_speaker`를 보내면 422 `SPEAKER_NOT_ALLOWED`(`field: provider_speaker`)입니다(파일에 없는 제공자를 추정하지 않음). 앱은 보낸 값을 `null`로 지워 `/uploaded`를 다시 호출하며 파일을 다시 올리지 않습니다 |
| 화자가 대화 상대가 아님 | 앱은 예약 전에 확인을 받으므로 사용자가 "대화 상대가 아니다"라고 하면 예약하지 않습니다(기록 삭제 불필요). `/uploaded`의 확인 요구(422)는 앱과 서버의 판정이 다를 때만 쓰는 경로입니다(예: 앱은 1명, 서버는 2명 이상으로 판정). 그 경로에서 사용자가 거절하면 앱이 기록을 삭제하고 안내합니다(삭제 API는 #123이며 그 전까지 `uploading` 기록이 남음) |
| 화자 검증 실패 뒤 상태 | 위 최종 선택값·화자 검증(`INVALID_INPUT`의 화자 형식 오류, `SPEAKER_REQUIRED`·`SPEAKER_NOT_FOUND`·`SPEAKER_NOT_ALLOWED`)에서 422이면 기록은 `uploading`으로 남고 올린 파일도 그대로 둡니다. 모든 경우 화자 값만 고쳐(또는 `null`로 지워) `/uploaded`를 다시 호출하며 파일을 다시 올리지 않습니다. '내용 형식'의 422는 이 복구에 해당하지 않고 기록을 삭제한 뒤 새로 예약합니다 |
| 화자 선택 이어가기 | `/uploaded`가 서버 판정에 따른 화자 오류(`SPEAKER_*`)로 422를 내리면 서버는 기록의 `error_code`에 그 사유를 소문자로 저장합니다(`speaker_required`·`speaker_not_found`·`speaker_not_allowed`). 앱을 닫았다가 다시 열어도 기록 목록·상세의 `status: uploading`과 `error_code: speaker_*`로 "화자 확인이 필요한 기록"을 알 수 있고, 앱은 이를 일반 업로드 중과 구분해 '확인 필요'로 표시합니다. 항목을 누르면 앱은 **본문 없이 `/uploaded`를 다시 호출**해 같은 `SPEAKER_*` 422와 서버가 판정한 `speakers`를 받고(서버가 파일을 다시 읽어 판정), 그 목록으로 선택 화면을 띄운 뒤 선택값을 담아 다시 호출합니다. 파일은 다시 올리지 않으며 통과하면 `uploaded`가 되고 `error_code`는 `null`로 지워집니다. 화자 목록을 기록 조회 응답에 넣지는 않습니다. 이 호출은 서버가 원본 파일(최대 25 MiB)을 다시 읽어 판정하므로 사용자가 항목을 탭할 때만 부르고, 응답을 받은 화면에서는 반복 호출하지 않으며 목록 갱신이나 폴링에 쓰지 않습니다. 호출이 드물다는 전제의 임시 선택이므로 실제 응답 시간을 측정한 뒤 화자 목록을 저장해 조회로 주는 방식 등으로 바꿀 수 있습니다. `INVALID_INPUT`의 형식 오류(예: 동일인 중복)는 `error_code`에 저장하지 않으며, 텍스트 버전 `confirm`의 `SPEAKER_*`도 기록의 `error_code`를 바꾸지 않습니다(초안이 `draft`로 남아 있고 다시 `confirm`하면 같은 오류와 `speakers`를 받음) |

##### 오류 응답 (`/uploaded`)

| 상태 | `code` | 상황 | 복구 |
|---|---|---|---|
| 401 | `UNAUTHENTICATED` | 로그인하지 않음·토큰 만료 | 다시 로그인 |
| 404 | `NOT_FOUND` | 없거나 남의 기록, 삭제 중인 기록 | 기록 목록을 새로고침 |
| 409 | `CONFLICT` | 기록이 `failed`여서 업로드를 확인할 수 없음 | 새 `record_id`로 예약 |
| 409 | `UPLOAD_MISSING`(`retryable: true`) | 원본이 없음 | 같은 주소로 다시 `PUT`, 만료·거절이면 재예약 |
| 409 | `UPLOAD_SIZE_MISMATCH`(`retryable: false`) | 원본은 있으나 크기가 `size_bytes`와 다름 | 기록을 삭제하고 새 `record_id`로 다시 예약 |
| 422 | `INVALID_INPUT` | 내용 형식 오류, 최종 선택값의 형식 오류(길이·쌍·동일인) | 내용 형식은 기록 삭제 후 새 예약, 최종 선택값은 값을 고쳐 재호출 |
| 422 | `SPEAKER_REQUIRED`·`SPEAKER_NOT_FOUND`·`SPEAKER_NOT_ALLOWED`(`retryable: true`) | 서버 판정에 따른 화자 오류. 응답에 `speakers`이며 기록의 `error_code`에 `speaker_*`를 저장 | 값을 고치거나 `null`로 지워 재호출. 앱을 닫았다 열었다면 목록의 `error_code`로 알아보고 본문 없이 `/uploaded`를 다시 호출해 `speakers`를 받음 |
| 503 | `DB_UNAVAILABLE`(`retryable: true`) | Storage에서 원본을 확인하지 못함 | 같은 호출을 다시 |

##### 화자 확인의 적용 범위

화자 확인은 카카오톡 내보내기 텍스트 기록에만 있습니다. 음성 기록과 카카오톡이 아닌 텍스트는 전체를 `persona`로 보므로 확인 대상이 아닙니다. 구체적인 DB 저장과 버전 연결은 #121·#112·#113에서 정합니다.

| 항목 | 내용 |
|---|---|
| 적용 대상 | 확인은 **그 기록의 최초 업로드**와 그 파일에서 만든 텍스트 `revision 1`, 그리고 기록 안의 화자(파일에 표시된 문자열)에 연결됩니다. 다른 기록과 다른 대화 상대에는 적용되지 않고, 표시 문자열을 사람·계정을 가리키는 전역 ID로 쓰지 않습니다 |
| 최초 확정 | `/uploaded`에서 위 검증을 통과한 값이 곧 확인입니다. 이 값으로 `revision 1`을 자동 확정하고 색인·분석을 시작합니다 |
| 승계 | 새 초안(`revision 2` 이상)을 확정(`confirm`)할 때 서버가 그 내용에서 화자 목록을 다시 판정합니다. 서버가 판정한 화자 집합이 확인 당시와 같고(각 이름의 앞뒤 공백을 제거한 문자열이 정확히 같은 집합이며 순서와 메시지 수는 무관), 확인한 `target_speaker`(2명 이상이면 `provider_speaker`도)가 그 집합에 있으면 기존 확인을 **승계**하며 앱의 추가 확인은 필요 없습니다. 내용만 다듬는 수정은 이 경우입니다 |
| 무효화·재확인 | 화자 이름을 바꾸거나 화자가 늘거나 줄어 승계 조건이 깨지면 확인이 무효이므로 `confirm`이 422(`SPEAKER_REQUIRED`·`SPEAKER_NOT_FOUND`·`SPEAKER_NOT_ALLOWED`)이고 응답에 서버가 본 `speakers`가 들어 있습니다. 새 초안은 `draft`로 남아 확정되지 않으며 이전 확정본이 계속 현재 버전입니다. 앱은 `confirm` 본문에 화자 값을 담아 다시 호출하거나, 초안을 `PATCH`로 고친 뒤 다시 `confirm`합니다(아래 '전사문 확정') |
| 카카오톡 형식이 사라진 초안 | 최초 업로드가 카카오톡으로 판정된 기록의 새 초안이 카카오톡 형식이 아니게 되면 승계할 수 없고 `confirm`은 422 `SPEAKER_NOT_FOUND`(`speakers`는 빈 배열)입니다. 화자 값을 지워도 통과하지 않으며, 앱은 초안을 카카오톡 형식으로 되돌려야 합니다. 화자 표시를 지워 전체를 `persona`로 만드는 우회를 막기 위한 규칙입니다 |
| 화자가 3명 이상 | 대화 상대(`target_speaker`)와 제공자(`provider_speaker`) 두 사람만 고르고, 나머지 화자는 `other`(문맥 전용)로 봅니다(`contracts/backend-ai/api-spec.md`의 화자 역할). 승계의 화자 집합에는 모든 화자가 포함됩니다 |
| 유효한 연결이 없을 때 | 확인이 승계되지 않은 텍스트 버전은 확정되지 않으므로 색인·말투 프로필 분석을 시작하지 않고 `reflected`로 표시되지 않습니다 |
| 초안 수정 중 | 초안을 만들거나 수정하는 동안에는 화자 확인을 검사하지 않고, 확정할 때 검사합니다 |

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

##### 상태 확인 주기

처리 중인 기록(`uploading`·`uploaded`·`processing`, `reflection_status=pending`)이 있는 동안 앱은 목록 화면이 보이는 동안만 5초 간격으로 조회하고, 조회가 실패하면 간격을 2배씩(최대 8배, 40초) 늘립니다(`mobile`의 현재 구현). 앱에는 별도의 총 대기 상한을 두지 않습니다. 서버가 `pending`을 언제까지 유지하고 언제 실패로 바꿀지는 정해지지 않았으며 #10·#112에서 정합니다. `status: uploading`이면서 `error_code`가 `speaker_*`인 기록은 사용자의 화자 선택을 기다리는 것이므로 처리 중 확인(폴링)의 대상으로 보지 않아도 됩니다.

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

확정본은 직접 고치지 않고 새 `revision`의 초안을 만들어 수정합니다. 텍스트 기록도 같은 방식으로 수정하며, 새 초안은 확정 API로 확정해야 개인화에 반영됩니다. 카카오톡 기록의 새 초안은 화자 확인이 승계되어야 확정됩니다(위 '화자 확인의 적용 범위').

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

이 API는 사용자가 전사문 초안을 확정할 때 앱이 호출합니다. 대상은 **모든 음성 기록의 초안**(`revision 1` 이상)과 **텍스트 기록의 초안 중 `revision 2` 이상**입니다. 텍스트 기록의 최초 `revision 1`은 서버가 자동으로 확정하므로 앱이 호출하지 않습니다. 이미 확정된 버전에 다시 호출하면 409 `CONFLICT`입니다. 카카오톡 기록의 초안은 확정할 때 서버가 내용에서 화자를 다시 판정해 기존 화자 확인이 승계되는지 검사하며, 승계되지 않으면 422 `SPEAKER_*`와 서버가 본 `speakers`를 돌려주고 초안을 확정하지 않습니다. 확정 뒤 색인과 프로필 분석은 서버가 이어서 수행하고, 진행은 기록의 `reflection_status`와 말투 프로필 조회의 분석 상태로 확인합니다.

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| text_version_id | path | uuid | 필수 | 확정할 초안 |
| target_speaker | body | string \| null | 선택 | (제안) 카카오톡 기록에서 화자 확인이 승계되지 않아 다시 선택할 때만 보냄. 생략은 기존 확인 유지, 값은 교체, `null`은 해제(`/uploaded`와 같은 규칙). 검증은 이렇게 합친 최종 값에 '최종 선택값'과 같이 적용하되 서버 목록은 이 초안의 내용에서 판정함 |
| provider_speaker | body | string \| null | 선택 | (제안) 위와 같음. 화자가 2명 이상일 때만 값을 보냄 |

##### 응답 (200) · 텍스트 버전

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 텍스트 버전 객체 | 이 장의 '응답 객체' 절에 정의한 텍스트 버전 객체 |

##### 오류 응답 (확정)

| 상태 | `code` | 상황 | 복구 |
|---|---|---|---|
| 401 | `UNAUTHENTICATED` | 로그인하지 않음·토큰 만료 | 다시 로그인 |
| 404 | `NOT_FOUND` | 없거나 남의 텍스트 버전 | 기록을 새로고침 |
| 409 | `CONFLICT` | 이미 확정된 버전이거나 `draft`가 아님 | 최신 버전을 조회 |
| 422 | `SPEAKER_REQUIRED`·`SPEAKER_NOT_FOUND`·`SPEAKER_NOT_ALLOWED`(`retryable: true`) | 카카오톡 기록의 화자 확인이 승계되지 않음. 응답에 `speakers` | 초안은 `draft`로 남음. 화자 값을 담아 다시 `confirm`하거나 `PATCH`로 고친 뒤 다시 `confirm` |
| 422 | `INVALID_INPUT` | 최종 선택값의 형식 오류(길이·쌍·동일인) | 값을 고쳐 다시 `confirm` |

<a id="a-profile"></a>

#### `GET /personas/{persona_id}/style-profile` (제안) — 현재 말투 프로필·분석 상태 조회

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 1 · #10, #113 |
| 내부 호출 | DB `get_current_style_profile(persona_id)` (`ready` 프로필만) + 분석 작업 상태(DB 담당과 확정) |

기록을 확정하면 서버가 색인과 프로필 분석을 자동으로 이어서 수행합니다. 앱은 이 API로 결과와 진행 상태를 확인합니다.

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| persona_id | path | uuid | 필수 | 대화 상대 id |

##### 응답 (200)

| 필드 | 형식 | 설명 |
|---|---|---|
| profile | 말투 프로필 객체 \| null | `ready` 프로필이 없으면(없음·생성 중·`stale`) `null` |
| profile_state | string | (제안) `none`(프로필 없음) · `building`(분석 진행 중) · `ready` · `stale`(자료가 바뀌어 다시 분석해야 함. 이 프로필은 개인화에 쓰지 않으므로 `profile`은 `null`). 앱이 "재분석 필요"를 표시하는 근거 |
| analysis | 분석 상태 객체 \| null | 진행 중이거나 마지막 분석 작업의 상태. 분석한 적이 없으면 `null` |

##### 분석 상태 객체

| 필드 | 형식 | 설명 |
|---|---|---|
| operation_id | uuid | 분석 작업 id |
| attempt | integer | (제안) 같은 자료에 대한 시도 번호(1부터). 서버가 올리며 앱은 보내지 않음 |
| status | string | `running` · `succeeded` · `failed` · `unknown` (게시 결과를 알 수 없음) |
| error_code | string \| null | 실패 시 안전한 코드(예: `insufficient_input`, `source_changed`, `ai_failed`). 질문·기록·예외 원문은 넣지 않음 |
| started_at, updated_at | datetime | 시작·갱신 시각 |

실패나 `unknown` 뒤에 서버가 분석을 자동으로 다시 시작하지 않습니다. 사용자가 다시 요청할 때만 아래 API로 시작합니다.

<a id="a-analysis"></a>

#### `POST /personas/{persona_id}/style-profile/analysis` (제안) — 말투 프로필 분석 시작·재요청

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 1 · #113 |
| 내부 호출 | 분석 작업 생성(DB 담당과 확정) → AI 분석·통합 호출 → `publish_style_profile` |

기록을 확정하면 서버가 분석을 자동으로 시작하므로 앱이 처음에 호출할 필요는 없습니다. 이 API는 실패·`unknown`·자료 개정 충돌 뒤에 사용자가 다시 요청할 때 씁니다. 색인이 실패한 기록(`reflection_status=failed`)도 이 API로 다시 요청하면 서버가 색인부터 다시 시작합니다. 색인·프로필의 세부 하위 상태는 #112에서 정합니다.

##### 요청 (JSON)

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| persona_id | path | uuid | 필수 | 대화 상대 id |
| operation_id | body | uuid | 선택 | 앱이 미리 만든 작업 id. 같은 id로 다시 보내면(응답 유실 등) 새 시도 없이 그 작업의 현재 상태를 돌려줌. 실패한 작업을 사용자가 다시 요청하는 수동 재시도에서는 이 값을 비우거나 새 값으로 보냄 |

##### 응답

| 상태 | 설명 |
|---|---|
| 202 | 새 분석 작업을 시작함. 본문은 분석 상태 객체 |
| 200 | 새 작업을 만들지 않고 기존 작업의 분석 상태 객체를 돌려줌(아래 '작업 식별과 재시도 규칙') |
| 404 | 없거나 남의 대화 상대 |
| 422 | 확정된 기록이 하나도 없음 (`INVALID_INPUT`) |

##### 작업 식별과 재시도 규칙

| 상황 | 처리 |
|---|---|
| 같은 `operation_id`로 다시 요청(응답 유실 등) | 새 작업·시도 없이 그 작업의 현재 분석 상태를 200으로 돌려줌. 이미 `succeeded`여도 같음 |
| 다른 요청인데 `running` 작업이 있음 | 새로 만들지 않고 그 작업의 상태를 200으로 돌려줌 |
| 마지막 작업이 `failed`이고 자료(`data_revision`)가 같음 | 사용자 수동 재시도. 서버가 같은 작업의 `attempt`를 1 올려 새 시도를 시작하고 202 |
| 마지막 작업이 `unknown` | 서버가 먼저 저장된 게시 결과를 확인. 게시됐으면 `succeeded`로 200, 새 시도 없음. 확인되지 않으면 새 시도를 만들지 않고 `unknown` 그대로 200(확인 대기). 이전 시도의 미게시·실패가 확인된 뒤에만 새 `attempt`를 시작 |
| 마지막 작업이 `succeeded`이거나 자료 `data_revision`이 바뀜 | 새 작업(`attempt` 1)을 시작하고 202. 이전 작업의 게시 권한은 폐기 |

`attempt`는 백엔드가 올리고 앱은 보내지 않습니다. 서버가 분석을 자동으로 다시 시작하지 않습니다. 백엔드 내부 규칙은 `contracts/backend-ai/api-spec.md`의 분석 작업·시도 설명을 따릅니다.

<a id="a-record-del"></a>

#### `DELETE /records/{record_id}` (제안) — 기록 삭제 요청

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 2 · #11, #123 |
| 내부 호출 | DB `request_record_deletion(record_id)` → 기록이 즉시 `deleting`으로 바뀌어 조회에서 사라지고 삭제 작업 생성 |

삭제 작업을 만든 것이지 파일 삭제가 끝난 것이 아닙니다. 같은 기록에 다시 요청하면 진행 중이거나 실패한 같은 작업을 돌려줍니다(202). 앱은 `status = completed`이고 `remaining_steps`가 비었을 때만 성공으로 표시합니다. 처리 규칙과 재요청 규칙은 '삭제 작업의 상태와 처리 규칙'을 따릅니다.

##### 삭제 범위와 남는 것

| 항목 | 내용 |
|---|---|
| 지워지는 것 | 원본 파일(Storage), 전사문(모든 `revision`), 검색 조각과 벡터, 이 기록에 의존하는 프로필 버전과 그 연결(아래 '말투 프로필') |
| 남는 것 | 과거 대화와 AI 답변(대화방·턴)은 지우지 않습니다(2026-11-23 결정). 삭제한 기록은 이후 검색·분석 입력에서 제외됩니다 |
| 말투 프로필 | 이 기록의 확정본을 출처로 가진 프로필 버전은 삭제 요청 즉시 최신이 아닌 것으로 처리되어 프롬프트에 쓰이지 않습니다(대화 상대의 `data_revision`이 오름). 즉시 쓰지 않는 것과 물리적 삭제는 별개이므로, `database` 단계가 출처 연결이 사라지기 전에 그 프로필 버전을 식별해 본문(`content`)까지 **지웁니다**. 이 기록과 무관한 프로필 버전이나 삭제 요청 뒤 새로 만들어져 이 기록을 쓰지 않은 버전은 지우지 않습니다. 이 정리까지 끝나야 `database` 단계가 끝난 것입니다. 다음 분석(`POST /personas/{persona_id}/style-profile/analysis`)에서 남은 확정 기록으로 다시 만들고, 남은 확정 기록이 없으면 프로필도 없습니다. 과거 대화 답변은 지우지 않습니다(제안, #113·DB 확인) |
| 삭제 중인 기록의 접근 | `deleting` 기록은 없는 기록과 같은 404입니다. `/uploaded`·텍스트 버전·확정처럼 그 기록의 id를 받는 API도 404입니다(`/uploaded`는 이전 계약의 409에서 바뀜). **삭제 요청(`DELETE`)은 예외**로, 진행 중이거나 실패한 같은 작업을 202로 돌려줍니다 |
| 업로드 취소 | 앱이 업로드를 취소할 때 예약만 있는 기록을 정리하는 데도 같은 요청을 씁니다 |

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻으며 앱이 보낸 `user_id`는 받지 않음 |
| record_id | path | uuid | 필수 | 기록 id |

##### 응답 (202) · 삭제 작업

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 삭제 작업 객체 | 이 장의 '응답 객체' 절에 정의한 삭제 작업 객체(`target_type = record`) |

##### 오류 응답

| 상태 | `code` | 상황 | 복구 |
|---|---|---|---|
| 401 | `UNAUTHENTICATED` | 로그인하지 않음·토큰 만료 | 다시 로그인 |
| 404 | `NOT_FOUND` | 없거나 남의 기록. 삭제가 이미 끝나 사라진 기록도 같음(진행 중인 삭제는 404가 아니라 같은 작업 202) | 목록을 새로고침(앱은 사라진 기록으로 처리하고 이전 작업 id로 상태를 확인) |
| 409 | `ACCOUNT_DELETING` | 계정 삭제 작업이 `completed`가 되기 전(`failed` 포함) | 계정 삭제 작업을 확인 |
| 503 | `DB_UNAVAILABLE`(`retryable: true`) | DB에 일시적으로 연결하지 못함 | 같은 요청을 다시(같은 작업을 돌려줌) |

<a id="a-persona-del"></a>

#### `DELETE /personas/{persona_id}` (제안) — 대화 상대 삭제 요청

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 2 · #123 |
| 내부 호출 | DB `request_persona_deletion(persona_id)`(제안, backend-db 문서의 '삭제 추가안') → 대화 상대와 그 기록이 즉시 `deleting`으로 바뀌어 조회에서 사라지고 삭제 작업 생성 |

기록 삭제와 같은 방식(202, 삭제 작업, 같은 대상에 다시 요청하면 같은 작업)입니다. 대화 상대 자체가 사라지므로 범위가 더 넓습니다. 이 API와 이력 삭제 범위는 #123의 확정 정책에 없는 제안이며 팀 합의가 필요합니다.

| 항목 | 내용 |
|---|---|
| 지워지는 것 | 이 대화 상대의 모든 기록(원본·전사문·조각·프로필 연결), 말투 프로필, 대화방과 턴(이 대화 상대와 나눈 대화 이력). 기록 삭제 때의 "과거 대화 보존"은 적용하지 않습니다(제안: 대화 이력까지 삭제, 팀 합의 필요) |
| 남는 것 | 계정과 다른 대화 상대의 자료 |
| 요청 직후 | 대화 상대(`status = deleting`)와 그 기록·대화방·프로필은 목록·상세에서 숨겨지고 이 대화 상대의 id를 받는 API는 404입니다. 새 대화·기록 생성도 404입니다. **삭제 요청(`DELETE`)은 예외**로 진행 중이거나 실패한 같은 작업을 202로 돌려줍니다 |
| 처리 | `storage` → `external_index` → `adapter` → `database`(기록 행은 backend-db 문서의 '상위 작업과 기록 삭제 보호'에 따라 삭제) |

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>` |
| persona_id | path | uuid | 필수 | 대화 상대 id |

##### 응답 (202) · 삭제 작업

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 삭제 작업 객체 | `target_type = persona` |

##### 오류 응답

기록 삭제 요청과 같습니다(401, 404, 409 `ACCOUNT_DELETING`, 503). 404는 없거나 남의 대화 상대이거나 삭제가 이미 끝난 경우입니다.

<a id="a-jobs"></a>

#### `GET /deletion-jobs · /deletion-jobs/{job_id}` (제안) — 삭제 진행·부분 실패 확인

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 2 · #11, #123 |
| 내부 호출 | DB `list_deletion_jobs(limit, offset)` · `get_deletion_job(job_id)` |

삭제 대상 파일 경로 목록(`manifest`)과 소유 토큰은 앱에 보내지 않습니다. 앱의 조회 방식은 '삭제 작업의 상태와 처리 규칙'의 '앱의 조회와 복원'을 따릅니다. 계정 삭제 작업은 로그인 계정이 사라진 뒤에는 조회할 수 없습니다(아래 `DELETE /account`).

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

##### 오류 응답

| 상태 | `code` | 상황 | 복구 |
|---|---|---|---|
| 401 | `UNAUTHENTICATED` | 로그인하지 않음·토큰 만료 | 다시 로그인 |
| 401 | `ACCOUNT_DELETED` | 계정 삭제 작업이 `completed`이고 로그인 계정이 사라짐 | 완료로 처리하고 로그인 화면으로(아래 `DELETE /account`) |
| 401 | `ACCOUNT_DELETION_INCOMPLETE` | 로그인 계정은 사라졌지만 계정 삭제 작업이 아직 `completed`가 아님 | 완료로 처리하지 않고 '마무리하는 중'으로 안내, 같은 토큰으로 다시 확인(아래 `DELETE /account`) |
| 404 | `NOT_FOUND` | 상세: 없거나 남의 작업 | 목록을 새로고침 |
| 503 | `DB_UNAVAILABLE`(`retryable: true`) | DB에 일시적으로 연결하지 못함 | 같은 요청을 다시 |

<a id="a-job-retry"></a>

#### `POST /deletion-jobs/{job_id}/retry` (제안) — 실패한 삭제 작업 다시 시작

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 2 · #123 |
| 내부 호출 | DB `retry_deletion_job(job_id)`(제안, backend-db 문서의 '삭제 추가안') |

`failed`인 작업을 같은 작업 id로 `pending`으로 되돌려 바로 다시 시도하게 합니다. 새 작업을 만들지 않고 `remaining_steps`도 그대로 두며, 자동 재시도 횟수는 0으로 초기화되고(새 예산), 새 시도 번호는 worker가 작업을 집을 때 올라갑니다.

| 작업 상태 | 응답 |
|---|---|
| `failed` | 202와 `pending`으로 바뀐 작업 객체 |
| `pending`·`running` | 200과 현재 작업 객체(이미 처리 중이므로 아무것도 바꾸지 않음. 다시 보내도 안전) |
| `completed` | 200과 현재 작업 객체(409가 아님. 자동 재시도가 먼저 끝낸 작업에 사용자가 재시도를 눌러도 앱이 응답을 그대로 완료로 처리) |
| 흡수된 작업(`parent_job_id`가 있음) | 200과 현재 작업 객체(아무것도 바꾸지 않음. 상위 작업이 처리하며 단독으로 집지 않으므로 앱은 이 작업의 다시 시도 버튼을 보이지 않고 상위 작업을 확인) |

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>` |
| job_id | path | uuid | 필수 | 삭제 작업 id |

##### 응답 · 삭제 작업

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 삭제 작업 객체 | 위 표의 상태별 응답(202 또는 200) |

##### 오류 응답

| 상태 | `code` | 상황 | 복구 |
|---|---|---|---|
| 401 | `UNAUTHENTICATED` | 로그인하지 않음·토큰 만료 | 다시 로그인 |
| 404 | `NOT_FOUND` | 없거나 남의 작업 | 목록을 새로고침 |
| 503 | `DB_UNAVAILABLE`(`retryable: true`) | DB에 일시적으로 연결하지 못함 | 같은 요청을 다시 |

<a id="a-account-del"></a>

#### `DELETE /account` (제안) — 계정 전체 삭제 요청

| 항목 | 내용 |
|---|---|
| 인증 | 필수 |
| 단계·이슈 | 고도화 2 · #123 |
| 내부 호출 | DB `request_account_deletion()`(제안, backend-db 문서의 '삭제 추가안') → 삭제 작업 생성. 이후 삭제 worker가 Storage·DB·Supabase Auth 순서로 처리 |

로그인 계정과 앱 데이터 전체를 지우는 요청입니다. 개별 기록 삭제의 "과거 대화 보존"은 적용하지 않으며 대화 이력도 모두 지웁니다(2026-11-23 결정). 이 요청은 되돌릴 수 없고, 앱은 사용자가 범위를 확인하고 되돌릴 수 없다는 것을 이해했다고 체크한 뒤에만 보냅니다. 요청 본문은 없습니다.

| 항목 | 내용 |
|---|---|
| 지워지는 것 | 모든 대화 상대와 그 기록(원본·전사문·조각·프로필), 대화방·턴(대화 이력), 이 계정의 앱 데이터 전부, Supabase 로그인 계정(`auth`) |
| 요청 직후 | 모든 대화 상대와 기록이 `deleting`이 되어 조회에서 숨겨집니다. 새 대화 상대·기록·대화 생성과 수정은 409 `ACCOUNT_DELETING`으로 거절하고, 삭제 작업 조회·다시 시작·같은 `DELETE /account`만 계속 쓸 수 있습니다. 작업이 `completed`가 되기 전에는 `failed`여도 같습니다 |
| 같은 계정에 다시 요청 | 진행 중이거나 실패한 같은 작업을 돌려줍니다(202) |
| 처리 | `storage` → `external_index` → `adapter` → `database` → `auth`. `storage` 단계는 기록의 원본 경로와 흡수한 하위 작업이 남긴 경로를 합친 `manifest`에 경로가 하나라도 있을 때 둡니다. 기록 행이 없어도 흡수한 경로가 남아 있으면 `storage`가 먼저 처리하고, 경로가 하나도 없을 때만 `database`부터 진행합니다. `auth`(Supabase Auth 사용자 삭제)는 다른 단계가 모두 끝난 뒤 마지막에만 실행합니다 |
| 앞 단계가 실패하면 | 로그인 계정이 남아 있는 동안에는 자동 재시도 대기(`pending`) 또는 `failed`로 조회되고(로그인 계정이 이미 사라진 작업은 아래 '앞 단계에서 로그인 계정이 먼저 사라진 경우'), `failed`는 `POST /deletion-jobs/{job_id}/retry`로 이어서 처리합니다. 부분 실패를 완료로 표시하지 않습니다 |
| 이미 열린 개별 삭제 작업 | 계정 삭제를 요청하면 그 계정의 열린(`pending`·`running`·`failed`) 기록 삭제 작업은 이 계정 삭제 작업이 **흡수**합니다(제안, 대화 상대 삭제도 같음). 이미 `persona` 작업에 흡수된 기록 작업은 계정 작업을 직접 가리키도록 다시 묶고 `persona` 작업 자신도 흡수합니다(상위는 항상 계정 작업 하나). 하위 작업이 남긴 원본 경로는 상위 `manifest`에 합쳐 계정 작업의 `storage` 단계가 지우며, 하위 작업의 `manifest`는 그 `storage`와 `database`가 끝나기 전에는 비우지 않습니다. 하위 작업은 이 작업의 `database` 단계가 성공할 때 같은 트랜잭션에서(다시 묶은 작업 포함, `parent_job_id`가 이 계정 작업인 모든 작업) `completed`로 닫히고 `manifest`가 비워집니다. 열린 개별 작업 때문에 계정 삭제가 거절되는 일은 없습니다 |

##### 완료 판정 (인증 계정이 사라지므로)

마지막 단계에서 로그인 계정이 지워지면 같은 토큰으로는 `GET /deletion-jobs`가 더 이상 되지 않고 작업의 `user_id`도 `null`이 됩니다. 이때 앱이 완료를 알 수 있도록 서버가 전용 신호를 돌려줍니다(제안). **완료는 로그인 계정이 사라졌다는 뜻이 아니라 계정 삭제 작업이 `completed`**(모든 단계와 `manifest` 비우기까지 끝남)라는 뜻입니다. 로그인 계정이 사라졌지만 작업이 끝나지 않았다면 완료 신호를 보내지 않습니다.

| 서버가 본 것 | 응답 |
|---|---|
| 토큰 확인(`/auth/v1/user`)이 삭제된 사용자(`user_not_found`)를 돌려주고, 토큰의 `sub`와 같은 대상의 계정 삭제 작업(`target_type = account`)이 `completed` | 401 `ACCOUNT_DELETED` |
| 같은 조건이지만 그 작업이 아직 `completed`가 아님(Auth 삭제 직후 작업 정리 전, 또는 Storage 정리가 끝난 뒤 `external_index`·`adapter`·`database` 단계 중에 Auth 사용자만 먼저 지워진 경우) | 401 `ACCOUNT_DELETION_INCOMPLETE` |
| 같은 조건이지만 `sub`의 계정 삭제 작업이 없음(예: 관리자가 직접 삭제) | 일반 401 `UNAUTHENTICATED` |

`sub`는 작업을 찾는 키로만 쓰고 권한 판단에는 쓰지 않습니다.

| 앱이 본 것 | 판정 |
|---|---|
| 조회가 200이고 `status = failed` | 실패. 사용자에게 알리고 다시 시작 요청 |
| 조회가 200이고 `pending`(자동 재시도 대기 포함)·`running` | 계속 확인 |
| 401 `ACCOUNT_DELETED` | **완료**. 일반 401 처리(로그인 만료 안내와 세션 무효화)보다 먼저 이 코드만 따로 분기해 '계정 삭제 완료'를 안내하고 로컬 정리를 합니다 |
| 401 `ACCOUNT_DELETION_INCOMPLETE` | **완료 아님**. 일반 401보다 먼저 분기해 '계정 삭제를 마무리하는 중'으로 보여 주고 같은 토큰으로 다시 확인합니다. 로컬 정리와 로그인 화면 이동은 하지 않습니다 |
| 일반 401 `UNAUTHENTICATED` | 로그인 만료이며 완료가 아닙니다. 계정이 남아 있는 경우에만 다시 로그인한 뒤 작업을 조회합니다. 계정 삭제를 요청한 뒤 토큰이 만료되어 이 응답을 받았다면 완료 여부를 알 수 없으므로(아래 설명) 완료로도 실패로도 단정하지 않습니다 |

앱이 같은 토큰으로 확인할 수 있는 것은 access token이 만료되기 전까지뿐입니다. 그 안에 `completed`가 되지 않으면 앱은 완료를 확인하지 못한 채 일반 401로 끝납니다. 이 경우에도 서버가 작업을 끝까지 처리하므로(아래 '마무리의 재시도') 앱은 완료를 단정하지 않고 '삭제를 처리하는 중일 수 있다'고 안내할 수 있습니다. 계정이 지워졌는데 삭제 작업이 없는 경우(예: 관리자가 직접 삭제)는 일반 401이고, 작업이 있는데 아직 `completed`가 아니면(예: Storage 정리 뒤 삭제가 진행 중인데 관리자가 Auth 사용자만 먼저 지움) `ACCOUNT_DELETION_INCOMPLETE`입니다. 어느 쪽도 성공으로 표시하지 않으며, 앱이 갱신 실패 같은 간접 증거로 성공을 추정하지 않습니다.

##### Auth 삭제 뒤 정리와 복구

| 항목 | 내용 |
|---|---|
| 정리 | worker는 Auth 사용자를 지운 뒤(이미 없으면 성공으로 봄) `auth` 단계를 끝내 작업을 `completed`로 바꾸고 `finished_at`을 채우며 `manifest`를 비웁니다. 서버는 이 시점부터 `ACCOUNT_DELETED`를 보냅니다 |
| Auth 삭제 뒤 중단 | Auth 삭제와 작업 정리 사이에 서버가 멈추면 `remaining_steps = ["auth"]`가 남아 있으므로, 소유 토큰이 만료된 뒤(5분) worker가 다시 집습니다. Auth 사용자 삭제는 이미 없으면 성공인 멱등 호출이라 정리만 이어서 끝냅니다. 작업은 작업 id로 처리하므로 `user_id`가 `null`이 되어도 영향이 없습니다 |
| 앱이 보는 상태 | 이 사이에 같은 토큰의 요청은 `ACCOUNT_DELETION_INCOMPLETE`를 받고, `completed`가 된 뒤에야 `ACCOUNT_DELETED`를 받습니다 |
| 마무리의 재시도 | 로그인 계정이 이미 지워진 작업(소유자 `user_id`가 `null`이고 `completed`가 아닌 '고아 작업')은 사용자가 인증도 retry도 할 수 없으므로 서버가 스스로 끝까지 처리합니다. 단계가 실패해도 자동 재시도 횟수를 쓰지 않고 `failed`로 멈추지 않으며 `pending`과 `next_attempt_at`(최대 30분 간격)으로 계속 다시 시도합니다. 감지와 복구 규칙은 backend-db의 '로그인 계정이 먼저 사라진 작업(고아 작업)'을 따르며, 운영자가 사용자 없이 작업 id로 다시 시작하는 호출(`retry_deletion_job`)은 보조 수단입니다. 로그인 계정이 남아 있는 동안의 실패(`storage`~`database`, Auth 사용자 삭제 호출)는 기존 규칙(자동 재시도 3번 뒤 `failed`, 사용자 retry)을 따릅니다 |

##### 앞 단계에서 로그인 계정이 먼저 사라진 경우

| 항목 | 내용 |
|---|---|
| 일어날 수 있는 경우 | 지울 Storage 원본이 이미 지워진 뒤(`storage` 단계 완료 후)이거나 지울 원본이 없는 사용자입니다. 기록이 남은 채 `storage`가 끝나기 전에 Auth 사용자만 지우려 하면 DB의 기록 삭제 보호가 막아 Auth 삭제가 거절되므로(DB 담당 확인 필요) 원본 파일이 고아로 남는 조기 소실은 생기지 않는 것으로 봅니다 |
| 감지 | 서버(삭제 worker)가 `user_id`가 `null`이고 `completed`가 아닌 작업을 고아 작업으로 봅니다. 이미 `failed`로 멈춘 작업도 포함합니다. Auth 서버를 따로 호출하지 않고 작업 행만 봅니다 |
| 복구 | 같은 worker가 고아 작업을 다시 집어 `remaining_steps`부터 끝냅니다. 앱이나 사용자의 요청은 필요 없습니다. 남은 단계는 이미 없는 행·객체를 성공으로 보는 멱등 처리입니다 |
| 대상 식별 | `user_id`가 비어 있으므로 작업 id와 `target_id`(계정 작업은 삭제된 사용자의 id), 작업의 `manifest`로 처리합니다 |
| 응답 | 완료로 앞당기지 않습니다. 작업이 있으면 `ACCOUNT_DELETION_INCOMPLETE`, `completed`가 된 뒤에야 `ACCOUNT_DELETED`입니다 |
| 작업이 없을 때 | 정리할 `manifest`가 없으므로 복구 대상이 아니고 서버는 일반 401로 응답합니다(운영 점검 범위) |

> 확인 필요(DB 담당 실측, #25 검증 한계): 삭제된 사용자의 토큰으로 `/auth/v1/user`를 부르면 `user_not_found`로 구별되는지, 이미 지운 사용자를 다시 지우는 Auth Admin 호출이 성공으로 취급되는지, 완료 뒤 작업 행에 개인 경로(`manifest`)가 남지 않는지, 흡수한 하위 작업이 상위 작업의 `database` 단계 끝에 함께 닫히는지, Auth 사용자 삭제의 cascade가 기록 삭제 보호(`guard_record_delete`)를 어떻게 지나는지(기록이 남은 채 Storage 정리 전이면 거절되고 정리 뒤에는 통과하는지), `user_id`가 `on delete set null`로 비는 시점. 확인되기 전까지 위 완료 판정과 복구 규칙은 제안입니다.

##### 요청

| 이름 | 위치 | 형식 | 필수 | 설명·제약 |
|---|---|---|---|---|
| Authorization | header | string | 필수 | `Bearer <Supabase JWT>`. 사용자 id는 서버가 토큰에서만 얻음 |

##### 응답 (202) · 삭제 작업

| 필드 | 형식 | 설명 |
|---|---|---|
| (본문) | 삭제 작업 객체 | `target_type = account`, `target_id`는 로그인한 사용자의 id |

##### 오류 응답

| 상태 | `code` | 상황 | 복구 |
|---|---|---|---|
| 401 | `UNAUTHENTICATED` | 로그인하지 않음·토큰 만료 | 다시 로그인 |
| 401 | `ACCOUNT_DELETED` | 계정 삭제 작업이 `completed`이고 로그인 계정이 사라짐 | 완료로 처리 |
| 401 | `ACCOUNT_DELETION_INCOMPLETE` | 로그인 계정은 사라졌지만 계정 삭제 작업이 아직 `completed`가 아님 | 완료로 처리하지 않고 같은 토큰으로 다시 확인 |
| 503 | `DB_UNAVAILABLE`(`retryable: true`) | DB에 일시적으로 연결하지 못함 | 같은 요청을 다시(같은 작업을 돌려줌) |

### 기록 파일 업로드

<a id="a-record-upload"></a>

#### 기록 파일 업로드 (제안) — 원본 파일을 private Storage에 올리는 방법

| 항목 | 내용 |
|---|---|
| 인증 | 서명 업로드 주소가 인증을 대신함. 앱은 Storage 요청에 `Authorization` 헤더를 보내지 않음 |
| 단계·이슈 | 고도화 1 · #9 |

##### 정해진 것과 정해지지 않은 것

| 항목 | 내용 |
|---|---|
| 저장 위치 | Supabase Storage private 버킷 `replica-records` (아직 생성되지 않음) |
| 저장 경로 | `{user_id}/{record_id}/original`. 서버가 `records` 예약 행을 먼저 만든 뒤에만 업로드 가능, 원본 덮어쓰기 불가 |
| 지원 형식 | `.txt`, `.wav`, `.mp3`, `.m4a` (이슈 #9). 지금 Mock 서버의 음성 허용 목록은 더 넓음 |
| 용량 제한 | 25 MiB(26,214,400바이트). 예약할 때 `size_bytes`로 검사 |
| 업로드 주체 | 앱이 Storage로 직접 올림(백엔드를 거치지 않음). 서버는 `POST /records` 응답의 `upload`로 서명 정보를 줌 |
| 요청·응답 규격 | `POST /records` 응답의 `upload`(`url`, `method`, `headers`, `expires_at`), 완료는 `POST /records/{record_id}/uploaded` |
| 끊긴 전송·응답 유실 | 앱은 먼저 `POST /records/{record_id}/uploaded`를 호출합니다. 200이면 올라간 것이므로 끝입니다. 409 `UPLOAD_MISSING`이면 원본이 없는 것이므로 같은 서명 주소로 `PUT`을 다시 보내고, 서명이 만료됐거나 같은 주소가 거절되면 같은 `record_id`로 재예약해 새 `upload`를 받습니다. 같은 주소를 다시 쓸 수 있는지는 실제 Storage로 확인하기 전이라 정하지 않았습니다. 409 `UPLOAD_SIZE_MISMATCH`이면 크기가 다른 객체가 이미 저장된 것입니다. 원본은 덮어쓸 수 없어 같은 `record_id`로는 복구되지 않으므로, 앱은 기록을 삭제(`DELETE /records/{record_id}`)한 뒤 새 `record_id`로 다시 예약합니다. 기기의 원본 크기가 예약 때 크기와 같으면 같은 파일·동의·화자 선택으로 다시 올리고, 다르면 파일이 바뀐 것이므로 사용자에게 다시 선택하게 합니다. 크기가 다른 객체가 남은 기록의 삭제와 원본 객체 정리는 삭제 작업(#11과 후속 구현 이슈)에서 정합니다. 완료 호출의 응답을 잃어도 같은 호출을 반복하면 되며 이미 처리된 기록은 200입니다 |
| Storage 오류 | Storage가 돌려주는 서명 만료·거절 오류는 백엔드 오류 봉투가 아니며, 앱은 백엔드 401(로그아웃)과 구분해 재예약으로 처리합니다. Storage의 상태 코드와 본문은 실제 호출로 확인한 뒤 적습니다(미확인) |
| 마스킹·원본 보관 범위 | 미정 (이슈 #74) |

<a id="s2"></a>

## 2. 앱 관련 오류에 대해 백엔드가 보내는 오류 메시지

### 오류 응답 본문 (구현)

백엔드가 직접 만드는 오류는 모두 아래 형식입니다. 앱이 단계와 재시도 가능 여부를 알 수 있습니다.

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
| error.speakers | string[] | (제안) 화자 오류(`SPEAKER_REQUIRED`·`SPEAKER_NOT_FOUND`·`SPEAKER_NOT_ALLOWED`)에서만 들어 있는 선택 필드. 서버가 파일에서 판정한 화자 이름 목록(메시지가 많은 순, 같으면 파일에 먼저 나온 순, 개수 상한 없음)이며 카카오톡이 아니면 빈 배열입니다. 다른 오류에는 이 키가 없습니다. 같은 사용자의 파일 안 이름이므로 다른 사용자에게 노출되지 않습니다 |

프레임워크가 만드는 오류도 같은 형식입니다 (#67). 존재하지 않는 경로(404 `NOT_FOUND`), 허용되지 않는 메서드(405 `METHOD_NOT_ALLOWED`, `Allow` 헤더 유지), 요청 본문 크기 초과(413), 깨진 multipart 등 본문을 해석하지 못한 경우(400 `INVALID_INPUT`)는 `detail` 대신 `error` 본문으로 응답합니다. 오류 본문에는 입력 내용이나 서버 내부 정보를 넣지 않습니다. 요청 본문이 JSON이 아닐 때의 `INVALID_INPUT`은 `field`에 `"0"` 같은 값이 올 수 있습니다.

### 오류 목록

| 상황 | HTTP | `code` | `message` (앱에 보내는 문구) | 현재 구현 | 발생 API | `retryable` |
|---|---|---|---|---|---|---|
| 지원하지 않는 음성 형식 | 415 | `UNSUPPORTED_AUDIO_FORMAT` | `지원하지 않는 음성 형식입니다(.확장자). 지원 형식: .aac, .m4a, .mp3, .mp4, .ogg, .wav, .webm`<br>확장자가 없으면 `(확장자 없음)`<br>content-type이 음성이 아니면 `음성 파일이 아닙니다. 녹음 파일을 다시 선택해 주세요.`<br>파일 머리 시그니처가 확장자와 다르면 `파일 내용이 .wav 음성 형식과 맞지 않습니다. 녹음 파일을 다시 선택해 주세요.` | 구현됨. 본문은 위 형식이며 `field`는 `file`. 허용 content-type: `audio/*`, `video/mp4`, `video/webm`, `application/octet-stream` (디코딩 검증은 하지 않음) | `/chat/voice` `/stt` | false |
| 음성 파일이 25MB 초과 | 413 | `FILE_TOO_LARGE` | `파일이 너무 큽니다. 최대 25MB` | 구현됨 (위와 같음). 요청 크기는 본문을 다 읽기 전에 확인하며(Content-Length 또는 읽은 바이트, 한도 25MB + multipart 여유 1MiB), 한도를 넘으면 본문 해석 없이 끊음 | `/chat/voice` `/stt` | false |
| 빈 음성 파일 | 400 | `EMPTY_FILE` | `빈 파일입니다.` | 구현됨 (위와 같음) | `/chat/voice` `/stt` | false |
| JSON 요청 본문이 1MiB 초과 | 413 | `REQUEST_TOO_LARGE` | `요청이 너무 커요. 내용을 줄여 다시 시도해 주세요.` | 구현됨 (#67) | 음성 업로드 외 모든 쓰기 API | false |
| 존재하지 않는 경로 | 404 | `NOT_FOUND` | `찾을 수 없는 경로예요.` (DB의 `NOT_FOUND`와 코드가 같고 문구만 다름) | 구현됨 (#67). 꺼진 개발용 `/stt`·`/tts`도 같은 응답 | 모든 경로 | false |
| 허용하지 않는 메서드 | 405 | `METHOD_NOT_ALLOWED` | `허용하지 않는 요청 방식이에요.` | 구현됨 (#67). `Allow` 헤더 포함 | 모든 경로 | false |
| 본문을 해석하지 못함 (깨진 multipart 등) | 400 | `INVALID_INPUT` | `입력한 내용을 확인해 주세요.` | 구현됨 (#67). `field`는 `null` | 본문이 있는 API | false |
| 요청 형식·값 오류 (필수 필드 누락, 형식 오류 등) | 422 | `INVALID_INPUT` | `입력한 내용을 확인해 주세요.` (`field`에 문제 필드) | 구현됨. `field`에 문제 필드 | 모든 API | false |
| 메시지가 비었음 | 422 | `MESSAGE_EMPTY` | `메시지를 입력해 주세요.` | 구현됨. 공백만 있는 메시지도 이 오류 | `/chat/text` | false |
| 메시지가 2000자 초과 | 422 | `MESSAGE_TOO_LONG` | `질문이 너무 길어요. 더 짧게 입력해 주세요.` | 구현됨 | `/chat/text` | false |
| 음성 질문(STT 전사문)이 16,000자 초과 | 422 | `MESSAGE_TOO_LONG` (`stage: llm`) | `질문이 너무 길어요. 더 짧게 입력해 주세요.` | 구현됨. 전사문은 자르지 않고 보관하고 AI·TTS는 호출하지 않으며, 같은 음성으로 다시 보내도 같은 결과 | `/chat/voice` | false |
| 음성 인식 결과가 비어 있음 (무음 녹음 등). AI를 호출하기 전에 차단 | 422 | `EMPTY_TRANSCRIPT` | `음성을 인식하지 못했어요. 다시 말씀해 주세요.` | 구현됨 | `/chat/voice` | true |
| STT 단계 실패 (시간 초과 포함) | 502 | `STT_FAILED` (`stage: stt`) | `음성을 글자로 바꾸지 못했어요. 다시 시도해 주세요.` | 구현됨 (`/chat/voice`) | `/chat/voice` | true |
| TTS 단계 실패 (시간 초과 포함). 단독 재시도·단독 호출 | 502 | `TTS_FAILED` (`stage: tts`) | `음성 합성에 실패했어요. 답변 글은 그대로 볼 수 있으니, 잠시 후 다시 시도해 주세요.` | 구현됨 (`/chat/turns/{turn_id}/tts`). `/chat/voice`의 TTS 실패는 아래 '오류가 아닌 부분 실패' | `/chat/turns/{turn_id}/tts` | true |
| 로그인하지 않음, 토큰 없음·만료·잘못됨 | 401 | `UNAUTHENTICATED` | `로그인이 필요하거나 만료되었어요. 다시 로그인해 주세요.` | 구현됨 | 인증 필요 API 전부 (`/health` 제외. 꺼져 있는 개발용 `/stt`·`/tts`는 켠 `dev` 환경에서 로그인이 필요하고 `local`에서만 필요 없음) | false |
| 계정 삭제가 끝나 로그인 계정이 사라짐 | 401 | `ACCOUNT_DELETED` | `계정 삭제가 끝났어요.` | 제안 (#123) | 인증 필요 API 전부(삭제된 사용자의 토큰이고 계정 삭제 작업이 `completed`일 때만) | false |
| 로그인 계정은 사라졌지만 계정 삭제 작업이 끝나지 않음 | 401 | `ACCOUNT_DELETION_INCOMPLETE` | `계정 삭제를 마무리하고 있어요.` | 제안 (#123) | 인증 필요 API 전부(삭제된 사용자의 토큰이고 계정 삭제 작업이 `completed`가 아닐 때) | true |
| 없거나 남의 항목 | 404 | `NOT_FOUND` | `찾을 수 없는 항목이에요. 목록을 새로고침해 주세요.` | 구현됨 | 항목 id를 받는 모든 API. 다른 사용자의 항목과 삭제 중인 항목도 같은 응답 | false |
| 이미 처리 중이거나 상태가 바뀜 | 409 | `CONFLICT` | `이미 처리 중이거나 상태가 바뀌었어요. 새로고침 후 다시 시도해 주세요.` | 구현됨 | 상태가 바뀔 수 있는 API | false |
| 계정 삭제가 진행 중임 | 409 | `ACCOUNT_DELETING` | `계정 삭제가 진행 중이에요. 완료될 때까지 새로 만들거나 바꿀 수 없어요.` | 제안 (#123) | 삭제 작업 조회·다시 시작을 뺀 쓰기 API | false |
| DB·Storage에 일시적으로 연결하지 못함 | 503 | `DB_UNAVAILABLE` | `일시적으로 저장소에 연결할 수 없어요. 잠시 후 다시 시도해 주세요.` | DB는 구현됨, Storage 연결 실패는 제안 | DB·Storage를 쓰는 모든 API | true |
| 업로드 완료를 알렸지만 원본이 없음 | 409 | `UPLOAD_MISSING` | `업로드가 끝나지 않았어요. 파일을 다시 올려 주세요.` | 제안 (현재 구현은 `CONFLICT`) | `/records/{record_id}/uploaded` | true |
| 올린 원본의 크기가 예약한 크기와 다름 | 409 | `UPLOAD_SIZE_MISMATCH` | `올린 파일의 크기가 예약한 크기와 달라요. 기록을 지우고 다시 올려 주세요.` | 제안 (현재 구현은 `CONFLICT`) | `/records/{record_id}/uploaded` | false |
| 카카오톡 기록에서 화자 값이 필요한데 없음 | 422 | `SPEAKER_REQUIRED` | `화자를 선택해 주세요.` | 제안 (현재 구현은 `INVALID_INPUT`) | `/records/{record_id}/uploaded` | true |
| 보낸 화자가 파일의 화자 목록에 없음 | 422 | `SPEAKER_NOT_FOUND` | `선택한 사람이 파일에 없어요. 다시 선택해 주세요.` | 제안 (현재 구현은 `INVALID_INPUT`) | `/records/{record_id}/uploaded` | true |
| 화자 값이 필요 없는 파일에 화자 값을 보냄 | 422 | `SPEAKER_NOT_ALLOWED` | `이 파일은 화자를 선택할 필요가 없어요.` | 제안 (현재 구현은 `INVALID_INPUT`) | `/records/{record_id}/uploaded` | true |
| 서버 내부 오류 (원인 상세는 서버 로그에만) | 500 | `INTERNAL_ERROR` | `서버에 문제가 생겼어요. 잠시 후 다시 시도해 주세요.` | 구현됨. 본문은 위 형식 | 모든 API | false |

### 오류가 아닌 부분 실패

아래 경우는 HTTP 200으로 응답하고, 실패한 부분만 응답 본문으로 알립니다.

| 상황 | HTTP | 응답 본문 필드 | `tts_error.message` | 발생 API |
|---|---|---|---|---|
| 답변 생성은 성공했지만 TTS 단계만 실패 (시간 초과 포함) | 200 | `tts_status: "failed"`, `audio_base64: null`, `audio_format: null`, `tts_error: {code: "TTS_FAILED", message}` | `음성 합성에 실패했어요. 답변 글은 그대로 볼 수 있으니, 잠시 후 다시 시도해 주세요.` | `/chat/voice` |

AI 서버가 원인인 오류(`AI_BUSY`, `AI_NOT_READY`, `AI_TIMEOUT`, `AI_FAILED` 등)는 **3장**에 정리했습니다.

DB가 원인인 오류(`NOT_FOUND`, `CONFLICT`, `DB_UNAVAILABLE` 등)는 **DB 규격 문서**에 정리했습니다.

같은 `code`는 한 가지 문구만 씁니다. 2장·3장·DB 규격 문서의 `code`와 문구는 같고, 상황이 다르면 `code`를 나눕니다.

AI·STT·TTS 단계의 시간 초과는 각 단계의 `code`(`AI_TIMEOUT`, `STT_FAILED`, `TTS_FAILED`)로 보냅니다. 전체 시간 초과 전용 코드는 두지 않고, 앱은 요청 종류별 자체 타임아웃(텍스트·TTS 180초, 음성 200초)으로 처리합니다. 백엔드의 AI 호출 대기 `LLM_TIMEOUT_SECONDS`(잠정 60초, #96에서 검토 중)는 앱의 대기 시간과 별개의 값입니다.

오류 본문에는 SQL, 예외 원문, 토큰, 질문·답변 내용, 서버 키를 넣지 않습니다.

앱의 현재 동작: 입력은 2000자 이내, 공백만 있는 메시지는 전송 불가, 응답 대기 중 중복 전송 방지, **요청별 타임아웃(텍스트·TTS 180초, 음성 200초)**, 실패·빈 응답은 오류 안내와 재시도·수정 버튼을 제공합니다.

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
| 413 | `PAYLOAD_TOO_LARGE` | `요청 크기가 한도를 초과했습니다.` | 원시 본문 256 KiB, 질문·이력 텍스트 16000자, 이력 20쌍 중 하나 초과 | 앱 질문 2000자·이력 20쌍 이내라도 UTF-8 JSON 본문이 256 KiB를 초과할 수 있음. 백엔드는 문자열별 한도와 직렬화한 본문 바이트 수를 별도로 검사하고, 한도를 넘는 본문은 전송하지 않음. 개수·글자 수 제한만으로 413을 배제하지 않으며 질문 원문·출력 예산은 임의로 줄이지 않음 | `llm_payload_too_large` |
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
