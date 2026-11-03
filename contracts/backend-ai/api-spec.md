# 백엔드 ↔ AI 호출 명세 초안

2026-10-15 개정 · PM 방향 확정 · 교차 담당자 합의 및 구현 전

이 문서는 고도화 단계까지 합의할 호출 형태를 제안한다. 구현은 baseline부터 진행한다. 실제 호스트·서비스 인증 방식·수치 한도·시간 제한은 담당자와 확정해야 하며, 아래 경로는 현재 실행 가능한 URL이 아니다. 예제는 모두 합성 자료다.

## 담당 경계

백엔드는 사용자 인증·접근 권한 확인, DB 저장과 조회를 맡는다. 백엔드 안에서 실행하는 AI 담당 모듈은 임베딩, 관련 기록 검색, 근거 선택과 프롬프트 구성을 맡는다. 학교 GPU는 전달받은 텍스트를 분석하거나 답변을 생성한다. GPU에는 DB 자격증명, 앱 사용자 JWT, 저장소 URL을 전달하지 않는다.

공통 LLM에 저장된 페르소나 명세를 적용한다. 대상 인물별 추가 모델 학습은 이번 구현 방향에 포함하지 않는다. STT·TTS API, 앱 상태 표시, 화자 선택 API와 DB 구현은 별도 담당 범위다.

## 호출 목록

| 단계 | 메서드·경로 | 목적 |
|---|---|---|
| baseline | POST /v1/baseline/generate | 개인화 없이 기본 대화 생성 |
| 고도화 | POST /v1/persona-profiles/analyze | 전체 확정 기록 중 한 분할 묶음의 말투·습관 분석 |
| 고도화 | POST /v1/persona-profiles/merge | 부분 분석 결과를 통합해 최종 명세 생성 |
| 고도화 | POST /v1/persona/generate | 페르소나 명세와 검색 근거를 적용한 답변 생성 |

모두 JSON 요청, 비스트리밍, 완성된 결과 1회 반환이다. baseline은 동시 생성 1건·대기열 없음으로 시작한다. 고도화의 분석·통합·대화는 설정된 동시 처리 용량과 토큰·KV 캐시 예산 안에서 수락한다. 용량 정책은 아래 동시 처리 절을 따른다. GPU 추론용 service caller 인증을 사용하며 앱에서 GPU를 직접 호출하지 않는다. 헤더 이름과 인증 방식은 미정이고 호출 예제의 AI_AUTH_HEADER는 담당자가 합의한 서비스 인증 헤더 전체를 뜻한다.

프롬프트의 작업·규칙은 AI 담당 코드에서 관리한다. 명세·기록·대화에 포함된 문장은 입력 데이터로 다루며 임의 system_prompt, 모델 경로, DB 연결정보, 실행 옵션을 요청 필드로 받지 않는다. 백엔드 AI 모듈은 입력 검증·자료 구성과 프롬프트 형식을 소유하고 GPU 서비스는 고정 템플릿으로 렌더링한다.

## 1. 기본 대화 생성

POST /v1/baseline/generate

요청은 request_id, message, history, max_new_tokens 네 필드다. 성공 응답은 request_id, message, finish_reason, ai_generated, context를 반환한다. 형식과 오류 판정 순서는 기존 [PR #29 계약](https://github.com/Aurelia-aurity/Replica/pull/29)을 유지한다. 요청에 페르소나나 검색 근거를 추가하지 않는다.

- 요청: examples/01-baseline-request.json
- HTTP 200: examples/01-baseline-response.json
- 구조 원본: schemas/baseline-request.schema.json, baseline-success.schema.json, baseline-error.schema.json

## 2. 페르소나 명세 생성·갱신

POST /v1/persona-profiles/analyze

| 요청 필드 | 설명 |
|---|---|
| request_id | 이 AI 호출 시도의 UUID |
| persona_id | 분석할 대상 인물의 UUID. 권한은 백엔드가 확인 |
| source_revision | 자료를 조회한 시점의 페르소나 자료 개정 번호 |
| source_snapshot_id | 전체 확정 자료 스냅샷의 UUID. 원문·출처 목록은 백엔드가 관리 |
| node_id | 이번 부분 분석을 식별하는 UUID. 작업·시도·스냅샷과 입력 묶음에 고정 |
| sources | 확정·접근 허용된 자료. 각 항목은 record_id, text_version_id, target_utterances |
| sources[].target_utterances | 상위 단계에서 대상 화자의 발화만 추린 문자열 배열. 오디오·미확정 전사문은 받지 않음 |

| HTTP 200 필드 | 설명 |
|---|---|
| request_id, persona_id, source_revision, source_snapshot_id, node_id | 요청과 같은 값 |
| source_text_version_ids | 실제 분석한 sources의 text_version_id 목록 |
| profile_content | schema_version, speech_style, conversation_habits, limitations |
| model_name | 분석을 수행한 공통 모델의 식별자. 페르소나별 학습 모델이나 adapter ID가 아님 |

speech_style에는 tone, address_terms, sentence_endings, frequent_expressions 배열을 둔다. conversation_habits는 대화 습관, limitations는 자료 부족 등 분석 한계를 담는다. profile_content.schema_version은 1이다. 근거가 부족한 항목은 빈 배열로 두고 성격·경험·관계를 추측해 채우지 않는다. 내용이 명세 형식에 맞지 않으면 성공으로 반환하지 않고 INFERENCE_FAILED로 처리한다.

- 요청: examples/02-analyze-request.json
- HTTP 200: examples/02-analyze-response.json
- 구조: schemas/personalization.schema.json의 analyzeRequest, analyzeSuccess

백엔드 AI 모듈은 get_personalization_input으로 얻은 동일 스냅샷의 전체 확정 기록으로 명세를 재생성한다. 기존 명세에 새 기록만 덧붙이거나 일부 기록을 표본으로 골라 완료 처리하지 않는다. analyze 한 번은 전달받은 부분만 분석하며, 전체 재생성 완료는 백엔드의 작업 검증으로 판정한다.

### 전체 기록 분할과 통합

1. 접근 가능한 대상 화자의 전체 확정 발화를 한 source_revision과 source_snapshot_id로 고정한다. 백엔드에는 전체 record_id·text_version_id 목록, 발화 순서와 분할 범위를 저장한다. 삭제 중·미확정·다른 인물 기록은 제외한다. 확정 발화가 없으면 분석·발행하지 않고 자료 부족으로 종료한다.
2. 실제 배포 모델의 tokenizer와 고정 chat template로 각 호출의 토큰 수를 계산한다. 고정 규칙·필드 직렬화·출력 예약 토큰까지 포함해 모델 및 서버 문맥 한도 이하로 나눈다. 문자 수와 HTTP 본문 한도도 따로 검사한다.
3. 큰 발화도 Unicode 경계에서 나누고 원래 발화 인덱스와 [start, end) 범위를 백엔드에 보존한다. 모든 대상 문자가 순서대로 정확히 한 번 포함되어야 한다. 공백만 있는 구간은 인접 구간에 포함한다. 임의 생략·샘플링·중복 덧붙임으로 한도를 맞추지 않는다. 각 잎 노드(leaf)를 analyze로 호출한다.
4. 성공한 부분 명세 2~8개를 아래 merge로 통합한다. 각 묶음도 정확한 토큰·본문 한도 안에 들어야 한다. 결과가 여러 개면 다시 통합하며 입력 노드 수를 줄인다. 홀로 남은 노드는 변경 없이 다음 단계로 넘긴다. 최소 2개를 합칠 수 없다면 실패로 종료하며, 단일 노드끼리 무한 반복하거나 내용을 잘라 진행하지 않는다.
5. 최종 루트가 전체 분할 범위를 누락·중복 없이 포함하는지 백엔드가 확인한 뒤에만 명세를 발행한다. 부분 결과는 현재 명세로 저장하지 않는다. 하나의 잎만 있으면 그 결과가 루트가 된다.

입력 전달은 누락 없이 관리하지만, LLM의 부분 분석·통합이 모든 말투 특징을 보존한다는 뜻은 아니다. limitations에 자료·분석 한계를 유지하며 누락·상충 특징은 품질 평가로 확인한다. 성격·경험·관계를 추측해서 채우지 않는다.

백엔드는 최초 호출 전에 전체 잎 분할과 작업별 최대 호출 수·전체 마감·자료량 한도를 확인한다. 통합 트리는 각 단계에서 노드 수가 감소하는 범위로 계획하며 실제 출력으로 다시 토큰 검사한다. 한도 밖이면 명시적으로 작업을 실패 처리하고 반영 완료로 표시하지 않는다. 한 글자의 최소 입력이나 유효 명세 두 개조차 규칙·출력 예산과 함께 들어가지 않으면 더 쪼개지 않고 실패한다. 수치 한도는 배포 설정으로 합의하며 무제한 처리·정해진 시간 내 완료를 보장하지 않는다.

잎이 N개이고 통합마다 노드가 최소 하나 줄어들면 새 호출은 최대 2N-1회다. 이 상한으로 작업 호출 예산을 확인하며 재시도는 포함하지 않는다. 서버는 분석·통합 출력 예약 토큰도 배포 설정으로 고정하고 그 값을 사전 계산에 사용한다. 작업이 실패·만료되면 같은 시도가 여전히 최신이더라도 게시할 수 없다. 최종 저장은 현재 시도 확인과 함께 작업 활성 상태·전체 마감 유효성도 원자적으로 검사한다.

호출 예산과 전체 마감은 analysis_attempt마다 적용한다. 사용자 수동 재시도로 새 시도가 등록되면 새 호출 예산과 새 마감으로 전체 잎부터 다시 시작한다. 여러 시도에 걸친 비용을 한 번의 2N-1회로 보장하지 않는다. 사용자 동작 없이 새 시도·대체 노드를 만들어 실패 호출을 우회하지 않는다. 단일 노드의 타임아웃과 마지막 통합의 타임아웃에도 동일하게 적용한다.

각 node_id는 한 analysis_operation_id·analysis_attempt·스냅샷 안의 불변 입력에 연결한다. 원문 범위 또는 자식 노드와 결과, 모델·tokenizer·template 버전을 백엔드가 연결해 저장한다. 다른 작업·시도의 결과를 가져다 섞지 않는다. GPU가 반환하는 출처 ID만으로 전체 처리 여부를 판정하지 않는다.

get_personalization_input의 기존 응답이 영속·불변 스냅샷 저장까지 보장한다는 뜻은 아니다. 백엔드·DB 연결에서는 조회 시점의 개정 번호와 전체 원문·출처가 일치하도록 읽고 스냅샷을 고정해야 한다. 분할 도중 현재 DB를 다시 읽어 새 원문을 같은 snapshot_id에 섞지 않는다. 기존 DB 함수에 필요한 저장·검증 보완은 교차 담당자 구현 조건이다. GPU 서비스 재시작으로 모델·tokenizer·template가 바뀌면 같은 분석 작업의 후속 노드를 계속 합치지 않고 작업을 실패 처리한다.

source_snapshot_id는 이번 고도화 초안에서 추가한 연결 식별자다. 기존 DB Row에 이미 존재한다고 가정하지 않는다. 저장된 프로필 id·version과 스냅샷·전체 출처의 대응을 영속화하고 조회하는 방법도 백엔드·DB 보완 조건에 포함한다. 이 연결이 없으면 개인화 대화 요청을 완성된 구현으로 처리하지 않는다.

### 부분 명세 통합 호출

POST /v1/persona-profiles/merge

요청은 request_id, persona_id, source_revision, source_snapshot_id, node_id와 inputs다. inputs는 {node_id, profile_content} 2~8개이며 자식 node_id는 서로 달라야 한다. 출력 node_id가 자식 ID와 같거나 순환하는 그래프는 허용하지 않는다. 백엔드는 각 입력이 현재 작업의 검증된 자식 결과와 일치하는지 검사한다.

HTTP 200은 위 다섯 식별·버전 필드와 profile_content, model_name을 반환한다. profile_content 형식은 부분 분석과 같다. 입력은 분석 대상 데이터로 취급하고 임의 prompt를 받지 않는다. 전체 출처·분할 범위는 백엔드가 자식 그래프에서 계산하며, 거대한 출처 목록을 매번 GPU에 보내지 않는다.

- 두 번째 부분 분석: examples/08-second-leaf-request.json, 08-second-leaf-response.json
- 통합 요청·응답: examples/09-merge-request.json, 09-merge-response.json
- 구조: schemas/personalization.schema.json의 mergeRequest, mergeSuccess

부분 분석과 통합의 HTTP 200은 해당 노드의 성공이다. 필수 메타데이터 불일치·잘못된 명세·출력 잘림·불완전 JSON은 사용 가능한 노드가 아니며 발행할 수 없다. 실패·429·전송 결과 불명 뒤 자동 재호출하지 않는다. 사용자가 시작한 한 작업 안에서 서로 다른 잎과 통합 노드를 순서대로 호출하는 것은 계획된 작업 수행이며 재시도가 아니다. 한 노드 실패 시 새 노드 발송을 중단하고 이미 실행 중인 결과도 실패한 작업의 발행에 쓰지 않는다.

백엔드는 최종 루트의 ID·자료 개정 번호·전체 출처·분할 범위와 내용을 검사한 뒤 publish_style_profile에 전달한다. DB가 프로필 id·version을 발급하고 저장·적용을 마쳐야 반영 완료다. GPU의 HTTP 200은 분석 성공이며 반영 완료가 아니다. 분석 중 자료가 바뀌어 발행이 거부되면 이전 분석 결과를 새 자료 버전으로 바꿔 저장하지 않는다. 자동 재분석 여부는 이번 계약에서 허용하지 않으며 사용자 재요청으로 다시 시작한다.

임베딩·색인 준비는 AI 담당 모듈이 별도로 수행하고 기존 save_record_chunks에 저장한다. 명세 반영 완료만으로 RAG 색인이 준비됐다고 판단하지 않는다. 기록 상태의 ‘반영됨’이 명세와 색인 준비를 함께 뜻한다면 두 결과를 모두 확인한 뒤 표시하는 것은 백엔드 담당이다. 재생성 중 기존 명세는 현재 자료 버전·권한 검사를 통과할 때만 사용한다. 기록 수정·삭제로 무효가 된 명세를 가용성을 이유로 계속 적용하지 않는다.

## 3. 개인화 대화 생성

POST /v1/persona/generate

| 요청 필드 | 설명 |
|---|---|
| request_id, message, history, max_new_tokens | baseline과 같은 의미 |
| persona_id | 백엔드가 권한을 확인한 대상 인물 |
| source_revision | 이번 검색·프로필 적용의 기준 자료 개정 번호 |
| style_profile | 저장된 id, version, source_revision, source_snapshot_id, content |
| evidence | AI 담당 모듈이 검색한 허용된 근거 목록. chunk_id, record_id, text_version_id, content |

백엔드 AI 모듈이 현재 적용 가능한 프로필을 읽고 질문 임베딩과 search_record_chunks로 근거를 선택한다. 이전 확정본·삭제 중 자료·다른 인물의 자료는 제외한다. style_profile.source_snapshot_id는 실제 저장된 프로필의 전체 출처 스냅샷을 가리킨다. 전체 출처 목록과 현재 접근 가능 여부는 백엔드에서 확인하며 대화 호출에 전체 기록 ID를 나열하지 않는다. style_profile.source_revision은 요청의 source_revision과 같아야 한다. 호출 준비 중 자료가 변경되면 그 호출에 사용할 스냅샷 일치 여부를 다시 확인한다. GPU는 DB의 현재 상태를 알지 못하므로 접근 권한·최신성 보장의 주체는 백엔드다. 생성 도중 삭제·자료 변경이 발생한 답변의 저장·전달 전 재확인도 백엔드가 맡는다.

| HTTP 200 필드 | 설명 |
|---|---|
| request_id, message, finish_reason, ai_generated, context | baseline과 같은 의미 |
| applied_profile | 요청에 전달한 프로필의 id, version, source_revision |
| evidence_used | 실제 답변에 사용한 evidence의 chunk_id 목록. 중복 불가 |
| model_name | 답변을 생성한 공통 모델 식별자 |

- 요청: examples/03-persona-request.json
- HTTP 200: examples/03-persona-response.json
- 근거 없는 질문: examples/04-no-evidence-request.json, 04-no-evidence-response.json
- 구조: schemas/personalization.schema.json의 personaRequest, personaSuccess

개인 경험·관계·사실은 원본 기록 근거가 있을 때만 답한다. 관련 근거가 없으면 알 수 없다고 답하며 이는 HTTP 200이다. 일반 대화는 근거가 없어도 페르소나 말투를 적용할 수 있다. history는 문맥이며 원본 기록 근거로 취급하지 않고 자동 학습 자료로 편입하지 않는다. evidence_used는 입력 evidence의 부분집합이어야 한다. GPU의 근거 ID 반환이 답변의 사실성을 증명하지는 않으며 내용 검증은 별도 평가 대상이다.

음성 질문도 STT 결과 텍스트로 같은 호출을 사용한다. 생성된 message를 백엔드가 TTS에 전달한다. 현재 초안은 음성용 별도 대본 재생성 API를 두지 않는다.

## 한도와 문맥 처리

아래 수치는 기존 baseline 및 합의용 제안이며 모델 검증·팀 합의 전이다. JSON Schema는 필드 구조를 검사하고 raw 본문 바이트·필드 간 일치·출처·권한은 별도로 검사한다.

| 항목 | 제안 한도 |
|---|---|
| HTTP 원시 본문 | 모든 호출 256 KiB = 262144 bytes |
| 현재 질문·이력의 각 문자열·개별 발화·개별 근거 내용 | 각각 16000 Unicode code points, 공백만 있는 값 거부 |
| 대화 이력 | 완결 user/assistant 20쌍 |
| max_new_tokens | 정수 1~1024, 백엔드 기본 160 |
| 분석 자료 | 한 호출 1~20개, 자료당 발화 1~100개; 원시 본문 한도도 동시에 적용 |
| 통합 입력 | 한 호출 2~8개, 자식 node_id 중복 불가 |
| 명세 문자열 배열 | 항목당 512자, 배열당 최대 16항목 |
| 대화 근거 | 0~10개, chunk_id 중복 불가 |

크기·문자 수 상한은 문맥 축소 전에 검사한다. 2000자 질문과 20쌍 이력도 전체 256KiB 이하를 보장하지 않는다. 실제 직렬화한 본문 바이트를 백엔드에서 확인한다.

대화에서 토큰 한도를 넘으면 오래된 완결 이력 쌍부터 제외하고 context에 사용·제외한 쌍 수를 반환한다. 합은 제출한 history 길이와 같아야 한다. 현재 질문·출력 예산·명세·근거를 몰래 줄이지 않는다. 이력을 모두 제외해도 초과하면 422다. 더 작은 허용 근거 집합을 만들 수 있을 때만 사용자가 시작한 후속 호출에서 근거 축소를 시도한다. 근거가 이미 비어 있거나 축소 후에도 넘치면 422로 종료한다. 질문·명세·출력 예산을 몰래 바꾸거나 추가 호출하지 않는다. 축소가 성공을 보장하지 않는다. 분석 호출도 넘치는 원문을 몰래 잘라 처리하지 않고 422를 반환한다.

GPU 서비스가 시작 전에 공통 모델 식별자를 백엔드 설정과 맞춘다. DB의 start_chat_stage는 AI 호출 전에 model_name을 요구하므로 성공 응답을 받은 뒤에야 모델 이름을 정하는 흐름으로 구현하지 않는다. baseline 응답에는 새 필드를 추가하지 않고 고도화 응답의 model_name은 설정과 대조한다.

## 동시 처리와 수락 정책

GPU 한 장이 동시 요청 한 건만 처리할 수 있다는 뜻은 아니다. 모델을 한 번 적재하고 여러 요청을 배치 처리하는 추론 엔진을 구현 후보로 둔다. vLLM 도입·실제 메모리 여유·동시 처리 수·응답시간은 아직 실측하지 않았다.

baseline은 기존 계약의 동시 1건·대기열 없음으로 구현한다. 고도화에서는 서버가 배포 시 설정한 전체 동시 실행 수와 각 요청의 토큰·KV 캐시 예산을 원자적으로 확보한 요청만 수락한다. 용량이 없으면 429를 반환하고 해당 요청의 작업을 뒤에서 실행하거나 서버에 무제한 대기시키지 않는다. 409 중복 request_id 판정은 네 경로 전체에 적용한다.

운영 모드는 구분한다. baseline 전용 단계는 baseline 경로만 제공하고 인스턴스 전체 1건을 유지한다. 고도화 공유 단계는 네 경로가 한 모델·전체 용량 C를 공유한다. 이때 baseline의 1건은 baseline 경로의 동시 수락 한도이며, 고도화 공유 단계에서 인스턴스 전체 1건이라는 이전 제한을 변경하는 것이다. wire schema 보존이 동시 수락 동작까지 같다는 뜻은 아니다.

고도화 공유 단계는 유효한 실행 중 request_id의 전역 중복부터 검사해 409를 반환한다. 새 ID는 baseline 경로 한도(1건), 분석 계열 한도, 전체 실행 수 C, 토큰·KV 예산을 모두 원자적으로 검사·예약하며 하나라도 부족하면 429다. 개인화 대화는 대화 계열이고 baseline 경로별 카운터를 사용하지 않는다. C=1이면 분석 계열 한도도 1이며 모든 경로가 직렬 처리된다. C≥2이면 분석 계열 한도는 C-1 이하로 설정한다.

다음 표는 고도화 공유 단계에서 다른 request_id, 정상 입력·모델, 충분한 토큰·KV 예산, 그 외 실행 중 요청 없음이 전제다. C=2의 분석 계열 한도는 1이다.

| 실행 중 → 새 요청 | C=1 | C=2 |
|---|---|---|
| baseline → analyze 또는 merge | 429 | 수락 |
| analyze 또는 merge → baseline | 429 | 수락 |
| baseline → baseline | 429 | 429: baseline 경로 한도 |
| 개인화 대화 → analyze 또는 merge | 429 | 수락 |
| analyze 또는 merge → 개인화 대화 | 429 | 수락 |
| analyze → merge 또는 analyze | 429 | 429: 분석 계열 한도 |
| 개인화 대화 → baseline 또는 개인화 대화 | 429 | 수락 |

실행 중 동일 request_id를 다른 경로에 보내도 위 표보다 전역 중복 검사 409가 우선한다. 실행 수에 여유가 있어도 토큰·KV 예산이 부족하면 429다. 연결이 끊겼다는 이유만으로 실행 수나 토큰 예약을 반환하지 않는다. 따라서 표의 실행 중 요청은 실제 GPU 계산이 종료된 요청을 포함하지 않는다.

초기 검증 목표는 같은 모델에서 대화 1건과 분석 계열 1건의 동시 처리다. analyze와 merge는 분석 계열이다. 전체 용량이 2 이상이면 분석 계열의 동시 수락을 전체 용량보다 최소 1건 적게 제한하고 대화용 여유를 남긴다. 토큰·메모리 예산도 함께 예약해야 하므로 슬롯만 남는다고 채팅 수락을 보장하지 않는다. 실제 용량이 1이면 분석 중 채팅이 429를 받을 수 있다. 모든 경로가 같은 물리 엔진을 사용하면 baseline도 같은 전체 예산에 포함하며 서로 다른 서버를 띄워 GPU 용량을 중복 계산하지 않는다.

백엔드 AI 모듈도 분석 노드를 한꺼번에 무제한 발송하지 않는다. 먼저 수락된 GPU 계산을 중단하거나 채팅 지연시간을 보장하는 정책은 이번 계약에 없다. 연결 종료 뒤에도 실제 계산이 끝나기 전에는 실행 수·토큰 예약을 반환하지 않는다. 어느 시도의 결과인지 확인하는 게시 가드는 병렬 실행에서도 유지한다.

## 오류

네 호출 모두 기존 baseline의 안전한 오류 envelope인 {request_id, error: {code, message}}를 사용한다. 원시 바이트 한도 → 서비스 인증 → 구조·타입·형식 → 파싱된 크기·개수 → 모델 준비 → 문맥 → 원자적 슬롯 수락 순서를 따른다. 정의하지 않은 필드는 거부한다.

| HTTP | error.code | 처리 |
|---|---|---|
| 400 | INVALID_REQUEST | 필수값·형식·중복 ID·요청 내 버전 일치 등 입력 오류 |
| 401 / 403 | SERVICE_UNAUTHENTICATED / SERVICE_FORBIDDEN | 서비스 인증·권한 오류 |
| 413 | PAYLOAD_TOO_LARGE | 본문·문자·개수 한도 초과 |
| 422 | CONTEXT_LIMIT_EXCEEDED | 필수 입력과 출력 예산이 모델 문맥을 초과 |
| 503 | MODEL_NOT_READY | 공통 모델 준비 전 |
| 409 | REQUEST_IN_PROGRESS | 같은 인스턴스에서 같은 request_id가 실행 중 |
| 429 | AI_BUSY | 설정된 동시 실행 수·요청별 토큰·KV 예산을 확보할 수 없음 |
| 504 | INFERENCE_DEADLINE_EXCEEDED | GPU가 실제 반환한 추론 마감 초과 안내 |
| 500 | INFERENCE_FAILED | 추론 실패·빈 출력·잘못된 명세 형식·입력에 없는 근거 ID 반환 |

409는 동일 sources 등의 내부 중복이 아니라 실행 중인 동일 요청 ID를 뜻한다. 본문의 중복 source text_version_id 또는 chunk_id는 400이다. 파싱·인증 전 등 request_id를 안전하게 얻을 수 없는 초기 오류만 null을 허용하며 나머지는 요청 ID와 같아야 한다. 질문·명세·기록·예외 원문·자격증명을 응답과 로그에 넣지 않는다.

## 타임아웃과 수동 재시도

자동 재시도는 모든 호출에서 하지 않는다. 앱·백엔드·GPU의 어느 시간 제한에 걸렸든 사용자가 재시도를 선택할 수 있다. HTTP 연결이 끝나도 GPU 추론이 끝났다고 판단하지 않는다. 실제 계산이 끝날 때까지 GPU는 request_id와 슬롯을 유지한다.

앱의 turn_id는 논리적 대화 턴을 식별한다. AI의 request_id는 개별 AI 호출 시도를 식별한다. 사용자 수동 재시도는 같은 turn_id를 유지하되 새 DB 시도 번호와 새 AI request_id를 사용한다. 각 ID의 대응은 백엔드가 관리하며 앱 입력을 AI request_id로 그대로 신뢰하지 않는다.

1. 재시도 요청을 받으면 백엔드는 기존 턴을 조회한다. 이미 완료된 답변은 재생성하지 않고 저장된 결과를 반환한다.
2. 같은 턴의 백엔드 처리 단계가 pending이면 해당 시도의 결과·타임아웃 상태부터 확인한다. 실행 중이라는 이유만으로 새 실행권을 부여하지 않는다.
3. 실제 시간 초과·결과 불명이 확인되면 해당 시도 번호로 그 백엔드 단계를 실패 종료한다. 이 상태는 해당 호출 시도의 결과이며 GPU 계산 중단을 뜻하지 않는다. 결과 저장이 먼저 성공했거나 상태가 바뀌면 재조회하고, 무조건 덮어쓰지 않는다.
4. DB의 start_chat_stage로 새 실행권을 얻은 요청만 새 request_id로 GPU를 한 번 호출한다. 이전 GPU 계산이 남아 있고 처리 용량이 부족하면 429를 반환할 수 있다. 여유 용량이 있으면 새 시도가 함께 실행될 수 있지만 이전 시도의 저장 권한은 되살아나지 않는다. 자동으로 기다리거나 다시 보내지 않는다.
5. 늦게 도착한 이전 결과는 이전 시도 번호로만 저장을 시도한다. 현재 시도와 다르면 거부한다. AI 응답 ID도 원 호출과 대조한다.

전송 타임아웃·연결 끊김은 결과 UNKNOWN이며 AI가 반환한 504로 바꾸지 않는다. 사용자 수동 재시도는 허용하되 영속 중복 실행 방지·결과 재조회·정확히 한 번 생성은 보장하지 않는다. 응답을 잃은 이전 추론이 이미 끝났다면 수동 재시도로 다시 생성할 수 있다. 동일 턴에 다른 질문·오디오를 보내면 기존 TURN_ID_CONFLICT 규칙을 따른다.

명세 분석은 대화 turn_id가 없는 별도 작업이다. 백엔드는 논리적 analysis_operation_id, 재시도 번호 analysis_attempt, 각 노드의 AI request_id 대응을 유지한다. 재시도는 해당 작업 전체의 새 analysis_attempt로 시작하고 잎·통합 결과를 새로 구성한다. 과거 시도의 노드를 재사용하지 않는다. 같은 확정 자료에 대한 수동 재시도는 같은 analysis_operation_id와 증가한 analysis_attempt를 사용한다. 자료가 바뀌거나 새 분석을 시작하면 새 analysis_operation_id를 만들고 이전 작업의 게시 권한을 폐기한다. 이 두 값은 백엔드 내부 메타데이터이며 GPU 요청 필드에 추가하지 않는다.

페르소나별 현재 분석 작업·시도를 백엔드·DB가 원자적으로 등록한다. 프로필 게시 시 현재 analysis_operation_id·analysis_attempt인지, source_revision·source_snapshot_id·전체 출처와 루트의 전체 분할 범위가 유효한지를 실제 프로필 저장과 같은 원자적 처리에서 확인한다. 사전 조회만 하고 별도로 publish_style_profile을 호출하는 방식은 이 조건을 충족하지 않는다. 같은 작업·시도의 재게시로 새 프로필 버전을 만들지 않고 이미 게시된 결과를 확인한다. 새 시도 등록 뒤 도착한 이전 결과는 새 결과 게시 전후 모두 게시할 수 없다.

기존 publish_style_profile 인자에는 분석 작업·시도 가드가 없다. 위 원자적 게시와 작업·요청 대응의 영속화는 백엔드·DB 담당자에게 필요한 연결 조건이며 아직 구현되거나 합의된 함수가 아니다. 이 연결이 준비되기 전에는 고도화 명세 갱신의 안전한 수동 재시도를 구현 완료로 처리하지 않는다. source_revision은 자료 버전이며 시도 번호를 대신하지 않는다.

AI 분석 응답 수신, 프로필 게시 성공, 게시 결과 불명은 구분한다. 게시 결과가 불명이면 작업·시도에 연결된 저장 결과를 조회하며 확인 없이 반영 완료로 표시하거나 자동 재게시하지 않는다. 자료 버전이 바뀐 결과를 최신 버전으로 바꿔 발행하지 않는다.

사용자가 재시도를 선택해도 기존 게시 결과가 불명이면 먼저 해당 작업·시도의 저장 결과를 확인한다. 게시 성공이 확인되면 저장된 결과를 반환하고 재추론하지 않는다. 조회로도 결과가 확인되지 않으면 재시도 요청을 받았다는 이유만으로 새 분석 시도를 등록하지 않고 확인 대기로 남긴다. 기존 시도의 미게시·실패가 확인된 뒤에만 새 시도의 전체 분석을 시작한다. 각 노드 발송 전에도 현재 작업·시도가 활성 상태이고 권한·자료 버전이 유효한지 확인하며, 폐기된 시도는 새 노드를 발송하지 않는다.

합성 수락 사례는 다음과 같다.

| 조건 | 기대 결과 |
|---|---|
| 같은 자료 버전·출처의 분석 A가 타임아웃, 수동 재시도 B를 현재 시도로 등록, 그 뒤 A 도착 | B 게시 전후 모두 A 게시 거부. B 이후 A로 새 프로필 버전을 만들지 않음 |
| 분석 응답은 받았지만 게시 결과 불명 | 저장 결과 확인 전 반영 완료 표시 없음, 자동 재게시 없음 |
| history=[], evidence=[], 질문·명세·출력 예산만으로 문맥 초과 | 422로 종료, 추가 AI 호출 없음, 필수 입력 변경 없음 |
| 사용자 요청으로 근거를 줄여 다시 호출했으나 여전히 문맥 초과 | 새 request_id로 시도한 뒤 422 종료, 자동 반복 없음 |

이 규칙은 #15의 UNKNOWN 이후 재전송 금지를 사용자 수동 재시도에 한해 변경하는 합의안이다. 앱·DB 초안의 실패 단계 재실행 규칙과 함께 합의해야 하며, 아직 해당 처리가 구현됐다는 뜻은 아니다.

## 호출 예

상세 단계별 호출은 [호출 흐름](api-call-flow.md)에 정리했다. 실제 URL과 인증은 합의 후 설정한다. 다음 명령은 문서 예시이며 여기서 실행하지 않았다. examples 폴더의 JSON을 그대로 사용한다. curl에 --retry를 추가하지 않는다.

```bash
curl --request POST "${AI_BASE_URL}/v1/baseline/generate" \
  --header 'Content-Type: application/json' \
  --header "${AI_AUTH_HEADER}" \
  --data-binary @examples/01-baseline-request.json

curl --request POST "${AI_BASE_URL}/v1/persona-profiles/analyze" \
  --header 'Content-Type: application/json' \
  --header "${AI_AUTH_HEADER}" \
  --data-binary @examples/02-analyze-request.json

curl --request POST "${AI_BASE_URL}/v1/persona-profiles/merge" \
  --header 'Content-Type: application/json' \
  --header "${AI_AUTH_HEADER}" \
  --data-binary @examples/09-merge-request.json

curl --request POST "${AI_BASE_URL}/v1/persona/generate" \
  --header 'Content-Type: application/json' \
  --header "${AI_AUTH_HEADER}" \
  --data-binary @examples/03-persona-request.json
```

운영 전에는 서비스 인증 방식, 암호화된 연결, 허용된 호출자, 백엔드 전체 대기시간과 GPU 추론 마감, 모델·tokenizer 한도, 프로필 발행·삭제 경쟁 상황의 DB 연결 처리를 담당자와 확정한다. 요청마다 접근 권한을 확인하고 GPU에는 검증한 자료만 보낸다.

## 합의와 구현 순서

이번 합의 범위는 네 호출의 경로·입출력·오류·제한·재시도 및 담당 경계다. 구현 순서는 baseline 기본 추론 API와 백엔드 연결 → 페르소나 명세 분석·저장·적용 → RAG와 개인화 대화다. #43·#44의 기존 baseline 구현 범위를 자동으로 넓히지 않는다.

전체 확정 기록 재생성·토큰 한도 내 분할 통합·설정 용량에 따른 동시 처리는 PM이 선택한 방향이다. 고도화 analyze 및 persona 요청은 이전 초안과 필드가 달라지며 merge가 추가된다. 배포된 API의 변경이 아니며 baseline wire schema는 유지한다. 교차 담당자 합의 전 초안이다. 현재 검토 상태는 review-status.md에서 확인한다. 앱·DB 상태 API, STT·TTS, 무제한 자료 처리, 영속 GPU 작업 조회·취소·중복 제거는 구현 범위에 포함하지 않는다.
