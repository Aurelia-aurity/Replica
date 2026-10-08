# Scripts

## GitHub Project → Notion

`notion_sync.py`는 이슈·PR 메타데이터와 Project 작업 상태를 Notion에 반영합니다. [운영 안내](../docs/architecture/notion-sync.md)에서 인증·사전검사·스키마 전환·보류 재개를 확인하세요.

```sh
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py'
python3 -B scripts/notion_sync.py --help
python3 -B scripts/notion_sync.py --dry-run
```

합성 검사는 네트워크·실제 토큰을 사용하지 않습니다. 운영은 trusted main 코드로 실행하고 로컬 실행과 동시에 쓰지 않습니다.
