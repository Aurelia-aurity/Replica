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
