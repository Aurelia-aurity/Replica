# Replica DB 구조 및 검증 결과

2026-10-09 기준. Supabase / Supabase Postgres Best Practices 스킬의 최소 권한·RLS·검증 지침을 적용했다.

## 적용 상태

- 대상: Supabase `Replica` 프로젝트 (`hfmrhmuhcbmskhjagpzg`).
- 기존 이력: `20261008055335 baseline_chat_schema`.
- 적용한 이력: `20261009073108 replica_db_redesign`.
- `redesign.sql`은 실제 적용 SQL의 보관본이다. 기존 baseline이 있어야 하며 이미 적용된 DB에 다시 실행하지 않는다.
- 애플리케이션 테이블 9개, 백엔드 전용 DB 함수 8개. Auth는 Supabase의 `auth.users`를 그대로 사용한다.
- 기존 Auth 사용자 2명은 변경하지 않았다. 기존 애플리케이션 데이터는 0건이었다.
- CLI migration 디렉터리와 로컬 DB는 아직 설정하지 않았다. 팀 개발 시작 전에 CLI로 원격 이력과 스키마를 동기화해야 한다.

## 테이블 관계

| 테이블 | 역할 | 주요 연결·규칙 |
| --- | --- | --- |
| personas | 복제 대상 및 데이터 개정 번호 | 사용자 소유, active/deleting |
| chat_sessions | 대화방 | 같은 사용자의 persona만 연결 |
| chat_turns | 질문·응답, STT/LLM/TTS 상태와 시간 | 같은 사용자의 session, 요청 해시·시도 번호 |
| records | 업로드 원본의 메타데이터·동의 | persona 연결, 파일 자체는 private Storage |
| record_text_versions | 검토·수정한 전사/텍스트 | record 연결, 현재 confirmed 버전은 하나 |
| style_profiles | 버전별 말투 프로필 | persona 연결, ready/stale, 원본 개정 번호 |
| profile_sources | 프로필을 만든 확정 텍스트 출처 | profile 및 text version과 사용자/persona가 일치해야 함 |
| record_chunks | 확정 텍스트의 RAG 조각·임베딩 | 모델·차원별 검색, 미확정/이전 버전 제외 |
| deletion_jobs | 삭제 진행·실패·재시도와 파일 경로 목록 | 대상 삭제 후에도 작업 내역 유지 |

`auth.users → personas → chat_sessions → chat_turns`가 대화 경로이고,
`personas → records → record_text_versions → record_chunks`가 개인화 자료 경로이다.
프로필은 `profile_sources`를 통해 실제로 사용한 확정 버전을 추적한다.
소유자 및 persona를 포함하는 복합 외래 키로 다른 사용자/대상의 자료 혼합을 차단한다.

## 백엔드 전용 DB 함수

아래 함수는 SQL 함수이다. 이를 호출하는 Python DB 접근 계층은 `backend/app/db/`에 구현했으며, [함수 사용 명세](../../docs/architecture/db-python-api.md)를 참고한다. FastAPI HTTP 엔드포인트는 아직 구현하지 않았다.

| 함수 | 백엔드에서 사용할 때 |
| --- | --- |
| create_chat_turn | 요청 ID와 입력 내용으로 대화 생성. 동일 요청은 재사용, 같은 ID의 다른 입력은 거부 |
| start_chat_stage | STT/LLM/TTS 실행권 확보. 반환된 `*_attempt`를 작업과 함께 보관 |
| finish_chat_stage | 실행 결과·처리 시간 저장. 현재 시도 번호가 아닌 늦은 결과 거부 |
| expire_chat_stages | 기준 시간보다 오래 실행 중인 단계를 실패 처리 |
| confirm_record_text | 초안을 확정하고 이전 확정 버전을 superseded로 전환 |
| publish_style_profile | 입력 개정 번호와 확정 출처를 검증해 새 프로필 발행 |
| search_record_chunks | 사용자/persona/모델/차원을 먼저 제한한 뒤 유사도 검색 |
| request_record_deletion | 삭제 작업 생성·재사용, 즉시 기록을 deleting으로 전환 |

- FastAPI는 검증한 JWT에서 사용자 ID를 얻어야 한다. 클라이언트가 보낸 `user_id`를 그대로 믿으면 안 된다.
- `service_role`은 RLS를 우회한다. 서버 비밀키이며 브라우저/모바일에 넣지 않는다.
- 일반 로그인 사용자는 자기 데이터 읽기만 가능하며, 테이블 쓰기 및 위 DB 함수 실행 권한이 없다.
- STT/LLM/TTS 외부 호출은 `start_chat_stage`가 성공한 요청만 수행한다. 재시도는 기존 단계를 실패 처리한 뒤 새 시도 번호로 시작한다.
- 요청 ID는 클라이언트가 재전송에도 유지한다. 음성은 원본 바이트의 SHA-256을 전달한다.
- 주요 오류: `P0002` 대상 없음/사용 불가, `22023` 입력·요청 ID 충돌, `55000` 상태 충돌/늦은 응답, `23514` 무결성 위반.
- 대화 목록은 `turn_no`로 정렬한다. 번호에 빈 구간이 생기는 것은 정상이다.
- 개인화 작업은 시작 시 `personas.data_revision`을 읽고 완료 시 같은 값을 전달한다. 확정 자료가 변경되면 기존 ready 프로필은 stale이 된다.
- 기존 source가 변경되기 전 만든 chunks는 보관되어도 검색에서 제외된다. 물리적 정리는 별도 worker 책임이다.

## 실행한 검증

`verify_redesign.sql`을 원격 DB에서 두 번 실행했으며 두 번째 실행에는 출처 없는 프로필의 지연 제약 검사도 포함했다. 전체 성공.
실제 Auth ID는 시험 권한의 주체로만 사용하고 계정 정보/설정을 수정하지 않았다.
시험 데이터 생성·변경·삭제는 한 트랜잭션에서 실행한 뒤 ROLLBACK했다. Identity 번호는 롤백되지 않아 증가할 수 있다.

| 영역 | 확인한 결과 |
| --- | --- |
| 사용자 권한 | 두 계정 각각 9개 테이블의 자기 데이터 읽기 가능, 다른 계정 데이터 0건 |
| 쓰기 제한 | 일반 사용자 INSERT/UPDATE/DELETE 권한 없음, 실제 INSERT 및 서버 RPC 호출 거부, 익명 읽기 거부 |
| 관계 | 다른 소유자의 persona에 session 연결 거부 |
| 대화 | 동일 요청 중복 생성 없음, 다른 입력의 요청 ID 재사용·다른 사용자 session 사용 거부 |
| 실행 상태 | STT 이전 LLM, LLM 이전 TTS, 중복 시작, 완료 답변 덮어쓰기 거부 |
| 재시도 | STT/LLM의 이전 시도 결과 거부, TTS 실패 시 텍스트 유지, TTS 재시도 성공 |
| 시간 초과 | 오래된 LLM pending 실패 처리, 뒤늦은 결과 거부 |
| 확정 자료 | 확정 텍스트 직접 수정·미확정 chunk 생성 거부, 새 확정 버전 하나 유지 |
| 프로필 | 미확정 출처·지난 입력 개정 번호·출처 없는 ready 프로필 거부, 자료 변경 시 이전 프로필 stale |
| RAG | 다른 사용자·모델·차원·이전 확정 버전·삭제 중 기록 검색 제외 |
| 삭제 | 중복 삭제 요청은 같은 job, Storage 완료 표시 전 메타데이터 삭제 거부, 즉시 자료·프로필 읽기 차단 |
| 삭제 후 연결 | 시험용 Storage 완료 표시 후 record 삭제 시 text/chunk 연쇄 삭제, 삭제 job 보존 |

행 단위 잠금을 사용하는 함수의 상태 전이는 검사했지만, 여러 독립 연결의 동시 부하 시험은 하지 않았다.
HTTP API, 실제 업로드/다운로드/파일 삭제, AI 서비스 호출, 계정 삭제의 종단 간 시험은 포함하지 않는다.

## 남은 연동 작업과 주의사항

1. **private Storage 버킷 생성**: `replica-records` 버킷은 아직 없다. Storage API/대시보드에서 private로 만들고 파일 크기·MIME 제한을 정한다. SQL로 Storage 메타데이터를 직접 수정하지 않는다. 업로드/읽기 정책 2개는 생성했다. 경로는 `{user_id}/{record_id}/original`; 서버가 records 예약 행을 먼저 만든다. 원본 덮어쓰기는 허용하지 않는다.
2. **FastAPI API 연동**: 구현된 Python DB 모듈을 HTTP 엔드포인트에 연결한다. persona/session 생성·조회, 업로드 예약·상태 처리, 초안 저장/조회, 위 8개 함수 호출, 작업 목록 조회는 DB 모듈에 있다. 사용자 인증은 `Database.for_user()`로 확인하고, 모든 server-role 쿼리에는 인증된 소유자 조건을 넣었다. 정상 로그인 사용자의 실제 저장·조회와 다른 사용자 접근 차단은 통합 검증이 남아 있다.
3. **삭제 worker**: Storage API로 실제 원본 삭제 → 성공 확인 → 영향받은 프로필(출처 및 stale 콘텐츠 포함)·외부 인덱스·학습 산출물 정리 → records 및 파생 자료 삭제 → 남은 단계 0개 확인 → job 완료 순서가 필요하다. 이번 검증은 파일 없는 시험 행의 Storage 완료 표시만 모사했다. DB가 실제 파일 삭제를 확인해 주지는 않는다.
4. **대상/계정 삭제**: persona를 deleting으로 숨기고 관련 records 삭제 작업을 먼저 끝낸다. 대화·프로필과 외부 파일/인덱스/adapter까지 삭제한 다음 Auth API로 계정을 제거한다. records의 RESTRICT 외래 키와 삭제 guard는 무심코 하는 cascade 삭제를 막는다. 기존 JWT는 계정 삭제만으로 즉시 무효화되지 않으므로 세션 폐기와 민감 작업의 세션 검증도 필요하다.
5. **삭제와 기존 응답**: 원본을 삭제해도 과거 대화에 저장된 AI 답변은 자동으로 다시 쓰거나 지우지 않는다. 개인정보 삭제 정책에 따라 관련 대화 삭제/마스킹을 추가해야 한다. 이미 발급된 파일 signed URL의 취소도 RLS만으로 보장하지 못한다.
6. **임베딩/LoRA**: 임베딩 모델과 차원 미정이라 pgvector의 필터된 정확 검색을 쓴다. 모델 합의와 자료량 측정 후 ANN 인덱스를 추가한다. LoRA 학습 작업/adapter 테이블과 실행은 아직 구현하지 않았다.

## 최종 점검

- 모든 public 애플리케이션 테이블 RLS 활성화, 클라이언트가 실행 가능한 public DB 함수 0개.
- 롤백 후 애플리케이션 테이블 9개 모두 0건, Auth 2명, Storage 객체 0건.
- 새 DB 구조의 보안 경고 및 외래 키 인덱스 누락 경고 없음.
- 기존 Auth 설정의 [유출 비밀번호 차단 미활성화 경고](https://supabase.com/docs/guides/auth/password-security#password-strength-and-leaked-password-protection)는 남아 있으며 설정은 변경하지 않았다.
- 성능 점검의 [미사용 인덱스 안내](https://supabase.com/docs/guides/database/database-linter?lint=0005_unused_index) 6개는 실제 서비스 데이터/트래픽이 없는 초기 상태의 참고 사항이다. 지금 삭제하지 않고 사용 패턴을 측정한 뒤 판단한다.

재검증은 운영 트래픽이 없는 개발 환경에서 postgres 권한으로 `verify_redesign.sql` 전체를 한 번에 실행한다. 실행 도중 오류가 난 연결에서는 명시적으로 ROLLBACK한다. 데이터베이스 검증 통과는 파일 삭제·계정 삭제·실제 API 연동까지 완성됐다는 뜻이 아니다.
