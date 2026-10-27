#!/bin/bash
#SBATCH -p p02
#SBATCH --nodelist=DIS02
#SBATCH --gres=gpu:1
#SBATCH --time=03:00:00
#SBATCH --job-name=replica-prompt-exp-002-r6
#SBATCH --output=/abr/coss40/prompt-exp-002/slurm-%j.out
set -eu
# A full repository checkout at this path is required.
# 160 generations plus 6 warmups; same allocation as round 5.
cd /abr/coss40/prompt-exp-002
hostname
source /abr/coss40/replica-llm-venv/bin/activate
python experiments/results/2026-10-25-issue58-prompt-round2/source/run_prompt_exp.py --round 6
echo "EXIT 0"
