# Issue 100 - Source-only synthetic DEV tools

**FROZEN 2026-11-09.** Fixed seed `10009112026`; 1320 synthetic families.
The user's "지금 고정 + 생성" decision authorizes this criteria/data/judge-route
freeze and ONE school GPU generation job, not judging or service changes.
B/A/C imply 3960 planned generations, not already executed answers.
The separately authorized #95 DEV semantic-annotator check is validation only.
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

Generation and later scoring commands (scoring remains separately authorized):

```bash
python experiments/stage1-100/run100.py PACK MODEL FREEZE_RECEIPT NEW_OUTPUT
python experiments/stage1-100/scoring100.py PLAN RESOLVED_ANNOTATIONS NEW_SCORE_JSON
```

The runner requires the approved freeze receipt, criteria without
a draft first-line marker, `STAGE100_EXECUTE_DEV=YES`, and an existing explicitly selected node
Slurm allocation. Submit `dev.sbatch` only under the single-job authorization
and with an empty `squeue -h -u "$SLURM_LOGIN_USER"`. Existing training environment
only; scripts do not install packages or call judges.

`CRITERIA.md` is committed as the frozen criteria before generation.
The fixed primary360 uses one-sided exact McNemar alpha .05 plus >=5 percentage
point error reduction. Four paired-harm CP NI gates use Bonferroni alpha .0125,
margins .03 grounding/core and .05 safe/chat. C is diagnostic only.
`ANNOTATION_RUBRIC.md` defines blinded semantic speaker/time annotations.
Selected judge execution uses the local judge-v2 bridge
`.local-work/judge-v2/stage100_route.py`: grounding20/style20, lean envelope,
unchanged quality rubric/screen/audit/fallback, separate semantic annotation20,
then symmetric discordant-pair-only two-repeat Terra majority.
Private sidecars, annotation prompts and provider transport remain local as in
#95. `adjudication100.py` contains only provider-free selection/majority logic.
`CRITERIA.md` and `.local-work/issue100/PREFREEZE.md` define the policy, cost
sensitivities and limitations. Exact route identity is recorded in
`.local-work/issue100/JUDGE_ROUTE_IDENTITY.json`; it is not a freeze receipt.
Its pre-freeze status string and the pack's preparation status remain unchanged
to preserve their hashes; the separate receipt binds their frozen use.
The identity-covered annotation rubric likewise retains pre-freeze wording
byte-for-byte; the frozen criteria/receipt supersede its preparation status.
Human anchor labeling is skipped by user decision and recorded as a judge
limitation: repeat agreement and agent spot checks do not establish accuracy.

Future opt-in local commands, NOT authorization to run #100 judges now:

```bash
export PYTHONPATH=experiments/rag:experiments/stage1-95:experiments/stage1-100:.local-work/judge-v2
python .local-work/judge-v2/stage100_route.py prepare DATA PACK GENERATIONS NEW_PREPARED
python .local-work/judge-v2/stage100_route.py live PREPARED NEW_OUTPUT ROUTE_IDENTITY \
  --authorization EXPLICIT_FUTURE_AUTHORIZATION \
  --max-jev N --max-grounding N --max-style N \
  --max-semantic-jev N --max-semantic-terra N --max-readjudication N
```

All budgets default to zero; receipt caching cannot collapse independent
re-adjudication runs because each repeat has distinct opaque IDs and calls.
The live route rejects changed manifest inputs and code/rubric identities.

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
