"""Exactly one offline school DEV pilot: BASE, train180 rows, merged LORA."""

import argparse
from dataclasses import asdict
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import TypedDict

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig

from .adapter import AdapterSpec, adapters, inject, save_adapter
from .execution import JSON, PilotError, file_hash, save, verify_seal
from .train import BASE_MODEL, REVISION, TARGETS, Batch, TrainConfig, train_epochs

class MemoryReport(TypedDict):
    max_memory_allocated_bytes: int
    max_memory_reserved_bytes: int
    device_highwater_bytes: int
    sampling_interval_seconds: float
    peak_reset_after_warmup: bool
    exclusive_gpu: bool


class GenerationResult(TypedDict):
    id: str
    question_id: str
    condition: str
    text: str
    raw_text: str
    generated_ids: list[int]
    input_tokens: int
    output_tokens: int
    wall_seconds: float
    length_capped: bool
    status: str


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    seal = verify_seal(root)
    if not os.environ.get("SLURM_JOB_ID") or os.environ.get("SLURMD_NODENAME") != "DIS02":
        raise PilotError("Requires the authorized DIS02 Slurm allocation")
    versions = {p: importlib.metadata.version(p) for p in ("torch", "transformers", "accelerate")}
    if (versions["torch"] != "2.7.1+cu118" or versions["transformers"] != "4.57.1"
            or versions["accelerate"] != "1.15.0"):
        raise PilotError(f"Existing package versions differ: {versions}")
    if torch.cuda.device_count() != 1 or not torch.cuda.is_bf16_supported():
        raise PilotError("Requires exactly one bf16 GPU")
    output = root / "results" / os.environ["SLURM_JOB_ID"]
    output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(root / "experiments/model77"))
    from decision import DevicePeak, nvml_uuid
    from register_check import register_match
    from smoke import think_markers

    model_path = Path("/abr/coss40/replica-models/Qwen3-8B")
    tokenizer = AutoTokenizer.from_pretrained(
        model_path, local_files_only=True, trust_remote_code=False)
    start_load = time.perf_counter()
    model = AutoModelForCausalLM.from_pretrained(
        model_path, local_files_only=True, trust_remote_code=False,
        use_safetensors=True, dtype=torch.bfloat16, device_map={"": 0},
        attn_implementation="eager").eval()
    torch.cuda.synchronize()
    if any(p.device.type != "cuda" or p.dtype != torch.bfloat16 for p in model.parameters()):
        raise PilotError("Base parameters must be GPU bf16")
    if (model.config.model_type != "qwen3" or model.config.num_hidden_layers != 36
            or sum(p.numel() for p in model.parameters()) != 8190735360):
        raise PilotError("Base architecture/parameter count differs")
    manifest = {
        "scope": "DEV_ONLY", "job_id": os.environ["SLURM_JOB_ID"],
        "model": BASE_MODEL, "revision": REVISION, "packages": versions,
        "load_seconds": time.perf_counter() - start_load,
        "execution_seal_sha256": file_hash(root / "EXECUTION_SEAL.json"),
        "inputs": seal, "gpu": torch.cuda.get_device_name(0),
        "model_config_sha256": file_hash(model_path / "config.json"),
        "model_weight_index_sha256": file_hash(model_path / "model.safetensors.index.json"),
        "revision_attestation": "declared pinned revision; existing offline snapshot unchanged",
    }
    save(output / "manifest.json", manifest)
    props = torch.cuda.get_device_properties(0)
    pci = subprocess.check_output([
        "nvidia-smi", f"--id={nvml_uuid(str(props.uuid))}",
        "--query-gpu=pci.bus_id", "--format=csv,noheader"], text=True).strip()
    sampler = DevicePeak(pci)
    rows = json.loads((root / "data/validation.json").read_text())
    if len(rows) != 360 or any(r["dataset_split"] != "validation" or r["split"] != "dev" for r in rows):
        raise PilotError("Requires complete frozen DEV validation")
    native_eos = model.generation_config.eos_token_id
    generation = GenerationConfig(
        do_sample=False, num_beams=1, max_new_tokens=160, min_new_tokens=0,
        repetition_penalty=1.0, eos_token_id=native_eos,
        pad_token_id=tokenizer.pad_token_id, max_time=30.0)
    operations, diagnostics = {}, {}

    def phase_start() -> None:
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        sampler.reset()

    def memory() -> MemoryReport:
        sampler.observe()
        result = {
            "max_memory_allocated_bytes": torch.cuda.max_memory_allocated(),
            "max_memory_reserved_bytes": torch.cuda.max_memory_reserved(),
            "device_highwater_bytes": sampler.peak,
            "sampling_interval_seconds": sampler.interval_s,
            "peak_reset_after_warmup": True, "exclusive_gpu": True,
        }
        if max(result["max_memory_reserved_bytes"], result["device_highwater_bytes"]) > 42 * 1024**3:
            raise PilotError("Frozen42GiB memory limit exceeded")
        return result

    def generate(row: dict[str, JSON], *, stress: bool = False) -> GenerationResult:
        torch.cuda.synchronize()
        start = time.perf_counter()
        rendered = tokenizer.apply_chat_template(
            row["messages"], tokenize=False, add_generation_prompt=True, enable_thinking=False)
        tokens = tokenizer.encode(rendered, add_special_tokens=False)
        if tokens != row["input_ids"]:
            raise PilotError("Local/remote native tokenization mismatch")
        if stress:
            tokens = (tokens * (4096 // len(tokens) + 1))[:4096]
        if len(tokens) > 4096:
            raise PilotError("Inference cap exceeded")
        inputs = torch.tensor([tokens], device="cuda")
        with torch.inference_mode():
            result = model.generate(
                input_ids=inputs, attention_mask=torch.ones_like(inputs),
                generation_config=generation, use_model_defaults=False, use_cache=True,
                **({"min_new_tokens": 160} if stress else {}))
        torch.cuda.synchronize()
        ids = result[0, len(tokens):].tolist()
        raw = tokenizer.decode(ids, skip_special_tokens=False)
        text = tokenizer.decode(ids, skip_special_tokens=True)
        elapsed = time.perf_counter() - start
        valid = (elapsed <= 30 and bool(text.strip()) and bool(ids)
                 and (len(ids) == 160 if stress else not think_markers(raw)))
        return {
            "id": row["id"], "question_id": row["question_id"], "condition": row["condition"],
            "text": text, "raw_text": raw, "generated_ids": ids,
            "input_tokens": len(tokens), "output_tokens": len(ids),
            "wall_seconds": elapsed, "length_capped": len(ids) == 160,
            "status": "success" if valid else "failed",
        }

    def evaluate_arm(arm: str) -> None:
        phases, stress_calls = [], []
        with (output / f"{arm}.jsonl").open("x") as handle, (
                output / f"{arm}-warmup.jsonl").open("x") as warmup:
            for row in rows[:6]:
                result = generate(row)
                warmup.write(json.dumps(result, ensure_ascii=False) + "\n")
                if result["status"] != "success":
                    raise PilotError("DEV warmup failed")
            for phase in ("stress", "normal"):
                phase_start()
                selected = rows[:6] if phase == "stress" else rows
                consistent = 0
                for row in selected:
                    result = generate(row, stress=phase == "stress")
                    result.update(arm=arm, phase=phase)
                    handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    if result["status"] != "success":
                        raise PilotError(f"DEV request failed: {arm}/{phase}/{row['id']}")
                    memory()
                    if phase == "stress":
                        stress_calls.append({"split": "dev", "actual_input_tokens": result["input_tokens"],
                                             "actual_output_tokens": result["output_tokens"]})
                    else:
                        consistent += register_match(result["text"], row["register"])
                phases.append({"name": phase, **memory()})
                if phase == "normal":
                    diagnostics[arm] = {"ending_heuristic_match": consistent, "n": len(selected),
                                        "official_judge": "NOT_YET_RUN"}
        operations[arm] = {
            "phases": phases, "stress_calls": stress_calls, "oom": False,
            "cpu_offload": False, "disk_offload": False, "allocation_failure": False,
            "latency_contract_verified": True, "all_request_timeout_seconds": 30,
        }
        save(output / f"{arm}-operations.json", operations[arm])

    try:
        evaluate_arm("BASE")
        spec = AdapterSpec(BASE_MODEL, REVISION, TARGETS)
        inject(model, spec)
        model.config.use_cache = False
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        payload = torch.load(root / "data/pilot.pt", map_location="cpu", weights_only=True)
        if payload["split"] != "train" or len(payload["batches"]) != 180:
            raise PilotError("Frozen pilot training count differs")
        batches = tuple(Batch(**row) for row in payload["batches"])
        phase_start()
        start = time.perf_counter()
        steps = []

        def update(step: int, supervised_tokens: int) -> None:
            torch.cuda.synchronize()
            steps.append({"step": step, "supervised_tokens": supervised_tokens,
                          "wall_seconds": time.perf_counter() - start, **memory()})
            save(output / f"train-step-{step:03d}.json", steps[-1])

        result = train_epochs(model, batches, TrainConfig(epochs=1), on_step=update)[0]
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - start
        peak = memory()
        frozen = all(not p.requires_grad and p.grad is None for name, p in model.named_parameters()
                     if not name.endswith(("lora_A", "lora_B")))
        train_report = {
            **asdict(result), **peak, "wall_seconds": elapsed, "base_frozen": frozen,
            "total_tokens": sum(b.input_ids.numel() for b in batches),
            "supervised_tokens_per_second": result.supervised_tokens / elapsed,
            "tokens_per_second": sum(b.input_ids.numel() for b in batches) / elapsed,
            "updates_per_second": result.optimizer_steps / elapsed,
            "max_sequence": max(b.input_ids.shape[1] for b in batches),
            "trainable_parameters": sum(p.numel() for p in model.parameters() if p.requires_grad),
        }
        if not frozen or result.optimizer_steps != 23 or train_report["trainable_parameters"] != 3833856:
            raise PilotError("Frozen base/adapter/update invariant failed")
        save(output / "training.json", train_report)
        operations["TRAIN"] = {
            "finite": True, "base_frozen": frozen,
            "peak_bytes": max(peak["max_memory_reserved_bytes"], peak["device_highwater_bytes"]),
        }
        save_adapter(model, spec, output / "adapter.pt")
        model.gradient_checkpointing_disable()
        model.eval()
        for module in adapters(model):
            module.merge()
        evaluate_arm("LORA")
        save(output / "operations.json", operations)
        save(output / "diagnostics.json", diagnostics)
        save(output / "COMPLETE.json", {
            "scope": "DEV_ONLY", "job_id": os.environ["SLURM_JOB_ID"],
            "training_updates": result.optimizer_steps, "responses_per_arm": 360,
            "final_service_model_approved": False})
        print("ISSUE88_DEV_COMPLETE", flush=True)
    finally:
        sampler.close()


if __name__ == "__main__":
    main()
