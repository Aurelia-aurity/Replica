# Phase A 평가 기준서 (`CRITERIA.md`)

## 1. 실험 범위 및 고정 조건
- 고정 원칙: 본 문서는 합성 데이터 생성, 모델 추론, 채점 전에 사전 고정한다. 확정된 `CRITERIA.md`의 SHA256 해시를 `PHASE_A.md`에 기록한다(문서 자신의 Git SHA가 아님). 이번 단계에서는 데이터 생성, GPU 추론, 원격 Judge 호출을 수행하지 않는다.
- 기반 및 디코딩 조건: `Qwen3-8B` + `r1` 프롬프트 + `retrieval v2`. 오프라인 `BF16`, greedy 디코딩(non-thinking), 최대 출력 `160`토큰을 고정한다. 사실 근거는 최대 `2,000`토큰으로 제한하며, 전체 입출력 길이는 모델 컨텍스트 한도 내에서 관리한다.

## 2. 평가 데이터셋 및 층화 구조
- 신규 합성 DEV: 총 `360`개 문항(서로 다른 사건군 `360`개). 같은 사건군을 말높임(register)만 바꿔 반복 생성하지 않는다.
  - 질의 유형(6종, 각 `60`문항):
    - 핵심 사실 질의(Core 4, 총 `240`문항): `single`(`60`), `multi`(`60`), `ambiguity`(`60`), `false_premise`(`60`)
    - 비사실·억제 질의(`120`문항): `unanswerable`(`60`), `ordinary_chat`(`60`)
  - 말높임(3종, 각 `120`문항): 반말(`120`), 해요체(`120`), 하게체(`120`). 6개 질의 유형 × 3개 말높임 조합당 정확히 `20`문항씩 배정한다.
- 단위 정의: 사건군당 1문항이므로 문항 단위 추론과 사건군 단위 추론은 동일하며, 항목별(per-item) 지표는 기술통계로만 보고하고 추론은 문항별 전체 항목 충족 여부로 수행한다.

## 3. 비교 조건 (B, C, S, P)
- B (기준선): `Qwen3-8B` + `r1` + 단일 질의 `retrieval v2` 사실 근거(최대 `2,000`토큰).
- C (근거 상한 진단): B와 동일 프롬프트·디코딩을 사용하되, 검색 결과 대신 문항에 정의된 필수 Gold 증거 묶음만 제공한다. 검색 실패와 생성 한계를 분리 진단하는 용도이며 서비스 채택 후보에서 제외한다.
- S (말투 참고 주입 후보): B와 동일한 사실 근거(`2,000`토큰)에 더해, 별도 말투 참고 JSON 블록(최대 `400`토큰)으로 화자 검증·말높임·대화 의도가 일치하는 당사자 발화 `2~4`개를 주입한다.
  - 문맥 유출 통제: 말투 예시 발화 자체에는 일상적 맥락이나 사건 정보가 포함될 수 있다. 말투 예시 안의 정보를 사실 근거로 승격하거나 답변에 새로운 사실로 사용하는 행위를 금지한다. 말투 예시에서만 유래한 내용을 사실처럼 단정하면 근거 날조(`fabricated`)로 판정한다.
- P (질의 분해 후보): 복합·모호 질의를 하위 검색 질의로 분해한 뒤 하위 항목별로 `retrieval v2`를 수행·병합하여 사실 근거 총량을 `2,000`토큰 이내로 맞춘다. 말투 참고 블록은 사용하지 않는다.

## 4. 사실 근거 상태 분류 (B 기준)
- 충분(Sufficient): 문항의 모든 필수 Gold 증거 ID가 B의 검색 결과에 포함되고, 요구되는 핵심 슬롯 표층형이 근거 본문에 모두 존재하는 상태.
- 부족(Insufficient): 필수 Gold 증거 ID 또는 핵심 슬롯 표층형 중 하나라도 누락된 상태. (`unanswerable` `60`문항은 사실 답변을 내릴 근거가 없으므로 정의상 '사실 답 근거 부족'에 해당하며, `ordinary_chat` `60`문항은 사실 근거 분류 대상이 아니므로 `N/A`로 처리한다.)
- 모순(Contradictory): 질문의 거짓 전제를 바로잡는 Gold 반박 근거가 B의 검색 결과에 충분히 존재하는 상태(검색된 근거들끼리 서로 충돌한다는 뜻이 아님).

## 5. 채점 체계 및 통계 검증
- 판정 파이프라인: 사실성·근거성(Grounding)은 기존 규칙 기반 검사 + `Jev` + `Terra` 앙상블을 그대로 사용하고, 말투·화법(Style)은 기존 `Terra` 채점기를 그대로 사용한다. 모든 비율 지표는 건수(`n/N`)와 양측 95% Clopper-Pearson(CP) 신뢰구간을 함께 보고하며, 분모가 `0`인 지표는 `N/A`로 표기한다.
- 비열등성(Non-Inferiority) 게이트: 후보 `2`종(S, P) × 지표 `4`종에 대해 Bonferroni 보정(`α = 0.05 / 8 = 0.00625`)을 적용한다. 문항 쌍에서 (기준선 B 성공 & 후보 실패) 불일치 비율 `p_10`의 단측 99.375% CP 상한 ≤ 마진(`δ`) 이어야 통과한다.
  1. 전체 Grounding(`no fabricated` AND `no unsupported_negation`, `360`문항): 마진 `3.0%p`
  2. 핵심 사실 정답률(`core_correct`)(`240`문항): 마진 `3.0%p`
  3. 안전한 답 보류 성공률(`safe_abstention`, `unanswerable`)(`60`문항): 마진 `5.0%p`
  4. 일상 대화 성공률(`chat_success`, `ordinary_chat`)(`60`문항): 마진 `5.0%p`
  - 표본 크기 한계 및 사후 보정 금지: 표본 `60`문항에서는 관측 불일치가 `0/60`이어도 단측 99.375% CP 상한이 `8.11%p`이므로 마진 `5.0%p` 이내임을 통계적으로 입증할 수 없다. 따라서 `unanswerable` 및 `ordinary_chat` 비열등성 게이트는 현 표본 크기에서 결론 불충분(inconclusive) 처리하며, 이를 통과시키기 위해 사후에 문항을 추가 생성하거나 유의수준·마진을 완화하는 보정을 금지한다. 결과적으로 Phase A 단독으로는 S와 P의 서비스 최종 채택이 불가하다.
- 우월성(Superiority) 검정: 단측 정확 McNemar 검정에 Holm 단계적 보정(총 `4`개 가설, 전체 `α = 0.05`)을 적용한다.
  - 후보 S: (1) 전체 `360`문항 목표 말높임 일치율 `+10.0%p` 이상 개선, (2) 사전 프로브로 고정된 자기높임 유발 `180`문항에서 당사자 자기높임 오류율 `-5.0%p` 이상 감소(단, 기준선 B의 오류 건수가 `0`이면 개선폭 판정 불가로 처리).
  - 후보 P: `multi` + `ambiguity` 합산 `120`문항에서 (3) 필수 Gold 근거 완전 회수율 `+10.0%p` 이상 개선, (4) 필수 슬롯 답변 누락률 `-10.0%p` 이상 감소.

## Execution contract

All applicable NI and superiority gates must pass; an inconclusive gate is not adoption. For S, both style endpoints are required; for P, both retrieval/omission endpoints are required. Holm covers all four endpoints. The 180 self-honorific probes are frozen as 30 per type (10 per register/type cell). No post-hoc endpoint/subset changes.

Retrieval v2 stays at threshold 0.5458711981773378, k=3, window=3, evidence JSON <=2000 tokenizer tokens. C is a diagnostic gold intervention, not a guaranteed correct answer. S uses B facts byte-for-byte; insufficient verified examples make the S arm ineligible, not a fallback. Wrong-speaker examples, promotion of style-only facts, or a token-cap breach invalidate the affected arm.

Before generation, freeze corpus, question-only item plans, utterance tags, model/tokenizer revisions, prompt bundles and code hashes. Plans are authored from questions only, without gold/slots/answers. Judge labels remain unchanged: grounding rules + Jev screen + Terra, style Terra. S/B and P/B compare identical question identities. Item recall means all predeclared support IDs/surfaces present AFTER context pruning; answer omission means any required item absent. A chunk hit alone does not prove item recall.
