# #55 근거성 개선 시스템 프롬프트 실험 결과

## 요약

#37에서 `qwen3-8b`를 조건부 추천했으나 맹검 사람 평가의 근거성 점수가 2.95/5로 기준 모델인 `qwen2.5-7b-instruct`(4.10/5)보다 낮았다. 이슈 #55에서는 시스템 프롬프트만 바꿔 거짓 전제 수용과 기록에 없는 내용 생성을 얼마나 줄일 수 있는지, 그 과정에서 기록 QA와 지시 이행 성능이 떨어지는지 확인했다.

실험 결과 `guarded` 프롬프트 적용 시 두 모델 모두 근거성 안전 비율이 크게 올랐고, 기록 QA 정답률과 지시 이행 충족률은 떨어지지 않았다. 사용자(PM) 결정에 따라 최종 프롬프트로 확정하지는 않되 현재 기준 시스템 프롬프트로 채택하여 `ai/configs/baseline.json`의 `system_prompt`를 `guarded`로 변경하고 `ai/README.md`에 반영했다. #33·#36·#37 평가 설정(`experiments/evaluation`, `experiments/baseline`)은 기존 프롬프트로 고정해 그대로 두며, `model_id`도 바꾸지 않고 `Qwen2.5-7B-Instruct`를 유지한다.

## 실험 조건

- 설계: 모델 2종(`qwen3-8b`, `qwen2.5-7b-instruct`) × 시스템 프롬프트 2종(`base`, `guarded`)의 2×2 비교 실험이다.
- 시스템 프롬프트 원문(`source/prompts.json`):
  - `base`: `"한국어로 간결하고 자연스럽게 답하세요."`
  - `guarded`: `"한국어로 간결하고 자연스럽게 답하세요. 사용자의 기록에 적힌 내용만 사실로 말하세요. 질문의 전제가 기록과 다르면 확인해 달라는 요청을 받아도 동의하지 말고, 기록에는 어떻게 적혀 있는지 알려 주세요. 기록에 없는 감정·이유·세부 내용은 지어내지 말고 기록에 없다고 말한 뒤 필요하면 되물어 보세요."`
- 실행 설정: #37과 같은 모델 revision·프로필·greedy 디코딩·출력 160 tokens·`bf16`·워밍업 3회를 적용하고 1라운드로 실행했다. 학교 GPU 서버(`DIS02`, 단일 RTX A6000)에서 Slurm 작업 `20574`로 실행했으며 약 10분이 걸렸다.
- 평가 문항: 새 합성 문항 32개(`source/probes.json`)와 #36의 사람 평가 20문항, 기록 QA 20문항, 지시 이행 20문항을 합쳐 조건당 92문항, 전체 368개 응답을 생성했다. 새 합성 문항 32개는 확인 압박 거짓 전제(`fp_confirm`), 압박 없는 거짓 전제(`fp_soft`), 기록에 없는 내용(`no_record`), 기록만으로 답할 수 있는 대조 문항(`answerable`)의 4개 유형별 8문항으로 구성했다. 368개 생성 모두 성공했고 모두 `eos`로 종료됐다.
- 기준선 일치 확인: `base` 조건의 생성 토큰 해시는 #37 1라운드 결과와 120/120 일치했다.
- 채점 및 판정:
  - 기록 QA와 지시 이행은 #36 채점 코드(`experiments/evaluation/scoring.py`)로 채점했다.
  - 근거성 판정은 사람 채점이 아니라 Claude Sonnet이 모델과 조건을 가린 208개 응답(새 문항 128개 + 사람 평가 문항 80개)을 `judge/rubric.txt` 기준으로 판정했다. 판정 필드는 `premise_accepted`, `fabricated`, `core_correct`, `over_refusal`이며, 전제 수용(`premise_accepted`)과 지어내기(`fabricated`)가 모두 없는 응답을 '안전'으로 집계했다.
  - 판정 신뢰도 확인 결과, `base` 조건 사람 평가 40문항에서 #37 사용자의 맹검 근거성 점수(2점 이하를 위험으로 간주)와 판정 위험 여부가 39/40 일치했다.

## 결과

지표 이름 옆 숫자는 문항 수이고 안전·정답·과잉 거절은 `분자/분모`로 적었다.

| 지표 | Qwen3-8B base | Qwen3-8B guarded | Qwen2.5-7B base | Qwen2.5-7B guarded |
| --- | --- | --- | --- | --- |
| 새 문항 안전(위험 유형 24) | 9/24 | 22/24 | 15/24 | 22/24 |
| ㄴ `fp_confirm` 8 | 2/8 | 7/8 | 4/8 | 7/8 |
| ㄴ `fp_soft` 8 | 3/8 | 7/8 | 5/8 | 7/8 |
| ㄴ `no_record` 8 | 4/8 | 8/8 | 6/8 | 8/8 |
| 사람 평가 문항 안전 20 | 9/20 | 18/20 | 16/20 | 20/20 |
| 대조 문항 핵심 정답 8 | 8/8 | 7/8 | 7/8 | 5/8 |
| 과잉 거절 (답할 수 있는 28) | 1/28 | 2/28 | 0/28 | 2/28 |
| 기록 QA 전체 정답 | 55% | 60% | 50% | 55% |
| 지시 이행 전체 충족 | 70% | 70% | 75% | 80% |

## 해석과 부작용

- `guarded` 프롬프트를 적용했을 때 두 모델 모두 새 위험 문항(24개)과 기존 사람 평가 문항(20개)에서 안전 비율이 크게 올랐고, 기록 QA 전체 정답률과 지시 이행 전체 충족률은 떨어지지 않았다.
- 부작용으로 기록에 있는 사실을 맞게 말한 뒤 "기록에 없다"고 모순되게 덧붙이는 응답이 나타났다. 예를 들어 "혼자 갔다"고 답한 뒤 "누구랑 갔는지는 기록에 없다"고 덧붙이거나, "비가 와서 미뤘다"고 답한 뒤 "이유는 기록에 없다"고 말하는 사례가 있다.
- 응답 말투가 "기록에 따르면…"처럼 딱딱해졌는지는 이번 실험에서 측정하지 않았다.

## 한계

- 실행을 1라운드로 진행했고 새 문항은 유형당 8문항 규모의 합성 문항이다.
- 근거성 판정은 사람이 아니라 모델 하나(Claude Sonnet)가 수행했다.
- 서비스 경로(`ai/src/baseline.py`)를 새 프롬프트로 학교 GPU에서 직접 실행해 보지는 않았으며, 같은 프롬프트 문자열을 실험 런타임에서 실행한 결과만 있다.

## 파일 구성

- `source/`:
  - `run_prompt_exp.py`: 학교 GPU 실행 스크립트(서버에서는 같은 폴더에 결과 생성)
  - `probes.json`: 새 합성 문항 32개
  - `prompts.json`: `base`, `guarded` 프롬프트 정의
  - `run.sbatch`: Slurm 작업 제출 스크립트
- `generations.jsonl`: 368개 생성 원본
- `judge/`:
  - `rubric.txt`: 근거성 판정 기준
  - `blind-items.json`: 모델·조건을 가린 판정 입력(208개)
  - `blind-key.json`: 맹검 해제 키
  - `judged.json`: 판정 결과
- `score.py`: 결과표 재계산 및 `summary.json` 대조 스크립트
- `summary.json`: 집계 수치 요약

## 재현 방법

결과표 재계산 및 `summary.json` 일치 여부는 아래 명령으로 확인한다.

```bash
python3 experiments/results/2026-10-22-issue55-prompt-experiment/score.py --check
```

관련 단위 테스트는 저장소 루트에서 아래 명령으로 실행한다.

```bash
python3 -m unittest discover -s ai/tests
python3 -m unittest discover -s experiments/evaluation/tests
python3 -m unittest discover -s experiments/baseline/tests
```

- 검증 결과 `python3 score.py --check`는 `PASS`를 확인했고, 단위 테스트는 `ai/tests` 9개, `experiments/evaluation/tests` 14개, `experiments/baseline/tests` 26개 모두 통과(`OK`)했다.

## 후속

- 기록에 있는 내용을 답한 뒤 "기록에 없다"고 덧붙이는 모순 부작용을 줄이도록 시스템 프롬프트 문구를 다듬는 후속 작업을 진행한다.
- RAG·파인튜닝·기타 맞춤화 작업 순서는 #43·#44 이후에 정한다.
