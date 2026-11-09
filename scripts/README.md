# Scripts

## GitHub Project ↔ Notion

`sync_simple.py`는 GitHub 이슈·PR 메타데이터와 6개 작업 상태(`백로그`·`준비 중`·`진행 중`·`검토 중`·`완료`·`취소`)를 GitHub Project와 Notion 데이터 소스 사이에서 전체 대조하며 `notion_sync.py`는 CLI 진입점입니다. GitHub 종료·재오픈·연결 PR 사실을 우선하고 한쪽 변경은 전파하며 양쪽이 서로 다르게 변경되면 Notion 값을 우선합니다. 마지막 확인 기준은 Notion의 숨김 `rich_text` 속성 `동기화 기준`에 `{v:1,status,facts:{closed,reason,ready}}` JSON으로 저장하고 Project → Notion 표시 속성 → `동기화 기준` 순서로 쓰기와 재조회 확인을 수행합니다. 영구 보류나 별도 shadow 모드는 두지 않으며 같은 엔진의 `--dry-run`과 공개 `sync-simple-plan` 실행 산출물로 실행 계획을 확인합니다. 자세한 규칙과 운영 전환 절차는 [동기화 운영 안내](../docs/architecture/notion-sync.md)를 참고하세요.

```sh
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py'
python3 -B scripts/notion_sync.py --dry-run
```

새 동기화 코드는 Python 3.9에서 불러올 수 있으며 표준 라이브러리만 사용합니다. 합성 검사는 외부 네트워크나 실제 토큰을 사용하지 않습니다. 운영 반영은 신뢰된 `main` 워크플로에서 단일 `replica-notion-sync` 직렬 그룹으로 실행하고 로컬 실행과 동시에 쓰지 않습니다.

## 변경 기반 및 주기적 일반 동기화

`ordinary_sync_request.py`는 정확한 `main` SHA·워크플로·저장소·실행자(actor)·첫 시도(attempt=1)와 Cloudflare Worker relay의 일회용 실행 허가를 확인한 뒤 `sync_simple.py` 전체 동기화를 실행하고 결과를 해시 체인 chunk와 seal로 보고합니다. 웹훅·helper 수신 요청뿐 아니라 Worker 분당 cron이 최소 300초 간격으로 요청하는 수신 ID 없는 주기적 전체 스캔도 동일한 일반 워크플로 경로를 사용합니다.

일반 실행의 증거 콜백 전송이 실패했을 때는 원본 실행 산출물의 `notion-relay-evidence.json`을 사용해 저장된 콜백만 재전송할 수 있습니다. 이 명령은 실행 허가 요청이나 동기화 엔진을 다시 실행하지 않습니다.

```sh
python3 -B scripts/ordinary_sync_request.py --deliver-evidence /absolute/path/notion-relay-evidence.json
```

`project_status_request.py`는 macOS/Linux(`fcntl` 잠금 지원) 환경에서 권한이 확인된 기존 이슈의 일반 3개 상태(`백로그`·`준비 중`·`진행 중`)만 변경합니다. 실행 전 D1 예약과 로컬 저널을 기록하며 응답이 불확실하거나 실패하면 기존 UUID와 로컬 기록을 보존해 같은 요청을 복구합니다(임의 재시도나 새 UUID 발급 금지). `CONFIRMED`·`CONFIRMED_NOOP`는 Project 반영 확인과 동기화 대기열 등록을 의미하며 Notion 반영 완료를 뜻하지 않습니다. 자세한 설정과 복구 절차는 [relay 안내](../cloudflare/notion-relay/README.md)에 있습니다.

## GitHub → Discord

`discord_notify.py`는 주요 Issue·PR 사건, 선택한 CI 및 동기화 실행 결과를 조회해 Discord Webhook으로 전달합니다. `.github/discord-notifications.json`과 Actions 변수·Secret을 사용하며 기본 비활성 상태입니다. 최초 기준선과 전송 보류 조치는 PM main 수동 실행으로만 준비합니다. 계정 매핑·알림 대상·전송 확인은 [Discord 운영 안내](../docs/discord.md)를 따릅니다.

`notification_report.py`는 schema=1 호환 및 schema=2의 시각 검증 결과를 포함한 비밀 없는 데이터 계약을 정의합니다. `discord_transport.py`는 고정 API·Webhook, artifact 검증, 별도 상태 브랜치의 CAS 저장을 담당합니다. 로컬 합성 검사만으로 실제 전달을 확인했다고 판단하지 않습니다.
