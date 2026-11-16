#!/bin/bash
# Prepared only: submit after the other GPU job ends and launch is authorized.
#SBATCH --job-name=replica-persona-smoke
#SBATCH --gres=gpu:1
set -euo pipefail

: "${SLURM_JOB_ID:?Run inside a Slurm GPU allocation}"
: "${SLURM_SUBMIT_DIR:?Submit from the staged repository root}"
: "${REPLICA_PYTHON:?Set the existing GPU venv Python absolute path}"
: "${REPLICA_MODEL_PATH:?Set the verified local Qwen3-8B snapshot directory}"
export REPLICA_MODEL_ID=Qwen/Qwen3-8B
export REPLICA_MODEL_REVISION="${REPLICA_MODEL_REVISION:-b968826d9c46dd6066d109eabc6255188de91218}"
export HF_HUB_OFFLINE=1
export HF_HUB_DISABLE_PROGRESS_BARS=1
export TQDM_DISABLE=1
cd "$SLURM_SUBMIT_DIR"
exec "$REPLICA_PYTHON" -u ai/scripts/smoke_persona.py \
    --model-path "$REPLICA_MODEL_PATH" "$@"
