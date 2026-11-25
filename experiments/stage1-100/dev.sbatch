#!/usr/bin/env bash
# PREPARED ONLY. No submission/freeze authorized by source-only QA.
# Planning estimate 3h20m; with 25-50% headroom 4h10m-5h, excluding queue/judges.
# Manual pre-submit check: set SLURM_LOGIN_USER to the approved submission login
# username, not the Slurm accounting account. Submit only if the username is
# verified and squeue -h -u "$SLURM_LOGIN_USER" succeeds and lists zero jobs.
#SBATCH --job-name=replica100-dev
#SBATCH --partition=p02
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=4
#SBATCH --time=06:00:00

set -euo pipefail
if [[ -z ${SLURM_JOB_ID:-} ]]; then
  printf '%s\n' 'SLURM_JOB_ID: Existing allocation required' >&2
  exit 1
fi
if [[ -z ${STAGE100_EXECUTE_DEV:-} ]]; then
  printf '%s\n' 'STAGE100_EXECUTE_DEV: Separate DEV execution authorization required' >&2
  exit 1
fi
test "$STAGE100_EXECUTE_DEV" = YES
if [[ -z ${STAGE100_EXPECTED_NODE:-} ]]; then
  printf '%s\n' 'STAGE100_EXPECTED_NODE: Explicit expected Slurm node required' >&2
  exit 1
fi
node_label='[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
if [[ ${#STAGE100_EXPECTED_NODE} -gt 253 ||
      ! $STAGE100_EXPECTED_NODE =~ ^$node_label(\.$node_label)*$ ]] ||
   [ "${SLURMD_NODENAME:-}" != "$STAGE100_EXPECTED_NODE" ]; then
  printf '%s\n' 'Expected one hostname matching the allocated Slurm node.' >&2
  exit 1
fi
if [[ -z ${STAGE100_REPO:-} ]]; then
  printf '%s\n' 'STAGE100_REPO: Explicit repository root required' >&2
  exit 1
fi
if [[ -z ${STAGE100_PACK:-} ]]; then
  printf '%s\n' 'STAGE100_PACK: Explicit prepared pack required' >&2
  exit 1
fi
if [[ -z ${STAGE100_FREEZE_RECEIPT:-} ]]; then
  printf '%s\n' 'STAGE100_FREEZE_RECEIPT: Separately approved future freeze receipt required' >&2
  exit 1
fi
if [[ -z ${STAGE100_OUTPUT:-} ]]; then
  printf '%s\n' 'STAGE100_OUTPUT: New output file required' >&2
  exit 1
fi
test "$STAGE100_EXECUTE_DEV" = YES
if [[ -z ${STAGE100_PYTHON:-} ]]; then
  printf '%s\n' 'STAGE100_PYTHON: Absolute training interpreter path required' >&2
  exit 1
fi
if [[ "$STAGE100_PYTHON" != /* || ! -f "$STAGE100_PYTHON" || ! -x "$STAGE100_PYTHON" ]]; then
  printf '%s\n' 'STAGE100_PYTHON must name an executable file by absolute path.' >&2
  exit 1
fi
if ! CDPATH= cd -- "$STAGE100_REPO" >/dev/null 2>&1; then
  printf '%s\n' 'Cannot enter the configured repository directory.' >&2
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$STAGE100_REPO/experiments/rag:$STAGE100_REPO/experiments/stage1-95:$STAGE100_REPO/experiments/stage1-100"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
if [[ -z ${STAGE100_MODEL:-} ]]; then
  printf '%s\n' 'STAGE100_MODEL: Existing frozen model directory required' >&2
  exit 1
fi

# Shell launch errors and library diagnostics may contain operating paths.
# Only the runner's fixed CLI diagnostics cross the batch stderr boundary.
safe_runner_stderr() {
  local line reported=
  shopt -u nocasematch
  while IFS= read -r line || [[ -n $line ]]; do
    case "$line" in
      'Output already exists'|'Freeze receipt file not found'|\
      'Required experiment artifact not found'|'Separate DEV execution authorization is required'|\
      'Expected one explicit Slurm hostname'|'Expected hostname must match the allocated Slurm node'|\
      'Criteria remain unfrozen; no generation allowed'|'Experiment access denied'|\
      'Could not access an experiment artifact'|'Freeze receipt mismatch'|'Prepared code/profile changed'|\
      'Model type/revision differs from pinned #77 profile'|'Tokenizer differs from preparation'|\
      'Invalid experiment data or configuration'|'Experiment runtime failed'|\
      'Invalid arguments. Run with --help for usage.'|\
      'Cannot initialize experiment runner; check the training interpreter and required dependencies.'|\
      'Experiment runner failed; check the runtime dependencies and configured inputs.')
        printf '%s\n' "$line" >&2
        ;;
      *)
        if [[ -z $reported ]]; then
          printf '%s\n' 'Python launch or runtime diagnostic withheld; check the configured interpreter, its permissions, and experiment inputs.' >&2
          reported=1
        fi
        ;;
    esac
  done
}

# Route stderr for the whole launch/wait group, including any diagnostic
# Bash emits when its child exits by signal. Keep stdin and stdout unchanged.
if {
  {
    (exec "$STAGE100_PYTHON" experiments/stage1-100/run100.py -- \
      "$STAGE100_PACK" "$STAGE100_MODEL" "$STAGE100_FREEZE_RECEIPT" "$STAGE100_OUTPUT") 1>&3
  } 2>&1 | safe_runner_stderr
} 3>&1; then
  exit 0
else
  exit "${PIPESTATUS[0]}"
fi
