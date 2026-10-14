# Notion 변경 요청 relay

Notion의 작업 상태 변경과 로컬 agent의 일반 Project 상태 변경을 기존 전체 동기화로 연결합니다. Worker는 Project나 Notion을 수정하지 않습니다. GitHub App으로 고정된 일반 workflow를 요청하고, 실행과 readback 결과를 D1에 기록합니다. 기존 5분 동기화와 PM 재개 workflow는 계속 사용합니다.

이 코드는 최종 검토 전 후보입니다. 실제 Cloudflare 계정, D1, GitHub App, Notion 연결, 비밀키, 웹훅은 아직 구성하지 않았습니다. `wrangler.jsonc`는 `STOPPED`이며 D1 ID는 자리표시자입니다. 별도 승인 없이 배포하거나 활성화하지 마세요.

## 로컬 확인

Node 26에서 확인했습니다. `npm ci --ignore-scripts` 후 이 디렉터리에서 실행합니다.

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

이 절차는 운영자가 relay와 일반 workflow를 확인하고 활성화한 뒤 사용합니다. 현재 후보는 배포 전입니다. 에이전트는 사용자가 변경을 허용한 기존 Issue와 연결된 Notion 페이지를 확인하고, `백로그`·`준비 중`·`진행 중` 중 하나만 요청합니다. 완료·종료·PM 보류 재개는 이 명령의 대상이 아닙니다.

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

## 배포 전에 확인할 구성

이 표는 필요한 설정안입니다. 실제 설치·키 등록·배포·활성화 승인과 값 확인은 최종 검토 후 별도로 받습니다. 비밀값은 채팅이나 저장소에 넣지 않습니다.

| 위치 | 설정 | 범위 |
| --- | --- | --- |
| GitHub App | Replica만 설치, Actions 쓰기·Metadata 읽기 | 단일 workflow로 권한을 줄일 수 없어 저장소 전체 Actions 권한 |
| Worker secret | `GITHUB_APP_PRIVATE_KEY_PKCS8` | App 키를 로컬에서 PKCS8로 변환하여 숨김 입력으로 등록 |
| Worker vars | `GITHUB_APP_CLIENT_ID`, `GITHUB_INSTALLATION_ID` | 실제 설치 식별자와 일치 |
| Worker vars | `GITHUB_WORKFLOW_ID`, `GITHUB_BOT_ACTOR_ID` | main의 `notion-sync-request.yml`과 해당 App bot의 실제 ID |
| Worker secret | `NOTION_READ_TOKEN` | 대상 데이터 소스만 읽는 별도 Notion 연결 |
| Worker vars | `NOTION_KIND_PROPERTY_ID`, `NOTION_KEY_PROPERTY_ID`, `NOTION_TASK_PROPERTY_ID`, `NOTION_STATE_PROPERTY_ID` | 기존 종류·동기화 키·작업 상태·내부 상태 속성의 실제 ID |
| Worker vars | `NOTION_WORKSPACE_ID`, `NOTION_INTEGRATION_ID`, `NOTION_SUBSCRIPTION_ID` | 정확한 웹훅 결속 |
| Worker secret | `NOTION_VERIFICATION_TOKEN` | 수동으로 확인한 웹훅 검증 토큰 |
| Worker secret / local helper | `HELPER_REQUEST_KEY` | 일반 예약·허가·결과 요청용, 결과 callback 키와 반드시 다름 |
| Worker secret / GitHub secret | `WORKFLOW_CALLBACK_KEY` / `NOTION_RELAY_CALLBACK_KEY` | 실행 claim·chunk·seal용, Project/Notion 쓰기 권한 없음 |
| GitHub vars | `NOTION_RELAY_URL`, `NOTION_RELAY_HOST` | query·path·port 없는 정확한 HTTPS `workers.dev` 호스트 |
| GitHub vars | `NOTION_RELAY_WORKFLOW_ID`, `NOTION_RELAY_BOT_ACTOR_ID` | Worker 설정과 동일 |
| GitHub vars | `NOTION_RELAY_ENABLED` | 모든 결속 확인 전 `false` |
| GitHub ordinary workflow | 기존 `PROJECT_TOKEN`, `NOTION_TOKEN`과 기존 sync vars | 기존 동기화 권한을 사용하며 Worker에는 전달하지 않음 |

고정 자원은 repository `1392442366` (`Aurelia-aurity/Replica`), Project `PVT_kwHOBda_2c4BmKKV`, Status field `PVTSSF_lAHOBda_2c4BmKKVzhk0RA4`, 데이터 소스 `398c7ff6-9c66-43de-ba4d-3f2670b25a87`입니다. 현재 공개 저장소의 main ref를 무인증 읽기로 확인하므로 App에 Contents 권한을 추가하지 않습니다. 비공개 저장소는 지원 범위 밖입니다.

승인 후 순서는 다음과 같습니다.

1. GitHub App·읽기 전용 Notion 연결·Cloudflare Free 계정을 정확한 대상으로 구성하고 ID와 권한을 확인합니다.
2. main 반영은 별도 승인 후 진행합니다. 일반 workflow ID와 bot ID를 조회하되 `NOTION_RELAY_ENABLED=false`를 유지합니다.
3. 실제 D1을 새로 만들고 대상 ID를 확인한 뒤 migration을 적용합니다. 기존 운영 DB 삭제·초기화·기록 정리는 이 절차에 포함되지 않습니다.
4. Worker는 `STOPPED`로 배포하고 숨김 입력으로 분리된 키를 등록합니다. 분당 cron은 신규 실행 요청을 허용하지 않습니다.
5. 웹훅 초기 검증 때만 로컬 RSA-OAEP 공개키와 일회용 nonce, 15분 만료를 설정하고 `BOOTSTRAP_ENABLED=true`를 사용합니다. Worker는 후보 토큰의 암호문만 저장합니다. 운영자가 로컬에서 복호화하여 실제 Notion UI에서 확인하고, 검증된 토큰을 별도 secret으로 등록합니다. 첫 수신 후보를 자동 신뢰하지 않습니다.
6. 초기 설정을 끄고 URL·속성·source·subscription·workflow·actor 결속과 Free 사용량을 확인합니다. 실제 서비스 검사 범위를 승인받은 뒤 활성화합니다.
7. 승인된 단일 Issue로 정상 변경·자체 이벤트 무시·지연된 실제 변경·helper 응답 손실·전체 실행 보류를 확인합니다. Project readback과 해당 Notion 상태·내부 checkpoint를 따로 확인합니다.

초기 검증 URL `/notion?setup=<nonce>`는 Notion 등록 후 유지합니다. 런타임은 query를 서명 권한으로 사용하지 않습니다. 요청 URL·query·본문·토큰을 로그로 남기지 않습니다. 실패한 초기 설정의 슬롯 재설정도 정확한 대상에 대한 운영자 작업으로 처리합니다.

## 무료 운영 범위

유료 플랜·도메인·자동 업그레이드는 사용하지 않습니다. 신규 helper 기록 1,000개, Notion 수신 19,000개, 실행 요청 1,000개, 한 실행 관측 20,000행을 상한으로 둡니다. 한 번의 drain은 최대 3개 최신 페이지를 조회합니다. 불확실 기록을 삭제해 공간을 만들지 않습니다. 상한이나 API 오류 때문에 relay가 막히면 기존 5분 동기화를 사용하되 relay의 불확실 기록은 그대로 남깁니다.

로컬 검사는 운영 Free 한도 충족을 증명하지 않습니다. 실제 배포 전에 RSA 서명·초기 암호화·chunk/seal·SQL의 CPU와 subrequest 수, D1 읽기·쓰기·저장 용량을 확인해야 합니다. 분당 cron도 요청과 D1 사용량에 포함합니다. 서비스별 현재 한도는 [Workers limits](https://developers.cloudflare.com/workers/platform/limits/)와 [D1 limits](https://developers.cloudflare.com/d1/platform/limits/)에서 확인합니다. 무료 상한을 맞추지 못하면 비활성 상태와 기존 동기화를 유지합니다.
