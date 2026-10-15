# GitHub Project ↔ Notion 동기화

> **현재 계약:** 사용자 화면 상태는 `백로그`·`준비 중`·`진행 중`·`검토 중`·`완료`·`취소`의 6개입니다. 양방향 요청은 기존 일반 상태 3개만 받습니다. Notion `요청 상태`와 `요청 처리` 선택지는 각각 기존 5개를 유지합니다. 실제 Project/Notion 변경과 운영 전환은 코드 리뷰와 별도 PM 승인 뒤 수행합니다.

GitHub 이슈·PR의 종료·재오픈·연결 PR 사실을 우선합니다. 일반 작업 상태는 Project에서 변경하며, 양방향 전환 후 Notion 보드 이동도 변경 요청으로 처리합니다. Notion은 일정·메모·자료를 보존합니다. Python 표준 라이브러리와 GitHub Actions로 운영합니다.

## 팀 사용 방법

작업은 GitHub 이슈로 만들고 관련 PR에 `Closes #번호` 등 명시적인 종료 연결을 둡니다. 6개 작업 상태는 Replica GitHub Project와 동기화합니다. 기존 `NOTION_BIDIRECTIONAL_ENABLED` 값을 보존하며, 이 값이 true인 경우 백로그·준비 중·진행 중 사이 Notion 카드 이동을 요청으로 처리합니다. 역방향·단계 건너뛰기도 허용합니다. `취소`는 GitHub에서 검증한 종료 사실을 표시하는 종단 상태이며 요청 대상으로 받지 않습니다. 일정·메모·페이지 본문은 보존합니다.

조회는 5분 예약이며 5분 내 완료를 보장하지 않습니다. 감지 전에는 접수 표시가 없고 감지 후 반영 대기, 확인 완료 후 반영 완료로 표시합니다. 거절·충돌은 요청 상태와 사유를 남기고 유효한 GitHub 위치로 복원합니다. 중간 이동 이력은 저장하지 않습니다. 양쪽 다른 값의 변경 순서가 입증되지 않으면 PM 확인으로 넘깁니다. Notion 페이지 수정 시각이나 마지막 편집자를 상태 이동 시각·요청자로 추정하지 않습니다.

공개 링크 편집을 유지하므로 외부인의 카드 이동도 요청이 될 수 있습니다. 내부 기록도 편집 가능하며 숨김·schema·hash는 승인 인증 수단이 아닙니다. 정상 PM 재개 경로는 numeric PM actor·새 workflow_dispatch·첫 attempt·triggering actor 일치 검사를 유지하지만 내부 기록 변조를 기술적으로 차단하지 않습니다. 쓰기 직전 재조회와 사후 확인을 수행하되 조건부 쓰기가 없는 짧은 경쟁 구간의 이동 손실 가능성은 남습니다. 상태와 종료 사유를 계산하는 기준은 [sync_engine.py](../../scripts/sync_engine.py), 운영·실행 절차는 이 문서를 따릅니다.

| 상태 | 기준 |
| --- | --- |
| 백로그 | 새 작업, 착수 준비 전 |
| 준비 중 | 담당·범위·선행 작업 정리 중 |
| 진행 중 | 작업 진행 중 |
| 검토 중 | main에 종료 연결된 열린 PR이 리뷰 가능 |
| 완료 | 이슈가 완료 사유로 종료됨 |
| 취소 | 이슈가 미계획 또는 중복 사유로 종료됨 |

새 이슈는 백로그, 재오픈은 리뷰 가능한 연결 PR이 있으면 검토 중, 없으면 백로그로 한 번 전환합니다. Draft는 제외합니다. 마지막 리뷰 가능한 PR이 없어지면 진행 중으로 한 번 복귀하며, 이후 팀원이 바꾼 일반 상태는 보존합니다. PR 병합 자체로 완료를 추정하지 않습니다.

`COMPLETED` 종료는 `완료`, 확인된 `NOT_PLANNED`·`DUPLICATE` 종료는 `취소`로 표시합니다. 알 수 없거나 모순된 종료 사유는 추정하지 않고 기존 보류·확인 경로를 따릅니다. 일정은 보존합니다. 중복 대표 링크는 GitHub의 검증된 명시적 관계만 사용합니다. 링크가 없는 중복도 `취소`로 표시하고 대표 이슈 확인 안내를 남깁니다.

종료 정보 모순·최초 이관 충돌은 `보류:` 안내를 표시하고 Project 상태를 보존합니다. 다른 정상 이슈는 계속 처리합니다. 값이 맞아져도 PM 지정 재개 전에는 보류를 해제하지 않습니다. 최초 이관 대상인 기존 열린 이슈의 상태가 미지정이고 검토·재오픈 자동 규칙도 적용되지 않으면 보류합니다. PM이 Project에서 백로그·준비 중·진행 중 중 하나를 정한 뒤 새 수동 실행으로 재개합니다.

Issue 제목은 `#번호 원본 제목`, 담당자·라벨은 쉼표 구분 문자열로 표시합니다. PR은 메타데이터만 동기화하고 Issue 상태 규칙을 적용하지 않습니다. GitHub 본문·댓글·코드·이메일을 복사하지 않으며 기여도 점수는 계산하지 않습니다. Discord 전달은 별도 [알림 작업](../discord.md)이 담당하고 동기화 작업은 비밀 없는 실행 결과 artifact만 제공합니다.

## 화면 구성

상단은 **통합 보드 · 전체 이슈** 두 탭입니다. 보드는 6단계 순서를 사용하고 카드에 담당자·일정·라벨을 표시합니다. `취소` 빈 그룹 노출은 실제 rollout 화면에서 확인하며, 그 확인 전에는 보기를 임의로 바꾸지 않습니다. 번호는 제목에 포함합니다.

현재 board/calendar 필터는 Issue이며 상태가 있고, GitHub가 Open이거나 Closed/종료 사유 완료인 항목만 표시합니다. 검증된 미계획·중복 종료 Issue는 기존 종료 사유 조건에 따라 두 보기에서 빠지고, 전체 이슈에는 종료 사유·대표 링크·확인 안내와 함께 표시됩니다. 열린 Issue가 Project의 `취소` 값 때문에 PM 보류 중이면 Open 조건을 만족하므로 보드에 남을 수 있습니다. 필터는 유지하고 실제 rollout 화면에서 결과를 확인합니다.

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
| `NOTION_BIDIRECTIONAL_ENABLED` | Actions repository Variable. 현재 저장된 값을 전환 내내 보존합니다. true이면 기존 요청 흐름을 계속 처리하며, 무조건 false로 바꾸지 않습니다. 운영자가 rollout 중 일시 중지한 경우 검증 후 정확한 이전 값을 복원하고 readback합니다. |
| `NOTION_DATA_SOURCE_ID` | 기존 데이터 소스 UUID |
| `NOTION_CONTROL_PAGE_ID` | 기존 Sync 관리행 UUID |
| `PROJECT_ID` | Just-Simple0의 비공개 Replica Project #4 node ID |
| `PROJECT_OWNER_ID` | Project 소유자의 고정 node ID |
| `PROJECT_STATUS_FIELD_ID` | Status single-select 필드 node ID |
| `PROJECT_STATUS_OPTIONS` | 6개 상태 이름 → 실제 옵션 ID의 JSON 객체. 기존 5개 ID를 그대로 두고 `취소` ID는 Project 필드 readback 후에만 추가합니다. |
| `PM_GITHUB_USER_ID` | 재개를 승인할 PM의 고정 numeric user ID |

```json
{"백로그":"option-backlog","준비 중":"option-ready","진행 중":"option-progress","검토 중":"option-review","완료":"option-done","취소":"operator-bound-cancel-option-id"}
```

위 옵션 ID는 설명용 값입니다. 실제 ID를 추정하지 말고 Project Status 필드의 실제 readback 결과에서 `취소` ID를 얻은 뒤 6개 이름·ID 전체를 비교합니다. 기존 5개 ID는 바꾸지 않습니다. Project 접근 권한이 있는 인증으로 아래 읽기 전용 쿼리를 실행하면 Project·Status 필드·옵션 ID를 확인할 수 있습니다. 기존 로컬 인증에 권한이 없다면 등록된 Actions secret을 꺼내지 말고 PM의 승인된 인증 경로에서 확인합니다.

```sh
gh api graphql -f query='query { user(login: "Just-Simple0") { id projectV2(number: 4) { id number owner { ... on User { id login } } fields(first: 100) { nodes { ... on ProjectV2SingleSelectField { id name options { id name } } } pageInfo { hasNextPage endCursor } } } } }'
```

hasNextPage가 true이면 후속 페이지를 읽어 Status 필드를 확인해야 합니다. field/option ID를 이름에서 추정하거나 API 권한 확인 없이 입력하지 않습니다.
 팀원 Project 편집 권한은 저장소 collaborator 권한과 별도로 확인합니다.

## 적용 순서

1. 코드 변경의 독립 리뷰와 보호된 `main` 반영 승인을 마친 뒤, 운영 담당자는 당시 Actions 설정값을 기록합니다. 자동 실행을 일시 중지하거나 설정을 바꾸면 이전의 정확한 값을 보존하고 검증 후 복원합니다. 양방향 설정은 현재 값이 true이므로 무조건 false로 바꾸지 않습니다.
2. 새 consumer 코드를 반영합니다. 아직 기존 5개 Project 옵션과 Notion schema라면 새 consumer의 정확한 6개 옵션·schema 사전검사가 쓰기 없이 실패해야 합니다. 기존 consumer도 새 옵션이 추가되기 전까지는 기존 계약으로 동작합니다.
3. PM이 Project의 동일 Status 필드에 `취소` 옵션을 추가하고 실제 ID를 읽습니다. Actions `PROJECT_STATUS_OPTIONS`에는 기존 5개 name/ID를 그대로 두고 읽은 `취소` ID만 추가합니다. 6개 전체의 실제 필드 readback과 Actions mapping이 일치하지 않으면 진행하지 않습니다.
4. Notion `작업 상태`에 `취소` 옵션을 추가하고 readback합니다. `요청 상태`의 다섯 기존 작업 상태와 `요청 처리`의 다섯 처리 상태는 그대로 둡니다. 기존 board/calendar 필터가 취소를 제외하는지, 전체 이슈의 종료 사유·대표 링크가 보이는지, `hideEmptyGroups=false`에 따른 빈 취소 열을 실제 화면에서 확인합니다. 구체 화면을 본 뒤 PM이 필요한 보기 변경 여부를 결정합니다.
5. 전체 원본을 대상으로 trusted `main`에서 dry-run을 실행합니다. `full_effect_report`는 현재 bidirectional 설정을 반영해 전체 Issue·PR·Notion·Project·기존 관리행·복구 상태의 잠재 효과 범위를 나타내며 정확한 실행 write 목록이 아닙니다. source·schema·설정·checkpoint drift가 있으면 다시 실행하고 새 범위를 승인받습니다. 관리행이 없거나 ID/key/type이 맞지 않으면 preflight는 쓰기 없이 실패하고 새 관리행을 만들지 않습니다. 전체 dry-run은 특정 Issue 번호로 제한되지 않습니다.
6. 보고서 전체 범위에 대한 별도 PM 승인 후에만 전체 실행을 수행합니다. 기존 행 ID·메모·일정·본문, 같은 행에 대한 확인된 백필, 전체 source에서 새로 발견된 적격 Issue/PR 행, Project/Notion 결과, control summary와 복구 상태를 확인합니다. 모순·불명확한 종료 사유는 보류하며 덮어쓰지 않습니다.
7. 검증 중 바꾼 설정이 있다면 처음 기록한 정확한 값을 복원하고 실제 readback합니다. 기존값이 true이면 true로 복원해 요청 흐름을 유지합니다. 새 Project/Notion 옵션을 삭제하는 대신 문제가 있으면 옵션을 유지하고 승인된 forward-fix를 적용합니다. 후속 반복 실행과 실제 화면을 확인하기 전에는 운영 완료로 기록하지 않습니다.

코드 반영 뒤 아직 5개 Project 옵션/Notion 스키마라면 새 코드의 preflight는 쓰기 없이 실패합니다. Project `취소` 옵션 ID가 정확히 바인딩되고 Notion 작업 상태에 취소 선택지가 확인된 뒤에만 sync가 진행됩니다. 새 옵션을 삭제하는 방식으로 되돌리지 말고, 문제가 생기면 기존 설정을 보존한 채 forward-fix합니다.

## 실행·보류 재개

```sh
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py'
python3 -B scripts/notion_sync.py --help
python3 -B scripts/notion_sync.py --dry-run
```

로컬 실행은 안전하게 전달된 인증 환경이 이미 있을 때만 사용하고 Actions와 동시에 실행하지 않습니다. `NOTION_SYNC_ENABLED`가 비활성이면 일반 로컬 실행도 건너뜁니다. Actions 수동 실행에는 필수 `approved_sha`를 선택 ref의 전체 40자리 SHA와 똑같이 입력합니다. `main`, `fix/17-project-add-readback`, `feat/32-bidirectional-sync` 외 ref/tag, 비PM, actor와 triggering actor 불일치, 재실행, SHA 불일치는 checkout 전에 거부됩니다. 수동 실행만 동기화 프로세스에 enabled=true를 주며 `dry_run=true`는 읽기 전용입니다. 두 feature ref는 번호 없는 dry-run만 허용하고, 실제 쓰기와 번호 지정 보류 재개는 `main`에서만 허용합니다. feature ref 검증은 trusted `main`의 producer·consumer 및 schema2 보고서 검증을 대체하지 않습니다. 운영 dry-run과 제한 실제 검증은 trusted `main`의 동일한 전체 SHA를 사용합니다. 제한 실제 검증은 자동 동기화가 중지된 동안 repository Variable `NOTION_BIDIRECTIONAL_ENABLED=true` 및 수동 `dry_run=false`로 수행합니다. 설정을 바꿨다면 성공·실패와 관계없이 기록한 정확한 이전값으로 복원하며, 현재 true를 무조건 false로 바꾸지 않습니다.

PM이 GitHub 모순·Project 상태를 정리한 뒤 trusted `main` SHA의 Actions 수동 실행에서 resolve_issue_numbers에 `17,20`처럼 입력합니다. 전체 원본 dry-run 보고서를 사용하며, 현재 bidirectional 값을 보존합니다. 실제 재개는 동일한 보고와 source에 대한 별도 승인이 있어야 합니다. 번호는 중복 제거하고 잘못된 입력·비PM 실행·미보류 대상은 거부합니다. 취소 Project 상태는 유효한 재개 기준이 아니며 PM이 기존 절차에 따라 일반 Project 상태를 정한 뒤 새 승인으로 재개합니다. PM 승인은 모순된 종료 정보나 조회 실패를 무시하지 않습니다.

번호를 지정한 보류 재개는 PM이 새로 시작한 수동 실행의 첫 번째 시도에서만 승인으로 인정합니다. 원래 실행자와 현재 요청자의 numeric ID가 모두 PM인지 확인하며, 실행 시도 번호가 없거나 재실행이면 쓰기 전에 거부합니다. 실패 후 다시 승인하려면 기존 실행의 Re-run 대신 Run workflow에서 현재 값을 확인하고 번호를 새로 지정합니다.

승인 기록·checkpoint·Notion 표시·집계까지 확인한 뒤 재개 완료로 기록합니다. 일반 확인 안내는 보존합니다. 표시 복구가 미완료이면 보류 집계에 남기고 다음 실행에서 승인된 표시만 복구합니다.

## 실패·보존

전체 조회·인증·스키마 검증 실패는 쓰기 전에 중단합니다. 적용 중 쓰기 결과가 불명확하면 추가 쓰기를 멈추고 영속 기록을 유지합니다. 전체 성공 시각은 모든 대상 처리가 성공할 때만 갱신합니다. 보류가 남으면 부분 결과를 기록합니다. 사전검증이나 안내 갱신이 실패하면 관리행 요약은 이전 값일 수 있으므로 GitHub URL이 있으면 해당 workflow를, URL이 비어 있으면 저장소 Actions에서 현재 실행 결과를 확인합니다.

Notion의 `GitHub 수정`과 `동기화 시각`은 UTC 분 단위로 표시하지만 source 시각, cutoff·이벤트, 내부 `last_success_at`/`last_result.at`은 전체 정밀도를 유지합니다. readback은 timezone-aware 단일 시각이 전송한 분 단위 instant와 정확히 같아야 하며, 초가 0이 아니거나 `end`/`time_zone`이 null이 아니면 실패합니다. 관리행 내부 성공 시각이 비어 있는 구형 상태의 기존 표시값은 보존합니다. `일정`·메모·본문은 이 표시 투영의 대상이 아닙니다.

Notion data source 조회는 모든 페이지의 `object=list`, `type=page_or_data_source`, `page_or_data_source` object, `results` 목록과 Boolean `has_more`를 검증합니다. `next_cursor` 키는 항상 있어야 하며, 다음 페이지가 있으면 비어 있지 않은 새 문자열, 마지막 페이지면 `null`이어야 합니다. `request_status`는 선택 필드입니다. 있으면 `type=complete`만 허용하고, 누락 외의 unknown/malformed 값과 `incomplete`는 쓰기 전에 거부합니다. `incomplete_reason`이 있으면 `query_result_limit_reached`만 인식합니다. active/archived 조회는 각각 독립적으로 10,000건 경계에 도달하면 전체 결과가 보장되지 않는 것으로 보고 거부합니다. dry-run 진단은 페이지 수·결과 개수·허용된 상태 범주·형식 유효성만 출력하며 원문, cursor, ID, credential은 기록하지 않습니다. 이전 실패 응답을 보관하지 않아 해당 실행에서 상태 필드가 누락됐는지 malformed였는지는 아직 확인되지 않았습니다.

생성은 Pending create fence → Notion 행 식별 → add pending 영속 확인 → Project 추가 1회 → 성공 응답 item ID 즉시 영속 저장·readback → 직접 node와 전체 목록 관계 확인 → 초기 상태/checkpoint → 사용자 표시 순서입니다. 반환 ID는 확인 전에도 별도 readback 기록에 보존하며 confirmed 관계와 혼동하지 않습니다. ID 저장 확인 전에 관계 조회·초기화하지 않습니다. 성공 응답 저장 후 가시성 지연은 후속 실행 최대 3회 확인합니다. 네트워크 실패도 1회이며 조회 직전 고유 토큰과 횟수를 영속 확인합니다. 토큰 저장 후 중단은 예약 1회로 계산하며 같은 토큰을 중복 계산하지 않습니다. 즉시 readback 내부 재시도와 후속 3회 예산은 별개입니다.

직접 ID·목록·대상 Issue·저장소·보관 상태·현재 요청/사실이 일치해야 계속합니다. 중복·다른 ID·명백한 모순은 즉시 PM 보류, 3회 실패는 ADD_READBACK_EXHAUSTED로 PM 확인입니다. 응답 ID가 없거나 저장 미확인이면 자동 읽기만 허용하고 PM이 재개합니다. 불확실한 add를 자동 재전송하지 않습니다. 기존 반환 ID 없는 legacy pending도 이 경계를 유지합니다.

Issue 내부 상태 v2는 v1의 pending/hold/resume/projection/식별자와 checkpoint를 보존하고 baseline/request/readback/notion_write를 확장합니다. 기존 행의 최초 이관은 안정적인 양쪽 일치만 baseline으로 초기화합니다. 새 이슈는 자동 백로그 초기화의 쓰기와 readback을 확인한 뒤 baseline을 확정합니다. 양방향 전환 중 기존 행의 불일치나 미완료 작업을 자동 승자/새 요청으로 만들지 않습니다. 전송 직전에 sent를 영속 확인하며 재시작의 sent/uncertain은 결과 조회만 합니다. 모든 PATCH 전송 속성을 readback하고 하나라도 불일치면 완료/baseline을 확정하지 않습니다.

문제가 생기면 옵션·관리행·키·내부 기록을 삭제하거나 초기값으로 덮지 않습니다. 운영 설정은 기록된 기존값으로 복원하고, 설정을 끄는 조치가 승인된 경우에도 검증 뒤 기존값을 다시 readback합니다. v2를 읽지 못하는 과거 SHA로 돌아가는 것만으로는 안전한 rollback이 아니므로, 새 코드를 보완하는 forward-fix를 우선합니다.

Notion unique constraint·Project 원자적 CAS가 없어 다른 도구와의 동시 쓰기는 지원하지 않습니다. Actions는 하나의 concurrency 그룹으로 직렬화하며 쓰기 직전 재확인으로 경합을 감지합니다. 휴지통 전체 조회를 보장하지 않으므로 관리 항목은 삭제하지 말고 복구 후 실행합니다.

## #32 전환 검증

이 절은 #32 당시의 전환 기록입니다. 현재 #39의 코드 수락과 실제 운영 전환은 위의 적용 순서 및 단계별 수락을 따릅니다. 기존 board/calendar 필터와 양방향 설정을 보존하고, 실제 화면·Actions/API 확인 전에는 운영 완료로 표시하지 않습니다.

## 근거

- [기술 계약](notion-sync-plan.md)
- [GitHub Issues GraphQL](https://docs.github.com/en/graphql/reference/issues)
- [GitHub Projects GraphQL](https://docs.github.com/en/graphql/reference/projects)
- [Projects Actions 자동화](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions)
- [Notion 요청 제한](https://developers.notion.com/reference/request-limits)
