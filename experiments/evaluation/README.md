# Evaluation metrics (#36)

This tool keeps three axes separate: human semantic judgments, deterministic
machine checks, and operational measurements. There is no composite quality
score or service acceptance threshold. The original #33 dataset, settings,
responses and grades remain the historical reference.

## Scope and fixed inputs

- `human.json`: 20 free-response synthetic cases. Humans annotate first-round
  answers for six grounding metrics. No automatic semantic labeling.
- `record_qa.json`: 20 separate JSON tasks using the same material. Exact keys;
  string/null values only; one object; no duplicate/extra/missing keys, fences,
  prose, or repairs. String values use NFC and outer-whitespace removal only.
- `instructions.json`: 20 literal tasks: 12 single, eight compound. Contains,
  absent, line count and whole-response prefix. NFC; substring matching; strict
  prefix spaces; CRLF/CR → LF; remove at most one terminal LF. Empty/failed
  attempts fail every rule. These are project checks, not official IFEval.
- `release.json`: hashes of confirmed inputs and semantic annotation contract.
- `kobest-lock.json`: official harness/data revisions and full test hashes.

QA whole accuracy, format validity and per-question slot accuracy are separate.
Known/null subgroups average eligible per-question ratios equally, with correct
slot, eligible slot and eligible question counts. Zero denominators are null
(N/A). Do not add overlapping human unsupported/contradiction rates together.

Generation inherits the baseline protocol: fixed Korean system prompt, greedy,
160-token main cap, three warmups, three rounds in fixed order. Each suite loads
the model independently and starts a fresh system/user turn per attempt.
There are 180 main attempts across suites, not 180 independent cases. Successful
token-limit responses are scored as truncated; 320-token supplements are stored
separately. The journal phase `baseline` denotes scheduled main attempts, even
for the new suites. First outcomes remain authoritative. There is no automatic
generation retry/resume; diagnostics require a distinct run and must not replace
main outcomes. Interrupted/unexecuted slots remain pending.

## Generation on the school GPU

Use the already observed baseline environment: Python 3.12.7, CUDA-capable RTX
A6000, torch 2.7.1+cu118, transformers 4.57.1, accelerate 1.15.0, safetensors
0.8.0. The user ran all three suites on DIS02 on 2026-10-13; returned logs
were revalidated locally. `SLURM_JOB_ID`, a virtual environment, CUDA and
native bf16 support are required. No packages are installed by these scripts.

Run from the extracted bundle root. Existing output directories are rejected.

```bash
source /abr/coss40/replica-llm-venv/bin/activate
python experiments/evaluation/run.py --suite human --model-path /abr/coss40/replica-models/Qwen2.5-7B-Instruct --output experiments/results/issue36-human-001
python experiments/evaluation/run.py --suite record_qa --model-path /abr/coss40/replica-models/Qwen2.5-7B-Instruct --output experiments/results/issue36-qa-001
python experiments/evaluation/run.py --suite instructions --model-path /abr/coss40/replica-models/Qwen2.5-7B-Instruct --output experiments/results/issue36-instructions-001
python experiments/evaluation/report.py --run experiments/results/issue36-human-001 --output experiments/results/issue36-human-scoring-001
python experiments/evaluation/report.py --run experiments/results/issue36-qa-001 --output experiments/results/issue36-qa-scoring-001
python experiments/evaluation/report.py --run experiments/results/issue36-instructions-001 --output experiments/results/issue36-instructions-scoring-001
```

`report.json` retains all rounds, pending slots and operational scope. A full
three-round mean exists only when every scheduled main outcome is recorded.
Generation failures are execution failures but zero machine credit when
attempted. They remain missing for the six human content metrics and fail human
whole-case/pair success. Human labels that are pending remain pending.
Model artifact hashes and evaluator source hashes identify runs; model names
alone do not establish identical weights. Reports and labels use new paths.

## Human annotations

Edit a copy of `human-labels.json`, preserving all response/identity/hash fields.
Give every unique final personal proposition a `claims` entry with `claim`,
`answer_span`, `label` (supported/unsupported/contradicted), and `mode`
(asserted/speculative). Quotations/retracted claims are excluded; note those
decisions. Speculative personal facts still require evidence. Contradicted
claims also count as unsupported.

Annotate each known slot as correct/incorrect/omitted/over_abstained and each
unknown slot's explicit abstention as true/false. Preserve spans and evidence in
the supplied fields. Mark misleading-premise handling and unresolved internal
contradiction; add notes, then set both review flags. The tool aggregates these
human judgments; it does not assess their semantic validity. The grading file
omits the model name, but baseline outputs already seen are not blind.

```bash
python experiments/evaluation/report.py --run experiments/results/issue36-human-001 --labels /path/to/completed-human-labels.json --output experiments/results/issue36-human-graded-001
# Optional retrospective G01–G05 annotation; original #33 grades are untouched.
python experiments/evaluation/report.py --retrospective-grounding --run /path/to/baseline-run-001 --output experiments/results/issue36-baseline-grounding-001
```

Existing 20 quality scores retain four equal-weight categories: naturalness,
instruction following, usefulness and grounding. Do not replace them with the
six grounding metrics. New grounding pairs each have five planned pairs; both
sides must pass. Shared originals are reused and are not new independent cases.

The completed #36 expansion has 20 user overall ratings, not four new category
scores. Semantic annotations were authored by the assistant with user decisions
on ambiguous cases; they are not an independent human reassessment or objective
semantic ground truth. The pre-run contract's planned blind grading was not
achieved. Keep this deviation visible without rewriting the frozen release.

## KoBEST: separate environment and protocol

Official task definitions:
[lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness/tree/d6de81643928d653435c431bae19945d41d32520/lm_eval/tasks/kobest).
Data: [skt/kobest_v1](https://huggingface.co/datasets/skt/kobest_v1/tree/a5ea15e3ac77ed694b79f6204eb31889a2ba989f).
The pinned code requires Python >=3.10. The actual Python 3.12.7 GPU smoke and
full test completed; `pip check` reported no broken requirements. Use a separate
venv so benchmark installation does not change the baseline environment.

In the observed school setup, ABRM02 could download dependencies but DIS02 had
DNS failures. The baseline interpreter targets `/raid/coss40/anaconda3/bin/python3`,
which existed only on DIS02. Activating that venv on ABRM02 did not make its
interpreter usable. Prepare data/wheels on ABRM02 with its available Python,
then create and install the benchmark venv on an allocated DIS02 node.
Commands below document the successful setup; use fresh directories on a new
installation. The failed empty `issue36-kobest-data` directory was preserved.

```bash
# Login/preparation node: public data download, no GPU needed.
python experiments/evaluation/kobest.py prepare --data-dir /abr/coss40/issue36-kobest-data-002
git clone https://github.com/EleutherAI/lm-evaluation-harness.git /abr/coss40/issue36-lm-eval
git -C /abr/coss40/issue36-lm-eval checkout d6de81643928d653435c431bae19945d41d32520
python -m pip wheel --no-deps --wheel-dir /abr/coss40/issue36-kobest-wheels /abr/coss40/issue36-lm-eval rouge-score sqlitedict word2number setuptools wheel
python -m pip download --dest /abr/coss40/issue36-kobest-wheels --find-links /abr/coss40/issue36-kobest-wheels --only-binary=:all: --python-version 3.12 --constraint experiments/evaluation/runtime-constraints.txt "lm_eval==0.4.14.dev0" "transformers==4.57.1"
```

```bash
# Allocated DIS02 node. --system-site-packages alone inherits Anaconda, not
# replica-llm-venv. Disable that inheritance and add only the baseline library.
/abr/coss40/replica-llm-venv/bin/python -m venv --system-site-packages /abr/coss40/replica-kobest-venv
source /abr/coss40/replica-kobest-venv/bin/activate
python - <<'PY'
from pathlib import Path
import sysconfig
cfg = Path('/abr/coss40/replica-kobest-venv/pyvenv.cfg')
cfg.write_text(cfg.read_text().replace('include-system-site-packages = true', 'include-system-site-packages = false'))
(Path(sysconfig.get_path('purelib')) / 'replica-baseline.pth').write_text('/abr/coss40/replica-llm-venv/lib/python3.12/site-packages\n')
PY
python -c "import torch, transformers, accelerate, safetensors; print(torch.__version__, transformers.__version__, accelerate.__version__, safetensors.__version__); print(torch.cuda.is_available(), torch.cuda.is_bf16_supported())"
python -m pip install --no-index --find-links /abr/coss40/issue36-kobest-wheels setuptools wheel
python -m pip install --no-index --find-links /abr/coss40/issue36-kobest-wheels --no-build-isolation --constraint experiments/evaluation/runtime-constraints.txt -e /abr/coss40/issue36-lm-eval
python -m pip check
python -m pip freeze --all > /abr/coss40/issue36-kobest-packages.txt
# Allocated GPU node, in the separate venv. Smoke results cannot be final scores.
python experiments/evaluation/kobest.py run --smoke --harness /abr/coss40/issue36-lm-eval --data-dir /abr/coss40/issue36-kobest-data-002 --model-path /abr/coss40/replica-models/Qwen2.5-7B-Instruct --output experiments/results/issue36-kobest-smoke-001
python experiments/evaluation/kobest.py run --harness /abr/coss40/issue36-lm-eval --data-dir /abr/coss40/issue36-kobest-data-002 --model-path /abr/coss40/replica-models/Qwen2.5-7B-Instruct --output experiments/results/issue36-kobest-001
```

The wrapper uses official preprocessing/prompts/choices and raw likelihoods:
0-shot, no chat template, no system instruction, batch size one, bf16, eager
attention, no generation cap or three-round repetition. Only data transport
changes to pinned local JSONL test files. All five full test sets are required:
BoolQ 1,404, COPA 1,000, HellaSwag 500, SentiNeg 397, WiC 1,260 (total 4,561).
Native accuracy is reported; HellaSwag acc_norm is retained. Optional upstream
F1 is omitted to avoid a separate remote metric-module download. The five-task
equal mean is a project aggregate, not an official benchmark aggregate.

Calculation errors produce no final scores. After resolving the cause, the
same command with `--resume` may reuse the original likelihood cache only if
weights, source/config, Python and all installed package versions match.
Completed output cannot resume. Smoke/full identities cannot mix. The original
failed attempt is retained. Cached request counts are not completed-document
counts; only validated full sample coverage can produce final scores.

The first full school attempt hit its Slurm time limit after all 10,122 choice
requests, before a final report existed. A new allocation with `--resume` and
the same output directory reused the cache and produced validated results for
all 4,561 documents. In an extracted non-Git bundle, the upstream Git hash probe
printed `fatal: not a git repository`; exit 0 and validated report coverage,
not this diagnostic alone, established completion. Wrapper source hashes and
the clean pinned harness revision were checked separately. The freeze file
remains on the school host; the returned identity contains package inventory.

Resume identity preserves every visible package distribution, including venv
overlays and priority, rather than overwriting duplicate package names. Collected
results validate every expected choice likelihood and binary sample metric;
HellaSwag acc_norm is required and checked against its sample mean.

## Model comparison profiles (#37)

`model-profiles.json` pins each compared model's repo id and revision, chat
template arguments and the KoBEST BOS policy. Baseline profile path:
`/abr/coss40/replica-models/Qwen2.5-7B-Instruct`. `--profile` records these in the
run config/identity; collection rejects mixed profiles across suites/KoBEST.
Without `--profile`, configs, identities and behavior equal the #36 baseline.
The declared revision is not proof of the local weights; artifact hashes are.

- `qwen3-8b`: `enable_thinking=False`; its template otherwise starts in thinking
  mode and spends the 160-token budget on `<think>`.
- `kanana-1.5-8b-instruct-2505`: Llama-family BOS. Generation templates already
  emit `bos_token`; KoBEST uses `add_bos_token=True` (Qwen-family: `False`).
- `ax-4.0-light`: generation `eos_token_id` 27 is `<|im_end|>`. Its config sets
  `use_cache: false`; the runner's fresh `GenerationConfig` keeps the default
  cache. Confirm throughput in the GPU smoke run.
- All models: `generate(..., use_model_defaults=False)`. transformers 4.57.1
  otherwise refills default-valued fields from a model's `generation_config`
  when it was saved with >=4.50 (Qwen3: `do_sample=True`, temperature/top-p/top-k),
  silently replacing greedy decoding. The #36 Qwen2.5 config predates 4.50, so
  its behavior is unchanged.

Download on ABRM02 (DIS02 has no DNS), then run each suite per profile:

```bash
cd /abr/coss40/replica-models
# Baseline reuses the existing Qwen2.5-7B-Instruct folder (download metadata
# revision a09a354...); its artifact hashes are recorded per run.
huggingface-cli download skt/A.X-4.0-Light --revision ba21c20ea1b31ded1ec3e2fb432335077dc4be98 --local-dir A.X-4.0-Light
huggingface-cli download skt/A.X-3.1-Light --revision 9b41bb2406472634d8812c0b8931fa40fa9a6c3a --local-dir A.X-3.1-Light
huggingface-cli download Qwen/Qwen3-8B --revision b968826d9c46dd6066d109eabc6255188de91218 --local-dir Qwen3-8B
huggingface-cli download Qwen/Qwen3-4B-Instruct-2507 --revision cdbee75f17c01a7cc42f958dc650907174af0554 --local-dir Qwen3-4B-Instruct-2507
huggingface-cli download kakaocorp/kanana-1.5-8b-instruct-2505 --revision c963a5f4f6496c749f94064a20b33028b0db9f19 --local-dir kanana-1.5-8b-instruct-2505
```

```bash
# Example for one profile; repeat with the matching profile/folder pair.
P=qwen3-8b M=/abr/coss40/replica-models/Qwen3-8B
for s in human record_qa instructions; do
  python experiments/evaluation/run.py --suite $s --profile $P --model-path $M --output experiments/results/issue37-$P-$s-001
done
python experiments/evaluation/kobest.py run --profile $P --harness /abr/coss40/issue36-lm-eval --data-dir /abr/coss40/issue36-kobest-data-002 --model-path $M --output experiments/results/issue37-$P-kobest-001
```

## Collection and local checks

```bash
python experiments/evaluation/collect.py --human-run experiments/results/issue36-human-001 --qa-run experiments/results/issue36-qa-001 --instructions-run experiments/results/issue36-instructions-001 --human-labels /path/to/completed-human-labels.json --kobest-run experiments/results/issue36-kobest-001 --output experiments/results/issue36-collected-001
python -m unittest discover -s experiments/evaluation/tests -v
python -m unittest discover -s experiments/baseline/tests -v
# Optional CPU-only API integration in the benchmark venv; synthetic likelihoods.
python experiments/evaluation/tests/check_kobest_api.py --harness /abr/coss40/issue36-lm-eval --data-dir /abr/coss40/issue36-kobest-data-002
```

Without labels or KoBEST results, collection reports those dependencies as
pending. `evaluation_data_complete` describes observed data coverage only;
issue acceptance also requires human verification, required independent review
and recording actual school results. Mock runs never complete that flag.
Timing covers synchronized GPU generation only. Warmups, supplements and
diagnostics are excluded. TTFT, end-to-end time and whole-GPU VRAM are not
measured; allocator peaks are labeled accordingly.

## Completed current-model reference

[2026-10-13 Qwen2.5-7B-Instruct results](../results/2026-10-13-qwen2.5-7b-evaluation/README.md)
contain the source logs, user ratings, explicitly attributed semantic judgments,
machine reports and limits. The result directory has a CPU-only verification
command that regrades published generation logs and validates the projected
KoBEST sample metrics. Full `collect.py` additionally needs the original harness
result retained outside Git; the projection does not bypass its hash checks.
KMMLU is deferred because this release focuses on record grounding and literal
instruction following; knowledge evaluation can be scoped separately. These
project rules are not an official IFEval score or a service acceptance threshold.
