# Replica GitHub → Notion 동기화 계획

## 범위와 수락 조건
- Syllva는 사용하거나 변경하지 않는다. 교수 회신 전 기여도 점수/팀 PR·브랜치 정책은 확정하지 않는다.
- 저장소 Aurelia-aurity/Replica의 이슈와 PR 메타데이터만 사용자가 지정한 Replica 부모 페이지 아래 새 데이터 소스로 단방향 전송한다. 본문, 댓글, 코드, 이메일은 복사하지 않는다.
- GitHub 관리: 제목, 종류(Issue/PR/Sync), 번호, URL, 상태(Open/Closed/Draft/Merged), 작성자·담당자(login 문자열), 라벨(문자열), GitHub 수정 시각, 동기화 키.
- Notion 관리: 작업 상태(백로그/진행 중/검토 중/완료), 일정(date range), 메모, 페이지 본문. 동기화 PATCH에서 제외한다. PR merged와 Issue closed는 GitHub 사실만 갱신하며 작업 상태는 자동 완료하지 않는다.
- 표준 Notion 데이터베이스의 작업 보드, 일정 캘린더, PR 목록 보기를 구성한다. 자체 화면이나 승인/알림 흐름 구현은 없다. Gemini review N/A: 표준 DB 보기 설정과 서버측 동기화이며 별도 UI/사용자 흐름 구현이 없음.

## 최소 구성
- Python 3 표준 라이브러리 스크립트 scripts/notion_sync.py, unittest fake HTTP 검증, docs/architecture/notion-sync.md, .github/workflows/notion-sync.yml.
- GitHub API repository.id + issue.id로 키를 만들고 모든 상태의 repository issues를 페이지 단위로 읽는다(PR 포함). PR 상세 GET으로 merged/draft를 정확히 구분한다. Notion query도 끝까지 페이지 단위 조회한다. incomplete 결과/중복 키는 fail closed. API 버전 Notion 2026-03-11, GitHub 2026-03-10. 원격 API/토큰을 CLI로 임의 변경하지 않는다.
- 매 실행 GitHub 현재 snapshot에서 시작하여 이벤트 payload/PR 코드/본문은 실행 입력으로 쓰지 않는다. Notion 기존 항목의 GH 수정 시각이 더 최신이면 덮어쓰지 않는다. GitHub 조회 실패시 기존 Notion 항목 삭제/완료 처리 금지. 원본에서 사라진 항목도 그대로 보존한다.
- 스키마와 데이터 소스 ID, 관리 행을 쓰기 전 검증한다. 기존 Notion 항목은 정확한 키 기준 upsert, 수동 속성 및 본문 보존. archived/trash 관리 항목 발견시 중복 생성 방지를 위해 중단한다(활성 및 archived partition 조회, trash 접근 한계는 문서화).
- Notion API에는 원자적 unique constraint가 없다. GitHub workflow concurrency 한 그룹 cancel-in-progress=false를 쓰며 수동 동시 실행은 금지한다.
- 별도 관리 행의 Pending create rich_text 필드에 생성 직전 키를 기록/PATCH readback 확인한 후 POST pages를 딱 한 번 호출한다. 성공시 생성 키 readback 확인 후 pending을 지운다. 생성 timeout/5xx면 pending을 유지하고 실패한다. 다음 실행은 pending 키의 행이 있으면 재사용·해제하며, 없으면 자동 생성하지 않고 수동 조사를 요청한다. 관리 행 삭제/초기화 금지. 관리행 마지막 성공 시각은 전체 성공시에만 기록한다.
- GET/query/PATCH 안전한 동일 payload는 429/일시 오류 제한 재시도, Retry-After 준수(최대 허용 지연을 넘으면 실패). create는 자동 재시도하지 않는다. API 응답/토큰/개인 메타데이터를 오류 로그에 출력하지 않는다. HTTPS 고정 호스트·redirect 금지, request timeout/적정 요청 간격.

## Actions/권한
- issues, pull_request_target 지정 types, 15분 schedule, workflow_dispatch. pull_request_target은 main의 고정 checkout만 하고 fork/PR ref·코드·이벤트 문자열을 실행하지 않는다. checkout action full SHA pin, persist-credentials=false. GITHUB_TOKEN contents/issues/pull-requests read만.
- secrets.NOTION_TOKEN, vars.NOTION_DATA_SOURCE_ID, vars.NOTION_CONTROL_PAGE_ID, vars.NOTION_SYNC_ENABLED=true가 필요하다. 준비 전 비활성 gate. GitHub 자동 토큰 사용, PAT 생성 없음. Notion internal connection은 부모 페이지에만 연결, Read/Insert/Update content, user/comment 권한 불필요. 연결 토큰은 사용자 비공개 UI로 Actions secret 등록; 채팅/실제 dotenv 읽기 금지.
- 사용자 제공 부모 아래 DB/관리행/표준 보기 생성은 현재 요청 범위다. 등록된 MCP OAuth는 Actions API credential이 아니므로 임의 추출하지 않는다. 코드 완료와 실 운영 완료를 구분한다. 새 main push 권한을 과거 승인에서 추론하지 않는다.

## 검증/리뷰
- 계획 native web ChatGPT 검토 후 구현. 최종에는 전체 sync 코드/워크플로우/문서/테스트/계획 풀코드를 독립 검토한다. 리뷰 프로젝트는 Replica + path hash, 모델/slider 실제 확인, Pro 부재면 동일 모델 최대 강도 및 evidence. 원본 schema2 manifest 보존, consistency check.
- unittest: pagination/incomplete, 최초 생성·재실행 upsert, 상태/PR draft/merged, 수동 속성 미전송, 최신 시각 보존, 중복/archived 키 중단, 429 retry 및 권한 오류 중단, uncertain create fence 보존·재개·불명시 block, credential/response redaction, fixed endpoints/redirect 거절.
- 실제 Notion 스키마/표준 보기/관리행 readback. 자격증명 설정 후 workflow 실제 실행 및 동일 항목 재실행으로 중복 없음·수동 일정/메모 보존 readback을 확인해야 실 운영 완료. 자격증명 대기면 pending으로 표시하고 commit/push와 가동을 분리한다.

## 근거
- https://developers.notion.com/reference/post-page (data_source_id parent)
- https://developers.notion.com/reference/query-a-data-source (pagination/incomplete/archived partitions)
- https://developers.notion.com/reference/patch-page (omitted properties preserved)
- https://docs.github.com/en/rest/issues/issues (issues include PR; IDs are issue IDs)
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows (target default branch security/schedule)

## 계획 검토 지적 반영: 실행 계약 고정
- bootstrap은 현재 총괄이 연결된 Notion MCP의 create_database(parent.page_id, SQL DDL), create_pages(parent.data_source_id), create_view(검증된 view DSL)로 수행하고 fetch readback 후 ID를 GitHub vars에 등록한다. 동기화 스크립트는 bootstrap을 하지 않는다. 토큰은 사용자가 Actions secret 비공개 UI로 등록한다. 정상 가동 전 vars ENABLED=false 유지.
- 스키마: 제목/title; 종류/select(Issue,PR,Sync); 번호/number; GitHub URL/url; GitHub 상태/select(Open,Closed,Draft,Merged); 작성자/rich_text; 담당자/rich_text; 라벨/rich_text; GitHub 수정/date; 동기화 키/rich_text; 동기화 시각/date; Pending create/rich_text. 수동 속성: 작업 상태/select(백로그,진행 중,검토 중,완료), 일정/date(range), 메모/rich_text. 모든 필드는 nullable. 일반항목 제목/키/종류/번호/URL/상태/수정시각은 snapshot에서 필수. 관리행은 키=replica-sync-control, 종류=Sync, 번호/URL/GH상태/GH수정은 비어 있고 Pending은 초기 빈 문자열.
- canonical key는 gh:<positive repo id>:issue:<positive issue id>. Replica expected repository id=1392442366, repository name fixed Aurelia-aurity/Replica. 담당자/라벨은 unique sorted strings를 JSON array(ensure_ascii=False)로 직렬화한다. 줄바꿈/구분자/순서에 의한 충돌 방지. 빈 목록은 []. 날짜는 timezone-aware ISO를 비교하고 같으면 현재 GH 값을 repair한다.
- 전체 preflight 경계: GitHub 모든 페이지·모든 PR 상세 + Notion schema/관리행/활성 및 archived 모든 페이지 + 키·중복·pending·날짜 검증을 전부 완료한 뒤 첫 write. Late read failure/invalid timestamp/duplicate는 write 0회. apply 단계 중 원격 실패는 부분 적용 가능하며 last-success 시각은 갱신하지 않는다.
- 관리행은 configured ID와 queried 유일 replica-sync-control 행이 같아야 한다. 부모 data_source_id 정확히 일치, active/not trash, 종류 Sync, 필요한 스키마 일치. 모든 일반행도 configured parent 일치 확인. 잘못된 설정은 원격 write 전에 실패.
- create 상태 머신: pending PATCH→GET으로 같은 키 확인→POST 딱 1회→GET 생성행 키/parent 확인→pending clear. 모든 POST 오류(4xx/429/5xx/timeout/응답손실), 성공 뒤 GET/clear 실패는 pending을 보존한다. 다음 실행 전체 preflight에서 pending키 행이 유일·active하면 정상 upsert와 clear, 없거나 archived면 자동 create/PATCH 없이 실패한다. 4xx/429도 보수적으로 운영자가 조사하고 가동중지 상태에서 pending만 명시적으로 해제한다. GET readback이 빈 결과여도 자동 재생성하지 않는다.
- retry: 4회까지, request timeout 30s, 일반 요청 간격 0.4s, Retry-After 없으면 1/2/4초 backoff+jitter. Retry-After <=60s만 대기하며 그보다 길면 실패(조기 재시도 없음). 429 public_api_request_blocked는 재시도 금지. 529 overload/500/502/503/504 및 transport read/동일값 PATCH만 제한 재시도. page create 자동 retry 0회. 응답 본문은 분류에만 사용, 출력하지 않는다.
- 테스트 추가: late page/detail/query failure zero writes, canonical collision/invalid key, control binding/archived/kind/schema, all create error outcomes/clear failure and recovery, no credential when disabled zero requests, fixed workflow triggers/group/SHA/ref/permissions/gate, PR Open/Draft/Closed/Merged precedence, all manual props/body preservation. Trash rows inaccessible to query: 사용자가 동기화행 휴지통 이동하지 않도록 문서화하며 행을 복구한 뒤 rerun; create pending은 유지. 독립 동시 실행 금지/Notion unique constraint 부재 한계 문서화.
- 검토의 REQUIRED 항목은 기존 설계의 모호한 계약을 위와 같이 명확히 해 해소한다. 새 권한/삭제/양방향 상태 변경은 추가하지 않음. 구현·테스트와 최종 독립 검토에서 이 계약을 다시 확인한다.
