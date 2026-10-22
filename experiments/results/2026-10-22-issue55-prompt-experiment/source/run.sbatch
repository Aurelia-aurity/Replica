#!/bin/bash
#SBATCH -p p02
#SBATCH --nodelist=DIS02
#SBATCH --gres=gpu:1
#SBATCH --time=01:30:00
#SBATCH --job-name=replica-prompt-exp
#SBATCH --output=/abr/coss40/prompt-exp-001/slurm-%j.out
set -u
cd /abr/coss40/prompt-exp-001
hostname
source /abr/coss40/replica-llm-venv/bin/activate
python run_prompt_exp.py
echo "EXIT $?"
