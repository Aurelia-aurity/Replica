# Scripts

## GitHub Project ↔ Notion

`notion_sync.py`는 이슈·PR 메타데이터와 Project 작업 상태를 Notion에 반영하고, 양방향 설정이 켜져 있으면 Notion의 일반 3상태 이동을 요청으로 처리합니다. 사용자 화면 상태는 6개이며 `취소`는 확인된 GitHub 종료 결과로만 표시합니다. Project의 여섯 번째 옵션 ID는 실제 읽기 결과에 바인딩되어야 합니다. `요청 상태`와 `요청 처리`는 각각 기존 다섯 선택지를 유지합니다. dry-run의 `full_effect_report`는 전체 원본과 양방향 요청·복구·관리행 처리의 잠재 효과 범위이며 정확한 write 목록이 아닙니다. [운영 안내](../docs/architecture/notion-sync.md)에서 schema rollout·설정 보존·보류 재개를 확인하세요.

```sh
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py'
python3 -B scripts/notion_sync.py --help
python3 -B scripts/notion_sync.py --dry-run
```

합성 검사는 네트워크·실제 토큰을 사용하지 않습니다. 운영은 trusted main 코드로 실행하고 로컬 실행과 동시에 쓰지 않습니다.

## GitHub → Discord

`discord_notify.py`는 주요 Issue·PR 사건, 선택한 CI 및 동기화 실행 결과를 조회해 Discord Webhook으로 전달합니다. `.github/discord-notifications.json`과 Actions 변수·Secret을 사용하며 기본 비활성 상태입니다. 최초 기준선과 전송 보류 조치는 PM main 수동 실행으로만 준비합니다. 계정 매핑·알림 대상·전송 확인은 [Discord 운영 안내](../docs/discord.md)를 따릅니다.

`notification_report.py`는 schema=1 호환 및 schema=2의 시각 검증 결과를 포함한 비밀 없는 데이터 계약을 정의합니다. `discord_transport.py`는 고정 API·Webhook, artifact 검증, 별도 상태 브랜치의 CAS 저장을 담당합니다. 로컬 합성 검사만으로 실제 전달을 확인했다고 판단하지 않습니다.
