# Issue 100 - Source-only synthetic DEV tools

**FROZEN 2026-11-09.** Fixed seed `10009112026`; 1320 synthetic families.
The user's "지금 고정 + 생성" decision authorizes this criteria/data/judge-route
freeze and ONE school GPU generation job, not judging or service changes.
B/A/C imply 3960 planned generations, not already executed answers.
The separately authorized #95 DEV semantic-annotator check is validation only.
Source manifests are reproducibility checks, not freeze receipts.
This sanitized distribution changes operational metadata only. Historical
Git objects, freeze tarballs, and receipts remain unchanged; recorded freeze
digests describe those original bytes, not byte identity with sanitized files.

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

```bash
# Step 1: Queue check immediately before a separately authorized manual Slurm submission
# Replace the synthetic example value with the approved submission login username
export SLURM_LOGIN_USER="operator"
squeue -h -u "$SLURM_LOGIN_USER"
```

> Stop condition: immediately before submitting, replace the synthetic `"operator"` example so `SLURM_LOGIN_USER` is set to the approved submission login username (not the Slurm accounting account), and verify that the `squeue` command above exits with status `0` and outputs zero job rows of any status. If `SLURM_LOGIN_USER` is unset or unverified, the query fails, or any job row of any status is returned, stop immediately and never run the submission block below.

```bash
# Step 2: Single separately authorized manual Slurm submission
# Run only immediately after Step 1 exits 0 with zero job rows of any status
export STAGE100_EXECUTE_DEV=YES
export STAGE100_EXPECTED_NODE="gpu-node-01.example.invalid"
export STAGE100_REPO="/path/to/Replica"
export STAGE100_PACK="/path/to/prepared-pack/pack.json"
export STAGE100_MODEL="/path/to/frozen-model"
export STAGE100_FREEZE_RECEIPT="/path/to/freeze-receipt.json"
export STAGE100_OUTPUT="/path/to/new-generations.jsonl"
export STAGE100_PYTHON="/path/to/training-venv/bin/python"
sbatch --nodelist="$STAGE100_EXPECTED_NODE" experiments/stage1-100/dev.sbatch
```

Generation and later scoring commands (scoring remains separately authorized):

```bash
# Run only inside an existing Slurm allocation; replace the synthetic hostname with the authorized actual allocated node
export STAGE100_EXECUTE_DEV=YES
export STAGE100_EXPECTED_NODE="gpu-node-01.example.invalid"
python experiments/stage1-100/run100.py PACK MODEL FREEZE_RECEIPT NEW_OUTPUT
python experiments/stage1-100/scoring100.py PLAN RESOLVED_ANNOTATIONS NEW_SCORE_JSON
```

Both `dev.sbatch` and `run100.py` require `STAGE100_EXECUTE_DEV=YES`, nonempty
`SLURM_JOB_ID`, and an explicit single-host `STAGE100_EXPECTED_NODE` with no
default or bypass. `STAGE100_EXPECTED_NODE` must be one ASCII hostname (short or
dotted, up to 253 characters, labels 1 to 63 characters using letters, digits,
and interior hyphens; no Slurm host lists, ranges, option strings, spaces, or
trailing dot) and must match `SLURMD_NODENAME` verbatim, including case and
short/dotted form. This is a runtime check against Slurm-provided values, not
independent scheduler attestation. Missing, invalid, or mismatched approval or
node placement fails before reading experiment artifacts (not before module
imports). Neither entry path submits, queries, or requests allocations, and real
site identifiers or operating values must not be printed or committed.

Before one separately authorized manual `sbatch` submission, select the approved
single node outside Git, replace the synthetic `"operator"` example with the
approved submission login username (not the Slurm accounting account) in
`SLURM_LOGIN_USER`, confirm immediately before submitting that `squeue` exits
`0` with zero job rows of any status, export all eight required `STAGE100_*`
variables above, and pass `--nodelist="$STAGE100_EXPECTED_NODE"` to `sbatch`.
Existing Slurm resource options, `STAGE100_PYTHON`, `STAGE100_MODEL`,
`PYTHONPATH`, and CLI path contracts remain unchanged. Scripts do not install
packages or invoke judges, and this document neither claims nor authorizes a new
GPU run.

`run100.py` retains the approved freeze-receipt check and the runtime
`CRITERIA.md` first-line unfrozen-marker guard: it verifies the runtime
`CRITERIA.md` path (not `CONFIRM_CRITERIA.md`), rejects a criteria file whose
first line carries the unfrozen marker, checks prepared-pack, criteria, and
rubric digests against the approved freeze receipt, and then checks code/profile
digests listed in the pack, including `run100.py`. An original unchanged local
receipt, pack, and criteria set matches only the original runner and local
evidence; it cannot validate changed runner code or sanitized criteria. Any
separately authorized future run against changed code requires a new matched
prepared pack, frozen inputs, and approved freeze receipt.

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

## One confirmation (2026-11-10)

`CONFIRM_CRITERIA.md` freezes ONE confirm-v3 run before responses. The same
authoring logic/pools use seed 10009112027 and namespace `s100-c3`; DEV's five
source artifacts still regenerate byte-identically. Freshness is zero identical
joint family combinations and disjoint IDs, not disjoint names/templates.
The source audit records 0 identical families/IDs, 414 reused provider pairs,
and 302 repeated question rows (mostly the unchanged chat templates).

```bash
python experiments/stage1-100/generate100.py NEW_DATA_DIRECTORY \
  --seed 10009112027 --namespace s100-c3
python experiments/stage1-100/confirm_freshness100.py \
  experiments/stage1-100/dev-v3 NEW_DATA_DIRECTORY NEW_FRESHNESS_JSON
# After separately authorized judging, use THIS scorer for the confirmation:
python experiments/stage1-100/scoring_confirm100.py \
  NEW_DATA_DIRECTORY/plans.json RESOLVED_ANNOTATIONS NEW_SCORE_JSON
```

Primary, arms, model, retrieval, rubrics, 20/20/20 route and majority are unchanged.
Only NI changes to signed net paired harm with the Tango score upper bound,
one-sided alpha .0125 and the same margins; old CP harm limits remain descriptive.
The confirmation scorer supplies the frozen output status without modifying the
hash-bound historical DEV scorer. `retry_confirm100.py` generalizes the DEV
checkpoint-resume wrapper: one identical-payload retry for structural or no-label
capacity/transport failure; second failure is INCONCLUSIVE, never dropped labels.
The local prepared command/receipt and submission evidence are kept outside git.
The retry wrapper defaults to the prepared route under the checkout root. When
using a separate checkout without that route, use the existing `--route` option
to point to the same readable file. Missing or unusable routes are setup errors
and produce no run output; the wrapper does not supply a replacement.
No judges, additional generation jobs or service changes are implied.
