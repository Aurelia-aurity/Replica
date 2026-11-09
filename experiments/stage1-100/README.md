# Issue 100 - Source-only synthetic DEV tools

**DRAFT / NOT FROZEN.** Fixed seed `10009112026`; 1320 synthetic families.
No generation, judges, remote execution or service contract changes belong to
this source-only step. B/A/C imply 3960 later generations, not executed answers.
Source manifests are reproducibility checks, not freeze receipts.

## Arms and artifacts

- B uses the unchanged #95 whole-question retrieval v2/r1 path.
- A changes only the provider/other line gloss (O3) in model-facing content.
- C uses required original persona source chunks, empty for safe/chat.

`dev-v3/corpus.json` contains source records and public questions;
`plans.json` has predeclared membership only. `identity-ledger.json` maps
selected synthetic speakers; provider header normalization to `나` is shared
across arms. `gold-context.json` is only the C intervention.
`evaluation-sidecar.json` holds source witnesses, subjects/event times,
required supports/surfaces, corrections and unknowns; preparation never opens it.
No #95 response file or external/private corpus is an authoring input.

## Commands

Run from repository root with Python 3.12 and the existing RAG requirements:

```bash
export PYTHONPATH=experiments/rag:experiments/stage1-95:experiments/stage1-100
python experiments/stage1-100/generate100.py NEW_DATA_DIRECTORY
python experiments/stage1-100/source_qa100.py DATA NEW_QA_JSON --tokenizer-path LOCAL_TOKENIZER_DIR
python experiments/stage1-100/prepare100.py DATA KURE_SNAPSHOT TOKENIZER_DIR NEW_OUTPUT_DIRECTORY
```

The authoring CLI refuses overwrite. Preparation uses local pinned KURE on CPU
and the native Qwen3-8B tokenizer; it preserves B's evidence selection and fails
if A overflows rather than independently pruning it. All source/code/tokenizer
hashes and rendered-to-source IDs are recorded. QA enumerates every possible
family-local retained subset for token caps without needing embedding retrieval.
Full KURE retrieval/prepared packs remain separate from source-only QA.

Prepared future commands (not authorized by these documents):

```bash
python experiments/stage1-100/run100.py PACK MODEL FREEZE_RECEIPT NEW_OUTPUT
python experiments/stage1-100/scoring100.py PLAN RESOLVED_ANNOTATIONS NEW_SCORE_JSON
```

The runner requires a separate future approved freeze receipt, criteria without
a draft first-line marker, `STAGE100_EXECUTE_DEV=YES`, and an existing explicitly selected node
Slurm allocation. `dev.sbatch` is prepared only; never submit without separate
authorization and an empty `squeue -h -u "$SLURM_LOGIN_USER"`. Existing training environment
only; scripts do not install packages or call judges.

`CRITERIA.md` must remain uncommitted and marked **DRAFT - NOT FROZEN** now.
The fixed primary360 uses one-sided exact McNemar alpha .05 plus >=5 percentage
point error reduction. Four paired-harm CP NI gates use Bonferroni alpha .0125,
margins .03 grounding/core and .05 safe/chat. C is diagnostic only.
`ANNOTATION_RUBRIC.md` defines blinded semantic speaker/time annotations.
Actual unchanged judge policy hashes are a future freeze prerequisite.

## Tests

```bash
PYTHONPATH=experiments/rag:experiments/stage1-95:experiments/stage1-100 \
  uv run --no-project --python 3.12 --with pytest \
  --with-requirements experiments/rag/requirements.txt \
  python -m pytest experiments/stage1-100 experiments/stage1-95 -q -p no:cacheprovider
```

Tests cover B message byte identity against the actual #95 preparation path,
O3 metadata-driven changes, fixed source counts/diversity/leakage, C source
identity, manifest hashes, execution denial, and hand-computed scoring math.
Synthetic source fixtures and scores are not measured model quality.
