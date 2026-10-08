# #17 동기화 기술 계약

설계는 독립 GPT-6 최대 추론 검토와 Gemini ultra UI·오류·PM재개 계획 검토를 거쳐 수락했다. 이 문서는 구현 기준이며 실제 운영 성공을 선언하지 않는다. 기존 코드의 15개 속성·4단계 동기화를 19개 속성·5단계와 GitHub Project 원본 관리로 전환한다.

## 식별·원본

- 저장소 Aurelia-aurity/Replica와 repository ID 1392442366을 함께 검증한다.
- 개인 소유자 Just-Simple0의 비공개 Project #4, 소유자 node ID, Project node ID, Status single-select 필드 ID와 5개 옵션 ID를 설정값/조회 결과로 대조한다. 표시 이름만으로 대상을 선택하지 않는다.
- GITHUB_TOKEN은 저장소 Issue/PR 사실 조회, PROJECT_TOKEN은 Project 전용 client, NOTION_TOKEN은 Notion 전용 client다. fallback은 없다.
- Project 항목의 자연키는 Issue content node ID, Notion 키는 gh:<repository ID>:issue:<numeric issue ID>다. PR에는 기존 Issue 기반 키를 유지한다.
- 다른 저장소·Draft item·Project의 PR item은 보존하며 상태 자동화에서 제외한다. 관리 대상 중복·불명확한 항목은 쓰기 전에 중단한다.

## 상태 결정 우선순위

1. CLOSED: COMPLETED는 완료, NOT_PLANNED는 미계획/상태 비움, DUPLICATE는 중복/상태 비움, 알 수 없는 종료는 확인 필요/상태 비움이다. duplicateOf 관계는 종료 사유와 별개다. 중복 링크 누락은 일반 확인 안내, COMPLETED/NOT_PLANNED와 중복 관계의 모순은 보류다.
2. OPEN 새 재오픈: 현재 리뷰 가능한 종료 연결 PR이 있으면 검토 중, 없으면 백로그로 한 번 처리한다. 처리한 event ID로 중복 전환을 막는다.
3. 현재 main에 종료 연결된 열린 non-Draft PR이 하나라도 있으면 검토 중이다. 단순 제목·댓글·번호 언급을 종료 연결로 추정하지 않는다.
4. 직전 자동 검토 주기에 해당하는 마지막 리뷰 가능한 PR이 사라지면 진행 중으로 한 번 복귀한다. 팀원이 이미 백로그·준비 중 등으로 변경했다면 해당 일반 상태를 보존한다.
5. 최초 이관/신규 초기화 이후의 일반 상태는 Project 값을 표시한다. 열린 완료·자동 주기 없는 검토 중 등 해석 불가 상태는 보류한다. 차단 라벨은 상태를 바꾸지 않는다.

## 최초 이관·시간 경계

전체 source 조회를 시작하기 전에 검증된 GitHub 서버 시각 T를 확보한다. 전체 사전검증 후 최초 원격 쓰기로 Sync 관리행에 같은 T를 저장·readback한다. 더 늦은 조회 종료·저장 시각으로 바꾸지 않는다. 확인된 기준선은 재사용한다. 저장 결과가 불명확하면 다른 쓰기를 중단하고 다음 실행에서 관리행을 먼저 확인한다.

createdAt<T는 기존 이슈, >T는 새 이슈다. 해상도 때문에 모호한 경계는 보류한다. 조회 종료/T 저장 사이 생긴 이슈도 신규이며 snapshot에 없으면 다음 전체 조회에서 발견한다. 재오픈 초기 기준선은 T 이전 이벤트만 포함한다. T 이후 재오픈을 개별 기록 생성 시 과거 사건으로 소비하지 않는다.

기존 Notion 일반 상태를 한 번 이관한다. Project와 다른 일반 상태이면 보류/Project 보존, 기존 양쪽 상태가 비어 있어도 보류다. 종료·새 재오픈·현재 리뷰 PR을 우선한다. 이관 완료 뒤 Notion 상태는 역반영하지 않는다. Project mutation이 없는 정상 일치도 이관 완료 기록을 저장·readback한다.

## 전체 읽기 사전검증

저장소 모든 REST 페이지·PR 상세·GraphQL 종료/대표 관계/재오픈/연결 PR, Project 모든 필드/항목, Notion 스키마·관리행·활성/보관 행의 모든 페이지를 읽는다. incomplete·중복·반복/누락 cursor·권한/식별자 오류·잘못된 JSON·타임스탬프는 쓰기 0회로 실패한다. Issue updatedAt이 그대로여도 Project 수동 변경을 읽는다.

원격 쓰기 전에 source fingerprint를 재확인한다. Issue ID/state/stateReason/duplicateOf/updatedAt/재오픈 ID와 연결 PR의 ID·열림/Draft·base·종료 연결, Project 항목/옵션 ID를 포함한다. 최초 이관에만 Notion page ID·상태·이관 여부·pending·보류/재개 입력을 추가한다. 자체 pending 저장의 예상 변화는 외부 변경과 구분한다. 일정·메모·본문 변화는 상태 경합으로 간주하지 않는다.

## 내부 상태·전환·복구

동기화 내부 상태 rich_text에 JSON version=1을 보관한다. Issue 식별자·이관 완료·재오픈 기준선/마지막 처리 ID·PR 검토 주기/복귀 완료·연결 hash·pending·보류·승인된 표시 복구를 포함한다. Sync 관리행은 T·실행 ID·최근 결과·전체 성공 시각을 보관한다. 숨김은 ACL이 아니다. 정상 미초기화와 손상된 JSON을 구분하며 손상·다른 version/ID·크기 초과를 초기값으로 덮지 않는다.

전환 순서:

1. 전체 사전검증·기준선 readback 후 보류 기록을 먼저 판단한다.
2. pending(전환 종류·ID·이전/목표 옵션·fingerprint·확정할 checkpoint)을 저장·readback한다.
3. 현재 GitHub/Project/필요한 최초 Notion 입력을 재조회한다. 경합이면 재판정 또는 보류한다.
4. 필요한 Project mutation을 한 번 호출하고 현재 사실·목표 값을 readback한다. checkpoint 확정·pending 해제 저장/readback 후 사용자 표시를 반영한다.
5. mutation/기록 결과 불명은 현재 사실을 재조회해 확인 가능한 완료만 확정한다. 과거 pending을 자동 재실행하지 않는다.
6. checkpoint 성공/표시 실패는 Project를 읽어 표시만 복구하고 재오픈·복귀를 반복하지 않는다.

Notion 행이 없으면 기존 Pending create fence → 행 생성/동기화 키 식별/readback → 내부 pending/readback → 필요한 Project add 1회/content ID 재조회 → checkpoint → 표시 순서다. Project 확인 전 사용자 작업 상태는 비운다. 생성 결과 불명은 기존 fence로 재조회하며 행이 미식별인 상태에서 Project를 추가하지 않는다.

Project add 불명 시 전체 content ID 조회에서 1개면 재사용, 0개는 결과 확인 보류, 여러 개는 중복 보류다. 0개를 미실행으로 추정하지 않는다. clientMutationId를 서버 멱등성 보장으로 취급하지 않는다. Project mutation/Notion 생성에는 자동 재시도가 없다. 조회 POST와 mutation POST를 구분한다.

## 보류·PM 재개·결과 표시

내부 보류 사유를 저장·readback한 뒤 `보류:` 사용자 표시를 저장·readback한다. 내부 기록 성공/표시 실패는 다음 실행에 보류를 유지하며 표시만 복구한다. 값 일치만으로 해제하지 않는다. 내부 저장 결과가 불명확하면 재조회하고 확인 불가 시 다른 쓰기를 중단한다.

resolve_issue_numbers는 양의 정수 목록을 중복 제거하고 대상 저장소·기존 보류·workflow_dispatch·PM 고정 numeric actor ID를 검증한다. 현재 Project 값 승인이지 GitHub 모순/조회 실패 우회가 아니다. 승인자·run ID·승인값·fingerprint를 저장/readback한 뒤 현재 사실을 기준으로 재개한다.

checkpoint 확인 후 해결된 보류만 해제하고 일반 확인 안내를 보존한다. 표시 readback까지 성공해야 재개 완료다. 실패 시 승인된 표시 복구 미완료 기록을 남겨 다음 실행에서 승인·현재 사실을 재검증하고 표시만 복구한다. 새 모순이면 재보류한다.

보류 개수·번호는 내부 보류와 표시 미완료 기록을 전수 재계산한다. 단순 차감하지 않는다. 표시·집계 실패는 전체 성공/재개 완료로 안내하지 않는다. 가능한 실패·부분 결과를 기록하고 안내 갱신 자체가 불가하면 이전 결과를 보존한다. 마지막 전체 성공 시각은 모든 대상 성공 시에만 변경한다.

## 운영 경계·검사

trusted main만 checkout하고 read-only repository 권한, SHA pinned checkout, persist-credentials=false, 단일 concurrency/cancel-in-progress=false를 유지한다. fork/PR 코드·입력 문자열을 실행하지 않는다. disabled 상태에서도 명시적 수동 dry-run만 허용하며 양쪽 쓰기 0회다.

구버전 중지·진행 중 실행 종료 확인 → 백업 → 검토된 main 코드 → 19속성/5단계 준비 → disabled dry-run → 승인된 실제 이관 → 반복 실행/보존 확인 → 정기 운영 순서다. 스크립트는 스키마·뷰를 자동 변경하지 않는다.

의미있는 합성 검사는 시간 경계, 자체 pending/외부 경합 구분, 정상 미초기화/손상 기록, no-op 이관, 종료/중복/PR/재오픈 우선순위, 검토 복귀 1회·일반 상태 보존, 보류 표시·승인 복구·집계, 각 쓰기/readback 실패, create/add 응답 유실 0/1/다수, 전체 pagination 실패, dry-run 무쓰기, 토큰 분리·redaction, 일정·메모·본문·PR 보존을 포함한다.

Notion 실제 필터·로딩·빈 열, 실제 Project/Actions 권한·API 쓰기, 운영 재실행은 별도 필수 수락이다. unique constraint/원자적 CAS/휴지통 조회의 한계를 인정하며 합성 검사나 설계 승인으로 운영 완료를 주장하지 않는다.
