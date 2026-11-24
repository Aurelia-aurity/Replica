# 개인화 DB 수명주기 호출 계약

#121의 SQL 구현은 infra/supabase/20261124043921_personalization_lifecycle.sql에 있다. 선행 패치는 #124의 record_metadata.sql이다. 아래 RPC는 모두 서버 역할만 실행한다. Python 호출부는 검증한 사용자 ID를 공통 _rpc에서 p_user_id로 주입한다.

## 원본 스탬프와 쓰기 규칙

personas.source_stamp는 자료 개정·활성 상태가 바뀔 때 새 UUID가 된다. 기존 확정·동의·기록 상태·삭제 트리거가 올리는 data_revision과 함께 검사한다. 호칭 편집은 스탬프를 바꾸지 않는다.

새 처리의 쓰기는 페르소나를 먼저 잠근 뒤 텍스트·작업·색인·턴을 잠근다. 화자·원문 변경은 새 텍스트 개정을 확정한다. 텍스트 개정 외부에서 역할이나 마스킹 정책을 바꾸는 담당은 advance_personalization_source를 먼저 호출해 이전 프로필·색인을 무효화하고 새 스냅샷을 만든다. 실패한 요청을 무조건 재시도하지 않고 현재 스탬프·작업 결과를 다시 조회한다.

스냅샷의 masking_policy_version은 호출부가 적용한 정책의 식별자다. 이 구현은 실제 민감정보 탐지·마스킹을 수행하지 않는다. 운영 마스킹 합의 전 시험은 합성 자료를 사용한다.

## RPC 인자

아래 모든 함수에는 첫 인자로 p_user_id uuid가 있다. 시간은 시간대를 포함한 ISO 8601을 사용하며 DB에서 받은 마이크로초를 보존한다. UUID는 JSON 문자열, jsonb는 JSON 객체·배열로 보낸다.

| 함수 | 나머지 인자 | 반환 |
|---|---|---|
| confirm_record_text_v2 | p_text_version_id uuid, p_source_format text, p_units jsonb, p_speakers jsonb | 확정 텍스트 행 |
| capture_personalization_snapshot | p_persona_id uuid, p_snapshot_id uuid, p_masking_policy_version text | 불변 스냅샷 행 |
| advance_personalization_source | p_persona_id uuid, p_expected_stamp uuid | 새 스탬프 UUID |
| start_profile_analysis | p_snapshot_id uuid, p_operation_id uuid, p_deadline timestamptz, p_runtime jsonb | 작업·시도 행 |
| start_profile_analysis_node | p_operation_id uuid, p_attempt integer, p_node_id uuid, p_request_id uuid, p_kind text, p_ranges jsonb, p_children uuid[] | 노드 행 |
| finish_profile_analysis_node | p_operation_id uuid, p_attempt integer, p_node_id uuid, p_request_id uuid, p_runtime jsonb, p_status text, p_content jsonb, p_model_name text, p_error_code text | 노드 행 |
| publish_analysis_profile | p_operation_id uuid, p_attempt integer, p_root_node_id uuid | 게시된 프로필 행 |
| get_profile_analysis_result | p_operation_id uuid, p_attempt integer | 커밋 결과 JSON |
| start_record_index_build | p_snapshot_id uuid, p_build_id uuid, p_embedding_model text, p_embedding_revision text, p_dimensions integer | 빌드 행 |
| append_record_index_chunks | p_build_id uuid, p_chunks jsonb | 요청 배치의 청크 수 |
| activate_record_index_build | p_build_id uuid | 활성 빌드 행 |
| fail_record_index_build | p_build_id uuid | 실패 빌드 행 |
| read_persona_context | p_persona_id uuid, p_embedding_model text, p_embedding_revision text, p_query_embedding vector, p_limit integer, p_min_similarity double precision | 스탬프·스냅샷·프로필·근거 JSON |
| get_record_reflection | p_record_id uuid | none, pending, reflected, failed |
| bind_persona_chat_turn | p_turn_id uuid, p_request_id uuid, p_snapshot_id uuid, p_build_id uuid, p_profile_id uuid | 턴 입력 결속 행 |
| check_persona_chat_turn | p_turn_id uuid, p_request_id uuid, p_attempt integer | true 또는 오류 |
| finish_persona_chat_turn | p_turn_id uuid, p_request_id uuid, p_attempt integer, p_ok boolean, p_text text, p_ms integer, p_error_code text, p_total_ms integer, p_finish_reason text | 턴 행 |

기존 SQL RPC와 Python 함수의 서명은 유지한다. 새 개인화 처리로 전환한 페르소나는 publish_analysis_profile을 사용한다. 분석 작업이 등록된 페르소나의 새 프로필을 기존 게시 함수로 넣는 호출은 현재 시도 검사를 우회하므로 거부한다.

## 텍스트와 화자 확인

p_source_format은 plain, kakao, audio다. 기록은 ready, 텍스트는 draft여야 한다. 확정 RPC가 기존 버전을 superseded로 전환하고 자료 개정도 함께 갱신한다.

p_units는 원본 텍스트 안의 순서대로 정렬한 배열이다. 각 항목은 start, end와 선택적인 speaker, date를 받는다. 범위는 0부터 시작하는 Unicode 코드포인트 [start,end)이며 UTF-8 바이트와 다르다. 실제 파서가 모든 발화를 이 배열에 넣어야 한다. DB는 범위의 유효성·순서·비중첩과 화자 선택을 검사한다.

~~~json
[
  {"start":0,"end":5,"speaker":"대상","date":"2026-11-24"},
  {"start":6,"end":10,"speaker":"제공자","date":"2026-11-24"}
]
~~~

일반 텍스트·음성에는 speaker를 보내지 않고 전체를 persona 역할로 본다. 카카오톡은 기록 안의 이름을 기준으로 target·provider·other를 결정한다. 각 발화에는 새 UUID를 발급해 확정 버전에 고정한다. 이 UUID를 이후 source unit 식별자로 사용한다.

p_speakers는 target_speaker·provider_speaker의 부분 객체다. 키 생략은 이전 확정본 또는 최초 업로드 확인값을 유지하고 명시적 null은 해제한다. 이전과 화자 집합이 바뀌면 명시적으로 재확인해야 하며, 실패한 초안은 draft, 이전 버전은 confirmed를 유지한다. 원본이 카카오톡이면 화자 형식을 제거한 편집본을 일반 텍스트로 바꿔 확정할 수 없다. 미확인 레거시 확정본은 추정해서 개인화하지 않고 새 초안에서 파서 메타데이터를 명시해 확정한다.

## 분석·전체 입력 범위·게시

1. capture_personalization_snapshot으로 현재 확정·동의·ready 자료를 한 트랜잭션에서 읽는다. 반환 manifest의 발화 UUID, 텍스트, 역할, 원본·텍스트 버전, 날짜·순서는 이후 변경되지 않는다. 같은 snapshot ID를 다른 입력으로 재사용하면 충돌이다.
2. start_profile_analysis를 호출한다. runtime은 model, tokenizer, template의 불변 버전 문자열을 담는다. attempt는 페르소나별로 서버가 증가시킨다. 같은 operation ID의 동일 요청은 기존 작업을 반환한다. 수동 재시도에는 새 operation ID를 사용한다.
3. 실제 AI 호출 전에 각 analyze 노드를 등록한다. p_ranges는 다음과 같으며 persona 역할의 발화만 포함한다. leaf의 children은 빈 배열이다.

~~~json
[{"unit_id":"11111111-1111-4111-8111-111111111111","start":0,"end":5}]
~~~

4. 완료 결과는 finish_profile_analysis_node로 저장한다. status는 done·failed·unknown이며 done에만 content와 model_name을 보낸다. request ID와 runtime은 등록 당시 값과 같아야 한다. 실패·불명은 작업을 종료하며 새 시도를 자동으로 만들지 않는다.
5. merge 노드는 같은 작업·시도의 완료된 자식 2~8개를 참조하고 ranges는 빈 배열로 보낸다. 자식 결과와 범위가 결속되며 입력이 바뀌는 노드 ID 재사용은 거부한다.
6. 마지막 root를 게시한다. 최신 시도·기한·원본 스탬프·스냅샷·전체 persona 입력의 연속 범위를 검사한다. 누락과 중복을 모두 거부하고, 프로필·출처·커밋 영수증을 같은 트랜잭션에 저장한다. 같은 root의 재호출은 같은 프로필을 반환한다.

get_profile_analysis_result의 publication_state는 committed·not_committed·unresolved다. committed는 프로필 ID를 보존한다. 실패·중단·기한 만료·후속 시도로 더 이상 게시할 수 없는 작업만 not_committed다. 아직 게시할 수 있는 실행 중 작업은 unresolved다. 없는 작업의 P0002나 네트워크 실패는 미커밋의 증명이 아니며 UNKNOWN으로 다뤄야 한다.

## 색인과 반영 상태

인코더 ID·리비전·차원은 빌드에 고정한다. 청크 배치를 여러 번 저장할 수 있지만 전체 원본 범위를 검증하기 전에는 활성화할 수 없다. 실패한 교체는 기존 유효 빌드를 유지한다. 새 빌드가 완전하면 한 트랜잭션에서 활성 빌드를 교체한다.

청크 항목은 id, chunk_no, content, coverage, embedding을 받는다. coverage는 노드 범위와 같은 형식이며 같은 텍스트 버전·화자·역할·날짜의 인접 범위만 한 청크에 담는다. content는 그 범위를 이어 붙인 실제 텍스트와 같아야 한다. 모델·차원이 다른 벡터, 영벡터, 변경된 청크 ID 재사용을 거부한다. vector 인자는 PostgREST에 "[1,0]"처럼 보낸다.

reflection은 확정·화자 확인 전이면 none, 현재 색인과 프로필 양쪽이 같은 스냅샷을 반영했으면 reflected다. 나머지는 현재 작업·빌드의 실패 여부에 따라 failed 또는 pending이다. 기록 저장이나 GPU HTTP 200만으로 reflected를 만들지 않는다.

## 대화 호출과 결과 저장

read_persona_context는 같은 원본 스탬프의 활성 인덱스와 현재 프로필을 함께 읽는다. 임베딩 모델·리비전·차원을 확인하고 근거는 1~10개 한도에서 반환한다. 결과의 chunk ID, 역할, 원본·확정본 ID, coverage를 AI 요청을 조립할 때까지 보존한다.

LLM 시도는 기존 start_chat_stage_v2로 먼저 등록한다. 그 뒤 context의 snapshot·build·profile ID를 bind_persona_chat_turn으로 연결하고, AI를 호출하기 직전에 check_persona_chat_turn을 호출한다. 성공 답변은 finish_persona_chat_turn으로 저장한다. 기존 완료 RPC로 호출해도 새 턴 가드가 같은 입력의 유효성을 검사한다. 입력 연결 행이 정리된 뒤에도 턴의 결속 표식이 남아 오래된 결과 저장을 차단한다.

## 오류와 배포

22023·23514·22P02는 인자·무결성 오류, P0002는 없거나 소유권 밖의 대상, 55000은 입력·상태 충돌이다. P1201·P1202·P1203은 각각 SPEAKER_REQUIRED·SPEAKER_NOT_FOUND·SPEAKER_NOT_ALLOWED로 매핑한다. HTTP 상태만으로 분류하지 않고 PostgREST 본문의 code를 사용한다.

원격 적용은 새 증분 패치 이름 personalization_lifecycle_121로 한 번 실행한다. 기존 스키마는 지우거나 재실행하지 않는다. verify_personalization_lifecycle.sql은 합성 사용자·행을 롤백한다. 복구 시 백엔드의 새 개인화 경로 활성화를 되돌리고 추가 데이터·RPC는 보존한다. 자료 정리 worker는 관련 snapshot·노드·색인도 삭제 범위에 포함해야 한다. 과거 대화 보존 정책은 #123을 따른다.

후속 파일 20261124050058_personalization_lifecycle_followup.sql은 그다음에 personalization_lifecycle_121_followup으로 한 번 적용한다. 외래 키 인덱스와 만료된 분석의 failed 판정을 보완하며, 이미 배포한 원본 파일은 다시 실행하지 않는다.

2026-11-24 개발 프로젝트에 첫 패치를 20261124045853, 후속을 20261124050317로 적용했다. 두 패치 적용 뒤 원격 롤백형 검증을 통과했다. 실제 PostgREST 요청 8개의 시도 등록 경합에서 시도 번호 1~8을 중복 없이 받았고, 같은 결과를 동시에 게시한 두 요청은 같은 프로필 ID와 committed 영수증을 반환했다. 두 계정의 스냅샷 조회 격리, 활성 색인·프로필의 동일 스탬프, 원본 변경 뒤 컨텍스트 차단도 확인했다. 임시 계정과 합성 데이터는 정리했다.

~~~powershell
npm install --prefix .cache/db-check --no-package-lock --no-save @electric-sql/pglite@0.3.14
node infra/supabase/tests/verify_personalization_lifecycle.mjs .cache/db-check/node_modules/@electric-sql/pglite/dist/index.js
~~~

검증기는 저장소의 기존 SQL 전체와 새 패치를 적용한 뒤 롤백형 시나리오를 실행한다. 실제 GPU·마스킹·앱 연결은 해당 파트의 후속 통합 검증이다.

## Python 호출과 검증

`await Database(...).for_user(access_token)`으로 얻은 `UserDatabase`에서 SQL 이름과 같은 비동기 메서드를 호출한다. `p_user_id`는 전달하지 않는다. 메서드의 정확한 인자·기본값은 [Python 함수 표](api-spec.md#개인화-python-함수-121)에 있으며, SQL의 `p_ms`는 Python에서 `elapsed_ms`다.

```python
snapshot = await user.capture_personalization_snapshot(
    persona_id, snapshot_id=uuid4(), masking_policy_version="synthetic-v1")
operation = await user.start_profile_analysis(
    snapshot["id"], operation_id=uuid4(), deadline=deadline, runtime=runtime)
result = await user.get_profile_analysis_result(
    operation["operation_id"], attempt=operation["attempt"])
```

2026-11-24 Python 함수 18개의 전송 인자·검증된 소유자 주입·화자 SQLSTATE 매핑을 포함해 백엔드 오프라인 시험 219개와 하위 검사 29개가 통과했다(실연결 선택 검사 8개는 기본 실행에서 제외). 별도로 `REPLICA_DB_MUTATING_TEST=1`을 설정하고 `python -m pytest -q tests/test_db_full_live.py`를 실행해 실제 개발 Supabase 대상 시험 2개가 통과했다. 기존 공개 함수 32개의 호환성과 새 함수 18개의 확정→스냅샷→분석·병합→게시→색인→대화 저장→원본 무효화를 확인했다. 각 시험은 임시 계정 2개와 합성 자료를 생성하고 종료 시 정리한다. 실행에는 로컬 `backend/.env`의 Supabase 설정이 필요하며 키·토큰은 출력하거나 커밋하지 않는다.
