#!/bin/bash
# Submit from the repository root; select partition/node/time with sbatch flags.
#SBATCH --job-name=replica-baseline-api
#SBATCH --gres=gpu:1
set -euo pipefail

: "${SLURM_JOB_ID:?Run inside a Slurm GPU allocation}"
: "${SLURM_SUBMIT_DIR:?Submit from the repository root}"
: "${REPLICA_PYTHON:?Set the GPU venv Python absolute path}"
: "${REPLICA_MODEL_PATH:?Set the downloaded local Qwen model directory}"
: "${REPLICA_PORT:?Choose an unused loopback port}"
cd "$SLURM_SUBMIT_DIR"
exec "$REPLICA_PYTHON" -u ai/src/server.py \
    --model-path "$REPLICA_MODEL_PATH" --port "$REPLICA_PORT" "$@"
