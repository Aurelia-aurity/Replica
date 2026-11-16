#!/bin/bash
#SBATCH --job-name=replica96-timing
#SBATCH --partition=p02
#SBATCH --nodelist=DIS02
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=4
#SBATCH --time=04:00:00
#SBATCH --output=slurm-%j.out
set -euo pipefail
cd "${SLURM_SUBMIT_DIR:?}"
export REPLICA_MODEL_ID=Qwen/Qwen3-8B
export REPLICA_MODEL_REVISION=b968826d9c46dd6066d109eabc6255188de91218
export REPLICA_MODEL_PATH=/abr/coss40/replica-models/Qwen3-8B
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export HF_HUB_DISABLE_PROGRESS_BARS=1 TQDM_DISABLE=1
export PYTHONDONTWRITEBYTECODE=1 TOKENIZERS_PARALLELISM=false
export HF_HOME="$SLURM_SUBMIT_DIR/hf-cache"
PYTHON=/abr/coss40/replica-llm-venv/bin/python
"$PYTHON" -B -u experiments/warmup_timing/verify_gpu.py > preflight.json 2>/dev/null
exec "$PYTHON" -B -u experiments/warmup_timing/measure_gpu.py \
  --schedule run-inputs/schedule.json --output-dir results 2>/dev/null
