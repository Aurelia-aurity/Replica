# Model 77 GPU load and generation smoke

Operational smoke only, not a model comparison or quality evaluation.
No service model is selected by this run.

## Local checks

From the repository root, without GPU dependencies:

```bash
python3 -m unittest discover -s experiments/model77 -v
python3 -m py_compile experiments/model77/smoke.py
bash -n experiments/model77/smoke.sbatch
```

## School execution

Copy `experiments/model77/{smoke.py,profiles.json,smoke.sbatch}` and
`ai/configs/baseline.json` to `/abr/coss40/model-77/`, preserving paths.
Check `squeue -u coss40` before submitting; only one GPU job may be active.

```bash
cd /abr/coss40/model-77
sbatch experiments/model77/smoke.sbatch
```

The wrapper requests `p02`, `DIS02`, one GPU, and 40 minutes, using the existing
`/abr/coss40/replica-llm-venv/bin/python`. It runs only Qwen3-8B and
kanana-2-3b-instruct, sequentially in separate Python processes so CUDA memory
is released between models. It exits after both processes, retaining failures.
No installs, downloads, servers, or deletion are needed.

Models are loaded offline from `/abr/coss40/replica-models/<name>` with
`trust_remote_code=False`, safetensors, bf16 weights and eager attention.
The runner checks native model type and the local config download metadata
revision when present; missing metadata is explicitly `declared_only`.
Non-baseline models require an exact `DONE <model_id>` log line in
`/abr/coss40/issue77-download.log`; pre-existing Qwen3-8B is the exception.

## Inputs and results

The guarded `ai/configs/baseline.json` system prompt receives a short synthetic
persona/register line. Five synthetic cases cover greeting, one-line evidence
recall, unknown information, false premise, and a 하게체 request.
Generation is greedy with `max_new_tokens=160`, preserving the model's complete
EOS list and disabling model generation defaults. Qwen uses
`enable_thinking=False`; Kanana-2 uses `thinking_mode='no_think'`.
Required non-thinking templates must end in an empty closed think block.

`results/<Slurm-job-id>/<model>.json` retains environment, pinned profile,
local config/template snapshots and hashes, rendered prompts, full outputs
and generated token IDs. It records load seconds, peak allocated/reserved
CUDA bytes, per-case generation seconds, tokens/second and `eos/length/other`
termination. Divide bytes by `1024**3` for GiB; allocator measurements are not
whole-device `nvidia-smi` measurements. No warmup is excluded from these numbers.

Raw decoding is checked before special-token stripping for think/analysis
markers. This cannot detect untagged reasoning prose; inspect all five outputs.
`smoke_ok` requires five nonempty EOS-terminated replies with no marker leakage.
It does not score grounding or register quality.

Midm, Qwen3-14B and Kanana-1.5 profiles are prepared, not run by this wrapper.
Before a parent-authorized follow-up, confirm completed downloads and inspect
each local config, tokenizer template and generation EOS list, particularly
Kanana-1.5's Mixtral template. Do not infer support from a prepared profile.

## Fixed #77 generation runner

`prepare.py` reuses #59's unchanged chunker, CPU embedding/search, retrieval-v2
context restoration, contextual C and r1 renderer. It takes explicit `--rag`,
`--corpus`, `--r1` (historical saved prompts), `--protocol`, `--dev-spec` and
`--output` paths. It does not calibrate thresholds. Output artifacts use exclusive
creation: `evidence.json`, aggregate `coverage.json`, shared `bundle.json` and
`manifest.json`. The bundle freezes a seed-77 question shuffle, interleaved B/C,
semantic messages and the profile-file hash. The model order is Qwen3-8B,
Kanana-2-3B, Midm, Qwen3-14B, Kanana-1.5.

Use the existing #59 environment without installing anything. `audit.py` accepts
`--bundles DEV_BUNDLE HELDOUT_BUNDLE --tokenizers TOKENIZER_DIRECTORY --output DIR`.
It loads tokenizers only, reports every actual native-template input length,
refuses inputs above 4096 and packs six DEV-only stress inputs per model.
Stress contexts repeat original same-persona DEV records in original order;
only delimiter whitespace and the final source-prefix length vary to obtain
exactly 4096 actual tokens. No decision input is padded or truncated.
`audit-dev.json` excludes heldout rows and is the only audit sent for DEV.

The sequential wrapper defaults to DEV:

```bash
cd /abr/coss40/model-77
sbatch experiments/model77/decision.sbatch dev
```

Required remote files are `inputs/dev/bundle.json`, `inputs/audit-dev.json` and
`inputs/stress-<model>.json`, plus the runner modules and existing model files.
The wrapper requests one DIS02 GPU and the existing Python environment; it
refuses a competing coss40 job. It does not install or download anything.

`decision.py` validates artifact hashes, revision metadata and native templates
before model loading. Each model gets six excluded DEV warmups, six memory-only
4096-input/160-output probes, and the selected bundle's B/C rows. Decision
decoding has no minimum output, uses greedy bf16/eager batch one, native EOS,
160 maximum new tokens and repetition penalty 1.0. A 30-second generator timeout
invalidates the run. No repair or quality-based regeneration is performed.

`outputs.jsonl` is append-only with fsync after each call. Resume with
`--export=ALL,MODEL77_RUN_ID=<original-result-directory>` on `sbatch`; unchanged
completed normal/stress IDs are skipped, failed or mismatched rows block resume.
Warmups are rerun after every fresh load and retained separately in
`warmups-<job>.jsonl`; they are never included in latency summaries.

Generator wall time includes rendering, tokenization, H2D, prefill, generation,
synchronization and both raw/clean decoding. Load time is separate; TTFT is not
instrumented. Summaries record B/C median and nearest-rank p95, generation-only
time, token totals and length caps. Phase-reset CUDA allocated/reserved peaks
and 10 ms NVML whole-device samples include resident weights. The memory value
is the maximum of reserved and observed device memory, not model size.
Sampling may miss short peaks, so both allocator peaks remain available.

Heldout generation requires separately authorized input transfer and a JSON file
with `authorized=true`, nonempty `approver`/`date`, and the exact
`bundle_sha256`/`protocol_sha256`. This task does not create that authorization.
After approval and transfer, the future launch is:

```bash
sbatch --time=04:00:00 --export=ALL,MODEL77_RUN_ID=decision-v1 \
  experiments/model77/decision.sbatch decision inputs/decision-authorization.json
```

The signed file is an execution interlock, not a cryptographic signature.
`summary-<job>.json` contains effective generation settings; `manifest.json`
records hashes, runtime versions, template and unchanged official `rope_scaling`.
Kanana-2 uses official YaRN factor 40 even at short context; its warning does not
change the configuration. Quality impact is unmeasured.
