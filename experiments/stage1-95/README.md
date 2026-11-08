# Stage 1 (#95) - Offline Phase A

Offline Phase A experiment tools for issue #95. `CRITERIA.md` is frozen separately; this directory makes no service changes.

## Environment & Dependencies

- **Retrieval Import**: Imports existing `experiments/rag` retrieval v2 (`ai/src/rag` does not exist in this checkout).
- **Python Environment**: Operator-supplied existing environment; do not run package installs.
- **Module Path**: Run from repository root with:
  ```bash
  export PYTHONPATH=experiments/rag:experiments/stage1-95
  ```

## Pack Preparation (`prepare95.py`)

```bash
python experiments/stage1-95/prepare95.py CORPUS_PATH PLAN_PATH ENCODER_PATH TOKENIZER_PATH OUTPUT
```

- Writes a new-only four-arm synthetic DEV pack and never generates model answers.
- **Encoder**: Local KURE snapshot pinned to revision `8b418a58414668e75532ed045c22d9ca018ae2b2`.
- **Schema & Tags**: Validated against `DevPlan` in `prepare95.py` and `UtteranceTag` in `exemplars.py`. `item_spans` must be literal question substrings (`multi` and `ambiguity` require at least two planned or recognized queries).
- **Style Exemplars (`S`)**: Requires 2–4 exact persona source turns matching authored `register` and `intent` (treated as style data, not facts). An inadequate pool rejects pack generation.
- **Decomposed Retrieval (`P`)**: Runs unchanged v2 retrieval per item and performs a round-robin whole-record merge under 2,000 tokens. `item_retained_ids` are trace records only and do not prove support recall.

## Execution (`run_dev.py` & `dev.sbatch`)

```bash
STAGE95_EXECUTE_DEV=YES python experiments/stage1-95/run_dev.py PACK MODEL OUTPUT
```

- Requires explicit `STAGE95_EXECUTE_DEV=YES` and an existing `DIS02` Slurm allocation.
- `dev.sbatch` is prepared but not submitted (estimated 90–150 GPU minutes, unmeasured; 180-minute ceiling).
- **Pre-Submission Check**: The operator must verify an empty `squeue` before any separately authorized submission.
- No paid judge is built into the runner.

## Testing & Verification Status

Test command (from repo root):
```bash
PYTHONPATH=experiments/rag:experiments/stage1-95 python -m pytest experiments/stage1-95 -q -p no:cacheprovider
```

- Tests use small synthetic fixtures only, not a full 360-question dataset.
- **Not Verified in Phase A**: Live GPU execution, full 360-question encoder pack preparation, and quality judges are unverified. Full corpus authorship and a hash freeze are required before eventual execution.
