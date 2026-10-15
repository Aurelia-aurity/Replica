# GitHub Project ↔ Notion 동기화

GitHub 이슈·PR의 종료·재오픈·연결 PR 사실을 우선합니다. 일반 작업 상태는 Project에서 변경하며, 양방향 전환 후 Notion 보드 이동도 변경 요청으로 처리합니다. Notion은 일정·메모·자료를 보존합니다. Python 표준 라이브러리와 GitHub Actions로 운영합니다.

## 팀 사용 방법

작업은 GitHub 이슈로 만들고 관련 PR에 `Closes #번호` 등 명시적인 종료 연결을 둡니다. 5단계 작업 상태는 Replica GitHub Project와 동기화합니다. `NOTION_BIDIRECTIONAL_ENABLED=true`로 전환한 뒤 백로그·준비 중·진행 중 사이 Notion 카드 이동을 요청으로 처리합니다. 역방향·단계 건너뛰기도 허용합니다. false는 확장 내부 상태를 읽으며 요청 수락을 중지하는 단방향 모드입니다. 일정·메모·페이지 본문은 보존합니다.

조회는 5분 예약이며 5분 내 완료를 보장하지 않습니다. 감지 전에는 접수 표시가 없고 감지 후 반영 대기, 확인 완료 후 반영 완료로 표시합니다. 거절·충돌은 요청 상태와 사유를 남기고 유효한 GitHub 위치로 복원합니다. 중간 이동 이력은 저장하지 않습니다. 양쪽 다른 값의 변경 순서가 입증되지 않으면 PM 확인으로 넘깁니다. Notion 페이지 수정 시각이나 마지막 편집자를 상태 이동 시각·요청자로 추정하지 않습니다.

공개 링크 편집을 유지하므로 외부인의 카드 이동도 요청이 될 수 있습니다. 내부 기록도 편집 가능하며 숨김·schema·hash는 승인 인증 수단이 아닙니다. 정상 PM 재개 경로는 numeric PM actor·새 workflow_dispatch·첫 attempt·triggering actor 일치 검사를 유지하지만 내부 기록 변조를 기술적으로 차단하지 않습니다. 쓰기 직전 재조회와 사후 확인을 수행하되 조건부 쓰기가 없는 짧은 경쟁 구간의 이동 손실 가능성은 남습니다. 상태와 종료 사유를 계산하는 기준은 [sync_engine.py](../../scripts/sync_engine.py), 운영·실행 절차는 이 문서를 따릅니다.

| 상태 | 기준 |
| --- | --- |
| 백로그 | 새 작업, 착수 준비 전 |
| 준비 중 | 담당·범위·선행 작업 정리 중 |
| 진행 중 | 작업 진행 중 |
| 검토 중 | main에 종료 연결된 열린 PR이 리뷰 가능 |
| 완료 | 이슈가 완료 사유로 종료됨 |

새 이슈는 백로그, 재오픈은 리뷰 가능한 연결 PR이 있으면 검토 중, 없으면 백로그로 한 번 전환합니다. Draft는 제외합니다. 마지막 리뷰 가능한 PR이 없어지면 진행 중으로 한 번 복귀하며, 이후 팀원이 바꾼 일반 상태는 보존합니다. PR 병합 자체로 완료를 추정하지 않습니다.

미계획·중복·종료 사유 불명으로 닫힌 이슈는 상태를 비우고 전체 이슈에서만 확인합니다. 일정은 보존하되 이슈 일정에서 숨깁니다. 중복 대표 링크는 GitHub의 명시적 관계만 사용합니다. 링크가 없는 중복은 중복으로 유지하고 일반 확인 안내를 표시합니다.

종료 정보 모순·최초 이관 충돌은 `보류:` 안내를 표시하고 Project 상태를 보존합니다. 다른 정상 이슈는 계속 처리합니다. 값이 맞아져도 PM 지정 재개 전에는 보류를 해제하지 않습니다. 최초 이관 대상인 기존 열린 이슈의 상태가 미지정이고 검토·재오픈 자동 규칙도 적용되지 않으면 보류합니다. PM이 Project에서 백로그·준비 중·진행 중 중 하나를 정한 뒤 새 수동 실행으로 재개합니다.

Issue 제목은 `#번호 원본 제목`, 담당자·라벨은 쉼표 구분 문자열로 표시합니다. PR은 메타데이터만 동기화하고 Issue 상태 규칙을 적용하지 않습니다. GitHub 본문·댓글·코드·이메일을 복사하지 않으며 기여도 점수는 계산하지 않습니다. Discord 전달은 별도 [알림 작업](../discord.md)이 담당하고 동기화 작업은 비밀 없는 실행 결과 artifact만 제공합니다.

## 화면 구성

상단은 **통합 보드 · 전체 이슈** 두 탭입니다. 보드는 5단계 순서와 빈 열을 유지하고 카드에 담당자·일정·라벨을 표시합니다. 번호는 제목에 포함합니다.

보드·이슈 일정은 Issue이고 유효 상태가 있으며, GitHub가 Open이거나 Closed/종료 사유 완료인 항목을 표시합니다. 거절·충돌로 GitHub 위치에 복원한 보류 카드도 표시합니다. 상태 없음 그룹은 숨깁니다. 전체 이슈에는 취소·중복·보류·확인 안내까지 표시합니다. 원본 DB 보드·일정도 동일 기준을 사용합니다.

보드에 Project 링크와 양방향 요청·처리 표시·공개 링크 편집 범위 안내를 둡니다. 접힌 운영 안내의 Sync 관리행은 제목에서 최근 전체 완료/부분 반영 결과와 시각을, 확인 필요에서 보류 개수·번호를, 동기화 시각에서 마지막 전체 성공 시각을 확인합니다. GitHub URL은 고정 Notion sync workflow로 연결됩니다. 자동화는 관리행의 요약만 갱신하며 개인 메모·일정·페이지 본문을 덮지 않습니다. 발표·회의 캘린더·개발 간트·마일스톤 DB는 변경하지 않습니다. 로딩 25개·빈 열·더보기·필터는 실제 UI에서 검증합니다.

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
| `NOTION_BIDIRECTIONAL_ENABLED` | Actions repository Variable. 기본 false; 제한 실제 검증 때 자동 동기화를 중지한 채 true로 설정하고, 실패하면 false로 되돌립니다. 최종 운영 활성화 때 true를 유지합니다. false는 요청 수락 중지 |
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

1. PR의 독립 리뷰를 마치고 보호된 `main` 반영에 대한 사용자의 명시 지시를 확보합니다. 기존 자동 동기화를 중지하고 구버전 실행이 끝났는지 확인한 뒤 코드·스키마·보기·Project 상태·관리행·일정·메모를 백업합니다.
2. 검토된 producer와 consumer를 함께 `main`에 반영하고, `NOTION_BIDIRECTIONAL_ENABLED=false`를 유지한 채 원격 `main` SHA와 trusted checkout이 일치하는지 확인합니다. 구버전 consumer는 schema2 보고서를 지원하지 않으므로, 이 단계 전에 제한 실제 검증이나 schema2 보고서 소비를 진행하지 않습니다.
3. 코드가 반영된 뒤 작업 상태 select에 준비 중을 추가합니다. 종료 사유 select(완료·미계획·중복·확인 필요), 대표 이슈 URL, 확인 필요 rich_text, 동기화 내부 상태 rich_text를 추가해 요청 처리 select(반영 대기·반영 완료·요청 거절·자동 확인 중·PM 확인 필요)와 요청 상태 select(기존 작업 상태 5개 값)를 포함한 총 21개 속성, 보기와 준비 안내를 검증합니다. Secrets·Variables를 준비하고 상태를 바꾸는 Project 기본 자동화(자동 닫기·Item closed·PR linked·Item added·PR merged 등)는 custom 규칙과 경쟁하지 않도록 비활성화합니다. 내부 속성은 보기에서 숨기되 접근 제어로 간주하지 않습니다. 스크립트는 DB·속성·보기를 자동 생성하거나 삭제하지 않습니다.
4. 자동 동기화와 양방향 기능을 모두 비활성으로 둔 채, PM이 trusted `main`의 정확한 SHA와 첫 실행 시도로 `dry_run=true`를 실행해 전체 조회·권한·스키마·ID·v2 기록·이관 계획·보고서를 확인합니다. 실제 쓰기와 번호를 지정한 PM 재개는 `main`의 정확한 ref에서만 허용합니다. `fix/17-project-add-readback`, `feat/32-bidirectional-sync`는 번호를 지정하지 않은 `dry_run=true` 조회만 허용하며, PM numeric actor, 원래 actor와 triggering actor 일치, 첫 실행 시도, `approved_sha == github.sha`가 모두 맞아야 합니다. checkout은 입력값이 아니라 immutable `github.sha`를 사용합니다. feature SHA 검증은 후보 코드의 검증일 뿐이며 trusted main consumer의 schema2 보고서 처리 검증을 대체하지 않습니다. dry-run은 GitHub·Notion 쓰기가 0회여야 합니다.
5. 자동 동기화는 계속 중지한 상태에서 Actions repository Variable `NOTION_BIDIRECTIONAL_ENABLED`를 true로 설정하고, trusted `main` SHA로 PM의 제한된 수동 실제 검증을 수행합니다. 이 설정은 workflow_dispatch별 입력이 아닙니다. 수동 실행에서 `dry_run=false`를 지정해 요청·거절·충돌·반복 무변경과 Discord 보고서 소비를 확인합니다. 필요한 경우 `resolve_issue_numbers`도 새 수동 실행에서 지정합니다. 쓰기 전 현재 대상 사실을 다시 확인합니다. 검증이 실패하면 repository Variable을 false로 되돌리고 pending과 fence를 보존한 채 원인을 확인합니다.
6. 제한 실제 검증이 성공하면 양방향 Variable은 true로 유지하고, 실제 Project·Notion 값, 수동 메모·일정·본문 보존, 보고서 결과와 안내의 일치를 확인한 뒤 자동 동기화를 활성화해 5분 예약을 재개합니다. 중복 없는 후속 실행까지 확인하며, 실제 API·Actions·UI 검증 전에는 운영 완료로 간주하지 않습니다.

읽기 사전검사는 새 스키마를 요구합니다. 구버전 스키마에서는 쓰기 없이 실패하며 준비 상태를 수정해야 합니다.

## 실행·보류 재개

```sh
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py'
python3 -B scripts/notion_sync.py --help
python3 -B scripts/notion_sync.py --dry-run
```

로컬 실행은 안전하게 전달된 인증 환경이 이미 있을 때만 사용하고 Actions와 동시에 실행하지 않습니다. `NOTION_SYNC_ENABLED`가 비활성이면 일반 로컬 실행도 건너뜁니다. Actions 수동 실행에는 필수 `approved_sha`를 선택 ref의 전체 40자리 SHA와 똑같이 입력합니다. `main`, `fix/17-project-add-readback`, `feat/32-bidirectional-sync` 외 ref/tag, 비PM, actor와 triggering actor 불일치, 재실행, SHA 불일치는 checkout 전에 거부됩니다. 수동 실행만 동기화 프로세스에 enabled=true를 주며 `dry_run=true`는 읽기 전용입니다. 두 feature ref는 번호 없는 dry-run만 허용하고, 실제 쓰기와 번호 지정 보류 재개는 `main`에서만 허용합니다. feature ref 검증은 trusted `main`의 producer·consumer 및 schema2 보고서 검증을 대체하지 않습니다. 운영 dry-run과 제한 실제 검증은 trusted `main`의 동일한 전체 SHA를 사용합니다. 제한 실제 검증은 자동 동기화가 중지된 동안 repository Variable `NOTION_BIDIRECTIONAL_ENABLED=true` 및 수동 `dry_run=false`로 수행하며, 실패하면 Variable을 false로 되돌립니다.

PM이 GitHub 모순·Project 상태를 정리한 뒤 trusted `main` SHA의 Actions 수동 실행에서 resolve_issue_numbers에 `17,20`처럼 입력합니다. 먼저 양방향 false의 dry-run으로 계획을 확인하고, 자동 동기화를 중지한 채 같은 승인 SHA를 지정한 새 PM 수동 실행에서 실제 재개를 수행합니다. 양방향 처리가 필요한 재개는 제한 실제 검증 절차에 따라 true를 사용합니다. 번호는 중복 제거하고 잘못된 입력·비PM 실행·미보류 대상은 거부합니다. PM 승인도 모순된 종료 정보나 조회 실패를 무시하지 않습니다.

번호를 지정한 보류 재개는 PM이 새로 시작한 수동 실행의 첫 번째 시도에서만 승인으로 인정합니다. 원래 실행자와 현재 요청자의 numeric ID가 모두 PM인지 확인하며, 실행 시도 번호가 없거나 재실행이면 쓰기 전에 거부합니다. 실패 후 다시 승인하려면 기존 실행의 Re-run 대신 Run workflow에서 현재 값을 확인하고 번호를 새로 지정합니다.

승인 기록·checkpoint·Notion 표시·집계까지 확인한 뒤 재개 완료로 기록합니다. 일반 확인 안내는 보존합니다. 표시 복구가 미완료이면 보류 집계에 남기고 다음 실행에서 승인된 표시만 복구합니다.

## 실패·보존

전체 조회·인증·스키마 검증 실패는 쓰기 전에 중단합니다. 적용 중 쓰기 결과가 불명확하면 추가 쓰기를 멈추고 영속 기록을 유지합니다. 전체 성공 시각은 모든 대상 처리가 성공할 때만 갱신합니다. 보류가 남으면 부분 결과를 기록합니다. 사전검증이나 안내 갱신이 실패하면 관리행 요약은 이전 값일 수 있으므로 GitHub URL이 있으면 해당 workflow를, URL이 비어 있으면 저장소 Actions에서 현재 실행 결과를 확인합니다.

Notion의 `GitHub 수정`과 `동기화 시각`은 UTC 분 단위로 표시하지만 source 시각, cutoff·이벤트, 내부 `last_success_at`/`last_result.at`은 전체 정밀도를 유지합니다. readback은 timezone-aware 단일 시각이 전송한 분 단위 instant와 정확히 같아야 하며, 초가 0이 아니거나 `end`/`time_zone`이 null이 아니면 실패합니다. 관리행 내부 성공 시각이 비어 있는 구형 상태의 기존 표시값은 보존합니다. `일정`·메모·본문은 이 표시 투영의 대상이 아닙니다.

Notion data source 조회는 모든 페이지의 `object=list`, `type=page_or_data_source`, `page_or_data_source` object, `results` 목록과 Boolean `has_more`를 검증합니다. `next_cursor` 키는 항상 있어야 하며, 다음 페이지가 있으면 비어 있지 않은 새 문자열, 마지막 페이지면 `null`이어야 합니다. `request_status`는 선택 필드입니다. 있으면 `type=complete`만 허용하고, 누락 외의 unknown/malformed 값과 `incomplete`는 쓰기 전에 거부합니다. `incomplete_reason`이 있으면 `query_result_limit_reached`만 인식합니다. active/archived 조회는 각각 독립적으로 10,000건 경계에 도달하면 전체 결과가 보장되지 않는 것으로 보고 거부합니다. dry-run 진단은 페이지 수·결과 개수·허용된 상태 범주·형식 유효성만 출력하며 원문, cursor, ID, credential은 기록하지 않습니다. 이전 실패 응답을 보관하지 않아 해당 실행에서 상태 필드가 누락됐는지 malformed였는지는 아직 확인되지 않았습니다.

생성은 Pending create fence → Notion 행 식별 → add pending 영속 확인 → Project 추가 1회 → 성공 응답 item ID 즉시 영속 저장·readback → 직접 node와 전체 목록 관계 확인 → 초기 상태/checkpoint → 사용자 표시 순서입니다. 반환 ID는 확인 전에도 별도 readback 기록에 보존하며 confirmed 관계와 혼동하지 않습니다. ID 저장 확인 전에 관계 조회·초기화하지 않습니다. 성공 응답 저장 후 가시성 지연은 후속 실행 최대 3회 확인합니다. 네트워크 실패도 1회이며 조회 직전 고유 토큰과 횟수를 영속 확인합니다. 토큰 저장 후 중단은 예약 1회로 계산하며 같은 토큰을 중복 계산하지 않습니다. 즉시 readback 내부 재시도와 후속 3회 예산은 별개입니다.

직접 ID·목록·대상 Issue·저장소·보관 상태·현재 요청/사실이 일치해야 계속합니다. 중복·다른 ID·명백한 모순은 즉시 PM 보류, 3회 실패는 ADD_READBACK_EXHAUSTED로 PM 확인입니다. 응답 ID가 없거나 저장 미확인이면 자동 읽기만 허용하고 PM이 재개합니다. 불확실한 add를 자동 재전송하지 않습니다. 기존 반환 ID 없는 legacy pending도 이 경계를 유지합니다.

Issue 내부 상태 v2는 v1의 pending/hold/resume/projection/식별자와 checkpoint를 보존하고 baseline/request/readback/notion_write를 확장합니다. 기존 행의 최초 이관은 안정적인 양쪽 일치만 baseline으로 초기화합니다. 새 이슈는 자동 백로그 초기화의 쓰기와 readback을 확인한 뒤 baseline을 확정합니다. 양방향 전환 중 기존 행의 불일치나 미완료 작업을 자동 승자/새 요청으로 만들지 않습니다. 전송 직전에 sent를 영속 확인하며 재시작의 sent/uncertain은 결과 조회만 합니다. 모든 PATCH 전송 속성을 readback하고 하나라도 불일치면 완료/baseline을 확정하지 않습니다.

rollback은 NOTION_BIDIRECTIONAL_ENABLED=false로 요청 수락을 중지하되 v2를 계속 읽으며 기존 미완료 작업을 보존합니다. v2를 읽지 못하는 과거 SHA로 돌아가는 것만으로는 rollback이 아닙니다. 관리행·키·내부 기록을 삭제하거나 초기값으로 덮지 않습니다.

Notion unique constraint·Project 원자적 CAS가 없어 다른 도구와의 동시 쓰기는 지원하지 않습니다. Actions는 하나의 concurrency 그룹으로 직렬화하며 쓰기 직전 재확인으로 경합을 감지합니다. 휴지통 전체 조회를 보장하지 않으므로 관리 항목은 삭제하지 말고 복구 후 실행합니다.

## #32 전환 검증

구현·합성 검사와 실제 운영 전환은 별도 수락입니다. 전환 시 원본 DB와 연결 보드의 요청 처리·요청 상태 표시, 보류 필터 제거, 상태 없음이 전체 이슈에 보이는지 확인합니다. Notion 메인·보드의 기존 GitHub 전용 안내도 함께 수정합니다. 공개 편집 설정과 API 접근은 읽기로 확인하고 제한된 실제 요청·거절·충돌·반복 무변경 검사를 수행합니다. 실제 UI·Actions/API 검증 전에는 운영 완료로 표시하지 않습니다.

## 근거

- [기술 계약](notion-sync-plan.md)
- [GitHub Issues GraphQL](https://docs.github.com/en/graphql/reference/issues)
- [GitHub Projects GraphQL](https://docs.github.com/en/graphql/reference/projects)
- [Projects Actions 자동화](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions)
- [Notion 요청 제한](https://developers.notion.com/reference/request-limits)
