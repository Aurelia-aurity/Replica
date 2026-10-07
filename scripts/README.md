# Scripts

팀 공통 실행·점검 보조 스크립트를 관리합니다.

스크립트 추가 시 목적, 실행 위치, 필요 도구와 예시 명령을 함께 기록합니다.

## GitHub → Notion

`notion_sync.py`는 Replica 이슈·PR 메타데이터를 Notion에 반영합니다. [연결·운영 안내](../docs/architecture/notion-sync.md)를 참고하세요.

```sh
python3 -B -m unittest discover -s scripts/tests -p test_notion_sync.py
```
