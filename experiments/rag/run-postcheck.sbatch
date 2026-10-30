#!/bin/bash
#SBATCH -p p02
#SBATCH --nodelist=DIS02
#SBATCH --gres=gpu:1
#SBATCH --time=07:30:00
#SBATCH --job-name=replica-rag-59-pc1
#SBATCH --output=slurm-rag59-pc1-%j.out
set -eu
REPLICA_BASE="${REPLICA_BASE:-/abr/coss40/prompt-exp-002}"
REPLICA_VENV="${REPLICA_VENV:-/abr/coss40/replica-llm-venv}"
REPLICA_MODEL="${REPLICA_MODEL:-/abr/coss40/replica-models/Qwen3-8B}"
: "${REPLICA_SOURCE_DIR:?Set the existing generation directory}"
: "${REPLICA_OUTPUT:?Set a fresh pc1 output directory}"
: "${REPLICA_CORPUS:?Set the corpus matching the source}"
REPLICA_SPLIT="${REPLICA_SPLIT:-dev}"
REPLICA_CONDITIONS="${REPLICA_CONDITIONS:-}"
cd "$REPLICA_BASE"
hostname
source "$REPLICA_VENV/bin/activate"
if [ -n "${REPLICA_PYDEPS:-}" ]; then export PYTHONPATH="$REPLICA_PYDEPS${PYTHONPATH:+:$PYTHONPATH}"; fi
python experiments/rag/postcheck.py \
    --source-dir "$REPLICA_SOURCE_DIR" --output "$REPLICA_OUTPUT" \
    --corpus "$REPLICA_CORPUS" --split "$REPLICA_SPLIT" \
    --conditions "$REPLICA_CONDITIONS" --model-path "$REPLICA_MODEL"
echo "EXIT 0"
