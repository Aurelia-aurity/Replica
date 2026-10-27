#!/bin/bash
#SBATCH -p p02
#SBATCH --nodelist=DIS02
#SBATCH --gres=gpu:1
#SBATCH --time=01:30:00
#SBATCH --job-name=replica-prompt-exp-002
#SBATCH --output=/abr/coss40/prompt-exp-002/slurm-%j.out
set -eu
# A full repository checkout at this path is required; not the #55 standalone bundle.
cd /abr/coss40/prompt-exp-002
hostname
source /abr/coss40/replica-llm-venv/bin/activate
python experiments/results/2026-10-25-issue58-prompt-round2/source/run_prompt_exp.py
echo "EXIT 0"
