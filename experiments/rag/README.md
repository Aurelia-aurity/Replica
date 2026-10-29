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

질문은 총 80개다. 날짜 숫자를 그대로 복사한 질문은 0개다. 상대적 지칭과 바꿔 말하기를 쓰고, 다중 근거 질문은 다른 기록·세션의 사실을 결합한다. 모호성은 서로 다른 두 사건이나 대상의 근거를, 거짓 전제는 전제를 바로잡는 근거를 gold로 둔다. 무근거와 일상 대화의 gold는 비어 있다. 최초 정답·인용구·gold ID는 검색 전에 고정했다. 이후 RCA에서 발견한 데이터 결함은 아래 `RCA corpus correction` 절에 따라 수정했으며, 기존 TEST 결과는 수정 전 코퍼스의 결과다.

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

### Prompt revision r2 (DEV-only)

`generate.py --prompt-version r1|r2|r3` selects the prompt revision; the default is
`r1`. Its rendered messages remain byte-identical to the frozen r1
`results/generation/prompts.jsonl`. Prompt and generation rows now include
`prompt_version`; older generation rows without the field load as r1.
`environment.json` also records the selected revision.

r2 replaces the baseline wording with memory-oriented conversation, explicit
speaker/event boundaries, partial answers, gentle false-premise correction,
in-character unknowns, and register examples including 하게체. A/B/C still differ
only inside the evidence block. Retrieval, decoding, the 160-token response cap,
and postprocessing are unchanged. Design used only DEV responses and labels;
no r2 quality result is claimed before GPU generation and judging.

```bash
.venv/bin/python -m pytest -q test_rag.py test_generate.py
.venv/bin/python generate.py --prompt-version r2 --dry-run --output .cache/r2-dry-run
```

For the parent's existing batch workflow, set `REPLICA_PROMPT_VERSION=r2`;
`run-generate.sbatch` forwards it to `--prompt-version` and defaults to r1 when
unset. Use a new `REPLICA_OUTPUT` directory to preserve previous runs. The worker
does not submit the batch job or invoke a judge.

Local r2 verification: `67 passed, 2 warnings in 1.70s` (existing Typer/Click
deprecations); `bash -n` and Python compilation passed. The r2 CLI dry run emitted
240 prompts (80 per condition) without loading a model. LSP remained unavailable
because `basedpyright-langserver` is not installed.

### Prompt revision r3 (minimal r1 change)

r3 keeps the r1 structure and user template. It replaces the conflicting
source-explanation/follow-up wording with gentle factual correction and a brief
in-character unknown, then adds short instructions for relevant evidence only,
usually 1–3 sentences, no closing questions/offers or source commentary, and the
persona's register in every sentence (including a compact 하게체 example), without
emoji. It does not reuse r2's rule blocks. Frozen r1 and r2 input artifacts are
checked byte-for-byte; A/B/C evidence selection and decoding are unchanged.
The supplied DEV regression analysis informed the change; no generation responses
or judge labels were opened for this revision.

```bash
.venv/bin/python -m pytest -q
.venv/bin/python generate.py --prompt-version r3 --dry-run \
  --output /Users/admin/Project/Replica/.local-work/rag/prompt-r3/dry-run
```

The existing batch wrapper also accepts `REPLICA_PROMPT_VERSION=r3`; choose a
fresh output directory. The fixed local `ACCEPTANCE_r3.md` governs later adoption:
one r3 generation run only, all nine criteria must pass, otherwise retain r1 and
stop tuning this round. CPU dry runs prepare 240 inputs, not quality evidence.
No GPU generation, batch submission, or judge execution is part of this change.

로컬 pytest는 `54 passed, 2 warnings in 0.99s`였다. 경고 2개는 기존 Typer/Click deprecation이다. CPU dry run은 `DRY_RUN_DONE prompts=240 conditions={'A': 80, 'B': 80, 'C': 80} model_loaded=False`로 끝났다. `bash -n run-generate.sbatch`와 새 Python 파일의 `py_compile`도 통과했다.

테스트는 조건별 입력 동일성, 임계값 미만·동일·초과 경계, 저장된 KURE 결과와 임계값 재적용 결과 일치, 정확한 gold, 페르소나 격리, 240행·결정적 순서, 맹검 키·문맥 대응과 잘못된 생성물 거부를 검사한다. Runtime 대역을 사용한 생성 파일·환경 파일 검사는 GPU 실측이 아니다. LSP는 `basedpyright` 미설치로 실행하지 못했다. GPU 생성, 원격 경로·패키지, 근거성·말투 품질, 실제 hybrid judge 판정은 아직 검증하지 않았다.

## RCA corpus correction (2026-10-29)

This revision corrects the frozen synthetic fixture after RCA sections 2 and 4.
The original retrieval/generation/judge artifacts remain historical, not results for
this revised corpus. Their source hashes, questions and q43 chunk references no
longer match; do not reuse them as corrected-corpus measurements or claim old
failures now pass. No encoder, model, judge, SSH or GPU call was made.

All 80 questions now declare `required_slots`: 60 factual questions have 186
short semantic targets; the 20 unanswerable/ordinary-chat questions have empty
lists. `slot_surface_forms` maps a canonical slot to explicitly reviewed lexical
alternatives (for example, `느슨한` -> `느슨했어`). The validator requires each
slot or an alternative to occur in the newline-concatenated exact `support.quote`
text, not just somewhere in a full record or even the full gold chunk. It rejects
missing/empty factual slots, nonempty nonfactual slots, duplicates, blank surfaces,
unknown surface keys and empty alternatives. The schema requires the slot list,
so old trace payloads without annotations are not current corpus inputs.
This is a deterministic coverage check, not an entailment or event-binding judge:
manual review still owns slot completeness, negation, speaker/event attribution,
and the meaning of listed alternatives. Question-given nouns need not be repeated
as independent answer slots (e.g. q34 asks which person and cake flavor). q55
retains its disambiguation instruction; only its factual names/venues are slots.

Decisions: q7 drops unasked date distinctions; q54 drops the unasked soil-state
reason. Their questions and gold IDs stay unchanged. q35 explicitly asks for the
adviser and container size/material; q36 explicitly asks for tool and soil state.
Both notes explain the change. Five ambiguity questions gain the precise
persona-spoken turn naming the second event/person. All other coverage repairs
only expand an exact quote within an already registered gold chunk.

q43 now explicitly recalls the 2025-08-15 visit in the 2025-08-19 chat: after
removing the stone, the persona boarded the bus. Only one persona turn changes.
The record ID stays `ed82baa3-6e3e-5802-b192-c1e39eba9051`. The revised text version
is UUIDv5(NAMESPACE_URL, `692ec1ca-7ed5-5fe3-a311-a2fba94829de:rca-event-identity-v1`)
= `575c58dc-8ef5-5877-9f20-398ed61bf505`. No original version-name generator is
present in this checkout; this explicit revision key makes the UUIDv5 update
reproducible. All six chunks are regenerated with the unchanged existing rule
UUIDv5(NAMESPACE_URL, `<text_version_id>:<1-based ordinal>`). The other 394 chunk
IDs, all 100 record IDs, all 80 question IDs, splits, families and question types
stay unchanged. The one referenced chat gold ID is updated below.

### Additional questions flagged by slot coverage

The initial check found 23 questions with incomplete support quotes. Six are
already named in the RCA repair request (q15/q23/q31/q35/q47/q71). The following
17 additional questions required quote expansion only; none needed a new gold
chunk or a changed question/expected answer. Lexical alternatives are shown in
`corpus.json`, not invented by the validator.

| Question | Slots absent from original support quotes |
| --- | --- |
| q3 | `느슨한`, `왼쪽 센서 선` |
| q6 | `도담`, `우산` |
| q22 | `수정` |
| q30 | `느슨한`, `단자` |
| q38 | `긴 반찬통`, `문을 밀고` |
| q41 | `앞치마` |
| q42 | `어릴 때 쓰던` |
| q44 | `다음` |
| q46 | `앞줄`, `골랐` |
| q50 | `표본 추출 과정` |
| q51 | `수정한 흐름` |
| q59 | `도착`, `시장` |
| q60 | `큰 사진` |
| q62 | `수첩`, `따로 뒀다` |
| q63 | `모아 작가`, `고운 작가` |
| q68 | `풀`, `종이` |
| q75 | `짧은 쪽`, `선반` |

### Verbatim before/after audit

Only question/answer/gold-support changes are expanded below. All 80 questions
receive slot metadata; the remaining 52 have no question/answer/support change.
Every quote is verbatim; multiple gold quotes are separated by `<br>`.

#### q2 (q-p1-school-stage-single-2)

| Field | Before | After |
| --- | --- | --- |
| question | `기타 연주할 때 곡을 막판에 바꿨다며. 결국 뭘 쳤어?` | `연주할 때 곡을 막판에 바꿨다며. 결국 뭘 쳤어?` |
| expected_answer | `봄소풍을 쳤다.` | `봄소풍을 쳤다.` |
| gold support | `e5f7ebd8-65f0-588f-8036-d93834a46b09`: `마지막 연습 때 봄소풍으로 바꿨어.` | `e5f7ebd8-65f0-588f-8036-d93834a46b09`: `마지막 연습 때 봄소풍으로 바꿨어.` |

#### q3 (q-p1-robot-club-multi-3)

| Field | Before | After |
| --- | --- | --- |
| question | `너희 만드는 차 팀 이름이랑, 자꾸 멈추던 걸 어떻게 고쳤는지 같이 알려줘.` | `너희 만드는 차 팀 이름이랑, 자꾸 멈추던 걸 어떻게 고쳤는지 같이 알려줘.` |
| expected_answer | `느린 번개 팀이고, 마루가 느슨한 왼쪽 센서 선을 다시 납땜했다.` | `느린 번개 팀이고, 마루가 느슨한 왼쪽 센서 선을 다시 납땜했다.` |
| gold support | `0bece01f-6e6b-5370-8dc6-64f8532e4019`: `팀 이름은 결국 느린 번개로 정했다.`<br>`79ad7b33-fd56-5828-9f30-5766b5b2f330`: `마루가 다시 납땜해 줬더니 됐어.` | `0bece01f-6e6b-5370-8dc6-64f8532e4019`: `팀 이름은 결국 느린 번개로 정했다.`<br>`79ad7b33-fd56-5828-9f30-5766b5b2f330`: `모터가 아니라 왼쪽 센서 선이 느슨했어. 마루가 다시 납땜해 줬더니 됐어.` |

#### q6 (q-p1-umbrella-false_premise-6)

| Field | Before | After |
| --- | --- | --- |
| question | `비 오던 날 정류장까지 혼자 뛰어갔다며?` | `비 오던 날 정류장까지 혼자 뛰어갔다며?` |
| expected_answer | `도담이와 우산을 같이 쓰고 갔다.` | `도담이와 우산을 같이 쓰고 갔다.` |
| gold support | `07531d23-ca1a-5c7c-8c9e-d3a337f8343e`: `정류장까지 둘이 갔다.` | `07531d23-ca1a-5c7c-8c9e-d3a337f8343e`: `도담이가 자기 우산을 같이 쓰자고 해서 정류장까지 둘이 갔다.` |

#### q7 (q-p1-study-spots-ambiguity-7)

| Field | Before | After |
| --- | --- | --- |
| question | `시험 앞두고 친구랑 먹을 데 앉아서 공부했잖아. 어디였고 누구랑 있었지?` | `시험 앞두고 친구랑 먹을 데 앉아서 공부했잖아. 어디였고 누구랑 있었지?` |
| expected_answer | `기말 전 토요일은 누리와 분식집, 시험 바로 전날은 보람이와 빵집 이층이다.` | `누리와는 동네 분식집에서, 보람이와는 빵집 이층에서 공부했다.` |
| gold support | `4f94044d-f042-51d8-9747-630ee753d7c8`: `누리랑 동네 분식집 구석에서 영어 단어를 외웠다.`<br>`79524406-881a-55c5-be26-43a5931cf161`: `보람이랑 새로 생긴 빵집 이층에서 봤어.` | `4f94044d-f042-51d8-9747-630ee753d7c8`: `누리랑 동네 분식집 구석에서 영어 단어를 외웠다.`<br>`79524406-881a-55c5-be26-43a5931cf161`: `보람이랑 새로 생긴 빵집 이층에서 봤어.` |

#### q15 (q-p1-birthday-plans-ambiguity-15)

| Field | Before | After |
| --- | --- | --- |
| question | `생일에 집에서 친구랑 놀았다는 얘기 있잖아. 누구네서 뭘 한 거야?` | `생일에 집에서 친구랑 놀았다는 얘기 있잖아. 누구네서 뭘 한 거야?` |
| expected_answer | `자기 생일에는 자기 집에서 은결이와 보드게임, 시온이 생일에는 시온이네서 쿠키를 구웠다.` | `자기 생일에는 자기 집에서 은결이와 보드게임, 시온이 생일에는 시온이네서 쿠키를 구웠다.` |
| gold support | `9810a8c2-5ca6-57b1-abac-d8013691cc5d`: `은결이랑 거실 바닥에서 오래된 보드게임을 꺼냈다.`<br>`7e3fa748-c0a2-50ea-886a-01b9840a1854`: `이번엔 같이 쿠키 구웠어.` | `9810a8c2-5ca6-57b1-abac-d8013691cc5d`: `내 생일엔 집에서 놀았다. 은결이랑 거실 바닥에서 오래된 보드게임을 꺼냈다.`<br>`7e3fa748-c0a2-50ea-886a-01b9840a1854`: `그건 내 생일 때고, 이번엔 같이 쿠키 구웠어.`<br>`c7752048-9030-5101-9056-c29319b4e1e3`: `응. 이번엔 시온이네 집에 갔어.` |

#### q22 (q-p2-deadline-false_premise-22)

| Field | Before | After |
| --- | --- | --- |
| question | `수정 요청 무시하고 저녁 약속 나갔던 날 말이야.` | `수정 요청 무시하고 저녁 약속 나갔던 날 말이야.` |
| expected_answer | `약속을 취소하고 사무실에 남아 수정했다.` | `약속을 취소하고 사무실에 남아 수정했다.` |
| gold support | `2817ef7b-63eb-52cb-aa78-d2f36e4068fb`: `저녁 약속은 취소하고 사무실에 남았다.` | `2817ef7b-63eb-52cb-aa78-d2f36e4068fb`: `퇴근 버튼을 누르기 직전에 수정 요청이 왔다. 문장을 고치는 건 금방인데 표가 계속 밀려서 시간이 걸렸다. 결국 저녁 약속은 취소하고 사무실에 남았다.` |

#### q23 (q-p2-seaside-weekends-ambiguity-23)

| Field | Before | After |
| --- | --- | --- |
| question | `바닷가 갔다가 뭐 먹었다는 날, 같이 간 사람이랑 어디서 먹었는지 헷갈려.` | `바닷가 갔다가 뭐 먹었다는 날, 같이 간 사람이랑 어디서 먹었는지 헷갈려.` |
| expected_answer | `나래와 동쪽 포구에서는 국숫집, 로운과 서쪽 해변에서는 모래사장에서 김밥을 먹었다.` | `나래와 동쪽 포구에서는 국숫집, 로운과 서쪽 해변에서는 모래사장에서 김밥을 먹었다.` |
| gold support | `e2148709-defb-57c6-ad43-d75179ed679c`: `나래랑 간 동쪽 포구`<br>`6221ea99-fd08-5a41-a478-b29d815d40f9`: `모래사장에 앉아서 김밥 먹었어.` | `e2148709-defb-57c6-ad43-d75179ed679c`: `나래랑 간 동쪽 포구는 바람이 너무 세서 돗자리도 못 폈다. 둘이 방파제 아래 작은 국숫집으로 피신했다.`<br>`6221ea99-fd08-5a41-a478-b29d815d40f9`: `모래사장에 앉아서 김밥 먹었어.`<br>`f85fdf15-a63a-5eac-935d-c55d55100dad`: `아니, 이번엔 로운이랑 서쪽 해변 갔어. 날이 잔잔했어.` |

#### q30 (q-p2-car-repair-false_premise-30)

| Field | Before | After |
| --- | --- | --- |
| question | `차 문제 생겨서 배터리 새걸로 갈았다며?` | `차 문제 생겨서 배터리 새걸로 갈았다며?` |
| expected_answer | `교체하지 않고 느슨한 단자 연결만 조였다.` | `교체하지 않고 느슨한 단자 연결만 조였다.` |
| gold support | `ea52945c-bdc8-5930-9e26-b35fe4b89e10`: `부품을 바꾸지는 않고 연결만 다시 조였다.` | `ea52945c-bdc8-5930-9e26-b35fe4b89e10`: `큰 고장일까 봐 겁났는데 정비소에서는 배터리 단자가 느슨한 거라고 했다. 부품을 바꾸지는 않고 연결만 다시 조였다.` |

#### q31 (q-p2-wedding-guests-ambiguity-31)

| Field | Before | After |
| --- | --- | --- |
| question | `결혼식에서 봉투 냈다는 얘기 있잖아. 어느 예식이었고 누가 맡았던 거지?` | `결혼식에서 봉투 냈다는 얘기 있잖아. 어느 예식이었고 누가 맡았던 거지?` |
| expected_answer | `다인이 결혼식에서는 모아가 모아서 냈고, 회사 선배 결혼식에서는 각자 직접 냈다.` | `다인이 결혼식에서는 모아가 모아서 냈고, 회사 선배 결혼식에서는 각자 직접 냈다.` |
| gold support | `ab87ba0e-f435-5eb2-87d3-45e3f854d913`: `다인이 결혼식에 갔는데`<br>`697814a5-21b1-5634-9a7a-62a447f4c1b1`: `이번에는 각자 냈어. 나는 접수대에 직접 냈고.` | `ab87ba0e-f435-5eb2-87d3-45e3f854d913`: `다인이 결혼식에 갔는데 아는 얼굴이 많아 대기실부터 정신없었다. 축의금 봉투는 모아가 대표로 모아서 냈다.`<br>`697814a5-21b1-5634-9a7a-62a447f4c1b1`: `이번에는 각자 냈어. 나는 접수대에 직접 냈고.`<br>`f4c8aabf-8393-5e96-8405-988cac1d11e6`: `응. 이번엔 회사 선배 결혼식이었어. 지난번 친구들 모인 날이랑 달라.` |

#### q35 (q-p3-kimchi-sharing-multi-35)

| Field | Before | After |
| --- | --- | --- |
| question | `무가 매워서 어떻게 손봤더라? 알려 준 분한테는 뭘 담아 드렸어?` | `무가 매워서 어떻게 손봤더라? 알려 준 분은 누구였고, 그분께 드릴 때 어떤 크기와 재질의 통에 담았어?` |
| expected_answer | `배를 갈아 넣었고 아람 씨에게 작은 유리 통에 담아 드렸다.` | `배를 갈아 넣었고 아람 씨에게 작은 유리 통에 담아 드렸다.` |
| gold support | `dd713a5a-4fc4-5980-a22d-4906a6a07159`: `배를 갈아 넣어 보라고 알려 줬다.`<br>`95bc0dcd-368d-5d13-9336-afdd78fe9b16`: `작은 유리 통째로 줬어.` | `dd713a5a-4fc4-5980-a22d-4906a6a07159`: `옆집 아람 씨가 배를 갈아 넣어 보라고 알려 줬다.`<br>`95bc0dcd-368d-5d13-9336-afdd78fe9b16`: `아람 씨한테 작은 유리 통째로 줬어.` |

#### q36 (q-p3-balcony-plants-multi-36)

| Field | Before | After |
| --- | --- | --- |
| question | `시들해 보이던 꽃은 어떤 데로 옮겼고, 물 줄 때는 뭘 보고 정해?` | `시들해 보이던 꽃은 어떤 데로 옮겼고, 물 줄 때는 어떤 도구로 흙의 어떤 상태를 확인해?` |
| expected_answer | `흰색 긴 화분으로 옮겼고 나무젓가락으로 흙 마른 상태를 보고 물을 준다.` | `흰색 긴 화분으로 옮겼고 나무젓가락으로 흙 마른 상태를 보고 물을 준다.` |
| gold support | `ca2e629f-8e02-56d2-90b8-962b76add1b9`: `제라늄은 흰색 긴 화분으로 옮겨 심었다.`<br>`f7641556-4290-5860-a43e-74b3f90f661d`: `나무젓가락을 꽂았다 빼서 흙이 마른 게 보일 때 줘.` | `ca2e629f-8e02-56d2-90b8-962b76add1b9`: `제라늄은 흰색 긴 화분으로 옮겨 심었다.`<br>`f7641556-4290-5860-a43e-74b3f90f661d`: `나무젓가락을 꽂았다 빼서 흙이 마른 게 보일 때 줘.` |

#### q38 (q-p3-fridge-repair-false_premise-38)

| Field | Before | After |
| --- | --- | --- |
| question | `엄마 냉장고 모터 나가서 새걸로 바꿨다며?` | `엄마 냉장고 모터 나가서 새걸로 바꿨다며?` |
| expected_answer | `모터는 이상 없었고 긴 반찬통이 문을 밀고 있었다.` | `모터는 이상 없었고 긴 반찬통이 문을 밀고 있었다.` |
| gold support | `d94b1d1d-22e0-599d-bfb5-fc7c376357bb`: `모터는 이상 없다고 하셨다.` | `d94b1d1d-22e0-599d-bfb5-fc7c376357bb`: `긴 반찬통이 문을 밀고 있었던 거였다. 모터는 이상 없다고 하셨다.` |

#### q41 (q-p3-sewing-apron-single-41)

| Field | Before | After |
| --- | --- | --- |
| question | `요리할 때 자꾸 풀리던 옷은 어떻게 손봤어?` | `요리할 때 자꾸 풀리던 옷은 어떻게 손봤어?` |
| expected_answer | `앞치마 끈 대신 단추 두 개를 달았다.` | `앞치마 끈 대신 단추 두 개를 달았다.` |
| gold support | `c07e67b0-d5e6-5982-ae70-d0d7e313a656`: `끈 대신 옆에 단추 두 개를 달았다.` | `c07e67b0-d5e6-5982-ae70-d0d7e313a656`: `앞치마 끈이 자꾸 풀려서 직접 고쳐 봤다. 재봉틀은 오래 안 써서 밑실 감는 것부터 기억이 안 났다. 설명서를 한참 들여다보다 끈 대신 옆에 단추 두 개를 달았다.` |

#### q42 (q-p3-sewing-apron-single-42)

| Field | Before | After |
| --- | --- | --- |
| question | `고친 앞치마에 덧댄 천은 어디서 난 거야?` | `고친 앞치마에 덧댄 천은 어디서 난 거야?` |
| expected_answer | `어릴 때 쓰던 줄무늬 베개 커버를 잘랐다.` | `어릴 때 쓰던 줄무늬 베개 커버를 잘랐다.` |
| gold support | `d22d1e64-4b48-5035-baa0-3805fa46ce30`: `줄무늬 베개 커버 잘라서 덧댔어.` | `d22d1e64-4b48-5035-baa0-3805fa46ce30`: `너 어릴 때 쓰던 줄무늬 베개 커버 잘라서 덧댔어.` |

#### q43 (q-p3-temple-walk-multi-43)

| Field | Before | After |
| --- | --- | --- |
| question | `절 올라가다 쉬면서 뭘 마셨고, 돌아올 때는 어떻게 내려왔어?` | `절 올라가다 쉬면서 뭘 마셨고, 돌아올 때는 어떻게 내려왔어?` |
| expected_answer | `연근차를 마셨고 내려올 때 작은 마을버스를 탔다.` | `연근차를 마셨고 내려올 때 작은 마을버스를 탔다.` |
| gold support | `371cf3b1-6028-5264-a571-61ef33812033`: `연근차를 마시고`<br>`59d53d3f-48c5-5dd9-8f45-c9a98b7869a6`: `작은 마을버스 탔어.` | `371cf3b1-6028-5264-a571-61ef33812033`: `연근차를 마시고`<br>`dd18b622-a06b-53ab-aafd-ec10b4a54775`: `8월 15일에 절 갔던 날, 돌멩이를 빼고 나서는 작은 마을버스 탔어.` |

#### q44 (q-p3-phone-class-multi-44)

| Field | Before | After |
| --- | --- | --- |
| question | `휴대폰 수업에서 만든 앨범 이름이랑, 다음엔 무슨 사진으로 해 볼 건지 알려줘.` | `휴대폰 수업에서 만든 앨범 이름이랑, 다음엔 무슨 사진으로 해 볼 건지 알려줘.` |
| expected_answer | `오늘의 하늘이고 다음에는 손주 사진을 넣으려 한다.` | `오늘의 하늘이고 다음에는 손주 사진을 넣으려 한다.` |
| gold support | `21bdfbdf-f518-542c-8c86-28f5d5330fd1`: `연습 앨범 이름은 오늘의 하늘이었다.`<br>`931834a7-bbf4-5d2b-96a8-40c1aa622da7`: `네가 보내 준 손주 사진을 넣어 보려고.` | `21bdfbdf-f518-542c-8c86-28f5d5330fd1`: `연습 앨범 이름은 오늘의 하늘이었다.`<br>`931834a7-bbf4-5d2b-96a8-40c1aa622da7`: `다음 수업엔 네가 보내 준 손주 사진을 넣어 보려고.` |

#### q46 (q-p3-theatre-ticket-false_premise-46)

| Field | Before | After |
| --- | --- | --- |
| question | `엄마 공연은 맨 앞줄에서 보기로 했다며?` | `엄마 공연은 맨 앞줄에서 보기로 했다며?` |
| expected_answer | `처음에는 앞줄을 골랐지만 가운데 구역 뒤쪽으로 바꿨다.` | `처음에는 앞줄을 골랐지만 가운데 구역 뒤쪽으로 바꿨다.` |
| gold support | `f27fec65-71b0-569c-920d-dc59a408cd7d`: `가운데 구역 뒤쪽으로 바꿨다.` | `f27fec65-71b0-569c-920d-dc59a408cd7d`: `앞줄이 남아 있길래 골랐다가 무대가 높다는 후기를 보고 마음이 바뀌었다. 결국 가운데 구역 뒤쪽으로 바꿨다.` |

#### q47 (q-p3-coast-trips-ambiguity-47)

| Field | Before | After |
| --- | --- | --- |
| question | `엄마 바다 보러 갔을 때 점심 뭐 먹었지? 누구랑 간 날인지도 잘 생각 안 나.` | `엄마 바다 보러 갔을 때 점심 뭐 먹었지? 누구랑 간 날인지도 잘 생각 안 나.` |
| expected_answer | `언니와 간 날은 생선구이, 동네 모임과 간 날은 들깨칼국수다.` | `언니와 간 날은 생선구이, 동네 모임과 간 날은 들깨칼국수다.` |
| gold support | `0ffa9177-46af-5c60-853e-fefe167102cb`: `언니랑 버스를 탔다.`<br>`bba0cfc6-ceb8-52b0-9527-7e2501ff11f6`: `이번엔 들깨칼국수 먹었어.` | `0ffa9177-46af-5c60-853e-fefe167102cb`: `가을 바다를 보러 언니랑 버스를 탔다. 바람이 차서 오래 서 있지는 못했다. 점심은 항구 안쪽에서 생선구이를 먹었다.`<br>`bba0cfc6-ceb8-52b0-9527-7e2501ff11f6`: `이번엔 들깨칼국수 먹었어.`<br>`fe8b00f9-2acf-5e89-bad2-85231b4d5243`: `이번엔 동네 모임에서 간 거라 좀 북적였어. 언니랑 단둘이 간 때하고 달라.` |

#### q50 (q-p4-conference-single-50)

| Field | Before | After |
| --- | --- | --- |
| question | `말로 설명하면 길어진다던 부분, 다음엔 어떤 자료로 보여 주시려고요?` | `말로 설명하면 길어진다던 부분, 다음엔 어떤 자료로 보여 주시려고요?` |
| expected_answer | `표본 추출 과정을 한 장짜리 흐름도로 만들려 한다.` | `표본 추출 과정을 한 장짜리 흐름도로 만들려 한다.` |
| gold support | `17ed58d4-3744-517e-9f21-e6800a004c2a`: `한 장짜리 흐름도로 만들려고요.` | `17ed58d4-3744-517e-9f21-e6800a004c2a`: `표본 추출 과정을 한 장짜리 흐름도로 만들려고요.` |

#### q51 (q-p4-mentoring-multi-51)

| Field | Before | After |
| --- | --- | --- |
| question | `후배 화면이 굳었던 원인이 뭐였고, 발표 때 또 문제가 생기면 쓸 건 뭔가요?` | `후배 화면이 굳었던 원인이 뭐였고, 발표 때 또 문제가 생기면 쓸 건 뭔가요?` |
| expected_answer | `중복 요청이 원인이었고 수정한 흐름을 녹화한 영상을 대비책으로 쓰기로 했다.` | `중복 요청이 원인이었고 수정한 흐름을 녹화한 영상을 대비책으로 쓰기로 했다.` |
| gold support | `c4821d1b-e5ea-5324-80d6-41c503eac2dd`: `같은 요청이 두 번 나가고 있었다.`<br>`40c649b7-a89c-5c8d-b3ae-789f7a7562f6`: `짧게 녹화해 두기로 했어요.` | `c4821d1b-e5ea-5324-80d6-41c503eac2dd`: `같은 요청이 두 번 나가고 있었다.`<br>`40c649b7-a89c-5c8d-b3ae-789f7a7562f6`: `수정한 흐름을 짧게 녹화해 두기로 했어요.` |

#### q54 (q-p4-plant-sitting-false_premise-54)

| Field | Before | After |
| --- | --- | --- |
| question | `잎이 축 늘어져서 가자마자 물 듬뿍 주셨죠?` | `잎이 축 늘어져서 가자마자 물 듬뿍 주셨죠?` |
| expected_answer | `흙이 젖어 있어 물을 주지 않았다.` | `물을 주지 않았다.` |
| gold support | `a5c0a77a-d071-5117-90aa-ae50e06fb43c`: `물은 주지 않고` | `a5c0a77a-d071-5117-90aa-ae50e06fb43c`: `물은 주지 않고` |

#### q59 (q-p4-night-train-multi-59)

| Field | Before | After |
| --- | --- | --- |
| question | `밤새 이동할 때 읽으려고 가져간 책이랑, 내려서 드신 아침은 뭐였어요?` | `밤새 이동할 때 읽으려고 가져간 책이랑, 내려서 드신 아침은 뭐였어요?` |
| expected_answer | `작은 서점의 밤을 가져갔고 도착 후 시장에서 콩나물국밥을 먹었다.` | `작은 서점의 밤을 가져갔고 도착 후 시장에서 콩나물국밥을 먹었다.` |
| gold support | `4d858e12-efa6-5135-9838-a1e76fd1e2dc`: `책은 작은 서점의 밤이었는데`<br>`825c2b8b-89a1-5d92-9724-c44609195d83`: `콩나물국밥 먹었어요.` | `4d858e12-efa6-5135-9838-a1e76fd1e2dc`: `밤기차를 타면 잘 줄 알았는데 창밖만 보다가 도착했다. 콘센트 있는 자리를 골랐는데 충전기를 집에 두고 왔다. 출발 전에 읽던 책을 끝내려 했던 계획도 틀어졌다. 책은 작은 서점의 밤이었는데 두 장도 못 넘겼다.`<br>`825c2b8b-89a1-5d92-9724-c44609195d83`: `역 건너편 시장에서 콩나물국밥 먹었어요.` |

#### q60 (q-p4-exhibition-multi-60)

| Field | Before | After |
| --- | --- | --- |
| question | `전시장 큰 사진은 어디로 옮기셨고, 선배한테 안내받으려면 언제 가야 해요?` | `전시장 큰 사진은 어디로 옮기셨고, 선배한테 안내받으려면 언제 가야 해요?` |
| expected_answer | `큰 사진은 입구 맞은편 벽이고 안내 담당은 금요일 오후다.` | `큰 사진은 입구 맞은편 벽이고 안내 담당은 금요일 오후다.` |
| gold support | `40503d92-ac7c-5ef3-96fd-837881a2b42c`: `입구 맞은편 벽으로 옮겼다.`<br>`82e57a09-9ef4-525e-a0e0-3ed4b6ecc29c`: `금요일 오후 안내를 맡았어요.` | `40503d92-ac7c-5ef3-96fd-837881a2b42c`: `가장 큰 사진은 입구 맞은편 벽으로 옮겼다.`<br>`82e57a09-9ef4-525e-a0e0-3ed4b6ecc29c`: `금요일 오후 안내를 맡았어요.` |

#### q62 (q-p4-workshop-notebook-false_premise-62)

| Field | Before | After |
| --- | --- | --- |
| question | `강의 내내 기념 수첩에 필기하셨다면서요?` | `강의 내내 기념 수첩에 필기하셨다면서요?` |
| expected_answer | `인쇄물 여백에 필기했고 수첩은 따로 뒀다.` | `인쇄물 여백에 필기했고 수첩은 따로 뒀다.` |
| gold support | `9acf397c-4f0f-5dab-ba74-1764a3c99cb8`: `인쇄물 여백에 적었다.` | `9acf397c-4f0f-5dab-ba74-1764a3c99cb8`: `발표 중에는 수첩에 메모하지 않고 나눠 준 인쇄물 여백에 적었다. 수첩은 표지가 예뻐서 선물하려고 따로 뒀다.` |

#### q63 (q-p4-bookshop-talks-ambiguity-63)

| Field | Before | After |
| --- | --- | --- |
| question | `서점에서 들은 작가님은 결말을 먼저 생각하고 쓰신댔나요? 어느 분 얘기였는지 가물가물해요.` | `서점에서 들은 작가님은 결말을 먼저 생각하고 쓰신댔나요? 어느 분 얘기였는지 가물가물해요.` |
| expected_answer | `모아 작가는 결말부터 정하지 않고, 고운 작가는 마지막 장면을 먼저 쓴다.` | `모아 작가는 결말부터 정하지 않고, 고운 작가는 마지막 장면을 먼저 쓴다.` |
| gold support | `05050f10-7a10-5f59-a7ad-a426b06e0e7f`: `초고를 쓸 때 결말부터 정하지 않는다고 하셨다.`<br>`870e5874-8ffb-5772-a8fb-0b3caf8a64f6`: `마지막 장면을 먼저 써 둔다고 하셨어요.` | `05050f10-7a10-5f59-a7ad-a426b06e0e7f`: `모아 작가님은 초고를 쓸 때 결말부터 정하지 않는다고 하셨다.`<br>`870e5874-8ffb-5772-a8fb-0b3caf8a64f6`: `고운 작가님은 마지막 장면을 먼저 써 둔다고 하셨어요.` |

#### q68 (q-p5-kite-grandchild-multi-68)

| Field | Before | After |
| --- | --- | --- |
| question | `아이랑 연 날리러 갔을 때 처음에 뭘 하느라 오래 걸렸고, 다음엔 뭘 만들기로 했어요?` | `아이랑 연 날리러 갔을 때 처음에 뭘 하느라 오래 걸렸고, 다음엔 뭘 만들기로 했어요?` |
| expected_answer | `엉킨 얼레 줄을 푸느라 오래 걸렸고 종이 바람개비를 만들기로 했다.` | `엉킨 얼레 줄을 푸느라 오래 걸렸고 종이 바람개비를 만들기로 했다.` |
| gold support | `fde06231-9a05-5a3a-bad4-5f9fae7e30c5`: `얼레 줄이 엉켜`<br>`ce699401-1e21-5c16-8a31-6fe84814200e`: `작은 바람개비를 만들어 보자더군.` | `fde06231-9a05-5a3a-bad4-5f9fae7e30c5`: `얼레 줄이 엉켜 풀다 보니 손주가 먼저 지쳤다.`<br>`ce699401-1e21-5c16-8a31-6fe84814200e`: `종이로 작은 바람개비를 만들어 보자더군.` |

#### q71 (q-p5-fishing-spots-ambiguity-71)

| Field | Before | After |
| --- | --- | --- |
| question | `친구분이랑 물가 가신 날, 어디 가셨고 뭐 좀 잡으셨어요?` | `친구분이랑 물가 가신 날, 어디 가셨고 뭐 좀 잡으셨어요?` |
| expected_answer | `성호와 저수지에서는 못 잡았고, 영식과 강 둑에서는 작은 붕어 두 마리를 잡아 놓아줬다.` | `성호와 저수지에서는 못 잡았고, 영식과 강 둑에서는 작은 붕어 두 마리를 잡아 놓아줬다.` |
| gold support | `ed0ccd66-d375-5ddc-b7d1-fc5b38bf4153`: `빈 바구니로 돌아왔다.`<br>`c1223fd7-281a-5042-8ea2-28d817bbdab3`: `작은 붕어 두 마리는 잡았지.` | `ed0ccd66-d375-5ddc-b7d1-fc5b38bf4153`: `빈 바구니로 돌아왔다.`<br>`c1223fd7-281a-5042-8ea2-28d817bbdab3`: `작은 붕어 두 마리는 잡았지. 너무 작아서 바로 놓아줬네.`<br>`63586786-6a1a-5728-a9db-ef661ecbe7e9`: `이번엔 영식이하고 강 둑 아래 갔네. 성호와 저수지 간 날하고는 달라.` |

#### q75 (q-p5-woodwork-multi-75)

| Field | Before | After |
| --- | --- | --- |
| question | `나무 자르다 길이가 안 맞은 건 어떻게 쓰기로 하셨고, 완성한 건 어디 두셨어요?` | `나무 자르다 길이가 안 맞은 건 어떻게 쓰기로 하셨고, 완성한 건 어디 두셨어요?` |
| expected_answer | `짧은 쪽은 책 받치는 칸으로 쓰기로 했고 선반은 현관 안쪽에 뒀다.` | `짧은 쪽은 책 받치는 칸으로 쓰기로 했고 선반은 현관 안쪽에 뒀다.` |
| gold support | `97b88856-732e-5008-b476-acdfa8cea34a`: `책 받치는 칸으로 쓰기로 했다.`<br>`dd9852f2-3212-5ac0-937e-648cc2afb2d2`: `현관 안쪽에 놓았네.` | `97b88856-732e-5008-b476-acdfa8cea34a`: `작업실에 남은 나무를 얻어 작은 선반을 만들었다. 길이를 재 놓고도 한쪽을 짧게 잘랐다. 모자란 쪽은 책 받치는 칸으로 쓰기로 했다.`<br>`dd9852f2-3212-5ac0-937e-648cc2afb2d2`: `현관 안쪽에 놓았네.` |

### q43 record turn and chunk identity audit

| Before | After |
| --- | --- |
| `아니. 내려올 땐 작은 마을버스 탔어. 발이 너무 뜨겁더라.` | `아니. 8월 15일에 절 갔던 날, 돌멩이를 빼고 나서는 작은 마을버스 탔어. 발이 너무 뜨겁더라.` |

| Old chunk ID | Revised chunk ID (same ordinal) |
| --- | --- |
| `0a423853-faff-5195-b52c-1374549342ee` | `2945b5b1-b3a0-545e-82ba-1ff97b30a61f` |
| `59d53d3f-48c5-5dd9-8f45-c9a98b7869a6` | `dd18b622-a06b-53ab-aafd-ec10b4a54775` |
| `c239ce70-0c29-5b70-af78-58133cb10d0e` | `32bf2264-87fd-5483-9b3f-22818e3469fc` |
| `877d8742-1ad8-5abb-8f67-c4e6f717257d` | `56b98141-1341-5e63-ab2e-8a9b09fd9d77` |
| `4e69401c-f59c-56a0-bed0-3c67da97882d` | `7e0274aa-6293-5086-8295-8b78ba8ba681` |
| `c6cde521-dce8-5a9e-862d-f9065d916913` | `f40d467b-3ee4-50dd-ab60-d15c43af904b` |

### Verification for this correction

Executed locally, without bytecode or pytest cache writes:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B corpus.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q -p no:cacheprovider test_rag.py
git diff --check
```

```text
VALID: personas=5 records=100 chunks=400 questions=80
dev: records=50 chunks=200 questions=40 types={'ambiguity': 5, 'false_premise': 5, 'multi': 10, 'ordinary_chat': 5, 'single': 10, 'unanswerable': 5}
test: records=50 chunks=200 questions=40 types={'ambiguity': 5, 'false_premise': 5, 'multi': 10, 'ordinary_chat': 5, 'single': 10, 'unanswerable': 5}
PASS: unique IDs; disjoint families; valid scoped gold; exact support quotes
PASS: unanswerable/ordinary gold empty; all factual gold persona-spoken
PASS: per-persona event families; known-name/noun particle checks
PASS: factual required slots covered by exact gold support surfaces
48 passed, 2 warnings in 0.47s
```

The command chain exited 0; `git diff --check` emitted no errors. The two warnings
are existing Typer imports of deprecated Click `get_binary_stream` and
`get_text_stream` (site-packages/typer/__init__.py:24-25). Eleven new tests exercise
required-field parsing, invalid slot contracts, accepted alternative surfaces,
and rejection when a fact exists in the gold chunk but not the support quote.
Gold references increase from 90 to 95; all exact quotes and scoped IDs validate.
The 5/100/400/80 counts and DEV/TEST family distribution are unchanged.

The historical `test_generate.py` suite was not run: it replays frozen retrieval
and prompt artifacts from before this data correction. Those inputs are now
stale, rather than evidence about this revision. No artifacts or tests were
rewritten to make historical replays pass. Full-project tests, retrieval quality
and model/judge quality were not run. LSP diagnostics were unavailable because
`basedpyright-langserver` (Python) and `biome` (JSON) are not installed; no package
was installed. No commits were made. The no-model-call instruction also excludes
agy drafting/polishing for this factual audit.

## Retrieval v2 on the corrected corpus

The preceding correction-only verification is historical. The new run is in
`results/retrieval-v2/`: `offline-metrics.json` contains per-question slot
coverage, split metrics, Qwen token counts, provenance and R1-R5 decisions.
`summary.md` is the Korean results summary; `rendered-b-v2.txt` contains one
complete system/user prompt. No GPU generation or judge was run; G1-G5 remain
unmeasured, so the offline pass alone does not adopt v2.

`history/corpus-r1r3.json` is the byte-identical `git show
HEAD:experiments/rag/corpus.json` snapshot. `history/source-r1/` preserves the
original three source files needed to verify historical manifest hashes.
`historical.py` explicitly adapts missing legacy slot annotations using existing
support answers; it never updates historical gold, text, prompts or results.
The current corpus schema still requires `required_slots`. Both historical
retrieval audits and all r1/r2/r3 prompt byte-identity tests pass.
`build_blind.py` now defaults to that snapshot for old generation artifacts;
`--prompt-version r1|r2|r3` selects their original schedule. This only prepares
local inputs and does not call a judge.

`run.py --output-dir` defaults to the original `results/<model>` path and still
refuses to replace an existing directory. `kure_old` was recomputed on CPU with
the corrected corpus and the pinned KURE revision. The DEV-only calibration
policy is unchanged, yielding `0.5458711981773378`.

`retrieval_v2.py` applies that threshold once to the top-1 score. Accepted
questions restore context around the top-k hits without per-chunk threshold
filtering. Chat windows merge at canonical same-speaker chunk boundaries;
short diaries include all paragraphs. Items retain their original chunk,
record and version IDs and `speaker_role`. Every `content` line adds
`date | speaker (speaker_role=role) |`; there are no extra JSON fields.
Items are deduplicated and ordered by record date, record UUID for date ties,
then original chunk/turn order. The lowest-scoring records are removed first
to meet the full rendered evidence-JSON token budget. Oversized single entries
fall back to hit chunks before pruning; an impossible budget raises rather
than silently truncating text.

`offline_v2.py` tests the 27 DEV-only combinations of k=(3,4,5),
window=(1,2,3), budget=(1500,1750,2000). Among candidates with DEV median <=1500,
it maximizes complete factual questions, then covered slots, then minimizes
median/max tokens and k/window/budget. It saves `kure_v2/dev-selection.json`
before TEST restoration/evaluation. The selected k=3, window=3, budget=2000
gives DEV 29/30 and TEST 28/30 complete questions. All 80 evidence JSON payloads
have median 1449 and maximum 1997 Qwen tokens, including IDs and metadata.
The tokenizer-only Qwen3-8B revision matches the generation baseline
(`b968826d9c46dd6066d109eabc6255188de91218`); its files are cached under
`.cache/qwen3-8b-tokenizer/`, with no Qwen weights downloaded.

Reproduction commands from this directory (the saved output directories already
exist, so use fresh paths for a new run):

```bash
HF_HUB_OFFLINE=1 .venv/bin/python run.py kure --device cpu \
  --output-dir results/retrieval-v2/kure_old
.venv/bin/python offline_v2.py \
  --source results/retrieval-v2/kure_old --output results/retrieval-v2
.venv/bin/python generate.py --dry-run --prompt-version r1 \
  --evidence results/retrieval-v2/kure_v2/evidence.json \
  --condition-set retrieval-v2 \
  --output results/retrieval-v2/generation-prompts
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest .
bash -n run-generate.sbatch
```

For `--condition-set retrieval-v2`, `--evidence` names the v2 export, and the
paired B_old export is its sibling `../kure_old/evidence.json`. The schedule is
B_old, B_v2, C, 80 rows each, using prompt r1 and corrected gold for C. The system,
persona and user instructions outside `<evidence>` are byte-identical to r1.
The saved dry run is `generation-prompts/prompts.jsonl` (240 rows); its
`manifest.json` records the corpus, both evidence exports and prompt SHA-256.
The original A/B/C CLI defaults are retained: explicitly pass
`--corpus history/corpus-r1r3.json` to replay historical evidence, or choose a
corrected-corpus evidence export. A mismatched corpus/export fails validation.

For the later GPU job, retain existing path/runtime options and set these
environment variables (job submission was not performed):

```bash
export REPLICA_PROMPT_VERSION=r1
export REPLICA_CONDITION_SET=retrieval-v2
export REPLICA_CORPUS="$REPLICA_BASE/experiments/rag/corpus.json"
export REPLICA_EVIDENCE="$REPLICA_BASE/experiments/rag/results/retrieval-v2/kure_v2/evidence.json"
# REPLICA_OUTPUT must name a fresh generation directory.
```

Final local checks: `108 passed, 2 warnings in 3.16s` for the entire folder;
both historical retrieval audits passed; the dry run emitted 240 r1 rows;
batch syntax and `git diff --check` passed. Warnings are the existing
Typer/Click deprecations. The final serialization code was replayed in
`.cache/retrieval-v2-replay/`: selected DEV settings, evidence bytes and all
offline metrics matched, with only the final source hashes refreshed.
LSP diagnostics remain unavailable because `basedpyright-langserver` is not
installed. No commits, SSH, GPU or judge calls were made. The Korean summary
used agy drafting and the humanize-korean standard path (5.6% change, gate OK);
private language-check evidence stays in `.cache/humanize/`.
