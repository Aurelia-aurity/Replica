# Supabase

DB 스키마는 이 폴더의 마이그레이션으로만 바꿉니다. 대시보드에서 직접 테이블을 고치지 않습니다.

- `config.toml`: 로컬 Supabase 설정 (`supabase init`으로 생성)
- `migrations/`: 스키마 변경 이력. 이미 적용된 파일은 고치지 않고 새 파일을 추가합니다.
- `seed.sql`: 로컬 전용 시험 계정 2개와 기본 페르소나. 클라우드에는 적용하지 않습니다.

설계 근거와 테이블 설명은 baseline DB 설계 문서를 참고합니다.

## 현재 스키마 (baseline)

| 테이블 | 내용 |
| --- | --- |
| `personas` | 대화 상대. baseline은 사용자당 기본 페르소나 1개 |
| `chat_sessions` | 사용자와 페르소나의 대화방, AI 생성 안내 확인 시각 |
| `chat_turns` | 질문 하나와 응답 하나. 단계별 상태와 구간 시간 |

- 모든 테이블에 `user_id`가 있고, 복합 FK로 다른 사용자 소유의 페르소나·대화방과 연결될 수 없습니다.
- RLS는 켜져 있고 정책은 없으며, `anon`·`authenticated` 역할의 테이블 권한을 회수했습니다. 앱 키로는 접근할 수 없고 백엔드만 접근합니다. 사용자별 정책은 로그인이 생기는 고도화 1에서 추가합니다.

## 로컬 실행

필요한 도구: Docker Desktop, Supabase CLI (작성 시 2.120.0으로 확인)

```bash
cd infra
supabase start      # 처음 한 번은 이미지 내려받기에 시간이 걸림
supabase db reset   # 마이그레이션 + seed.sql 적용
supabase stop
```

`supabase start`가 출력하는 DB URL(기본 `postgresql://postgres:postgres@127.0.0.1:54322/postgres`)을 `backend/.env`의 `DATABASE_URL`, `TEST_DATABASE_URL`에 넣습니다. 시험 계정 id는 `seed.sql`에 고정되어 있어 `backend/.env.example` 값을 그대로 쓸 수 있습니다.

## 마이그레이션 추가

```bash
cd infra
supabase migration new <변경_내용>   # 파일 이름은 직접 만들지 않음
# 파일 작성 후
supabase db reset                    # 로컬에서 처음부터 적용되는지 확인
```

PR이 병합된 뒤 클라우드 프로젝트에 적용합니다. 적용 후 Supabase 대시보드의 Advisors(보안·성능 권고)를 확인합니다.

## 클라우드 적용 상태

| 마이그레이션 | 클라우드 |
| --- | --- |
| `20261008055335_baseline_chat_schema` | 적용됨 (2026-10-08) |
| `20261008055430_chat_turns_fk_covering_index` | 미적용 |

현재 남아 있는 권고:

- `rls_enabled_no_policy` (INFO): 의도한 상태입니다. baseline은 백엔드만 접근합니다.
- `unused_index` (INFO): 아직 트래픽이 없어서 나오는 결과입니다.
- `unindexed_foreign_keys` (INFO): 두 번째 마이그레이션을 적용하면 해소됩니다.
- `auth_leaked_password_protection` (WARN): Auth 설정입니다. 회원가입이 생기는 고도화 1에서 켭니다.
