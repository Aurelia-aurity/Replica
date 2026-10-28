# 현 LLM baseline 평가

[Issue #33](https://github.com/Aurelia-aurity/Replica/issues/33)의 실행 도구입니다.
Qwen2.5-7B-Instruct를 현재 조건으로 측정한 뒤 모델별 비교의 기준으로 사용합니다.
한국어 자연스러움·지시 준수·일반 대화의 유용성·개인 사실의 근거성을 각 5문항씩 평가합니다.
`cases.json`의 20개 합성 질문과 rubric은 사용자 확정 버전 1.0입니다.
I05는 복합 지시, G05는 제공된 기록과 다른 기억에 동조하는지 확인하는 문항입니다.

## 학교 GPU 실행

기존 승인된 Slurm GPU 작업 셸에서 Python 3.12 가상환경과 로컬 모델을 준비합니다.
관측된 환경은 Python 3.12.7 / RTX A6000 48GB입니다. 실행기는 CUDA0와 네이티브 BF16을 검사합니다.
기존 baseline의 패키지 조합은 torch 2.7.1+cu118, transformers 4.57.1,
accelerate 1.15.0, safetensors 0.8.0입니다. 이 도구는 설치·다운로드·SSH를 수행하지 않습니다.

저장소 루트에서 실행합니다. `REPLICA_MODEL_PATH`는 학교 서버의 기존 로컬 모델 경로를 사용합니다.
출력 경로는 매번 **존재하지 않는 새 디렉터리**를 지정합니다.

```bash
python experiments/baseline/run.py --model-path "$REPLICA_MODEL_PATH" --output /tmp/replica-baseline-run-001
python experiments/baseline/report.py --run /tmp/replica-baseline-run-001 --prepare-scores --output /tmp/replica-baseline-scoring-001
```

모델은 한 번 적재합니다. 별도 질문으로 워밍업 3회 후, 고정된 문항 순서로 3라운드(60회)를 실행합니다.
시스템 프롬프트는 `한국어로 간결하고 자연스럽게 답하세요.`, greedy 생성과 160토큰 상한을 유지합니다.
각 호출은 새 system/user 메시지만 사용합니다. 대화 이력과 이전 호출의 KV cache를 전달하지 않습니다.
본 평가가 끝나면 EOS 없이 상한에 도달한 **각 응답**에 대해 320토큰 보충 실행을 한 번씩 수행합니다.
보충 결과도 상한에 도달할 수 있습니다. 본 평가 시간·점수에 보충 결과를 섞지 않습니다.

오류가 생긴 문항을 자동으로 재실행하지 않습니다. 입력 오류는 기록 후 다음 문항으로 진행합니다.
생성 오류는 GPU 상태를 한 번 검사합니다. OOM 복구 때만 allocator cache를 비우고 이후 결과를 따로 표시합니다.
워밍업 실패·치명적 CUDA 오류·기록 실패는 중단합니다. 하나라도 실패하면 종료 코드는 1입니다.
중단된 실행을 이어 쓰지 않습니다. 새 경로에서 새 실행을 시작합니다.

사용자는 `metadata.json`, `cases.json`, `events.jsonl` 세 파일을 함께 전달합니다.
원본 디렉터리에 채점 파일을 만들거나 원본을 수정하지 않습니다.
실행 중 종료되어도 성공·실패·시작 후 중단·미실행을 구분해 분석할 수 있습니다.
끝의 잘린 JSONL 줄만 손상으로 표시하며, 완전한 줄의 오류·순서 충돌·내용 hash 불일치는 거부합니다.
이벤트의 write·flush·fsync나 기록 상태 갱신 중 예외가 발생한 저널에는 종료 이벤트를 이어 붙이지 않습니다.
모든 슬롯 직후 중단되면 일정은 완료됐지만 실행 종료 상태는 interrupted일 수 있습니다.
종료 이벤트가 완전한 줄로 기록된 시점에 신호가 들어오면 원본에는 완료 결과가 남고 프로세스 종료 코드는 1일 수 있습니다.
리더는 실제 읽힌 원본을 검증합니다. 기록 실패나 중단 시 물리적 저장장치의 fsync 완료를 인증하지 않습니다.

## 사람 채점

`scoring-reference.json`에서 질문·기준·**첫 라운드 답변 그대로**를 보고 채점합니다.
빈 답변과 잘린 답변도 정상 생성되었다면 채점 대상입니다. 실패한 첫 라운드를 나중 답변으로 대체하지 않습니다.
`scores-template.json`의 해당 행에 `score`(정수 1~5), `reason`(한 줄 이유),
`fabricated_personal_fact`(true/false)를 기입하고 `graded_at`에 시간대 포함 ISO 날짜를 적습니다.
예: `2026-10-10T22:00:00+09:00`. 채점하지 않은 행은 세 필드를 모두 null로 둡니다.
완성한 파일은 `scores-completed.json` 같은 별도 파일로 저장합니다. 도구는 인간 점수를 추정하지 않습니다.

```bash
python experiments/baseline/report.py --run /tmp/replica-baseline-run-001 --scores /tmp/replica-baseline-scoring-001/scores-completed.json --output /tmp/replica-baseline-report-001
```

`summary.json`은 영역별 평균과 채점/대상/전체 문항 수, 네 영역의 동일 가중 평균을 제공합니다.
한 영역이라도 점수가 전혀 없으면 전체 평균은 null입니다. 20개 모두 채점되기 전에는 부분 결과입니다.
개인 사실 생성 여부와 이유는 사용자의 판단을 그대로 남깁니다. G05는 근거성 영역으로 평가합니다.

## 측정 범위와 재현성

`generation_ms`는 GPU 입력 준비 후 CUDA 동기화, 시작 시계, generate, 완료 동기화, 종료 시계 순서로 측정합니다.
토큰화·디코딩·파일 기록·모델 적재는 제외합니다. TTFT나 앱 응답 지연을 의미하지 않습니다.
load 시간은 tokenizer/model 적재·eval·동기화를 포함하며 Python import와 CUDA 초기화를 제외합니다.
생성 토큰 수에는 EOS가 포함됩니다. `generated_ids_excluding_terminal_eos`는 마지막 EOS만 뺀 ID 수입니다.
EOS가 마지막 허용 토큰에 나와도 EOS 종료로 기록합니다. EOS 없이 상한에 도달하면 token_limit입니다.
0초 측정의 처리량은 null입니다. 정상 생성된 빈 문자열·공백은 `output_empty=true`로 보존합니다.

메모리는 PyTorch allocator의 allocated/reserved 절대 peak(상주 모델 포함)와
시작 대비 allocated 증가량을 bytes로 기록합니다. 장치 전체 메모리 사용량은 아닙니다.
워밍업과 고정 순서가 reserved cache에 영향을 줍니다. 정상 실행 중에는 cache를 비우지 않습니다.
문항별 3회 원값·성공 결과의 중앙값/최솟값/최댓값과 표본 수를 제공합니다.
상한 종료·복구 이전/이후·워밍업·보충 실행은 각각 구분합니다. 세 번 반복으로 일반 성능 우위를 단정하지 않습니다.

dataset hash는 JSON 원본 UTF-8 bytes, rubric hash는 `json.dumps`의
`ensure_ascii=False, sort_keys=True, separators=(',', ':')` 결과 UTF-8 bytes(끝 개행 없음) 기준입니다.
원본 질문·rubric 또는 확정 선언이 달라지면 실행을 거부합니다.
실행 ID·dataset/config/code hash·정확한 출력 문자열 hash로 채점과 원본을 연결합니다.
hash는 내용 연결 근거이며 실행자나 채점자의 신원을 인증하지 않습니다.
분석·검증·hash는 파일별로 한 번 읽은 같은 bytes를 사용합니다. 분석 중 파일이 바뀌어도 서로 다른 버전을 연결하지 않습니다.
설정의 모델 ID와 baseline 참조 commit도 승인값과 일치해야 합니다. 이는 실제 가중치 정체성을 인증하는 검사는 아닙니다.
실행 코드의 Git commit과 모델 revision은 검증되지 않으면 null로 남깁니다.
메타데이터에 모델 경로·계정명·Slurm job ID·전체 환경변수·원시 오류 문자열을 기록하지 않습니다.

## 로컬 검사와 완료 조건

```bash
python3 -B -m unittest discover -s experiments/baseline/tests -v
```

로컬 검사는 가짜 CUDA·모델·시계를 사용하며 실제 GPU 성능을 측정하지 않습니다.
가짜 실행은 `execution_kind=mock`이고 GPU 사용자 실행은 `school_gpu_user_run`입니다.
로컬 구현 검증과 독립 리뷰가 끝나도 실제 학교 GPU 측정과 인간 채점이 들어오기 전에는 #33을 완료하지 않습니다.
다른 모델 비교와 파인튜닝은 이 기준값을 확인한 다음 단계입니다.

2026-10-10 실측·채점 결과는 [결과 기록](../results/2026-10-10-qwen2.5-7b-baseline/README.md)에 있습니다.
학교 GPU 본 평가 60회와 첫 라운드 사용자 채점 20문항을 완료했습니다.
이 결과는 평가 실행 완료의 근거이며 서비스 품질 합격이나 최종 모델 선정을 의미하지 않습니다.

구현은 #13 commit `705f2c2c9f9c0f426ac3376f9b9132021fb67139`의 적재·template·생성 정책을 참고한
실험 전용 resident adapter입니다. 공통 baseline 코드는 변경하지 않습니다. 향후 공통화는 별도 변경입니다.

측정 API 근거: [PyTorch synchronize](https://docs.pytorch.org/docs/2.7/generated/torch.cuda.synchronize.html),
[allocator peak](https://docs.pytorch.org/docs/2.7/generated/torch.cuda.max_memory_allocated.html),
[peak reset](https://docs.pytorch.org/docs/2.7/generated/torch.cuda.reset_peak_memory_stats.html),
[Transformers generation](https://huggingface.co/docs/transformers/v4.57.1/en/main_classes/text_generation).
