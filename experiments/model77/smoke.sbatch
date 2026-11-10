#!/bin/bash
#SBATCH --job-name=model77-smoke
#SBATCH --partition=p02
#SBATCH --nodelist=DIS02
#SBATCH --gres=gpu:1
#SBATCH --time=00:40:00
#SBATCH --output=/abr/coss40/model-77/slurm-%j.out
set -uo pipefail
cd /abr/coss40/model-77
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
status=0
for model in Qwen3-8B kanana-2-3b-instruct; do
    /abr/coss40/replica-llm-venv/bin/python experiments/model77/smoke.py \
        "$model" --output "results/$SLURM_JOB_ID/$model.json" || status=1
done
echo "SMOKE_EXIT=$status"
exit "$status"
