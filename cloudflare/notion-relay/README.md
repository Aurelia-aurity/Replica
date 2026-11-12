# Notion 변경 요청 및 주기적 동기화 relay

Notion의 작업 상태 변경, 로컬 에이전트 helper의 확인된 Project 일반 상태 변경, 그리고 분당 `scheduled()` cron의 주기적 전체 스캔 요청을 GitHub Actions의 일반 전체 동기화 워크플로(`notion-sync-request.yml`)로 연결합니다. Worker 자체는 Project나 Notion에 쓰기 작업을 수행하지 않습니다. 지정 사용자의 개인 액세스 토큰(PAT)으로 고정된 워크플로를 호출하고 실행 허가와 결과 재조회 확인을 D1에 기록합니다.

저장소의 `wrangler.jsonc`는 `STOPPED` 템플릿이며 실제 D1 ID·URL·비밀값은 운영자가 저장소 밖에서 관리합니다. 아래 설명은 새 단순 동기화 엔진(`sync_simple.py`) 및 주기적 스캔 연동 기준의 구현 계약이며 과거 Issue #38 당시 구형 엔진으로 수행했던 실환경 확인 이력과 구분합니다. 이번 변경의 실제 D1 마이그레이션 적용·Worker 배포·외부 API 연동·주기 실측은 별도 운영 전환 단계에서 수행합니다.

## 로컬 확인

기존 의존성이 설치된 상태에서 이 디렉터리와 저장소 루트에서 다음 명령으로 검증합니다(현재 작업 트리에는 Miniflare가 설치되어 있지 않으며 로컬 단위 테스트는 합성 응답과 SQLite를 사용합니다).

```sh
npm test
npm run test:runtime
npm run test:runtime:helper
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py'
```

- 웹훅의 `api_version`은 전달 메타데이터로만 취급하며 중복 판정에 사용하지 않습니다.
- 서명과 구독 결속이 맞더라도 `작업 상태` 외의 속성(예: 동기화 엔진이 갱신하는 `동기화 기준` 등)만 바뀐 이벤트는 기록하지 않고 `200`(`ignored`)으로 응답해 불필요한 재전송을 막습니다.
- 페이지 조회는 Notion API `2026-03-11`을 사용하며 `in_trash === false`를 확인합니다.
- 런타임 검사는 로컬 D1과 합성 응답을 사용하며 외부 API에 연결하지 않습니다.

## 동작 방식: 이벤트 드레인과 주기적 스캔

1. **웹훅 및 helper 이벤트 처리:** Notion 웹훅 수신이나 helper의 Project 변경 완료 후 대기 중인 요청이 있으면 일반 동기화 워크플로 실행을 요청합니다.
2. **분당 cron과 최소 600초(10분) 주기 요청:** Worker의 `scheduled()` 핸들러는 매분 실행되어 대기 중인 이벤트를 드레인하고 마지막 요청 이후 `PERIODIC_MIN_SECONDS`(기본값 600초/10분, 600 미만이나 잘못된 숫자 값은 거부) 이상 지났으면 수신 이벤트가 없더라도 주기적 전체 스캔을 요청합니다. 따라서 GitHub Project에서만 카드를 옮긴 경우에도 별도 웹훅 없이 주기적 스캔으로 동기화됩니다. 이 10분 간격은 Worker의 주기적 실행 요청에만 적용되며 웹훅·helper 이벤트 실행이나 별도의 GitHub 5분 예약 실행까지 10분당 1회로 제한하지는 않습니다.
3. **중복 방지와 원격 실행 확인:** 트랜잭션 기반 `request_slot`과 주기 제한 검사로 중복 cron POST를 방지하며 요청 전 두 동기화 워크플로(`notion-sync.yml`, `notion-sync-request.yml`) 전체에 대해 원격 실행 중인 작업이 없는지 확인합니다. 런타임·API·GitHub Actions 대기 시간 때문에 실제 반영 주기가 일정 시간 이내로 보장되지는 않습니다. 54개 Issue와 47개 PR 규모의 결정론적 CLI 픽스처 기준 합성 호출 예산은 실행당 REST 요청 5회(실행 결속 1회 + 저장소 1회 + `/issues` 2회 + `/pulls?state=all` 1회)이므로 Worker 주기적 요청만 따졌을 때의 기본 예산은 시간당 최대 6회 실행·REST 요청 30회이며 GitHub 이벤트와 자체 예약 실행은 별도로 추가됩니다.
4. **주기적 요청의 검증과 슬롯 복구:** 주기적 요청은 개별 수신 ID가 없지만 이벤트 기반 요청과 동일하게 신뢰된 일반 워크플로, 정확한 `main` SHA·실행자 결속, 일회용 실행 허가, 최대 10행 단위의 해시 연결 chunk 및 seal 검증을 거칩니다. 일반 실행 경로는 이미 확보한 동기화 사실 스냅샷과 확정된 Project 결과를 수집 단계에 재사용하고 콜백 전달 전에 계획 산출물을 먼저 저장합니다. 전달 여부가 불확실한 실행 요청은 자동으로 재전송하지 않습니다. 수신 ID가 없는 주기적 실행이 명확한 실패로 확인되면 증거를 보존한 채 슬롯을 해제해 다음 새로운 전체 대조가 진행될 수 있게 합니다. 반면 기존 수신 ID가 묶인 요청이나 전달 불확실 펜스는 운영자 확인 및 복구가 필요합니다.

## 상태와 증거 재전송

- `ACTIVE`는 신규 수신·예약·주기적/이벤트성 실행 요청을 허용합니다. `DRAINING`은 신규 작업과 신규 실행 요청을 막고 진행 중인 허가·결과 처리를 마칩니다. `STOPPED`는 새 허가와 실행 요청을 막지만 이미 시도한 작업 결과와 이미 실행을 허가받은 실행 증거는 기록합니다.
- 일반 실행에서 결과 콜백 전달이 실패한 경우, 실행 산출물에서 받은 `notion-relay-evidence.json`을 사용해 저장된 콜백만 다시 보낼 수 있습니다. 이 명령은 일회용 실행 허가 요청이나 동기화 엔진을 다시 실행하지 않습니다.

```sh
python3 -B scripts/ordinary_sync_request.py --deliver-evidence /absolute/path/notion-relay-evidence.json
```

<a id="에이전트의-일반-상태-변경"></a>

## 에이전트의 일반 상태 변경 (`project_status_request.py`)

macOS/Linux(`fcntl` 지원) 환경에서 에이전트가 허용받은 기존 Issue의 상태를 `백로그`·`준비 중`·`진행 중` 중 하나로 바꿀 때 사용합니다.

```sh
OPERATION_ID=$(python3 -c 'import uuid; print(uuid.uuid4())')
python3 -B scripts/project_status_request.py \
  --issue "$ISSUE_NUMBER" --page-id "$NOTION_PAGE_ID" --target "진행 중" \
  --operation-id "$OPERATION_ID"
```

- 실행 전 D1 예약을 고정하고 단 한 번 발급된 허가로 Project를 수정합니다.
- `CONFIRMED`·`CONFIRMED_NOOP`는 Project 상태 확인과 동기화 요청 등록을 뜻하며 Notion에 이미 반영되었다는 뜻이 아닙니다(`notion_reflected: false`).
- 명령 실패나 응답 유실(`DELIVERY_UNKNOWN`) 시에는 반환된 기존 UUID와 로컬 `.replica-status-requests` 저널을 보존해 동일한 인자로 재실행해야 하며 불확실한 상태에서 새 UUID를 만들거나 Project 변경을 별도로 재시도하지 않습니다. 변경 전 `PREPARED` 상태의 예약만 `--abort`로 안전 취소할 수 있습니다.

## 토큰 권한과 구성 변수

실제 비밀값과 ID는 저장소에 기록하지 않고 운영자가 관리합니다. 고정 호스트 검증, 리다이렉트 금지, 오류 마스킹, HMAC 키 분리를 유지합니다.

| 위치 | 설정 | 설명 |
| --- | --- | --- |
| Worker secret | `GITHUB_DISPATCH_TOKEN` | 지정 사용자의 classic PAT(`repo` 범위) 또는 협업자 환경에서 지원되는 fine-grained PAT(Actions 쓰기 + Contents/metadata 읽기). 저장소 push 및 Actions dispatch/읽기 권한이 필요하며 `/user`로 numeric user ID를 확인합니다. |
| Worker vars | `GITHUB_WORKFLOW_ID`, `GITHUB_DISPATCH_ACTOR_ID` | `notion-sync-request.yml` ID와 지정 사용자의 numeric user ID |
| Worker vars (선택) | `PERIODIC_MIN_SECONDS` | 주기적 전체 스캔 최소 간격(초). 기본값은 `600`(10분)이며 `600` 미만이나 잘못된 숫자 값은 거부합니다. |
| Worker secret | `NOTION_READ_TOKEN` | 지정된 데이터 소스만 읽는 읽기 전용 Notion 토큰 (Worker에는 Project/Notion 쓰기 토큰을 두지 않음) |
| Worker vars | `NOTION_KIND_PROPERTY_ID`, `NOTION_KEY_PROPERTY_ID`, `NOTION_TASK_PROPERTY_ID`, `NOTION_STATE_PROPERTY_ID` | 종류·동기화 키·작업 상태 및 **새 `동기화 기준` 속성 ID(`NOTION_STATE_PROPERTY_ID`)** |
| Worker vars | `NOTION_WORKSPACE_ID`, `NOTION_INTEGRATION_ID`, `NOTION_SUBSCRIPTION_ID` | 웹훅 결속 식별자 |
| Worker secret | `NOTION_VERIFICATION_TOKEN` | 확인된 Notion 웹훅 검증 토큰 |
| Worker secret / helper | `HELPER_REQUEST_KEY` | helper 예약·허가·결과 보고용 HMAC 키 (`WORKFLOW_CALLBACK_KEY`와 반드시 분리) |
| Worker secret / GitHub secret | `WORKFLOW_CALLBACK_KEY` / `NOTION_RELAY_CALLBACK_KEY` | 워크플로 실행 허가·chunk·seal 보고용 HMAC 키 |
| GitHub vars | `NOTION_RELAY_ENABLED`, `NOTION_RELAY_URL`, `NOTION_RELAY_HOST`, `NOTION_RELAY_ACTOR_ID`, `NOTION_RELAY_WORKFLOW_ID` | 정확한 HTTPS 호스트(경로·쿼리·포트 없음) 및 Worker와 일치하는 워크플로·actor 결속 |
| GitHub sync vars & secrets | `PROJECT_TOKEN`, `NOTION_TOKEN`, `PROJECT_ID`, `PROJECT_OWNER_ID`, `PROJECT_STATUS_FIELD_ID`, `PROJECT_STATUS_OPTIONS`(6개), `NOTION_DATA_SOURCE_ID`, `PM_GITHUB_USER_ID`, `NOTION_SYNC_ENABLED` | GitHub Actions 워크플로에서 사용하는 동기화 설정 (`NOTION_CONTROL_PAGE_ID`, `NOTION_BIDIRECTIONAL_ENABLED`는 새 엔진에서 미사용하나 롤백용으로 보존) |

## 운영 전환 및 롤백 주의사항

- **전환:** 기존 변수 값을 백업하고 동기화·relay를 먼저 중지·드레인한 뒤, Notion에 숨김 `rich_text` 속성 `동기화 기준`을 추가하고 과거 Sync 관리행은 상태 신호가 아니므로 숨기거나 이력용 행임을 표시합니다. **Worker를 재배포하기 전에 반드시 D1 마이그레이션 `migrations/0004_periodic.sql`을 먼저 적용해야 합니다.** 그 후 검토된 Worker를 `STOPPED` 상태로 배포하면서 `NOTION_STATE_PROPERTY_ID`를 새 `동기화 기준` ID로 바인딩합니다. 승인된 브랜치 dry-run과 수동 초기 정렬, `main` 병합을 마친 뒤 동기화와 relay를 켜고 첫 전체 스캔 및 재조회 확인 결과와 D1 `rows_read`·`rows_written`·저장 용량을 측정합니다.
- **롤백:** 모든 쓰기 경로와 relay를 먼저 중지·드레인합니다. `동기화 기준`과 기존 `동기화 내부 상태` 속성 및 설정값을 모두 보존한 채 Worker의 `NOTION_STATE_PROPERTY_ID`와 승인된 이전 코드를 복원합니다. 새 엔진이 동작한 뒤에는 구형 기준선이 오래된 상태이므로, 두 엔진을 동시에 켜거나 상태를 지우지 말고 반드시 기준선을 점검·정렬한 뒤 재가동합니다.

## 보존 한도와 미검증 사항

- **D1 마이그레이션과 상한·정리:** `migrations/0004_periodic.sql`은 `created_at`, `(phase,receipt_ids,updated_at)`, `(phase,created_at)`, 수신 기반 요청 집계 및 `receipts(request_id)` 인덱스와 빈 값을 허용하는 열 `relay_meta.last_periodic_prune_at`을 추가합니다. 수신 기반 일반 요청 1,000건, helper 기록 1,000건, 웹훅 수신 19,000건, 실행당 관측 20,000행 상한을 유지하며 주기적 스캔 이력은 일반 요청 누적 상한 밖에서 관리합니다. 기록 정리는 재시작이나 동시 tick 사이에서도 유지되는 영속적 CAS 가드를 통해 최대 1시간에 한 번만 시도하며 더 새로운 활성 스캔이 확인된 경우에 한해 7일이 지난 참조되지 않는 비현재 성공 주기적 요청 기록만 정리합니다. 실패·불확실 기록 및 helper·웹훅 증거는 삭제하지 않습니다.
- **이력 구분 및 미검증 범위:** 2026-10-16(Issue #38)의 실환경 반영 기록은 구형 엔진 기준의 과거 이력입니다. 이번 작업에서는 푸시·PR 반영·실제 D1 마이그레이션·Worker 배포·디스패치·외부 쓰기를 수행하지 않았으며 새 단순 엔진과 주기적 요청에 대한 실제 Cloudflare Free CPU·API·D1(`rows_read`·`rows_written`·저장 용량) 쿼터, 라이브 웹훅/Actions 연동, 알림 전달 및 실제 실행 주기는 아직 실환경에서 검증되지 않았습니다.
