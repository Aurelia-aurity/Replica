#!/bin/bash
#SBATCH --job-name=model77-dev
#SBATCH --partition=p02
#SBATCH --nodelist=DIS02
#SBATCH --gres=gpu:1
#SBATCH --time=02:00:00
#SBATCH --output=/abr/coss40/model-77/decision-%j.out
set -euo pipefail
cd /abr/coss40/model-77
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
export PYTHONPATH="/abr/coss40/rag-59/.pydeps${PYTHONPATH:+:$PYTHONPATH}"
mode="${1:-dev}"
case "$mode" in
    dev) bundle="inputs/dev/bundle.json"; audit="inputs/audit-dev.json"; approval=() ;;
    decision)
        : "${2:?Supply the separately signed decision authorization file}"
        bundle="inputs/heldout/bundle.json"; audit="inputs/audit.json"
        approval=(--authorize-decision "$2") ;;
    *) echo "Expected dev or decision" >&2; exit 2 ;;
esac
if [ "$(squeue -h -u coss40 -t RUNNING,PENDING | wc -l)" -gt 1 ]; then
    echo "Another school GPU job exists; refusing concurrent run" >&2
    exit 1
fi
for model in Qwen3-8B kanana-2-3b-instruct Midm-2.0-Base-Instruct Qwen3-14B kanana-1.5-15.7b-a3b-instruct; do
    /abr/coss40/replica-llm-venv/bin/python experiments/model77/decision.py \
        --model "$model" --bundle "$bundle" --dev-bundle inputs/dev/bundle.json \
        --audit "$audit" --stress "inputs/stress-$model.json" \
        --output "results/${MODEL77_RUN_ID:-$SLURM_JOB_ID}/$model" "${approval[@]}"
done
echo "MODEL77_EXIT=0"
