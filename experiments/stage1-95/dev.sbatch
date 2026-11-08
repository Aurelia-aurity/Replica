#!/usr/bin/env bash
# Prepared only: do not submit until a separate DEV execution is authorized.
# Estimated 90-150 GPU minutes, not measured; 180-minute scheduler ceiling.
# Before submission, manually verify: squeue -u coss40 is empty.
#SBATCH --job-name=replica95-dev
#SBATCH --partition=p02
#SBATCH --nodelist=DIS02
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=4
#SBATCH --time=03:00:00

set -euo pipefail
: "${SLURM_JOB_ID:?Existing Slurm allocation required}"
: "${STAGE95_EXECUTE_DEV:?Separate DEV execution authorization required}"
: "${STAGE95_REPO:?Explicit staged repository root required}"
: "${STAGE95_PACK:?Frozen DEV pack path required}"
: "${STAGE95_OUTPUT:?New output file path required}"
test "$STAGE95_EXECUTE_DEV" = YES
cd "$STAGE95_REPO"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$STAGE95_REPO/experiments/rag:$STAGE95_REPO/experiments/stage1-95"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
exec /abr/coss40/replica-train-venv/bin/python \
  experiments/stage1-95/run_dev.py \
  "$STAGE95_PACK" /abr/coss40/replica-models/Qwen3-8B "$STAGE95_OUTPUT"
