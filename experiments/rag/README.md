# Replica #59 오프라인 RAG 검색 실험

수작업으로 다시 쓴 한국어 합성 기록에서 관련 근거를 찾는 실험이다. 코퍼스는 `v2-natural`이며 문장 템플릿 생성은 쓰지 않는다. 청킹, 페르소나별 검색, DEV 임계값 보정과 TEST 평가까지 다룬다. LLM 답변 생성과 서비스 연동은 후속 단계다. 실인물의 기록이나 AI Hub·NIKL 원문은 사용하지 않았다. 이전 코퍼스와 결과는 폐기 대상으로 `.cache/superseded-v1/`에 분리했으며 현재 결과에 포함하지 않는다.

## 구성

| 파일 | 용도 |
| --- | --- |
| `corpus.json` | 직접 쓴 기록·질문과 검색 전에 고정한 gold |
| `corpus.py` | 무결성·지정 조사 검사, 화자 매핑, 결정적 청킹 |
| `retrieval.py` | NumPy 정확 코사인 검색, 지표, DEV 보정 |
| `run.py` | 두 인코더 실행과 JSON 결과 저장 |
| `audit_results.py` | 소스 hash·범위·지표 재계산·근거 출력 감사 |
| `test_rag.py` | 청킹·gold·조사·검색·지표 회귀 검사 |
| `model-lock.json` | 인코더 commit hash·접두사·차원 |
| `requirements.txt`, `requirements-lock.txt` | 직접 의존성과 실제 설치한 전체 버전 |
| `results/` | 모델별 JSON, `summary.md`, `verification.txt`, 원문 표본 `samples.md` |

가중치와 캐시는 `.cache/`, 가상환경은 `.venv/`에 두며 Git에서 제외한다.

## 데이터와 화자

5명은 17세 동생, 29세 친구, 56세 엄마, 38세 선배, 74세 할아버지다. 일기 50개와 짧은 대화 50개에서 청크 400개를 만든다. 사건군은 인물당 10개, 총 50개이며 인물별로 별개다. 같은 사건과 비슷한 재방문은 같은 사건군으로 묶어 DEV와 TEST에 나뉘지 않게 했다.

| 분할 | 사건군 | 기록 | 청크 | 단일 | 다중 | 무근거 | 거짓 전제 | 모호성 | 일상 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| DEV | 25 | 50 | 200 | 10 | 10 | 5 | 5 | 5 | 5 |
| TEST | 25 | 50 | 200 | 10 | 10 | 5 | 5 | 5 | 5 |

질문은 총 80개다. 날짜 숫자를 그대로 복사한 질문은 0개다. 상대적 지칭과 바꿔 말하기를 쓰고, 다중 근거 질문은 다른 기록·세션의 사실을 결합한다. 모호성은 서로 다른 두 사건이나 대상의 근거를, 거짓 전제는 전제를 바로잡는 근거를 gold로 둔다. 무근거와 일상 대화의 gold는 비어 있다. 정답·정확한 인용구·gold ID는 검색 전에 고정했고, 새 TEST 결과를 본 뒤 수정하지 않았다.

대화의 `나`는 정보 제공자(`provider`), 선택한 상대는 `persona`, 나머지는 `other`다. 사실 질문의 gold는 페르소나 발화에 연결한다. 다른 화자의 이야기도 검색 후보에는 들어가지만 페르소나 자신의 사실로 취급하면 안 된다. 일기 작성자는 기록 metadata의 화자다.

일기는 빈 줄 단락 단위, 대화는 같은 화자의 연속 발화를 최대 2턴까지 묶는다. 기록·버전·날짜·화자 경계를 넘지 않고 중첩이나 잘림을 두지 않는다. `chunk_id`는 text version과 순번으로 만든 UUID다. 화자·역할·날짜·기록·버전·원본 턴 번호는 별도로 보존한다. 장문 토큰 분할 전체는 구현하지 않았다.

조사 검사는 알려진 화자 이름과 지정 명사 뒤의 `와/과`, `을/를`, `은/는`, `이/가` 불일치를 잡는다. 이름 뒤 별칭 어미나 서술격 표현을 오탐하지 않도록 경계를 제한한다. 전체 한국어 형태소·문법 검사나 자동 교정은 아니다.

## 검색과 지표

두 분할 모두 같은 전체 아카이브를 검색하되 페르소나 필터를 순위 계산 전에 적용한다. L2 정규화한 `float32` 벡터의 NumPy 내적으로 비교하고 동점은 `chunk_id` 문자열 순서로 정렬한다. KURE는 접두사 없이, E5는 `query: `와 `passage: `를 붙인다. 입력 한도는 512 토큰이며 실제 최대는 KURE 94, E5 96이었다. gold·정답·family·분할 이름은 인코더 입력에 넣지 않는다.

Recall@1/3/5는 모든 gold를 찾은 `recall_all`과 하나라도 찾은 `recall_any`를 나눈다. 사실 근거가 있는 질문 30개씩이 분모다. `multi_chunk` 지표는 다중 질문 10개와 모호성 5개를 합친 15개가 분모이며, 유형별 지표도 별도로 저장한다. MRR은 페르소나의 전체 순위에서 첫 gold의 역순위를 평균한다.

무근거 탐지의 양성은 `unanswerable`이다. 일상 대화 5개를 뺀 DEV 35개에서 F1을 최대화하고, 동점이면 false-empty가 적은 값, 다시 동점이면 낮은 임계값을 고른다. 최상위 점수가 임계값 미만일 때만 무근거로 판정한다. TEST 검색 전에 `calibration.json`을 저장하며 TEST로 재보정하지 않는다. 일상 대화의 empty 비율은 별도로 기록한다.

각 모델의 `traces.json`은 질문·gold·전체 순위·top-5 ID와 점수를 담는다. `evidence.json`의 `topk_evidence`는 임계값 적용 전 상위 5개, `evidence`는 그중 임계값 이상인 항목이다. 각 근거 객체는 `chunk_id`, `record_id`, `text_version_id`, `content`, `speaker_role` 5개 필드만 담는다. `persona{display_name,provider_address}`도 함께 내보낸다. 후속 실험 입력이며 완전한 서비스 API 요청은 아니다.

## 실행

작업 디렉터리에서 아래 명령을 실제 실행했다. 환경은 Mac Apple M5, Python 3.12.15, MPS다.

```bash
cd /Users/admin/Project/Replica/.local-work/worktrees/issue59/experiments/rag
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip freeze --python .venv/bin/python
.venv/bin/python -m pytest -q test_rag.py
.venv/bin/python corpus.py
export HF_HOME="$PWD/.cache/huggingface" HF_HUB_DISABLE_TELEMETRY=1 TOKENIZERS_PARALLELISM=false
.venv/bin/python run.py kure --device mps --batch-size 8
.venv/bin/python run.py e5 --device mps --batch-size 8
.venv/bin/python audit_results.py kure
.venv/bin/python audit_results.py e5
```

초기 설치의 가상환경과 모델 캐시를 그대로 재사용해 새 코퍼스를 실행했다. 같은 전체 패키지 버전으로 새로 설치하려면 `requirements-lock.txt`를 쓴다. 전체 lock 설치 명령은 별도로 실행하지 않았다. 모델 키는 위치 인자다. 기존 결과 디렉터리는 덮어쓰지 않는다.

## 검증과 한계

pytest는 `37 passed, 2 warnings in 0.29s`였다. 경고는 외부 Typer/Click의 deprecation 경고 2개다. validator는 `VALID: personas=5 records=100 chunks=400 questions=80`으로 끝났다. 두 모델의 감사도 소스 hash, DEV 임계값 재현, 범위 격리, 지표 재계산과 근거 형식을 통과했다. 자세한 출력은 `results/verification.txt`에 있다. LSP는 `basedpyright` 미설치로 실행하지 못했다.

실측 표는 `results/summary.md`에 있다. TEST all-gold Recall@5는 KURE 80.0%, E5 53.3%이고 무근거 오수용은 각각 3/5, 2/5였다. 단일 관련도 임계값을 신뢰할 만한 사실 부재 판정기로 볼 수 없다. manifest에는 패키지 버전·모델 revision·데이터와 코드 SHA-256·DEV 보정 파일 SHA-256·Git HEAD를 담았다. 미커밋 코드이므로 HEAD만으로 소스를 식별하면 안 된다.

정확한 인용구 대응은 자동 의미 함의 판정이 아니다. 작성자가 기록과 gold를 대조했지만 독립적인 사람의 자연스러움·정답 검수는 아직 없다. 작은 합성 표본이며 분할별 무근거 질문은 5개뿐이다. 생성·LLM judge·학교 GPU·DB/API 연동·실제 접근 제어·민감정보 마스킹 탐지·Qwen 예산은 검증하지 않았다. 시간은 캐시를 재사용한 1회 관측치이며 워밍업과 peak RSS 측정은 없다.

## 답변 생성 비교 준비

`generate.py`는 80문항에 A(무근거), B(KURE 검색 근거), C(gold 근거)를 적용해 총 240개 입력을 만든다. A에는 빈 배열을 넣는다. B는 `results/kure/evidence.json`의 DEV 임계값 적용 후 `evidence`만 쓰며, 임계값 적용 전 `topk_evidence`로 대체하지 않는다. C에는 해당 문항의 gold 청크만 gold 순서대로 넣는다. 근거의 ID·본문·화자 역할과 페르소나 범위를 원본 코퍼스에 대조한다.

세 조건의 시스템 메시지와 질문은 같고 `<evidence>` 블록 안의 JSON만 다르다. 근거 객체에는 `chunk_id`, `record_id`, `text_version_id`, `content`, `speaker_role`만 넣는다. 현재 `ai/configs/baseline.json`의 guarded 프롬프트에 코퍼스의 이름·관계·말높임·정보 제공자 호칭을 덧붙인다. 해요체와 하게체도 원래 metadata대로 유지한다.

```bash
cd /Users/admin/Project/Replica/.local-work/worktrees/issue59/experiments/rag
.venv/bin/python -m pytest -q test_rag.py test_generate.py
.venv/bin/python generate.py --dry-run --output .cache/step2-dry-run
```

CPU dry run은 모델·torch·transformers를 불러오지 않고 `prompts.jsonl` 240행을 쓴다. 조건 A/B/C 순서 안에서 `question_id`로 정렬한다. 출력 디렉터리는 새로 만들어야 하며 기존 결과를 덮어쓰지 않는다.

GPU 생성은 #58의 `experiments/baseline/runtime.py`를 그대로 사용한다. `environment.json`의 #58 설정을 읽어 Qwen3-8B revision `b968826d9c46dd6066d109eabc6255188de91218`, greedy(`do_sample=False`), 최대 160 tokens, bf16, eager attention, `enable_thinking=False`를 적용한다. #58은 seed와 temperature를 따로 지정하지 않았으므로 여기서도 추가하지 않는다. 실제 반복은 1회이며 조건마다 첫 예정 입력으로 3회 워밍업한다.

생성 결과는 `generations.jsonl`에 질문·조건·페르소나·분할·유형·전달한 근거 ID, `raw_text`, 최종 `text`, 생성 시간·호출 시간·토큰 수·종료 사유·토큰 해시·상태를 저장한다. 후처리를 하지 않아 성공한 응답의 `raw_text`와 `text`는 같다. #58 runner에 없던 `evidence_used`는 추가하지 않는다. `environment.json`에는 Runtime이 관측한 패키지·GPU·모델 revision과 입력·실행 코드 hash, 프롬프트 hash를 남긴다. 모델 revision은 #58과 마찬가지로 profile의 선언값이며 원격 가중치를 새로 검증한 결과는 아니다.

`run-generate.sbatch`는 #58 r7의 p02/DIS02, GPU 1개, 07:30:00 배정을 따른다. #58에 QoS 지정은 없어 추가하지 않았다. `REPLICA_BASE`, `REPLICA_VENV`, `REPLICA_MODEL`, `REPLICA_OUTPUT`으로 경로를 바꿀 수 있다. 기본값은 과거 실행 자료의 `/abr/coss40/prompt-exp-002`, `/abr/coss40/replica-llm-venv`, `/abr/coss40/replica-models/Qwen3-8B`다. 로그는 제출 디렉터리의 `slurm-rag59-%j.out`에 쓴다. 부모가 파일과 경로를 검토한 뒤 제출하며, 이 작업에서는 제출하지 않았다.

원격에 해당 checkout과 #59 파일이 있는지, 모델 경로가 유효한지, venv에 `pydantic`·`typer`를 포함한 필요한 의존성이 있는지는 확인하지 않았다. 전체 저장소의 `ai/configs/baseline.json`, #58 결과 `environment.json`, `experiments/baseline/`도 필요하다. 새 가상환경을 만들거나 원격 의존성을 자동 설치하지 않는다.

## 맹검 판정 입력 준비

생성이 끝난 뒤 `build_blind.py`로 입력을 준비한다. 아래 명령은 사용 절차이며 이번 작업에서 실제 생성물이나 외부 judge를 실행하지 않았다.

```bash
.venv/bin/python build_blind.py --generations PATH/generations.jsonl --output .cache/blind-001
```

누락·중복·실패·다른 근거 ID가 있는 생성물은 거부한다. 문맥에는 질문과 같은 사건군의 해당 페르소나 기록, 그리고 gold가 참조하는 기록 전체를 넣는다. 원래 화자·역할·날짜와 청크 ID를 보존하고 A/B/C에 같은 문맥을 쓴다. gold는 support의 정확한 인용구·핵심 답과 `expected_answer`로 만든다. `ambiguity`는 기존 judge의 `answerable_cross_session`에 대응시키고 문항의 `note`를 판정 기준으로 보존한다.

seed 59로 섞은 `blind-items.json`에는 opaque bid와 기록·질문·정답 정보·응답만 남긴다. 조건·질문 ID·페르소나 ID·전달한 근거 ID는 `blind-key-DO-NOT-OPEN.json`에 분리한다. 응답 SHA-256으로 키와 입력을 연결한다. `profiles.json`은 bid별 말높임 목표이며 `preparation.json`에는 입력 hash와 준비 단계의 제약을 쓴다.

기존 judge는 `/Users/admin/Project/Replica/.local-work/aihub/eval-71630/judge-candidates/hybrid_judge.py`에 그대로 둔다. 복사하거나 실행하지 않는다. 현재 지원하는 목표는 `calm`(반말)과 `elder`(해요체)뿐이어서 p5의 하게체 48개 입력은 `unknown`으로 표시한다. 기존 live judge는 `unknown`을 거부한다. 하게체를 해요체로 바꿔 채점해서는 안 된다. 또한 기존 Terra rubric에는 71630·가족·반말이라는 전제가 남아 있다. 전체 240개를 판정하려면 부모가 `.local-work`에서 프로필 지원과 rubric을 합성 코퍼스·관계·말높임에 맞춰 먼저 조정해야 한다. 이 준비 스크립트는 그 변경을 대신하지 않는다.

## 2단계 로컬 검증

로컬 pytest는 `54 passed, 2 warnings in 0.99s`였다. 경고 2개는 기존 Typer/Click deprecation이다. CPU dry run은 `DRY_RUN_DONE prompts=240 conditions={'A': 80, 'B': 80, 'C': 80} model_loaded=False`로 끝났다. `bash -n run-generate.sbatch`와 새 Python 파일의 `py_compile`도 통과했다.

테스트는 조건별 입력 동일성, 임계값 미만·동일·초과 경계, 저장된 KURE 결과와 임계값 재적용 결과 일치, 정확한 gold, 페르소나 격리, 240행·결정적 순서, 맹검 키·문맥 대응과 잘못된 생성물 거부를 검사한다. Runtime 대역을 사용한 생성 파일·환경 파일 검사는 GPU 실측이 아니다. LSP는 `basedpyright` 미설치로 실행하지 못했다. GPU 생성, 원격 경로·패키지, 근거성·말투 품질, 실제 hybrid judge 판정은 아직 검증하지 않았다.
