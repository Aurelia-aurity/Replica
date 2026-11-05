# 백엔드에서 AI를 호출하는 순서

2026-10-15 · 합의용 호출 예시 · 실제 서버 구현 전

앱은 백엔드를 호출한다. 사용자 권한·DB 조회·처리 상태는 백엔드가 관리하고, 백엔드 안의 AI 담당 모듈이 학교 GPU를 호출한다. 아래 네 경로는 제안이며 실제 호스트와 서비스 인증은 담당자 합의 후 설정한다. 원문·명세를 담은 요청은 암호화된 승인 연결로 전송한다.

## 기록 등록·수정 후 전체 명세 재생성

```text
사용자가 전사·대상 발화 확인 및 확정
  → 백엔드: 권한 확인, 현재 전체 확정 기록 스냅샷 고정
  → AI 모듈: tokenizer로 전체 원문을 누락 없이 분할
  → GPU: POST /v1/persona-profiles/analyze (잎 노드마다 1회)
  → AI 모듈: 각 응답·입력 범위·작업/시도 일치 확인
  → GPU: POST /v1/persona-profiles/merge (2~8개씩, 필요 시 여러 단계)
  → AI 모듈: 최종 루트가 전체 스냅샷 범위를 포함하는지 확인
  → 백엔드/DB: 현재 작업·시도·버전·출처 검사와 명세 저장을 원자적으로 수행
  → 백엔드: 명세 반영 완료 확인
```

부분 분석 두 번과 통합 한 번으로 끝나는 작은 합성 예제를 제공한다. 모든 요청은 같은 persona_id, source_revision=3, source_snapshot_id를 사용한다.

| 순서 | API | 요청 JSON | 응답 JSON |
|---|---|---|---|
| 1 | POST /v1/persona-profiles/analyze | examples/02-analyze-request.json | examples/02-analyze-response.json |
| 2 | POST /v1/persona-profiles/analyze | examples/08-second-leaf-request.json | examples/08-second-leaf-response.json |
| 3 | POST /v1/persona-profiles/merge | examples/09-merge-request.json | examples/09-merge-response.json |

예제의 전체 원문은 한 text_version_id의 네 발화다. 첫 잎은 0·1번 발화 전체, 두 번째 잎은 2·3번 발화 전체를 담당한다. 같은 text_version_id가 여러 잎에 나타나도 발화 범위가 겹치지 않으면 정상이다. 각 analyze 요청 내부에서 같은 text_version_id를 중복 항목으로 보내지는 않는다. 전체 범위 검사는 examples/10-snapshot-manifest.json과 같이 백엔드가 관리하며 이 파일은 GPU 요청이 아니다.

GPU에는 매 호출의 request_id와 논리 노드 node_id를 따로 보낸다. merge.inputs에는 앞선 응답에서 검증한 node_id와 profile_content를 그대로 넣는다. 예제 JSON을 임의로 바꿔 실제 응답 대신 사용해서는 안 된다. GPU는 DB를 조회하지 않으며 snapshot_id만으로 숨은 기록을 읽지 않는다.

명세 저장에는 DB가 발급한 프로필 id·version을 사용한다. 이후 대화 예제의 프로필 ID는 합성 값이며 저장 성공을 뜻하지 않는다. 기존 publish_style_profile에는 작업/시도·전체 범위 원자적 검증을 위한 보완이 필요하다. 이 조건이 준비되기 전에는 안전한 갱신 구현 완료로 처리하지 않는다.

명세 생성과 별도로 AI 모듈이 전체 확정 자료의 임베딩·색인을 준비한다. ‘반영됨’ 상태가 프로필과 검색 색인을 모두 의미하면 양쪽 준비가 확인되어야 한다. 작업 도중 기록이 바뀌면 현재 작업은 발행할 수 없고 사용자 재요청으로 새 스냅샷을 만든다.

## 사용자 대화

```text
앱 → 백엔드: 질문과 대화 세션
  → 백엔드: 사용자·페르소나·세션 권한 확인, 최근 완결 대화 조회
  → AI 모듈: 현재 유효 명세 조회, 질문 임베딩, 허용 기록 검색
  → 백엔드: 저장된 사용자 편집 호칭을 persona에 구성, 근거를 화자별로 분할하고 speaker_role 지정
  → GPU: POST /v1/persona/generate
  → 백엔드: request_id·적용 프로필·사용 근거와 현재 자료 유효성 확인
  → DB: 현재 대화 시도로 결과 저장
  → 앱: 텍스트 응답 / 음성 대화 세션이면 TTS 결과 전달
```

- 개인화 대화: examples/03-persona-request.json → examples/03-persona-response.json
- 근거 없는 개인 사실 질문: examples/04-no-evidence-request.json → examples/04-no-evidence-response.json. 추측하지 않는 정상 답변이다.
- baseline: examples/01-baseline-request.json → examples/01-baseline-response.json. 명세·근거를 보내지 않는다.

개인화 요청의 persona에는 display_name과 provider_address(미설정 시 null)를 넣는다. provider_address는 style_profile.content.speech_style.address_terms의 자동 추출 후보를 사용자가 확인·수정한 값이다. evidence는 저장·임베딩 전에 백엔드가 민감정보를 마스킹한 내용이며, 각 항목의 speaker_role은 persona·provider·other 중 하나다. AI는 persona 역할만 본인의 발화·사실 출처로 쓰고 provider·other 발화를 페르소나에게 귀속하지 않으며 마스킹 플레이스홀더를 유지한다. 세부 규칙과 미정인 history 마스킹 정책은 통합 명세를 따른다.

음성 대화도 STT 결과 텍스트를 같은 generate 요청의 message에 담는다. LLM이 적용한 말투로 생성한 응답 message를 백엔드가 TTS에 전달한다. 텍스트 세션의 음성 입력 버튼과 음성 대화 세션의 구분은 앱·백엔드 계약을 따른다. GPU에 음성 파일이나 TTS 자격증명을 보내지 않는다.

## HTTP 호출 예

아래 명령은 각 호출의 형식을 보여주며 전체 작업 실행 스크립트가 아니다. api-calls 폴더에서 합의한 AI_BASE_URL과 AI_AUTH_HEADER를 설정한 뒤 사용한다. 실제 서버에는 보내지 않았다. 응답 검사·노드 검증·범위 확인·DB 발행을 생략한 채 차례대로 실행하면 통합 구현 검증이 되지 않는다.

```bash
# 기본 대화
curl --request POST "${AI_BASE_URL}/v1/baseline/generate" \
  --header 'Content-Type: application/json' \
  --header "${AI_AUTH_HEADER}" \
  --data-binary @examples/01-baseline-request.json

# 부분 분석: 첫 묶음, 두 번째 묶음은 08-second-leaf-request.json
curl --request POST "${AI_BASE_URL}/v1/persona-profiles/analyze" \
  --header 'Content-Type: application/json' \
  --header "${AI_AUTH_HEADER}" \
  --data-binary @examples/02-analyze-request.json

# 앞선 두 응답을 검증한 뒤 구성하는 통합 호출
curl --request POST "${AI_BASE_URL}/v1/persona-profiles/merge" \
  --header 'Content-Type: application/json' \
  --header "${AI_AUTH_HEADER}" \
  --data-binary @examples/09-merge-request.json

# 저장된 명세와 검색 근거를 적용한 대화
curl --request POST "${AI_BASE_URL}/v1/persona/generate" \
  --header 'Content-Type: application/json' \
  --header "${AI_AUTH_HEADER}" \
  --data-binary @examples/03-persona-request.json
```

타임아웃·429 뒤 curl --retry나 자동 재호출을 사용하지 않는다. 사용자가 재시도를 선택하면 백엔드가 기존 결과와 현재 시도를 확인한다. 대화는 같은 turn_id·새 시도·새 request_id, 분석은 같은 논리 작업의 새 analysis_attempt·새 노드들·새 request_id로 시작한다. 이전 GPU 계산이 남아 새 시도와 겹쳐도 이전 결과는 현재 결과를 덮어쓸 수 없다. 분석 재시도는 실패한 노드 하나만 보내는 방식이 아니라 전체 잎부터 새 노드 ID로 시작하며, 호출 수와 전체 마감은 새 시도에 적용한다. DB 게시 결과가 불명이면 먼저 저장 결과를 확인하며, 확인 전 새 시도 등록이나 재추론을 하지 않는다.

## 동시 처리 검증 목표

baseline 전용 운영은 인스턴스 전체 한 요청부터 구현한다. 고도화 공유 운영에서는 baseline 경로의 동시 한도 1과 전체 용량 C를 함께 적용한다. C=2이고 메모리 예산도 충분하면 baseline과 분석은 동시에 수락할 수 있지만 baseline 두 건은 수락하지 않는다. 이는 고도화 운영의 명시적 동작 변경이며 wire schema는 유지한다. 고도화에서는 대화 한 건과 분석 계열 한 건을 함께 실행해 메모리·응답시간·오류·요청 ID 분리를 확인한 뒤 용량을 설정한다. 슬롯과 토큰/KV 예산 확보 실패는 429, 같은 request_id 실행 중은 409다. 분석 노드의 동시 발송 수를 제한해 대화용 여유를 남기며 실측 없이 ‘A6000이므로 몇 건 보장’이라고 정하지 않는다.

문서의 합성 JSON·스키마·요청/응답 연결·본문 크기·명령 구문은 jsonschema가 설치된 Python 환경에서 verify_contract.py로 확인할 수 있다. 실행할 때 --baseline-dir에 기존 contracts/baseline 경로를 지정한다. 이 검사는 API 호출이나 GPU 실행을 하지 않으며 checks.json에 오프라인 결과를 기록한다. scenarios/acceptance.json은 구현 때 확인할 조건으로, 실제 동시성·DB 검사가 통과했다는 증거가 아니다.
