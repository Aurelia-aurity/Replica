# AI

학교 GPU에서 실행할 LLM 추론과 개인화 코드를 관리합니다.

- `src/`: 추론·말투 프로필·RAG 등 서비스 코드
- `tests/`: AI 코드 테스트
- `configs/`: 공유 가능한 모델·추론·개인화 설정
- `.env.example`: 비밀 값이 없는 설정 예시

## 오프라인 baseline (#13)

`src/baseline.py`는 학교 Slurm GPU 작업 셸에서 로컬 Qwen2.5-7B-Instruct를 BF16으로 실행하는 CLI입니다. API 서버와 개인화는 후속 과제입니다. 모델 가중치, 체크포인트, 개인 기록, 로컬 검색 색인은 커밋하지 않습니다. 비교·평가는 `experiments/`, 호출 계약은 `contracts/`에서 관리합니다.

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
