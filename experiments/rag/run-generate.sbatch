#!/bin/bash
#SBATCH -p p02
#SBATCH --nodelist=DIS02
#SBATCH --gres=gpu:1
#SBATCH --time=07:30:00
#SBATCH --job-name=replica-rag-59-generate
#SBATCH --output=slurm-rag59-%j.out
set -eu
# Submit from an existing directory. No QoS override was present in #58.
# Defaults are #58 paths, NOT a claim that the #59 files are already there.
REPLICA_BASE="${REPLICA_BASE:-/abr/coss40/prompt-exp-002}"
REPLICA_VENV="${REPLICA_VENV:-/abr/coss40/replica-llm-venv}"
REPLICA_MODEL="${REPLICA_MODEL:-/abr/coss40/replica-models/Qwen3-8B}"
REPLICA_PROMPT_VERSION="${REPLICA_PROMPT_VERSION:-r1}"
REPLICA_CONDITION_SET="${REPLICA_CONDITION_SET:-abc}"
REPLICA_CORPUS="${REPLICA_CORPUS:-$REPLICA_BASE/experiments/rag/corpus.json}"
REPLICA_EVIDENCE="${REPLICA_EVIDENCE:-$REPLICA_BASE/experiments/rag/results/kure/evidence.json}"
REPLICA_OUTPUT="${REPLICA_OUTPUT:-$REPLICA_BASE/experiments/rag/.cache/generation-${SLURM_JOB_ID}}"
cd "$REPLICA_BASE"
hostname
source "$REPLICA_VENV/bin/activate"
# Optional extra site dir for pydantic/typer when the shared venv lacks them.
if [ -n "${REPLICA_PYDEPS:-}" ]; then export PYTHONPATH="$REPLICA_PYDEPS${PYTHONPATH:+:$PYTHONPATH}"; fi
python experiments/rag/generate.py --model-path "$REPLICA_MODEL" --output "$REPLICA_OUTPUT" --prompt-version "$REPLICA_PROMPT_VERSION" --corpus "$REPLICA_CORPUS" --evidence "$REPLICA_EVIDENCE" --condition-set "$REPLICA_CONDITION_SET"
echo "EXIT 0"
