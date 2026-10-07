# GitHub → Notion 동기화

GitHub 이슈·PR을 Notion에서 보고 일정을 관리합니다. Python 표준 라이브러리와 GitHub Actions만 사용합니다.

팀 Notion의 Replica 부모 페이지에서 `GitHub 작업` 데이터베이스를 엽니다. 실제 페이지 주소와 연결 ID는 이 공개 저장소에 적지 않습니다.

- [저장소](https://github.com/Aurelia-aurity/Replica)

## 사용 방식

작업은 GitHub 이슈로 만들고 관련 PR에서 연결합니다. Notion의 `이슈 보드`에서는 작업 상태를 정하고 `일정`에 시작일·종료일을 입력합니다. `일정` 캘린더와 `PR 목록` 보기로 진행을 확인합니다. 새 이슈의 작업 상태는 비어 있으므로 처음 들어온 항목을 백로그 또는 진행 중으로 옮깁니다.

| GitHub에서 가져오는 정보 | Notion에서 팀이 관리하는 정보 |
| --- | --- |
| 제목, 종류, 번호, GitHub URL, GitHub 상태 | 작업 상태, 일정, 메모 |
| 작성자·담당자의 GitHub login, 라벨, GitHub 수정 시각 | 항목의 페이지 본문 |

`GitHub 상태`는 Open / Draft / Closed / Merged입니다. PR merge나 이슈 close는 사실만 반영합니다. `작업 상태`를 자동 완료하지 않습니다. 담당자·라벨은 정렬한 JSON 문자열로 표시하며 Notion 계정 매핑은 하지 않습니다. 이슈/PR 본문, 댓글, 코드, 이메일을 복사하지 않습니다.

기여도 점수, 팀 브랜치·PR·merge 규칙은 교수 회신과 팀 합의 후 따로 정합니다. 이 동기화에는 점수 산정이나 알림 봇이 없습니다.

## 최초 연결

데이터베이스·관리 행·표준 보기 생성은 Notion MCP로 한 번 수행합니다. 동기화 스크립트는 기존 ID만 사용하고 새 데이터베이스를 생성하지 않습니다. 필요한 설정은 다음과 같습니다. 실제 ID는 팀 Notion에서 확인해 Actions 변수에만 등록합니다.

| 설정 | 값 |
| --- | --- |
| `NOTION_DATA_SOURCE_ID` | 데이터베이스의 data source ID |
| `NOTION_CONTROL_PAGE_ID` | 동기화 관리 항목의 page ID |
| `NOTION_SYNC_ENABLED` | 준비 중 `false`, 연결 확인 후 `true` |

1. [Notion 내부 연결 관리](https://app.notion.com/developers/connections)에서 Replica용 internal connection을 만듭니다. 콘텐츠 읽기·생성·수정 권한만 설정합니다. 사용자 정보·댓글 권한은 필요 없습니다.
2. Replica 부모 페이지의 연결 메뉴에서 이 연결을 추가합니다. 팀이 데이터베이스의 GitHub 메타데이터를 볼 수 있도록 공유 범위를 확인합니다.
3. 저장소 **Settings → Secrets and variables → Actions → Secrets**에서 `NOTION_TOKEN`을 등록합니다. 토큰은 이 비공개 입력창에만 붙여 넣고 채팅이나 파일에 적지 않습니다. Codex의 Notion MCP 로그인은 Actions의 API 토큰을 대신하지 않습니다.
4. **Variables**에 위 세 값을 설정합니다. 준비가 끝날 때까지 `NOTION_SYNC_ENABLED=false`를 유지합니다. GitHub는 Actions의 읽기 전용 `GITHUB_TOKEN`을 자동 사용합니다.
5. 검토된 워크플로우가 main에 반영되고 연결 준비가 끝나면 enabled를 `true`로 바꾸고 **Actions → GitHub → Notion → Run workflow**로 첫 실행을 확인합니다.

워크플로우는 이슈·PR 이벤트, 수동 실행, 약 15분 간격의 정기 실행을 지원합니다. 모든 실행은 GitHub의 현재 전체 목록을 조회합니다. 이벤트가 밀리거나 누락되면 정기 실행이 보완합니다. GitHub schedule은 지연될 수 있으며 공개 저장소는 60일 활동이 없으면 정기 실행이 비활성화될 수 있습니다.

`pull_request_target`은 fork에서도 이벤트를 받을 수 있지만 **main 코드만 checkout**합니다. PR/fork 코드나 이벤트 본문을 실행하지 않습니다. checkout action은 공식 v4 commit SHA로 고정하고 Git 자격증명을 남기지 않습니다. workflow의 repository gate, main ref, 최소 권한을 유지하세요. main에 workflow를 바꾸면 Notion 쓰기 권한이 있는 실행 코드를 바꾸는 것이므로 리뷰가 필요합니다.

## 실행과 확인

```sh
python3 -B -m unittest discover -s scripts/tests -p 'test_notion_sync.py'
python3 -B scripts/notion_sync.py --help
```

자격증명을 이미 안전한 프로세스 환경으로 제공한 경우 `--dry-run`으로 모든 조회·스키마·중복·복구 조건만 검사할 수 있습니다. disabled 상태는 외부 요청을 하지 않습니다. enabled인데 필수 설정이 없으면 실패합니다. 로그에는 개수와 고정 오류 분류만 출력합니다.

관리 항목 `동기화 관리`의 `동기화 시각`은 전체 실행이 성공한 마지막 시각입니다. 원본 항목별 시각은 그 항목을 적용한 시각입니다. Actions에서 실패한 실행과 관리 항목의 시각을 함께 확인합니다. apply 도중 실패하면 일부 항목은 갱신될 수 있지만 전체 성공 시각은 남기지 않습니다.

실제 운영 확인은 GitHub에 정상 작업 이슈를 만든 뒤 수행합니다. 첫 실행으로 생성된 행에 일정·메모·본문을 적고, GitHub 제목을 수정해 다시 실행합니다. 행이 하나인지, 제목과 GitHub 상태가 갱신되는지, 수동 내용이 보존되는지 확인합니다. 현재 이슈가 없으면 관리 행만 확인할 수 있으므로 이 검증을 완료했다고 간주하지 않습니다.

## 실패와 복구

동기화 키는 `gh:<repository id>:issue:<issue id>`입니다. PR도 GitHub issues API의 issue ID를 사용합니다. 저장소 숫자 ID `1392442366`을 확인해 다른 저장소가 연결되는 것을 막습니다. 데이터 소스의 정확한 schema/name/type/select 옵션과 관리 행의 ID·부모·키·종류·활성 상태를 검증합니다. 전체 GitHub pagination·PR 상세·Notion 활성/보관 partition 조회를 마친 다음에만 쓰기를 시작합니다. 조회 누락·키 중복·잘못된 시각은 쓰기 전에 중단합니다.

GitHub 수정 시각보다 Notion에 기록된 GitHub 시각이 더 최신이면 덮어쓰지 않습니다. 같은 시각의 값 차이는 GitHub 값으로 복구합니다. 원본에서 사라진 항목은 삭제하거나 자동 완료하지 않습니다. 이슈를 다른 저장소로 이전하면 새 키가 필요하므로 기존 노트를 자동 이전하지 않습니다.

페이지 생성 전 관리 행의 `Pending create`에 키를 기록하고 readback을 확인합니다. create는 한 번만 시도합니다. 생성 뒤 오류나 응답 손실이 발생하면 이 기록을 유지합니다. 다음 실행에서 유일한 활성 행이 확인되면 재사용합니다. 확인되지 않으면 자동 생성하지 않고 실패합니다.

생성 오류(400/401/403/409/429 포함) 이후에도 같은 보수적 규칙을 적용합니다. `Pending create`가 남았을 때:

1. enabled를 `false`로 바꾸고 진행 중인 실행이 끝났는지 확인합니다. 로컬 동기화를 동시에 실행하지 않습니다.
2. 해당 키의 활성·보관·휴지통 항목을 실제 Notion에서 확인합니다. 행이 있으면 원래 위치로 복구하고 키를 유지합니다. 이후 다시 활성화해 실행하면 자동 복구됩니다.
3. 충분한 확인 후 생성되지 않았음이 확실할 때만 운영자가 `Pending create`를 비웁니다. 불확실하면 기록을 남긴 채 원격 상태를 조사합니다. 관리 행을 삭제하거나 초기화하지 않습니다.

Notion에는 unique constraint가 없으므로 다른 도구·로컬 실행까지 포함한 동시 writer는 지원하지 않습니다. Actions는 하나의 concurrency group으로 직렬화합니다. 밀린 pending 실행은 GitHub에서 대체될 수 있지만 전체 재조회와 schedule로 보완합니다. 운영자가 관리 키를 바꾸거나 페이지를 휴지통으로 이동하면 이 보호가 깨질 수 있습니다. API query가 휴지통의 모든 항목을 조회한다고 보장할 수 없으므로 동기화행은 삭제하지 말고 복구한 뒤 실행하세요.

조회/동일값 PATCH는 최대 4회 제한 재시도합니다. timeout 30초, 요청 간격 0.4초, Retry-After 최대 60초까지 준수하며 긴 대기는 실패로 다음 실행에 넘깁니다. 429 API 차단은 재시도하지 않습니다. create는 429/5xx/연결 실패에도 자동 재시도하지 않습니다. OAuth/API 토큰을 로그로 출력하지 않습니다.

## API 계약 근거

- [Notion 버전](https://developers.notion.com/reference/versioning): `2026-03-11`, data source API 사용.
- [페이지 생성](https://developers.notion.com/reference/post-page): `parent.data_source_id`.
- [페이지 수정](https://developers.notion.com/reference/patch-page): 생략한 속성 보존.
- [데이터 소스 조회](https://developers.notion.com/reference/query-a-data-source): cursor, incomplete 상태, archived partition.
- [요청 제한](https://developers.notion.com/reference/request-limits): 429/529와 Retry-After.
- [GitHub issues API](https://docs.github.com/en/rest/issues/issues): PR 포함, issue ID 사용.
- [Actions 이벤트](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows): default branch·target 실행과 schedule 제한.

계획 검토 반영 내역은 [동기화 계획](notion-sync-plan.md)에 기록합니다.

스키마 속성 이름과 select 옵션 집합은 정확히 일치해야 합니다. 데이터베이스 속성/옵션을 추가하거나 삭제할 때는 코드의 schema 계약과 테스트도 함께 수정하세요. 관리 행의 번호·GitHub URL·GitHub 상태·GitHub 수정은 비어 있어야 합니다.
