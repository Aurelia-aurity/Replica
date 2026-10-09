# GitHub Project → Notion 동기화

GitHub 이슈·PR과 Project가 작업 정보의 원본입니다. Notion은 진행 상황을 표시하고 일정·메모·자료를 모읍니다. Python 표준 라이브러리와 GitHub Actions로 운영합니다.

## 팀 사용 방법

작업은 GitHub 이슈로 만들고 관련 PR에 `Closes #번호` 등 명시적인 종료 연결을 둡니다. 5단계 작업 상태의 원본은 Replica GitHub Project입니다. Notion의 작업 상태를 직접 바꾸면 다음 동기화에서 Project 값으로 복구됩니다. 일정·메모·페이지 본문은 보존합니다. 상태와 종료 사유를 계산하는 기준은 [sync_engine.py](../../scripts/sync_engine.py), 운영·실행 절차는 이 문서를 따릅니다.

| 상태 | 기준 |
| --- | --- |
| 백로그 | 새 작업, 착수 준비 전 |
| 준비 중 | 담당·범위·선행 작업 정리 중 |
| 진행 중 | 작업 진행 중 |
| 검토 중 | main에 종료 연결된 열린 PR이 리뷰 가능 |
| 완료 | 이슈가 완료 사유로 종료됨 |

새 이슈는 백로그, 재오픈은 리뷰 가능한 연결 PR이 있으면 검토 중, 없으면 백로그로 한 번 전환합니다. Draft는 제외합니다. 마지막 리뷰 가능한 PR이 없어지면 진행 중으로 한 번 복귀하며, 이후 팀원이 바꾼 일반 상태는 보존합니다. PR 병합 자체로 완료를 추정하지 않습니다.

미계획·중복·종료 사유 불명으로 닫힌 이슈는 상태를 비우고 전체 이슈에서만 확인합니다. 일정은 보존하되 이슈 일정에서 숨깁니다. 중복 대표 링크는 GitHub의 명시적 관계만 사용합니다. 링크가 없는 중복은 중복으로 유지하고 일반 확인 안내를 표시합니다.

종료 정보 모순·최초 이관 충돌은 `보류:` 안내를 표시하고 Project 상태를 보존합니다. 다른 정상 이슈는 계속 처리합니다. 값이 맞아져도 PM 지정 재개 전에는 보류를 해제하지 않습니다. 열린 이슈의 상태가 미지정이고 검토·재오픈 자동 규칙도 적용되지 않으면 보류합니다. PM이 Project에서 백로그·준비 중·진행 중 중 하나를 정한 뒤 새 수동 실행으로 재개합니다.

Issue 제목은 `#번호 원본 제목`, 담당자·라벨은 쉼표 구분 문자열로 표시합니다. PR은 메타데이터만 동기화하고 Issue 상태 규칙을 적용하지 않습니다. GitHub 본문·댓글·코드·이메일을 복사하지 않으며 기여도 점수·Discord 알림은 제외합니다.

## 화면 구성

상단은 **통합 보드 · 전체 이슈** 두 탭입니다. 보드는 5단계 순서와 빈 열을 유지하고 카드에 담당자·일정·라벨을 표시합니다. 번호는 제목에 포함합니다.

보드·이슈 일정은 Issue이고 유효 상태가 있으며, GitHub가 Open이거나 Closed/종료 사유 완료이며 `보류:` 안내가 없는 항목만 표시합니다. 상태 없음 그룹은 숨깁니다. 전체 이슈에는 취소·중복·보류·확인 안내까지 표시합니다. 원본 DB 보드·일정도 동일 기준을 사용합니다.

보드에 Project 링크와 ‘상태는 GitHub에서 변경’ 안내를 둡니다. 접힌 운영 안내의 Sync 관리행은 제목에서 최근 전체 완료/부분 반영 결과와 시각을, 확인 필요에서 보류 개수·번호를, 동기화 시각에서 마지막 전체 성공 시각을 확인합니다. GitHub URL은 고정 Notion sync workflow로 연결됩니다. 자동화는 관리행의 요약만 갱신하며 개인 메모·일정·페이지 본문을 덮지 않습니다. 발표·회의 캘린더·개발 간트·마일스톤 DB는 변경하지 않습니다. 로딩 25개·빈 열·더보기·필터는 실제 UI에서 검증합니다.

## 설정

실행 설정은 Actions Variables, 토큰은 Secrets에 등록합니다. Notion 연결 ID는 공개 문서에 적지 않습니다. Project의 고정 식별자와 설정은 조회 결과로 함께 검증합니다. MCP/로컬 gh 로그인은 Actions 인증 성공의 근거가 아닙니다. 토큰 값을 채팅·파일·로그에 남기지 않습니다.

| Secrets | 목적 |
| --- | --- |
| `PROJECT_TOKEN` | 지정 개인 Project 읽기·상태 변경 |
| `NOTION_TOKEN` | 지정 Notion 데이터 소스 읽기·생성·수정 |

저장소 조회는 Actions의 읽기 전용 `GITHUB_TOKEN`을 사용하며 Project 토큰과 분리합니다. 접근 실패 시 다른 토큰으로 우회하지 않습니다.

| Variables | 의미 |
| --- | --- |
| `NOTION_SYNC_ENABLED` | 전환 중 false, 운영 준비 후 true |
| `NOTION_DATA_SOURCE_ID` | 기존 데이터 소스 UUID |
| `NOTION_CONTROL_PAGE_ID` | 기존 Sync 관리행 UUID |
| `PROJECT_ID` | Just-Simple0의 비공개 Replica Project #4 node ID |
| `PROJECT_OWNER_ID` | Project 소유자의 고정 node ID |
| `PROJECT_STATUS_FIELD_ID` | Status single-select 필드 node ID |
| `PROJECT_STATUS_OPTIONS` | 5단계 이름 → 실제 옵션 ID의 JSON 객체 |
| `PM_GITHUB_USER_ID` | 재개를 승인할 PM의 고정 numeric user ID |

```json
{"백로그":"option-backlog","준비 중":"option-ready","진행 중":"option-progress","검토 중":"option-review","완료":"option-done"}
```

위 옵션 ID는 형식 예시입니다. 실제 조회 결과로 바꾸고 이름·ID 모두 검증합니다. Project 접근 권한이 있는 인증으로 아래 읽기 전용 쿼리를 실행하면 Project·Status 필드·옵션 ID를 확인할 수 있습니다. 기존 로컬 인증에 권한이 없다면 등록된 Actions secret을 꺼내지 말고 PM의 승인된 인증 경로에서 확인합니다.

```sh
gh api graphql -f query='query { user(login: "Just-Simple0") { id projectV2(number: 4) { id number owner { ... on User { id login } } fields(first: 100) { nodes { ... on ProjectV2SingleSelectField { id name options { id name } } } pageInfo { hasNextPage endCursor } } } } }'
```

hasNextPage가 true이면 후속 페이지를 읽어 Status 필드를 확인해야 합니다. field/option ID를 이름에서 추정하거나 API 권한 확인 없이 입력하지 않습니다.
 팀원 Project 편집 권한은 저장소 collaborator 권한과 별도로 확인합니다.

## 적용 순서

1. 기존 자동화를 비활성화하고 구버전 실행이 끝날 때까지 기다립니다. 코드·스키마·보기·Project 상태·관리행·일정·메모를 백업합니다.
2. 작업 상태 select에 준비 중을 추가합니다. 종료 사유 select(완료·미계획·중복·확인 필요), 대표 이슈 URL, 확인 필요 rich_text, 동기화 내부 상태 rich_text를 추가해 총 19개 속성을 검증합니다. 내부 속성은 보기에서 숨기되 접근 제어로 간주하지 않습니다. 스크립트는 DB·속성·보기를 자동 생성하거나 삭제하지 않습니다.
3. Secrets·Variables를 준비하고, 상태를 바꾸는 Project 기본 자동화(자동 닫기·Item closed·PR linked·Item added·PR merged 등)는 custom 규칙과 경쟁하지 않도록 비활성화합니다.
4. PR의 독립 리뷰가 끝난 뒤 Actions → GitHub → Notion → Run workflow에서 검토한 브랜치와 같은 `approved_sha`를 입력해 `dry_run=true`로 실행합니다. 수동 실행은 `main` 또는 `fix/17-project-add-readback` ref만 허용하며, PM numeric actor, 원래 actor와 triggering actor 일치, 첫 실행 시도, `approved_sha == github.sha`가 모두 맞아야 합니다. checkout은 입력값이 아니라 immutable `github.sha`를 사용합니다. 자동 실행은 계속 `main`을 checkout하고 `NOTION_SYNC_ENABLED` Variables를 따릅니다. 비활성 상태에서 검증할 때 dry-run은 GitHub·Notion 쓰기가 0회여야 합니다.
5. dry-run 전체 조회·권한·스키마·ID와 이관 계획을 확인한 뒤 PM이 같은 승인 SHA로 새 수동 실행을 시작하고 `dry_run=false`를 선택해 실제 반영을 검증합니다. 필요한 경우 `resolve_issue_numbers`도 새 실행에서 지정합니다. 수동 실행에만 프로세스용 `NOTION_SYNC_ENABLED=true`를 전달하므로 저장소 자동화는 계속 비활성으로 둘 수 있습니다. 실패하면 pending과 fence를 보존하고 자동화를 켜지 않은 채 원인을 확인합니다.
6. 실제 Project·Notion 값과 수동 메모·일정·본문 보존, 중복 없는 두 번째 실행까지 확인하고 코드가 main에 반영된 뒤에만 자동화를 활성화합니다. 실제 API·Actions·UI 검증 전에는 운영 완료로 간주하지 않습니다.

읽기 사전검사는 새 스키마를 요구합니다. 구버전 스키마에서는 쓰기 없이 실패하며 준비 상태를 수정해야 합니다.

## 실행·보류 재개

```sh
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py'
python3 -B scripts/notion_sync.py --help
python3 -B scripts/notion_sync.py --dry-run
```

로컬 실행은 안전하게 전달된 인증 환경이 이미 있을 때만 사용하고 Actions와 동시에 실행하지 않습니다. `NOTION_SYNC_ENABLED`가 비활성이면 일반 로컬 실행도 건너뜁니다. Actions 수동 실행에는 필수 `approved_sha`를 현재 선택 ref의 전체 40자리 SHA와 똑같이 입력합니다. `main`과 `fix/17-project-add-readback` 외 ref/tag, 비PM, actor와 triggering actor 불일치, 재실행, SHA 불일치는 checkout 전에 거부됩니다. 수동 실행만 동기화 프로세스에 enabled=true를 주며, `dry_run=true`는 읽기 전용입니다. 비활성 상태의 실제 branch 검증은 독립 리뷰 완료 후 PM이 같은 SHA를 고른 새 workflow_dispatch에서만 수행합니다.

PM이 GitHub 모순·Project 상태를 정리한 뒤 Actions 수동 실행의 resolve_issue_numbers에 `17,20`처럼 입력합니다. dry_run=true로 계획을 확인하고, 승인한 전체 `github.sha`와 동일한 새 수동 실행에서 false를 선택합니다. 번호는 중복 제거하고 잘못된 입력·비PM 실행·미보류 대상은 거부합니다. PM 승인도 모순된 종료 정보나 조회 실패를 무시하지 않습니다.

번호를 지정한 보류 재개는 PM이 새로 시작한 수동 실행의 첫 번째 시도에서만 승인으로 인정합니다. 원래 실행자와 현재 요청자의 numeric ID가 모두 PM인지 확인하며, 실행 시도 번호가 없거나 재실행이면 쓰기 전에 거부합니다. 실패 후 다시 승인하려면 기존 실행의 Re-run 대신 Run workflow에서 현재 값을 확인하고 번호를 새로 지정합니다.

승인 기록·checkpoint·Notion 표시·집계까지 확인한 뒤 재개 완료로 기록합니다. 일반 확인 안내는 보존합니다. 표시 복구가 미완료이면 보류 집계에 남기고 다음 실행에서 승인된 표시만 복구합니다.

## 실패·보존

전체 조회·인증·스키마 검증 실패는 쓰기 전에 중단합니다. 적용 중 쓰기 결과가 불명확하면 추가 쓰기를 멈추고 영속 기록을 유지합니다. 전체 성공 시각은 모든 대상 처리가 성공할 때만 갱신합니다. 보류가 남으면 부분 결과를 기록합니다. 사전검증이나 안내 갱신이 실패하면 관리행 요약은 이전 값일 수 있으므로 GitHub URL이 있으면 해당 workflow를, URL이 비어 있으면 저장소 Actions에서 현재 실행 결과를 확인합니다.

Notion data source 조회는 모든 페이지의 `object=list`, `type=page_or_data_source`, `page_or_data_source` object, `results` 목록과 Boolean `has_more`를 검증합니다. `next_cursor` 키는 항상 있어야 하며, 다음 페이지가 있으면 비어 있지 않은 새 문자열, 마지막 페이지면 `null`이어야 합니다. `request_status`는 선택 필드입니다. 있으면 `type=complete`만 허용하고, 누락 외의 unknown/malformed 값과 `incomplete`는 쓰기 전에 거부합니다. `incomplete_reason`이 있으면 `query_result_limit_reached`만 인식합니다. active/archived 조회는 각각 독립적으로 10,000건 경계에 도달하면 전체 결과가 보장되지 않는 것으로 보고 거부합니다. dry-run 진단은 페이지 수·결과 개수·허용된 상태 범주·형식 유효성만 출력하며 원문, cursor, ID, credential은 기록하지 않습니다. 이전 실패 응답을 보관하지 않아 해당 실행에서 상태 필드가 누락됐는지 malformed였는지는 아직 확인되지 않았습니다.

생성은 Pending create fence → Notion 행 식별 → 내부 pending → Project 추가 → checkpoint → 표시 순서입니다. Project 추가의 성공 응답 item ID를 보관하고 완전한 전체 snapshot에서 그 ID와 대상 Issue가 모두 없을 때만 첫 조회를 포함해 최대 3회 readback합니다. ID가 다른 Issue·저장소·보관 항목을 가리키거나 같은 대상의 다른 active/archive item이 있으면 즉시 보류합니다. 확인된 pending 복구는 저장된 pending/checkpoint item ID와 동일한 활성 항목만 허용하며, 원래 항목이 사라지고 다른 ID가 나타나면 pending과 checkpoint를 보존해 보류합니다. PM 재개도 확정된 item ID를 바꿀 수 없습니다. 생성·Project mutation은 자동 재요청하지 않으며 readback을 마쳐도 항목이 안 보이면 pending을 보존합니다. 불명확한 결과를 미실행으로 추정하지 않습니다. 운영자는 자동화를 중지하고 대상을 조사합니다. 관리행·키·내부 기록을 삭제하거나 초기값으로 덮지 않습니다.

Notion unique constraint·Project 원자적 CAS가 없어 다른 도구와의 동시 쓰기는 지원하지 않습니다. Actions는 하나의 concurrency 그룹으로 직렬화하며 쓰기 직전 재확인으로 경합을 감지합니다. 휴지통 전체 조회를 보장하지 않으므로 관리 항목은 삭제하지 말고 복구 후 실행합니다.

## 근거

- [기술 계약](notion-sync-plan.md)
- [GitHub Issues GraphQL](https://docs.github.com/en/graphql/reference/issues)
- [GitHub Projects GraphQL](https://docs.github.com/en/graphql/reference/projects)
- [Projects Actions 자동화](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions)
- [Notion 요청 제한](https://developers.notion.com/reference/request-limits)
