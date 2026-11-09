#!/bin/bash
#SBATCH --job-name=model77-decision-v3
#SBATCH --partition=p02
#SBATCH --nodelist=DIS02
#SBATCH --gres=gpu:1
#SBATCH --time=04:00:00
#SBATCH --output=/abr/coss40/model-77/decision-v3-%j.out
set -euo pipefail
cd /abr/coss40/model-77/inputs-v3
test "${MODEL77_RUN_ID:?Must explicitly select decision-v3}" = decision-v3
test -f decision-authorization.json
sha256sum -c SHA256SUMS
if [ "$(squeue -h -u coss40 -t RUNNING,PENDING | wc -l)" -gt 1 ]; then
    echo "Another school GPU job exists; refusing concurrent run" >&2
    exit 1
fi
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
export PYTHONPATH="/abr/coss40/rag-59/.pydeps${PYTHONPATH:+:$PYTHONPATH}"
for arm in BASE CAND DIAG; do
    model=Qwen3-8B
    if [ "$arm" = CAND ]; then model=Qwen3-14B; fi
    status=0
    /abr/coss40/replica-llm-venv/bin/python experiments/model77/decision.py \
        --run-id decision-v3 --arm "$arm" --model "$model" \
        --bundle "arms/$arm/bundle.json" --dev-bundle "arms/$arm/dev/bundle.json" \
        --audit "arms/$arm/audit/audit.json" --stress "arms/$arm/audit/stress-$model.json" \
        --output "/abr/coss40/model-77/results/decision-v3/$arm" \
        --authorize-decision decision-authorization.json || status=$?
    if [ "$status" -ne 0 ]; then
        echo "ARM_FAILED $arm exit=$status" >&2
        if [ "$arm" != DIAG ]; then exit "$status"; fi
        echo "DIAG_FAILURE_INFORMATIONAL"
    fi
done
echo "MODEL77_SELECTION_GENERATION_EXIT=0"
