#!/bin/bash
#SBATCH -p p02
#SBATCH --nodelist=DIS02
#SBATCH --gres=gpu:1
#SBATCH --time=07:30:00
#SBATCH --job-name=replica-prompt-exp-002-r7
#SBATCH --output=/abr/coss40/prompt-exp-002/slurm-%j.out
set -eu
# A full repository checkout at this path is required.
# 400 generations plus 15 warmups; 2.5 times round 6's allocation.
cd /abr/coss40/prompt-exp-002
hostname
source /abr/coss40/replica-llm-venv/bin/activate
python experiments/results/2026-10-25-issue58-prompt-round2/source/run_prompt_exp.py --round 7
echo "EXIT 0"
