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

- Phase A used small synthetic fixtures; Phase B adds full deterministic-source checks.
- **Not Verified in Phase A**: Live GPU execution, full 360-question encoder pack preparation, and quality judges are unverified. Full corpus authorship and a hash freeze are required before eventual execution.

## Phase B deterministic feasibility DEV

`generate95.py` authors 360 new event families and personas, with one question
each, using seed `9508112026`. Each type/register cell has 20 questions and
10 predeclared self-honorific probes (180 total). The frozen source artifacts
are in `feasibility-dev-v2/`; `CRITERIA.md` is unchanged.

```bash
export PYTHONPATH=experiments/rag:experiments/stage1-95
# A new directory is required; never overwrite the committed freeze.
python experiments/stage1-95/generate95.py NEW_DATA_DIRECTORY
python experiments/stage1-95/prepare_feasibility.py \
  experiments/stage1-95/feasibility-dev-v2 \
  KURE_SNAPSHOT TOKENIZER_DIRECTORY NEW_OUTPUT_DIRECTORY
```

`corpus.json` contains the factual archive and public questions without oracle
fields. `style-sources.json` contains separately verified persona utterances
and is excluded from every factual retrieval index. `plans.json` contains
literal question-only spans and register/intent annotations.
`gold-context.json` supplies only the C intervention evidence.
`evaluation-sidecar.json` holds gold support IDs, slot surfaces, expected
omissions, conflicts and probe/cohort membership; preparation never reads it.
`event-ledger.json` records the authored event tuples for diversity audits.

The bank contains eight generic utterances per register/intent cell.
Family-specific seeded selection chooses two. Only 72 predeclared leakage
probes (four per type/register cell, zero-based member index modulo 5 equal
to 1) replace one generic example with an unrelated factual example.
The other 288 S prompts use generic examples with no event/place/object facts.

Preparation reuses `build_rows` and `run_dev.validate_pack`, the frozen KURE-v1
snapshot on CPU, and the native Qwen3-8B tokenizer. All arms retain r1:
evidence at most 2,000 tokens, S addition at most 400, and native prompt plus
160 output tokens at most 4,096. `manifest.json` freezes the corpus and oracle;
`preparation-manifest.json` binds the local prepared pack and tokenizer hashes.
The pack itself is a local execution artifact, not a model-answer dataset.

The missing-second-event and already-sufficient omission-risk cohorts are
predeclared challenges, not measured model omissions. Report actual support
recall only after context pruning. Model-answer omissions, style leakage and
quality require separately authorized GPU generation and judging. Phase B
performs neither; it installs no packages and submits no Slurm jobs.
