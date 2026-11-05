# AI

학교 GPU에서 실행할 LLM 추론과 개인화 코드를 관리합니다.

- `src/`: 추론·말투 프로필·RAG 등 서비스 코드
- `tests/`: AI 코드 테스트
- `configs/`: 공유 가능한 모델·추론·개인화 설정
- `.env.example`: 비밀 값이 없는 설정 예시

### 페르소나 가드 (`ai/src/persona_guard.py`)

`PersonaIdentity`, `check_reply`, `GuardResult`를 제공하는 표준 라이브러리 기반 모듈과 추가형 프롬프트 규칙 템플릿 `PERSONA_RULES`입니다. 개인화 경로에는 아래 축소 모드로 적용하며 baseline에는 적용하지 않습니다. 실제 모델 품질 영향은 아직 검증하지 않았습니다. 자세한 동작, 검증 결과와 한계는 [`experiments/persona_guard/README.md`](../experiments/persona_guard/README.md)를 참고하세요.

## 오프라인 baseline (#13)

`src/baseline.py`는 학교 Slurm GPU 작업 셸에서 로컬 모델을 BF16으로 실행하는 CLI입니다. 기존 GPU 실행 기록은 Qwen2.5-7B-Instruct 기준이며, #43 HTTP 서버는 아래 별도 절에서 다룹니다. 개인화는 후속 과제입니다. 모델 가중치, 체크포인트, 개인 기록, 로컬 검색 색인은 커밋하지 않습니다. 비교·평가는 `experiments/`, 호출 계약은 `contracts/`에서 관리합니다.

### 확인된 환경과 검증 상태

2026-10-09 사용자가 DIS02에서 제공한 패키지 메타데이터:

| 항목 | 값 |
| --- | --- |
| Python | 3.12.7 |
| PyTorch | 2.7.1+cu118 |
| Transformers | 4.57.1 |
| Accelerate | 1.15.0 |
| Safetensors | 0.8.0 |

이전 사용자 실행에서 RTX A6000 48 GB, driver 525.105.17, CUDA 연산과 BF16 단일 질문 응답을 확인했습니다. 그 실행은 이번 CLI의 검증을 대신하지 않습니다. `nvidia-smi`의 CUDA 12.0 표시는 driver 지원 정보입니다.

- GPU 없는 mock 검사는 로딩 옵션·배치·입력·오류 처리 계약을 확인합니다.
- 새 CLI의 실제 GPU 실행: 2026-10-09 사용자 제공 DIS02 로그에서 checkpoint 4/4 로딩과 응답 생성을 확인했습니다. Codex가 직접 서버에서 실행한 것은 아닙니다. 종료 코드는 별도로 전달받지 않았습니다.
- 모델 다운로드 revision: `a09a35458c702b33eeacc393d103063234e8bc28`. 2026-10-09 사용자 제공 ABRM02 출력에서 다운로드 메타데이터(`download/*.metadata`)의 첫 줄을 대조해 이 SHA 한 개만 확인했습니다. 해당 메타데이터의 revision 일치 근거이며 모델 파일의 해시·무결성 검사 결과는 아닙니다.
- `requirements.txt`는 관측한 직접 의존성을 고정합니다. `requirements-observed.txt`에는 2026-10-09 사용자가 DIS02에서 전달한 전체 설치 목록 40개를 기록했습니다. pip·setuptools와 전이 의존성도 포함한 관측 스냅샷이며, wheel hash를 검증한 lock 또는 새 환경에서 재설치를 검증한 결과는 아닙니다.

### 환경 준비

로그인 노드에서 파일을 다운로드하고 GPU 노드에서 오프라인 설치합니다. 기존 GPU venv가 참조하는 interpreter는 로그인 노드에 없어 `No such file or directory`가 발생할 수 있습니다. 활성화 prompt만 믿지 말고 GPU 노드에서 venv 실행 파일을 직접 사용하세요. 로그인 노드 다운로드 venv와 GPU 실행 venv는 별개입니다.

로그인 노드에서 저장소 루트의 requirements를 사용해 Python 3.12 wheel을 준비합니다. 기존 wheel이 있으면 재사용합니다. 학교 환경의 외부 HTTPS 접근이 허용되는 경우에만 실행합니다.

```bash
~/replica-download-venv/bin/python -m pip download -r ai/requirements.txt \
  --extra-index-url https://download.pytorch.org/whl/cu118 \
  --find-links="$HOME/replica-wheels-py312" \
  --dest "$HOME/replica-wheels-py312" \
  --python-version 3.12 --implementation cp --abi cp312 \
  --platform manylinux_2_28_x86_64 --platform manylinux_2_27_x86_64 \
  --platform manylinux2014_x86_64 --platform manylinux1_x86_64 \
  --platform linux_x86_64 --only-binary=:all:
```

모델도 로그인 노드에서 공식 Hub의 특정 revision으로 먼저 받아 shared home에 둡니다. 이미 다운로드한 모델은 재다운로드하지 않고 기존 revision 메타데이터부터 확인합니다. 모델 다운로드는 CLI 실행과 분리됩니다.

GPU 할당 예시입니다. DIS02 가용성은 현재 상태를 확인해야 합니다.

```bash
srun --gres=gpu:1 -p p02 --nodelist=DIS02 --job-name=replica-llm --time=00:30:00 --pty bash
hostname
nvidia-smi
~/replica-llm-venv/bin/python -m pip install --no-index \
  --find-links="$HOME/replica-wheels-py312" -r ai/requirements.txt
~/replica-llm-venv/bin/python -m pip check
```

### 실행

아래 명령은 GPU 노드의 저장소 루트에서 실행합니다. 저장소 파일을 학교 shared home에 먼저 복사해야 합니다. 자동 SSH 접속·업로드는 이 코드에 없습니다.

```bash
printf '%s\n' '발표 준비로 긴장하는 대학생에게 세 문장으로 조언해줘.' | \
  ~/replica-llm-venv/bin/python ai/src/baseline.py \
  --model-path "$HOME/replica-models/Qwen2.5-7B-Instruct"
```

질문은 stdin으로 받습니다. 위 예시는 합성 질문이며 개인 원문을 shell history에 남기지 마세요. `REPLICA_MODEL_PATH`로 경로를 지정할 수도 있습니다. system prompt와 기본 출력 토큰 제한은 `configs/baseline.json`, 일회성 제한은 `--max-new-tokens`(1~1024)입니다. 설정의 model_id는 참고 정보이며 Hub 다운로드 경로로 사용하지 않습니다.

현재 system prompt는 #55 프롬프트 실험의 개선안(`guarded`)이며 최종 프롬프트가 아닙니다. 기록에 없는 전제·감정을 받아들이지 않도록 지시를 더했고, 실험 결과와 알려진 부작용은 `experiments/results/2026-10-22-issue55-prompt-experiment/README.md`에 있습니다. #33·#36·#37 평가는 기존 프롬프트(`한국어로 간결하고 자연스럽게 답하세요.`)로 고정되어 있어 이 설정을 바꿔도 그 결과는 달라지지 않습니다.

CLI는 Python 3.12 가상환경·Slurm 작업 환경 표시·CUDA 접근·네이티브 BF16을 확인합니다. 환경변수 표시는 스케줄러 권한의 인증 수단은 아니며 실제 할당은 Slurm이 관리합니다. CUDA의 논리 장치 0을 사용하고 CPU fallback/offload를 허용하지 않습니다. 모든 로더는 로컬 파일만 사용합니다. 입력+출력 길이가 모델 context를 넘으면 실패합니다.

성공 시 응답만 stdout에 출력하며, 실패 시 고정 오류 코드와 안전한 안내를 stderr에 출력하고 1로 종료합니다. 원본 예외·traceback·질문·개인 경로를 오류에 출력하지 않습니다. API 공개와 AWS 연결은 #14의 학교 허용 조건 확인 후 별도 진행합니다.

### GPU 없는 검사와 실제 GPU 증거

```bash
python3 -m unittest discover -s ai/tests -v
```

mock 검사는 GPU·모델·네트워크 없이 실행합니다. 실제 검증에서는 GPU 작업 셸에서 새 CLI를 실행하고 명령·합성 질문·출력·종료 코드, GPU·dtype 및 패키지 정보를 기록합니다. 이전 수동 실행은 세 문장 요구를 완전히 지키지 못했지만, 2026-10-09 새 CLI 실행에서는 아래 세 문장 응답을 얻었습니다.

```text
첫째, 깊게 숨을 들이쉬고 천천히 내쉬며 마음을calm해보세요. 둘째, 준비된 내용을 다시 한 번 확인하고 자신감을 가져보세요. 셋째, 질문이 있으면 적극적으로 받아들이는 자세를 유지하세요.
```

질문과 실행 명령은 위 실행 예시를 사용했습니다. 모델 로딩부터 응답까지 도달했으므로 CLI의 Slurm·CUDA·네이티브 BF16·GPU 배치 검사를 통과한 경로입니다. 로그에는 별도의 dtype 출력이나 종료 코드가 없으며, 정확한 GPU 모델은 앞서 제공된 환경 정보에 근거합니다. `마음을calm해보세요`라는 혼합 표현은 품질 한계로 남깁니다. checkpoint 로딩 표시의 약 3초는 전체 응답 지연 측정값이 아닙니다. 품질·성능 평가는 후속 작업입니다.

실제 설치 목록은 GPU venv의 `python -m pip list --format=freeze` 출력으로 수집해 `requirements-observed.txt`에 기록했습니다. 모델 revision도 기존 다운로드 metadata와 대조했습니다. 모델이나 개인 기록 파일은 결과 문서에 첨부하지 않습니다. 새 CLI의 GPU 응답, 설치 버전, 모델 revision까지 실행 근거를 확보했습니다. 별도 실행 종료 코드는 수집하지 않았고, 깨끗한 환경에서의 재설치는 검증하지 않았습니다.

공식 참고: [Transformers 오프라인 설치](https://huggingface.co/docs/transformers/v4.57.1/installation), [모델 카드](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct).

## 개인화 API (#78)

`ai/src/server.py`는 한 번 로드한 모델과 추론 슬롯을 공유하며 CLI `--mode persona` 지정 시 `POST /v1/baseline/generate`와 `POST /v1/persona/generate` 두 경로를 모두 활성화합니다. 기본값 `--mode baseline`은 개인화 요청에 `503 MODEL_NOT_READY`를 반환합니다. `ai/src/persona_adapter.py`의 `PersonaInference`는 호출자가 #59 검색 v2로 선택·복원한 근거를 받아 r1 핵심 프롬프트에 프로필·이력·#70 가드·JSON 출력 규칙을 더합니다. GPU 서버는 검색을 다시 수행하지 않으며 실험용 r1과 바이트 단위로 같지는 않습니다. 모델 ID와 리비전은 설정 및 `REPLICA_MODEL_ID`, `REPLICA_MODEL_REVISION`으로 바꿀 수 있습니다. 시험 기준인 Qwen3-8B는 최종 서비스 모델이 아닙니다.

### 출력과 가드 정책 (PROPOSAL)

아래 기본값은 모두 **PROPOSAL**이며 윤서준(backend)·임재성(DB)의 확인이 남아 있습니다. `contracts/`는 수정하지 않았습니다.

- 모델은 한 번의 호출에서 `{"message":"...","evidence_used":[1]}` JSON을 출력합니다. 서버는 1부터 시작하는 인용 번호를 입력 근거의 `chunk_id` UUID로 변환합니다. 빈 목록도 허용합니다. 깨진 JSON, 필드 불일치, 범위 밖·중복·비정수 번호는 복구나 baseline 재호출 없이 `500 INFERENCE_FAILED`로 처리합니다. 반환된 ID는 모델이 선언한 출처이며 사실성을 증명하지 않습니다.
- `max_new_tokens`는 기존과 같은 총 `1..1024` 예산이며 JSON 구문과 인용 번호도 포함합니다. 짧은 예산에서 JSON이 잘리면 500이 될 수 있습니다. 예산을 늘리거나 자동 재생성하지 않습니다.
- 축소 가드는 `persona.display_name`만 이름·관계 두 별칭 범주에 넣습니다. `provider_address`는 자기 별칭으로 사용하지 않습니다. `style_profile.content.speech_style.tone`이 `반말`, `다정한 반말`, `해요체`, `하게체` 중 정확히 한 값일 때만 그 말높임을 가드 치환에 사용합니다. 빈 값·혼합·미지원 값은 추측 없이 `해요체`로 대체합니다. 이 대체값은 가드 치환문에만 적용하며 생성 프롬프트에는 받은 프로필을 그대로 전달합니다.
- 가드가 답변을 치환하면 `evidence_used=[]`로 비우고 원래 생성의 `finish_reason`을 유지합니다. 개인화 실패나 결과 불명 뒤 baseline을 자동 호출하지 않습니다. baseline은 별도 모드·경로로 명시적으로 선택합니다.

### 실행과 검증

기존 Python 3.12 GPU 환경과 로컬 모델 스냅샷을 사용합니다. 기본 설정에는 과거 모델 라벨이 있으므로 Qwen3-8B 시험에서는 식별자를 명시합니다.

```bash
REPLICA_MODEL_ID=Qwen/Qwen3-8B python ai/src/server.py \
  --port 18078 --mode persona --model-path "$REPLICA_MODEL_PATH"
python ai/scripts/smoke_persona.py --help
```

가짜 엔진과 실제 로컬 HTTP로 생성·계약·인용·축소 가드를 검사합니다. `ai/scripts/smoke_persona.py`와 `smoke_persona.sbatch`는 합성 입력만 사용하는 GPU 스모크 준비물이며 아직 제출·실행하지 않았습니다. 임베딩 실행 위치와 인덱스·메타데이터 및 백엔드 호출 전환은 담당자 합의가 남아 있고, 이 구현만으로 #78 전체 완료를 뜻하지 않습니다.

## 학교 GPU baseline HTTP API (#43)

`src/server.py`는 표준 라이브러리 `ThreadingHTTPServer`로 `POST /v1/baseline/generate`를 제공합니다. `src/inference.py`가 이력 축소와 생성을 맡으며 CLI의 환경 검사·로더를 재사용합니다. 새 런타임 의존성은 없습니다.

계약의 기준 소스는 병합된 `contracts/baseline` 및 `contracts/backend-ai/api-spec.md`의 섹션 1입니다. HTTP 테스트는 병합된 정식 예제를 사용하며 기존에 복사해 두었던 픽스처는 과거 기록용으로만 유지됩니다.

**2026-10-25 학교 DIS02에서 Qwen3-8B로 실모델 smoke를 1회 실행해 9개 항목을 모두 통과했습니다(Slurm 작업 23586, `--context-limit 4096`).** 모델 준비까지 약 200초, 첫 요청 11.1초, 이후 요청 0.3~4.5초였습니다. 서비스 인증·추론 마감·외부 연결·운영 문맥 한도·장시간 운영은 검증하지 않았습니다. 아래 운영 명령은 실행 예시입니다. 로컬에서는 GPU 없는 실제 loopback HTTP 테스트, Slurm 스크립트 구문 검사와 CLI 도움말을 확인합니다.

### 모델·요청·수락 규칙

- 베이스라인 API의 대상 모델은 아직 최종 선정되거나 확정되지 않았습니다. `ai/configs/baseline.json`은 메인 브랜치의 설정값을 유지하며 가드된 시스템 프롬프트도 이 파일에서 읽어옵니다. `model_id` 메타데이터는 `REPLICA_MODEL_ID`로, 선택적 `model_revision`은 `REPLICA_MODEL_REVISION` 환경 변수로 재정의할 수 있으나 실제 가중치는 항상 로컬 경로인 `--model-path` 또는 `REPLICA_MODEL_PATH`에서 로드됩니다. 선택적 리비전 값은 오프라인 로더에 전달되지만 로컬 스냅샷을 선택하거나 검증하지 않으므로 운영자가 정확한 리비전 디렉터리를 직접 제공해야 합니다.
- HTTP 템플릿은 `enable_thinking=False`, 생성은 greedy(`do_sample=False`, 새 `GenerationConfig`, `use_model_defaults=False`)입니다. CLI의 `enable_thinking`은 선택적 설정이며, 이 필드가 없는 기존 CLI 설정은 기존 템플릿 호출을 유지합니다.
- 요청은 `Content-Type: application/json`인 비압축 UTF-8 JSON이며, `Content-Length`가 필요합니다. 압축·chunked 전송은 지원하지 않고 400으로 거부합니다. 모든 요청에 `request_id`, `message`, `history`, `max_new_tokens`가 필요하며 임의 필드는 거부합니다.
- 판정 순서는 원시 바이트 413 → 서비스 인증 401/403 → 파싱·구조·타입·UUID·범위·공백·미정의 필드 400 → 파싱된 문자·이력 쌍 수 413 → 모델 준비 503 → 축소 후 문맥 422 → 원자적 수락 409/429입니다. 원시 본문은 262144바이트, 각 텍스트는 16000 Unicode code point, 이력은 20쌍, 출력 예산은 1~1024입니다. 오류 코드는 계약 예제의 고정 메시지를 사용하며 안전하게 검증한 요청 ID를 반환합니다. 인증 거부·파싱 전 원시 거부·깨진 JSON·잘못되거나 누락된 ID는 `null`입니다.
- 실제 tokenizer로 system·이력·현재 질문·generation prompt를 렌더링하고 출력 예산까지 합산합니다. 넘으면 가장 오래된 쌍 전체부터 제거합니다. 질문과 출력 예산은 줄이지 않으며 이력이 없어도 넘으면 422입니다. `context`의 사용/제외 쌍 수 합은 제출한 이력 길이입니다. 유효 문맥은 모델의 `max_position_embeddings`, tokenizer의 유효 양수 `model_max_length`, 선택적 `--context-limit` 중 최솟값입니다.
- 한 인스턴스에서 생성은 한 건만 실행하고 대기열은 없습니다. 하나의 lock으로 ID·슬롯을 확인하고 예약/반환합니다. 실행 중 같은 ID는 409, 다른 ID는 429입니다. 연결 종료나 504가 나도 실제 계산과 CUDA 동기화가 끝나기 전에는 ID·슬롯을 반환하지 않습니다. 종료한 ID의 영속 중복 방지나 결과 재조회는 제공하지 않습니다.
- 빈 출력·공백 출력·추론 예외는 안전한 500 `INFERENCE_FAILED`입니다. 성공은 `ai_generated: true`, `finish_reason: stop|length`와 context를 반환합니다. 애플리케이션 로그에는 ID·상태·소요시간·토큰 수만 남기며 원문·예외를 출력하지 않습니다.

### 미정인 인증·마감과 준비 상태

서비스 인증은 **보류**입니다. `server.allow_service(headers)`는 기본 허용이며, 나중에 인증 방식이 정해지면 이 지점에서 `None`(허용), 401, 403을 반환합니다. 훅은 원시 크기 검사 뒤, 본문 파싱 전에 호출하며 인증 거부에서는 ID를 읽으려고 본문을 파싱하지 않습니다. 서버 주소는 `127.0.0.1`로 고정되어 있고 외부 바인딩 옵션은 없습니다.

추론 마감 수치는 합의되지 않았습니다. `--deadline-seconds`는 선택적 양수 설정이며 기본값은 미설정입니다. 지정하면 수락한 생성 작업의 완료 대기에 적용하고, 504 후에도 계산은 계속됩니다. smoke의 `--timeout`은 클라이언트 대기 제한일 뿐 GPU 취소가 아닙니다. 전송 실패 결과는 UNKNOWN이며 자동 재시도하지 않습니다.

모델은 백그라운드에서 한 번만 로드합니다. 로딩 중 생성 요청은 503입니다. `GET /healthz`는 프로세스가 응답하면 200과 `{"ready": false|true}`, `GET /readyz`는 같은 본문으로 미준비 시 503, 준비 시 200을 반환합니다. 준비는 로드 성공 여부이며 유휴 슬롯을 뜻하지 않습니다. 로드 실패 후에는 미준비 상태를 유지하고 자동 재로딩하지 않습니다. 잘못된 시작 설정·없는 로컬 모델 경로·포트 충돌은 프로세스 시작 실패로 처리합니다.

### 준비·실행·종료

앞 절과 같은 Python 3.12 GPU venv 및 `requirements.txt` 환경을 사용합니다. Slurm GPU 할당·CUDA·네이티브 BF16·GPU 배치를 검사하고 CPU fallback이나 Hub 다운로드는 하지 않습니다. 로컬 모델 스냅샷은 별도로 준비해야 합니다. 새 GPU 환경 설치는 검증하지 않았습니다. 기존 GPU venv(`transformers==4.57.1`)에서 Qwen3-8B 로딩과 생성은 위 과거 smoke로 확인했습니다.

할당받은 GPU 작업 셸의 저장소 루트에서 직접 실행하는 예시입니다. 포트 `18043`은 운영자가 고르는 예시값이며 운영 포트 합의가 아닙니다.

```bash
export REPLICA_PYTHON="$HOME/replica-llm-venv/bin/python"
export REPLICA_MODEL_PATH="$HOME/replica-models/local-snapshot"
"$REPLICA_PYTHON" ai/src/server.py --port 18043
```

Slurm 배치 실행은 저장소 루트에서 제출합니다. partition·node·시간은 기존 학교 환경을 참고한 예시이며 현재 가용성·허용 조건을 별도로 확인해야 합니다. 로그 경로의 부모 디렉터리를 먼저 준비하세요.

```bash
export REPLICA_PYTHON="$HOME/replica-llm-venv/bin/python"
export REPLICA_MODEL_PATH="$HOME/replica-models/local-snapshot"
export REPLICA_PORT=18043
mkdir -p "$HOME/replica-logs/issue43"
sbatch --partition p02 --nodelist DIS02 --time 00:30:00 \
  --output="$HOME/replica-logs/issue43/slurm-%j.out" \
  ai/scripts/serve_baseline.sbatch --context-limit 4096
```

`4096`은 축소·문맥 초과 smoke 조건을 만들기 위한 선택적 시험 상한입니다. 운영 문맥 한도로 정한 값이 아니며, 실제 모델 한도를 늘리지 않습니다. 모델의 기본 유효 한도를 쓰려면 `--context-limit`를 생략합니다. Slurm 시간 제한도 API 추론 마감과 별개입니다.

다음 명령은 서버가 실행 중인 **같은 계산 노드 안에서** 실행합니다. 로그인 노드의 localhost는 다른 노드입니다. 외부 공개·SSH 터널·AWS 연결은 #44 범위이며 이 스크립트에는 없습니다.

```bash
curl -i http://127.0.0.1:18043/healthz
curl -i http://127.0.0.1:18043/readyz
python3 ai/scripts/smoke_baseline.py --port 18043
```

smoke는 준비 상태를 한 번 확인한 뒤 합성 요청으로 정상·이력 보존/축소·length·문자/원시 크기 초과·문맥 초과·중복 409·슬롯 점유 429를 시도합니다. `--history-chars`, `--context-chars`로 합성 텍스트 길이를 조절할 수 있습니다. 실제 tokenizer와 생성 시간에 따라 축소·length·422·경합 조건이 생기지 않을 수 있어 해당 결과는 `INCONCLUSIVE`로 표시합니다. 종료 코드는 0(전부 통과), 1(실패/전송 UNKNOWN), 2(실패 없이 미확정 항목 존재)입니다. 본문 전송 완료는 수락 확인이 아니므로 실모델 경합 결과를 확정적으로 보장하지 않습니다. 프롬프트나 답변 원문은 출력하지 않습니다.

CPU 통합 테스트는 백엔드의 `RemoteLLMClient`와 가짜 엔진이 연결된 실제 루프백 서버를 사용하여 성공 응답뿐만 아니라 `503`, `422`, `500` 오류 및 `401` null-ID 케이스를 검증합니다. 이 테스트는 `backend/requirements-dev.txt`와 `contracts/requirements-test.txt`가 설치된 환경에서 `PYTHONPATH=backend:ai/tests python -m pytest -q ai/tests/test_backend_client.py` 명령으로 실행할 수 있습니다.

직접 실행은 `Ctrl+C`로 HTTP 수신을 멈추며 실행 중 생성 스레드는 계산 종료까지 유지됩니다. 배치 작업은 `sbatch`가 반환한 ID로 `scancel JOBID`를 실행해 종료합니다. 작업 종료는 요청 취소 API가 아니며, 실행 중 요청의 정상 응답을 보장하지 않습니다. 로드 실패 시 작업을 종료하고 모델 경로·환경을 확인한 뒤 새 작업을 제출하여 `/readyz`를 다시 확인합니다.

### 로컬 검증과 남은 검증

```bash
python3 -m unittest discover -s ai/tests -v
bash -n ai/scripts/serve_baseline.sbatch
python3 ai/src/server.py --help
python3 ai/scripts/smoke_baseline.py --help
```

GPU 없는 테스트는 가짜 tokenizer·생성기와 실제 loopback HTTP를 사용합니다. 판정 순서, 계약 예제, ID 반환, 동시 수락, 연결 종료/504 뒤 슬롯 유지, 이력 축소, 빈 출력, length, 백그라운드 준비 상태와 합성 smoke 경로를 검사합니다. CUDA 동기화 호출 검사는 mock이며 실제 GPU 계산 종료를 측정한 것이 아닙니다. 위 테스트·구문·도움말과 학교 GPU smoke 1회 외에 지연 분포·품질·메모리 사용량·장시간 운영은 **미검증**입니다.
