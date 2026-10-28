# Notion 변경 요청 relay

Notion의 작업 상태 변경과 로컬 agent의 일반 Project 상태 변경을 기존 전체 동기화로 연결합니다. Worker는 Project나 Notion을 수정하지 않습니다. 지정 사용자의 개인 액세스 토큰(PAT)으로 고정된 일반 workflow를 요청하고, 실행과 readback 결과를 D1에 기록합니다. 예약 전체 동기화는 누락된 이벤트를 복구하는 용도로, PM 재개 workflow는 기존대로 계속 사용합니다.

2026-10-16(UTC) 기준 운영 중입니다. Worker는 `ACTIVE`로 배포돼 분당 cron으로 동작하고, 저장소 변수 `NOTION_RELAY_ENABLED`는 `true`이며 Notion 웹훅 구독도 활성 상태입니다. 저장소의 `wrangler.jsonc`는 계속 `STOPPED` 템플릿이고 D1 ID는 자리표시자입니다. 실제 운영 설정은 운영자가 저장소 밖에서 관리하므로 이 저장소에 실제 ID나 URL을 넣지 않습니다. 초기 검증용 값은 설정 후 제거했고 `BOOTSTRAP_ENABLED`는 `false`입니다.

## 로컬 확인

Node 26에서 확인했습니다. `npm ci --ignore-scripts` 후 이 디렉터리에서 실행합니다.

웹훅의 `api_version`은 전달 메타데이터로만 취급하며 수신 중복 판정에는 사용하지 않습니다.
서명과 구독 결속은 맞지만 작업 상태가 아닌 속성만 바뀐 이벤트는 기록하지 않고 `200`(`ignored`)으로 응답합니다. 기존 동기화가 내부 상태를 갱신할 때마다 이런 이벤트가 생기는데, 오류 응답을 주면 Notion이 재전송을 반복하다 구독을 멈출 수 있습니다. 거절할 때는 오류 코드만 로그에 남깁니다.
페이지 조회는 Notion API `2026-03-11`을 사용하며 `in_trash`가 `false`인지 확인합니다. 이 버전에서 삭제된 `archived` 필드는 요구하지 않습니다.

```sh
npm test
npm run test:runtime
npm run test:runtime:helper
```

런타임 검사는 Miniflare 5 beta의 공식 변환 API와 로컬 D1을 사용합니다. GitHub·Notion 응답과 Project 변경은 합성 데이터입니다. 외부 API로 연결하지 않으며 매번 새 로컬 저장소를 만듭니다. 검사 기록과 미확정 작업 기록은 자동 삭제하지 않습니다. 로컬 포트를 열 권한이 필요합니다. 저장소 루트에서는 다음 검사도 실행합니다.

```sh
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py'
```

## 상태와 복구

`ACTIVE`는 신규 수신·예약·실행 요청을 허용합니다. `DRAINING`은 신규 작업과 신규 실행 요청을 막고 기존 허가·결과 처리를 이어갑니다. `STOPPED`는 새 허가를 막지만 이미 시도한 작업의 결과와 이미 claim한 실행 증거는 기록합니다. 안전 취소는 D1의 `PREPARED` 상태에서만 가능합니다.

helper는 같은 자원의 예약을 D1에 고정한 뒤, 단 한 번 발급된 변경 허가를 받아 Project를 수정합니다. 응답을 잃으면 새 UUID로 다시 변경하지 않습니다. 로컬 기록의 기존 UUID로 재실행하여 저장된 결과만 전달합니다. `UNKNOWN_FENCED`나 `REJECTED_FENCED` 예약은 값이 목표와 같아져도 자동 해제하지 않습니다.

실행 요청도 POST 전에 D1에 기록합니다. 전달 여부가 불확실하면 고정된 run-name과 저장소·main SHA·workflow·actor·attempt로 기존 실행을 찾습니다. 실행 요청 POST는 자동 재전송하지 않습니다. 명확한 거절도 현재 구현에서는 예약을 유지하고 운영자 확인을 기다립니다. 이는 3회 상한보다 보수적인 처리입니다.

실행은 one-time claim 이후에만 기존 동기화를 수행합니다. 결과는 최대 10행씩 연결된 해시로 저장하며, 누락 없는 seal과 별도로 조회한 실행의 정상 종료가 모두 있어야 관측을 활성화합니다. 작업 보류나 불확실 상태는 반영 완료로 처리하지 않습니다. HTTP 성공·Actions의 녹색 표시·수신 generation만으로 완료를 판단하지 않습니다.

일반 실행 결과 전송이 실패하면 원본 artifact의 `notion-relay-evidence.json`을 확인합니다. 같은 이름의 파일을 신뢰하는 로컬 디렉터리에 두고, 원래 relay와 결과용 키를 지정한 뒤 다음 명령으로 증거만 재전송할 수 있습니다. 이 명령은 claim이나 동기화를 다시 실행하지 않습니다.

```sh
python3 -B scripts/ordinary_sync_request.py --deliver-evidence /absolute/path/notion-relay-evidence.json
```

## 에이전트의 일반 상태 변경

이 절차는 relay가 활성 상태일 때 사용합니다. 에이전트는 사용자가 변경을 허용한 기존 Issue와 연결된 Notion 페이지를 확인하고, `백로그`·`준비 중`·`진행 중` 중 하나만 요청합니다. 완료·종료·PM 보류 재개는 이 명령의 대상이 아닙니다.

로컬 환경에는 기존 `GITHUB_TOKEN`·`PROJECT_TOKEN`, `PROJECT_ID`·`PROJECT_OWNER_ID`·`PROJECT_STATUS_FIELD_ID`·`PROJECT_STATUS_OPTIONS`·`NOTION_DATA_SOURCE_ID`, 승인된 `NOTION_RELAY_URL`·`NOTION_RELAY_HOST`·`HELPER_REQUEST_KEY`가 필요합니다. 별도의 Notion 쓰기 토큰이나 실행 결과용 키를 helper에 전달하지 않습니다. 비밀값은 명령 인자·채팅·저장소에 넣지 않습니다.

다음 예시는 `ISSUE_NUMBER`와 `NOTION_PAGE_ID`에 확인한 대상이 설정돼 있다고 가정합니다. 새 요청의 UUID를 먼저 기록하고 결과와 함께 보관합니다.

```sh
OPERATION_ID=$(python3 -c 'import uuid; print(uuid.uuid4())')
python3 -B scripts/project_status_request.py \
  --issue "$ISSUE_NUMBER" --page-id "$NOTION_PAGE_ID" --target "진행 중" \
  --operation-id "$OPERATION_ID"
```

`CONFIRMED`·`CONFIRMED_NOOP`는 Project 확인과 relay 수신 기록을 뜻합니다. 출력의 `notion_reflected`는 `false`이며, 실제 Notion 작업 상태와 내부 checkpoint를 별도로 확인한 뒤 반영 완료를 보고합니다. 명령이 실패하거나 `DELIVERY_UNKNOWN`이면 반환된 UUID와 `.replica-status-requests` 기록을 보존합니다. 같은 대상·목표·UUID·journal로 다시 실행하며 새 UUID로 우회하거나 Project 변경을 별도로 반복하지 않습니다. 시도 여부가 불명확한 작업은 자동으로 다시 변경하지 않습니다.

변경 전 `PREPARED` 예약만 같은 인자에 `--abort`를 더해 안전 취소할 수 있습니다. `UNKNOWN_FENCED`·`REJECTED_FENCED`는 운영자 확인 대상입니다. relay 장애 중에는 기존 5분 동기화가 현재 상태를 대조하지만, helper의 미확정 변경을 자동 재시도하거나 예약을 해제하지 않습니다. Windows는 현재 helper 지원 범위 밖입니다.

## 실제 환경 확인

2026-10-16(UTC)에 Issue #38로 양방향을 한 번씩 확인했습니다. 이 확인은 위의 로컬·fault-injection 검사와 별개로 실제 서비스에서 수행한 것입니다.

- Notion 카드를 `진행 중`에서 `준비 중`으로 11:42:01에 옮기자 relay가 실행을 요청하고 claim했습니다. 동기화 run 38895592497(held 0, project_changes 1)이 Project를 11:48:01에 `준비 중`으로 바꿨고 약 6분이 걸렸습니다. 수신 기록은 반영 완료(STRICT_REFLECTION)로 표시됐고, 이 동기화가 Notion에 쓴 변경은 `ignored_echo`(HANDLED_ECHO)로 분류됐습니다.
- 에이전트 helper(`scripts/project_status_request.py`)로 Project를 11:52:11에 `준비 중`에서 `진행 중`으로 바꾸자 결과가 `CONFIRMED`였습니다. relay run 38897047001(held 0, project_changes 0)이 Notion을 12:01:14에 `진행 중`으로 바꿨고 약 9분이 걸렸으며 보류는 없었습니다.

앞선 실제 시험에서 찾은 문제는 PR #49(User-Agent), #50(작업 상태 외 속성만 바뀐 이벤트를 `200`/`ignored`로 처리, main 조회에 PAT 사용, 오류 코드 로그), #51(Project 상태를 새로 조회, 보류 재표시 때 작업 상태 유지), #52(Notion 페이지를 새로 조회, 완료된 요청과의 경쟁 제거)에서 고쳤습니다.

동기화 엔진이 작업을 보류하면 relay는 `ENGINE_HELD`로 보고하며, 기존 PM 재개(`resolve_issue_numbers`)가 여전히 필요합니다. 보류된 상태에서 카드를 다시 옮긴 요청은 PM 재개가 두 번 필요할 수 있습니다. 먼저 `DEFERRED_REQUEST_PENDING`을 처리하고, 이어서 현재 Project 값을 승인합니다.

알려진 한계와 후속 과제는 다음과 같습니다.

- 정상 상태의 Issue마다 동기화 실행당 Project 직접 조회 1회와 Notion 페이지 조회 1회가 추가됩니다.
- Discord 알림 job이 자체 상태 commit 직후 ledger `readback_mismatch`로 실패할 수 있습니다. relay와는 별도로 #18 범위에서 다룰 후속 과제입니다.

## 운영

멈출 때는 `RELAY_MODE`를 `STOPPED`로 두고 cron을 제거한 설정으로 다시 배포합니다. 이어서 저장소 변수 `NOTION_RELAY_ENABLED`를 `false`로 바꾸고 Notion 구독을 일시 중지합니다. 다시 시작할 때는 먼저 `ACTIVE`와 cron을 넣은 설정으로 배포하고, `NOTION_RELAY_ENABLED`를 `true`로 바꾼 뒤 Notion 구독을 재개합니다. cron을 새로 붙이면 첫 실행까지 몇 분 걸릴 수 있습니다.

상태는 D1의 receipts와 requests 단계에서 확인합니다. Worker 로그에는 `ProtocolError` 코드만 남으므로 원인 추적은 이 코드와 D1 기록을 함께 봅니다.

PAT은 classic PAT이며 `repo` 범위이고 2026-12-30에 만료됩니다. 교체하면 Worker secret `GITHUB_DISPATCH_TOKEN`을 갱신하고 소유자 ID·저장소 권한·main 조회를 다시 검증합니다.

## 구성

이 표는 필요한 설정 항목입니다. 실제 값은 운영자가 관리하며 이 저장소에는 두지 않습니다. 비밀값은 채팅이나 저장소에 넣지 않습니다.

| 위치 | 설정 | 범위 |
| --- | --- | --- |
| GitHub 사용자 | Replica 쓰기 권한이 있는 지정 계정 | 실제 PAT 소유자의 numeric 사용자 ID를 확인 |
| Worker secret | `GITHUB_DISPATCH_TOKEN` | 개인 PAT을 숨김 입력으로 등록, 코드·일반 변수·로그에 저장하지 않음 |
| Worker vars | `GITHUB_WORKFLOW_ID`, `GITHUB_DISPATCH_ACTOR_ID` | main의 `notion-sync-request.yml`과 지정 사용자의 실제 numeric ID |
| Worker secret | `NOTION_READ_TOKEN` | 대상 데이터 소스만 읽는 별도 Notion 연결 |
| Worker vars | `NOTION_KIND_PROPERTY_ID`, `NOTION_KEY_PROPERTY_ID`, `NOTION_TASK_PROPERTY_ID`, `NOTION_STATE_PROPERTY_ID` | 기존 종류·동기화 키·작업 상태·내부 상태 속성의 실제 ID |
| Worker vars | `NOTION_WORKSPACE_ID`, `NOTION_INTEGRATION_ID`, `NOTION_SUBSCRIPTION_ID` | 정확한 웹훅 결속 |
| Worker secret | `NOTION_VERIFICATION_TOKEN` | 수동으로 확인한 웹훅 검증 토큰 |
| Worker secret / local helper | `HELPER_REQUEST_KEY` | 일반 예약·허가·결과 요청용, 결과 callback 키와 반드시 다름 |
| Worker secret / GitHub secret | `WORKFLOW_CALLBACK_KEY` / `NOTION_RELAY_CALLBACK_KEY` | 실행 claim·chunk·seal용, Project/Notion 쓰기 권한 없음 |
| GitHub vars | `NOTION_RELAY_URL`, `NOTION_RELAY_HOST` | query·path·port 없는 정확한 HTTPS `workers.dev` 호스트 |
| GitHub vars | `NOTION_RELAY_WORKFLOW_ID`, `NOTION_RELAY_ACTOR_ID` | Worker 설정과 동일 |
| GitHub vars | `NOTION_RELAY_ENABLED` | 모든 결속 확인 전 `false` |
| GitHub ordinary workflow | 기존 `PROJECT_TOKEN`, `NOTION_TOKEN`과 기존 sync vars | 기존 동기화 권한을 사용하며 Worker에는 전달하지 않음 |

고정 자원은 repository `1392442366` (`Aurelia-aurity/Replica`), Project `PVT_kwHOBda_2c4BmKKV`, Status field `PVTSSF_lAHOBda_2c4BmKKVzhk0RA4`, 데이터 소스 `398c7ff6-9c66-43de-ba4d-3f2670b25a87`입니다. PAT으로 사용자 ID·저장소 ID·쓰기 권한을 먼저 검증한 뒤, 같은 PAT으로 main ref를 읽습니다. Cloudflare 공용 IP에서 무인증 조회 한도에 걸리는 것을 피하기 위해서입니다. 비공개 저장소는 지원 범위 밖입니다.

다른 개인 계정이 소유한 저장소의 협업자 접근에는 fine-grained PAT 제한이 있으므로 현재 운영은 classic PAT을 사용합니다. workflow 실행에 필요한 `repo` 범위와 만료일을 확인하고 별도 토큰을 사용합니다. classic PAT은 저장소 하나로 권한을 제한할 수 없으므로 Worker 코드의 고정 경로 검증이 토큰 자체의 권한을 줄여 주지는 않습니다. 기존 Project·Notion 토큰을 재사용하지 않습니다. PAT 교체 시 소유자 ID가 달라지면 Worker와 workflow의 지정 사용자 설정을 함께 갱신하고 다시 검증합니다.

처음 설치할 때의 순서는 다음과 같습니다.

1. 지정 사용자의 PAT·읽기 전용 Notion 연결·Cloudflare Free 계정을 정확한 대상으로 구성하고 ID와 권한을 확인합니다. App 설치나 App 개인키는 사용하지 않습니다.
2. main 반영은 별도 승인 후 진행합니다. 일반 workflow ID와 사용자 ID를 조회하되 `NOTION_RELAY_ENABLED=false`를 유지합니다. Draft에서 사용자·저장소·Actions 읽기를 먼저 확인할 수 있지만, main에 없는 새 workflow를 실행하거나 main 결속을 완화해서 시험하지 않습니다.
3. 실제 D1을 새로 만들고 대상 ID를 확인한 뒤 migration을 적용합니다. 기존 운영 DB 삭제·초기화·기록 정리는 이 절차에 포함되지 않습니다.
4. Worker는 `STOPPED`로 배포하고 숨김 입력으로 분리된 키를 등록합니다. 분당 cron은 신규 실행 요청을 허용하지 않습니다.
5. 웹훅 초기 검증 때만 로컬 RSA-OAEP 공개키와 일회용 nonce, 15분 만료를 설정하고 `BOOTSTRAP_ENABLED=true`를 사용합니다. Worker는 후보 토큰의 암호문만 저장합니다. 운영자가 로컬에서 복호화하여 실제 Notion UI에서 확인하고, 검증된 토큰을 별도 secret으로 등록합니다. 첫 수신 후보를 자동 신뢰하지 않습니다.
6. 초기 설정을 끄고 URL·속성·source·subscription·workflow·actor 결속과 Free 사용량을 확인합니다. 실제 서비스 검사 범위를 승인받은 뒤 활성화합니다.
7. 승인된 단일 Issue로 정상 변경·자체 이벤트 무시·지연된 실제 변경·helper 응답 손실·전체 실행 보류를 확인합니다. Project readback과 해당 Notion 상태·내부 checkpoint를 따로 확인합니다.

초기 검증 URL `/notion?setup=<nonce>`는 Notion 등록 후 유지합니다. 런타임은 query를 서명 권한으로 사용하지 않습니다. 요청 URL·query·본문·토큰을 로그로 남기지 않습니다. 실패한 초기 설정의 슬롯 재설정도 정확한 대상에 대한 운영자 작업으로 처리합니다.

## 무료 운영 범위

유료 플랜·도메인·자동 업그레이드는 사용하지 않습니다. 신규 helper 기록 1,000개, Notion 수신 19,000개, 실행 요청 1,000개, 한 실행 관측 20,000행을 상한으로 둡니다. 한 번의 drain은 최대 3개 최신 페이지를 조회합니다. 불확실 기록을 삭제해 공간을 만들지 않습니다. 상한이나 API 오류 때문에 relay가 막히면 기존 5분 동기화를 사용하되 relay의 불확실 기록은 그대로 남깁니다.

로컬 검사는 운영 Free 한도 충족을 증명하지 않으며, 운영 환경에서도 무료 한도 사용량은 아직 측정하지 않았습니다. RSA 서명·초기 암호화·chunk/seal·SQL의 CPU와 subrequest 수, D1 읽기·쓰기·저장 용량은 앞으로 확인해야 합니다. 분당 cron도 요청과 D1 사용량에 포함합니다. 서비스별 현재 한도는 [Workers limits](https://developers.cloudflare.com/workers/platform/limits/)와 [D1 limits](https://developers.cloudflare.com/d1/platform/limits/)에서 확인합니다. 무료 상한을 맞추지 못하면 비활성 상태와 기존 동기화를 유지합니다.
