# 주말 첫 통합: DB 담당 인계

기준: 2026-10-28. 사용자 지시에 따라 윤서준의 `docs/40-api-spec` 초안을 구현 기준으로 삼았다. 다른 담당자의 승인이나 API 구현 완료를 뜻하지 않는다.

## 기준 문서와 브랜치

- 백엔드↔DB: `contracts/backend-db/api-spec.md` — 원본 blob `970f1be4f9c75736a9fa457c35868db7a9ccd3e8`의 보존 사본.
- 앱↔백엔드: `contracts/app-backend/api-spec.md` — 원본 blob `84707751b927fdf28969ebd75cd644ebf044c5f3` 기반 작업 사본. #61 리뷰에 따라 413 설명의 정정안을 반영했다. 글자 수·이력 쌍 수 제한과 UTF-8 JSON 바이트 한도를 구분한다. 원본 `docs/40-api-spec` 브랜치는 변경하지 않았으며 원작성자 윤서준의 재리뷰·원본 반영 합의가 필요하다.
- 원본 브랜치: `docs/40-api-spec`, 작성 커밋 `3420b00`. 원문에 있는 ‘PR 없음’ 등 이전 시점 설명은 그대로 보존했다. 원문을 최신 확정본으로 오해하지 않는다.
- 기존 DB 함수 관리 원본: [Python DB 명세](db-python-api.md). 이번 호출 보완·선택 사항은 이 문서로 구분한다.
- 구현 브랜치: `jaesung_41-db-integration`. 기존 PR #45의 head `0772aa4`를 이어받았다. #45 브랜치와 다른 팀원 브랜치를 변경하지 않았다. `db_baseline`은 기존 스키마의 별도 구현이므로 이 DB 패키지 위에 덮어쓰지 않는다. 정본 제안은 Supabase Auth/REST/RPC 구현이며 다른 브랜치 정리는 팀 합의 후 한다.

## DB 담당 구현 범위

| 항목 | 구현 |
| --- | --- |
| 초안의 DB 함수 30개 | 기존 인자·기본값과 호출법 유지 |
| 최근 완결 대화 | `get_recent_history`: 질문·답변 모두 done인 최근 최대 20쌍을 시간순 반환. `before_turn_id`로 현재 턴과 이후 턴 제외. TTS 실패는 완결 텍스트를 제외하지 않음 |
| 전체 처리 시간 | `finish_chat_stage(..., total_ms=...)`로 결과와 같은 트랜잭션에 저장. 뒤늦게 측정하면 `record_turn_total_ms`로 최신 완료 단계·attempt 확인 후 기록. 덧셈이 아니라 요청별 측정값이며 다른 값 재기록 거부 |
| AI 요청 연결 | LLM 시작 시 `request_id`를 턴·소유자·현재 llm_attempt와 원자적으로 연결. 결과 저장 때 request_id·attempt 불일치 거부 |
| 생성 종료 사유 | LLM 성공 때 `finish_reason=stop/length` 저장. `length`도 성공. 완료된 답변·종료 사유는 덮어쓰기 금지 |
| DB→HTTP 오류 | `to_http_error`가 초안의 code/한국어 문구·HTTP·retryable과 오류 본문 6개 필드를 생성. FastAPI 비의존. 원문 예외·SQL·키 비노출 |
| 앱 응답용 필드 | `to_public_row`가 객체별 허용 필드만 투영. turn의 id→turn_id와 is_ai_generated 추가. user_id/attempt/요청 해시/파일 경로/삭제 manifest/미래 내부 필드 제외 |
| 로컬 설정 | 명시적 `Settings.from_env_file(path)` 제공. 자동 탐색·셸 실행·변수 치환 없음. 배포는 `Settings.from_env()` 유지 |
| 인증 전 개발 시험 | 테스트 전용 Auth 사용자 2명 생성→로그인→합성 자료 시험→계정·행 정리. 실제 로그인 화면 구현 없이 JWT 검증 가능. 인증 우회 모드는 없음 |

이번 확장 SQL은 `infra/supabase/weekend_integration.sql`이고 원격 이력은 `20261028053701 weekend_db_integration`이다. 기존 RPC 8개는 유지하고 v2 RPC 2개·시간 기록 RPC 1개를 추가했다. 이미 적용한 DB에 이 보관 SQL을 다시 실행하지 않는다. CLI migration 디렉터리 동기화는 별도이며 이 파일을 임의 타임스탬프 migration으로 보지 않는다.

## API 담당자가 연결할 최소 예시

`backend`가 Python import 기준 경로일 때:

```python
from pathlib import Path
from time import monotonic
from uuid import uuid4
from app.db import Database, Settings, DatabaseError, to_http_error, to_public_row

# 로컬 개발에서만 서버 시작 시 한 번. 배포는 Settings.from_env().
database = Database(Settings.from_env_file(Path(".env")))

async def handle_text(access_token, session_id, turn_id, message, ai_generate, model_name):
    started = monotonic()
    user_db = await database.for_user(access_token)  # HTTP Bearer 추출은 API 계층
    turn = await user_db.create_chat_turn(session_id, turn_id, input_mode="text", question=message)
    if turn["answer_status"] == "done":
        return to_public_row("turn", turn)  # 완성 답변을 다시 생성하지 않음
    history = await user_db.get_recent_history(session_id, before_turn_id=turn_id)
    request_id = uuid4()  # 같은 turn 재시도도 AI request_id는 새 값
    claimed = await user_db.start_chat_stage(turn_id, "llm", model_name=model_name, request_id=request_id)
    # 여기서 API/AI 담당 모듈이 계약대로 검증된 AI 결과만 반환해야 한다.
    # 예시의 ai_generate 호출 서명은 실제 AI 클라이언트 인터페이스가 아니다.
    result = await ai_generate(request_id=request_id, history=history, message=message)
    saved = await user_db.finish_chat_stage(turn_id, "llm", attempt=claimed["llm_attempt"],
        request_id=request_id, ok=True, text=result["answer"], elapsed_ms=result["llm_ms"],
        finish_reason=result["finish_reason"], total_ms=int((monotonic() - started) * 1000))
    return to_public_row("turn", saved)
```

이 예시는 DB 성공 경로만 보여준다. 앱 API의 answer/timings 등 응답 조립, AI 요청 직렬화·응답 ID/스키마 검사, AI 실패를 `finish_chat_stage(ok=False, request_id=request_id, error_code=...)`로 저장하는 처리는 API/AI 담당이 연결한다. ID를 연결한 LLM 시도는 실패 완료에도 같은 ID를 반드시 전달한다. UNKNOWN 결과에는 자동 재시도하지 않고 턴·시도 상태를 확인한다. `model_name`은 호출 전에 API/AI 설정에서 받아야 하며 응답이 올 때까지 기다렸다가 임의 값으로 claim하지 않는다.

```python
try:
    ...  # DB 호출
except DatabaseError as error:
    mapped = to_http_error(error, operation="create_chat_turn", turn_id=turn_id, stage="llm")
    # FastAPI 담당: JSONResponse(status_code=mapped.status_code, content=mapped.body)
    # 쓰기 결과 불명(mapped.outcome_unknown)이면 같은 ID로 조회 후 판단. 자동 재전송 금지.
```

앱이 보낸 잘못된 값임을 API가 확인한 경우에만 `input_source="client"`로 422를 선택한다. 기본은 서버 오류 500이어서 서버의 잘못된 상태 전이를 앱 탓으로 돌리지 않는다. 필드 이름·operation은 신뢰된 API 코드에서 지정한다. 프로필 개정 충돌을 자동으로 무한 재생성하지 말고 AI 담당 작업 정책에 따라 제한한다.

## 첫 통합의 경계와 결정 사항

1. **TTS 출력**: 서준 초안의 첫 응답은 base64다. DB는 tts_status/처리 시간만 보관하며 음성 바이트·base64·공개 URL을 저장하지 않는다. 이번 구현도 임의 Storage 방식을 도입하지 않았다. 과거 합성 음성 재재생, 완료된 음성 요청의 파일 재전송까지 필요하면 private 객체·수명·삭제 정책 합의와 별도 구현이 필요하다. 텍스트 답변의 재조회·TTS 실패 보존/재시도는 지원한다.
2. **로그인**: 앱 화면 구현과 무관하게 실제 Supabase JWT만 사용한다. 시험 계정은 테스트 스크립트에서만 생성하며 실제 사용자 데이터는 쓰지 않는다. 계정 세션의 즉시 폐기/계정 전체 삭제는 이번 baseline에 넣지 않는다.
3. **오류**: 초안 변환표는 구현했지만 앱/백엔드 쪽 채택·리뷰는 아직이다. SQLSTATE 55000은 PostgREST에서 HTTP 500이 될 수 있으므로 도메인 충돌로 먼저 분류한다.
4. **저장 자료**: records는 예약 메타데이터다. 실제 원본 업로드·무결성 확인·Storage 버킷·삭제 worker는 초안에서 별도/미정인 범위라 이번 DB 접근 모듈에 넣지 않았다. deletion job이 있다는 이유로 파일 삭제 성공을 반환하면 안 된다.
5. **고도화 계약**: PR #29의 불변 source_snapshot_id, analysis_operation_id/attempt, 전체 입력 범위와 원자적 프로필 게시, RAG 인덱스 완료 상태는 첫 baseline과 분리한다. 기존 data_revision/source_ids 검사만으로 해당 요구를 완성했다고 하지 않는다.
6. **병합**: 2026-10-28 PM 코멘트의 새 순서는 #63 선병합 → #61 base를 main으로 변경 → 누적 결과 재검증·리뷰다. #61은 #45의 SQL·검증·문서를 포함한다. #45의 대체 종료는 #61에 내용이 보존되고 담당자들이 처리 방향에 동의한 뒤 진행한다. #41/팀 합의를 자동 완료하지 않는다. #63과의 병합 충돌은 백엔드 API README·설정과 DB 사용법을 함께 보존하도록 정리한다.

## 실행·테스트

`backend`에서 기본 검사:

```text
python -X utf8 -m unittest discover -s tests -v
```

#63과 합친 백엔드는 Python 3.12에서 `pip install -r requirements-dev.txt` 후 `pytest`로 검사한다. DB 테스트는 `tests` 패키지에서 가져오므로 `PYTHONPATH=.:tests`가 필요하지 않다. 실제 DB 쓰기·읽기 검사는 기본 비활성 상태를 유지한다.

## 요청 ID 검증 후속 SQL

`guard_llm_request_id.sql`은 기존 `finish_chat_stage`의 서명·권한을 유지하면서 ID가 연결된 LLM의 레거시 완료를 행 잠금 안에서 거부한다. Python의 사전 검사와 별도로 직접 RPC 우회를 막는다. ID 없는 기존 LLM 호출, STT·TTS는 호환된다.

배포 순서: #63 실패 호출부의 ID 전달 수정 반영 → `guard_llm_request_id.sql` 적용 → `verify_llm_request_id.sql` 롤백 검증. 이미 적용된 `weekend_integration.sql`은 재실행하지 않는다. 이번 리뷰 보완에서는 후속 함수 교체와 성공·실패/잘못된 ID/다른 사용자/재시도/기존 호출 호환 검사를 한 트랜잭션에서 실행하고 ROLLBACK했으며, 후속 SQL을 원격 DB에 영구 적용하지 않았다.

### 2026-10-28 리뷰 보완 검증

- #63 F1 수정 커밋 `f548b84`: LLM 실패 완료에 원래 요청 ID를 전달한다. AI 오류·예상 밖 예외 모두 실패 저장 후 수동 재시도에서 새 ID를 사용하는 회귀 검사를 추가했다. #63 단독 `pytest`: 128개 통과.
- DB 단독 `pytest`: 48개 통과·실제 연결 선택 검사 7개 skip. ID 없는 연결 시도 완료는 RPC 호출 전에 거부하고, ID 없는 기존 시도의 성공·실패는 기존 RPC로 전달하는 것을 검사했다.
- #63 수정 커밋을 #61 작업 브랜치에 병합해 README·설정 예시·DB 저장 코드의 충돌을 해결했다. 이는 main 병합이 아니며 #63 선병합 뒤 #61의 base를 main으로 변경하는 절차를 유지한다. 원래 #45의 SQL·검증·문서와 HTTP 500/55000 분류 보완도 유지한다.
- Python 3.12.14에서 합친 `backend`의 기본 `pytest`: **176개 통과·7개 skip**, subtest 29개 통과. `PYTHONPATH`를 추가하지 않았다. Starlette TestClient의 기존 의존성 사용 중단 안내 1개가 있다.
- `.github/workflows/backend-tests.yml`에 Python 3.12 / Ubuntu·Windows의 오프라인 pytest를 추가했다. 실제 DB 읽기·쓰기 검사는 CI에서 명시적으로 비활성화한다. GitHub 실행 결과는 별도 확인한다.
- SQL 교체·합성 시나리오 롤백 후 persona/session/turn 각 0건, 기존 Auth 2명 유지, 후속 SQL 영구 미적용을 확인했다. 실제 앱·GPU·음성 공급자 통합이나 운영 배포 시험은 아니다.

데이터 변경이 있는 통합 시험은 기본 비활성이다. 개발 프로젝트의 서버 설정이 들어 있는 로컬 `.env`가 있고, 운영자가 임시 계정 생성·정리에 동의한 경우에만 실행한다. PowerShell:

```powershell
$env:REPLICA_DB_MUTATING_TEST = "1"
python -X utf8 -m unittest discover -s tests -p test_db_full_live.py -v
Remove-Item Env:REPLICA_DB_MUTATING_TEST
```

`test_db_full_live.py`는 앱 로그인 UI를 대신하는 테스트 도구일 뿐이다. 실행마다 무작위 이메일·비밀번호를 메모리에서 만들고 관리자 API로 이메일을 확인 처리해 메일을 보내지 않는다. 테스트 자체의 app_metadata 표식을 확인한 계정만 정리한다. 기존 계정은 수정하지 않는다. 파일과 AI 호출 없이 합성 결과/예약 행을 넣는다. 시험 record의 Storage 완료 표시는 **파일을 업로드하지 않은 이 시험에서만** 정리용으로 모사한다. 실제 worker에서 이를 재사용하면 안 된다.

검사 실패 때도 cleanup을 실행한다. cleanup 자체가 실패하면 같은 시험을 반복하기 전에 `replica_integration_run` 표식의 시험 계정만 확인한다. 임의 사용자를 삭제하거나 광범위한 테이블 초기화를 하지 않는다.

## 검증 기록

- 기존 30개 함수와 신규 2개 사용자 함수, 설정·오류 변환·필드 투영은 오프라인 검사를 제공한다.
- 원격 SQL 추가 전 rollback 사전 시험, 적용 후 rollback 회귀 시험 통과.
- 실제 계정 2개 통합 시험의 첫 실행에서 HTTP 500/SQLSTATE 55000 분류 문제를 발견했고 수정·회귀 검사 후 다시 실행해 통과했다. 실패한 첫 실행도 계정·행 cleanup 성공.
- 최종 DB 검사 구성 52개(오프라인 45, 임시 계정 통합 1, 읽기 전용 서버 5, 외부 JWT 전용 읽기 1). 외부 JWT를 별도로 주입하지 않아 해당 선택 검사만 skip한다. 두 임시 사용자의 실제 JWT를 사용하는 전체 함수 통합 시험은 텍스트·음성 메타데이터 저장, 최근 이력, 동시 실행권 1건, 늦은 시도 차단, 프로필·검색·삭제 요청과 사용자 간 차단을 포함한다.
- 최종 전체 실행 결과: 52개 중 51개 통과, 위 선택 검사 1개 skip. 실제 JWT를 사용한 쓰기·소유권 통합 시험은 통과했다.
- 기존 협업 스크립트 검사 100개, Python 구문 검사, `git diff --check`, 변경 파일의 실제 키 대조 검사 통과.
- 최종 cleanup 후: 시험 계정 0명, 기존 Auth 사용자 2명, 애플리케이션 테이블 9개 모두 0건, Storage 객체 0건 확인. 이 시험에서 만든 임시 계정·자료만 삭제했고 복구가 필요한 사용자 데이터는 없었다. Identity 번호는 증가할 수 있다.
- 보안 advisor: 새 스키마/RPC 경고 없음. 기존 [유출 비밀번호 차단 미활성화](https://supabase.com/docs/guides/auth/password-security#password-strength-and-leaked-password-protection) 경고는 유지하며 임의로 Auth 설정을 바꾸지 않았다. 성능의 [미사용 인덱스 안내](https://supabase.com/docs/guides/database/database-linter?lint=0005_unused_index) 4개는 초기 트래픽의 참고 항목으로 보존했다.
- 실제 앱·HTTP·AI·음성 파일 경로까지 검증한 것으로 해석하지 않는다. 공유 브랜치는 `jaesung_41-db-integration`이며, 원격 공유·후속 PR과 팀 합의 여부는 GitHub에서 별도로 확인한다.

## AI 활용·검토

- AI 활용 범위: Codex로 서준 초안과 기존 함수 대조, DB/Python 보완, 오류·응답 변환, 합성 통합 시험과 인계 문서를 작성했다.
- 검토·확인: 함수 30개의 인자 호환성과 32개 공개 함수 호출 여부, 자동·실제 사용자 시험, SQL rollback 검증, 비밀값 비노출과 시험 자료 정리를 확인했다. 상세 결과는 위 검증 기록에 적었다. 다른 담당자의 리뷰·승인과 앱/서버 통합 검증을 대신하지 않는다.
