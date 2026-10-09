# Scripts

## GitHub Project → Notion

`notion_sync.py`는 이슈·PR 메타데이터와 Project 작업 상태를 Notion에 반영합니다. [운영 안내](../docs/architecture/notion-sync.md)에서 인증·사전검사·스키마 전환·보류 재개를 확인하세요.

```sh
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py'
python3 -B scripts/notion_sync.py --help
python3 -B scripts/notion_sync.py --dry-run
```

합성 검사는 네트워크·실제 토큰을 사용하지 않습니다. 운영은 trusted main 코드로 실행하고 로컬 실행과 동시에 쓰지 않습니다.

## GitHub → Discord

`discord_notify.py`는 주요 Issue·PR 사건, 선택한 CI 및 동기화 실행 결과를 조회해 Discord Webhook으로 전달합니다. `.github/discord-notifications.json`과 Actions 변수·Secret을 사용하며 기본 비활성 상태입니다. 최초 기준선과 전송 보류 조치는 PM main 수동 실행으로만 준비합니다. 계정 매핑·알림 대상·전송 확인은 [Discord 운영 안내](../docs/discord.md)를 따릅니다.

`notification_report.py`는 동기화 결과의 비밀 없는 데이터 계약을 정의합니다. `discord_transport.py`는 고정 API·Webhook, artifact 검증, 별도 상태 브랜치의 CAS 저장을 담당합니다. 로컬 합성 검사만으로 실제 전달을 확인했다고 판단하지 않습니다.
