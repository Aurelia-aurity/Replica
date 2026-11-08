#!/bin/bash
#SBATCH --job-name=lora88-dev
#SBATCH --partition=p02
#SBATCH --nodelist=DIS02
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=64G
#SBATCH --time=03:00:00
#SBATCH --output=logs/lora88-%j.log
set -euo pipefail
cd /abr/coss40/lora-88/dev-v1
exec 9>/abr/coss40/lora-88/dev-v1/gpu-owner.lock
flock -n 9
other="$(squeue -h -u coss40 -o '%i' | awk -v own="$SLURM_JOB_ID" '$0 != own')"
if [ -n "$other" ]; then
  printf '%s\n' 'Another school job exists; refusing overlap.' >&2
  exit 1
fi
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
export PYTHONDONTWRITEBYTECODE=1
/abr/coss40/replica-llm-venv/bin/python -u -m experiments.lora88.dev_job \
  --root /abr/coss40/lora-88/dev-v1
