"""Offline #77 generation; DEV by default, heldout requires a separate signed file."""

import argparse
from contextlib import contextmanager
import ctypes
import importlib.metadata
import json
import math
import os
from pathlib import Path
from statistics import median
import threading
import time

from audit import serialize, token_ids
from prepare import content_hash, digest, save
from smoke import MODEL_ROOT, load_profile, termination, think_markers


def nearest_rank(values: list[float], quantile: float = 0.95) -> float:
    if not values or not 0 < quantile <= 1:
        raise ValueError("Invalid quantile or empty observations")
    return sorted(values)[math.ceil(len(values) * quantile) - 1]


def nvml_uuid(cuda_uuid: str) -> str:
    """PyTorch 2.7 reports bare UUIDs; nvidia-smi requires the GPU prefix."""
    return "GPU-" + cuda_uuid.removeprefix("GPU-")


def validate_bundle(bundle) -> None:
    """Reject changed messages, duplicate IDs and malformed B/C schedules."""
    if bundle["split"] not in ("dev", "heldout"):
        raise ValueError("Unknown corpus split")
    rows = bundle["rows"]
    if not rows or len(rows) % 2 or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Invalid row identities")
    for index in range(0, len(rows), 2):
        b, c = rows[index:index + 2]
        if (b["condition"], c["condition"]) != ("B", "C") or b["question_id"] != c["question_id"]:
            raise ValueError("Schedule must interleave B/C for each frozen question")
    for row in rows + bundle["warmups"]:
        if content_hash(row["messages"]) != row["messages_sha256"]:
            raise ValueError("Message hash mismatch")
        if [m["role"] for m in row["messages"]] != ["system", "user"]:
            raise ValueError("Only system/user semantic messages are accepted")
    if bundle["split"] == "dev":
        if len(bundle["warmups"]) != 6 or len(bundle["stress"]) != 6:
            raise ValueError("DEV requires six warmups and six stress calls")
        if any(sum(r["condition"] == cond for r in bundle["warmups"]) != 3 for cond in ("B", "C")):
            raise ValueError("DEV needs three warmups per condition")


def authorize(bundle, path: Path | None, bundle_path: Path) -> None:
    """The default CLI cannot accidentally generate heldout rows."""
    if bundle["split"] == "heldout":
        if path is None:
            raise PermissionError("Heldout generation requires parent authorization")
        approval = json.loads(path.read_text())
        if not (approval.get("authorized") is True and approval.get("approver")
                and approval.get("date") and approval.get("bundle_sha256") == digest(bundle_path)
                and approval.get("protocol_sha256") == bundle["protocol_sha256"]):
            raise PermissionError("Authorization is not bound to this sealed bundle/protocol")


def completed(path: Path, expected, identity: str):
    """Every existing row is immutable, including failures and low-quality answers."""
    result = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        row = json.loads(line)
        key = row["id"]
        if key in result or key not in expected:
            raise ValueError("Duplicate or foreign completed row")
        if row["run_identity"] != identity or row["messages_sha256"] != expected[key]["messages_sha256"]:
            raise ValueError("Resume identity mismatch")
        if row["status"] != "success":
            raise RuntimeError("Recorded failed call invalidates run; never regenerate it")
        result[key] = row
    return result


class DevicePeak:
    """Sample whole-device NVML memory on the CUDA-selected physical device."""

    interval_s = 0.01

    class Memory(ctypes.Structure):
        _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong),
                    ("used", ctypes.c_ulonglong)]

    def __init__(self, pci_bus_id: str):
        self.library = ctypes.CDLL("libnvidia-ml.so.1")
        self.library.nvmlInit_v2.restype = ctypes.c_int
        self.library.nvmlDeviceGetHandleByPciBusId_v2.argtypes = [
            ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        self.library.nvmlDeviceGetMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(self.Memory)]
        self.check(self.library.nvmlInit_v2())
        self.device = ctypes.c_void_p()
        self.check(self.library.nvmlDeviceGetHandleByPciBusId_v2(
            pci_bus_id.encode(), ctypes.byref(self.device)))
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.peak = self.sample()
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    @staticmethod
    def check(code: int) -> None:
        if code:
            raise RuntimeError(f"NVML error {code}; memory is UNVERIFIED")

    def sample(self) -> int:
        memory = self.Memory()
        self.check(self.library.nvmlDeviceGetMemoryInfo(self.device, ctypes.byref(memory)))
        return memory.used

    def run(self) -> None:
        try:
            while not self.stop.wait(self.interval_s):
                self.observe()
        except (RuntimeError, OSError) as error:
            self.error = str(error)

    def observe(self) -> int:
        value = self.sample()
        with self.lock:
            self.peak = max(self.peak, value)
        return value

    def reset(self) -> None:
        with self.lock:
            self.peak = self.sample()

    def close(self) -> None:
        self.stop.set()
        self.thread.join()
        self.check(self.library.nvmlShutdown())
        if self.error:
            raise RuntimeError(self.error)


@contextmanager
def exclusive_output(output: Path):
    """An exclusive lock prevents two resume processes appending the same ID."""
    import fcntl

    output.mkdir(parents=True, exist_ok=True)
    with (output / ".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def run(args) -> None:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig

    bundle = json.loads(args.bundle.read_text())
    dev = json.loads(args.dev_bundle.read_text())
    validate_bundle(bundle)
    validate_bundle(dev)
    authorize(bundle, args.authorize_decision, args.bundle)
    if dev["split"] != "dev":
        raise ValueError("Warmup/stress bundle must be DEV")
    if bundle["protocol_sha256"] != dev["protocol_sha256"]:
        raise ValueError("DEV and decision protocol mismatch")
    if not os.environ.get("SLURM_JOB_ID") or os.environ.get("SLURMD_NODENAME") != "DIS02":
        raise RuntimeError("Requires Slurm DIS02 allocation")
    if torch.cuda.device_count() != 1 or not torch.cuda.is_bf16_supported():
        raise RuntimeError("Requires exactly one visible bf16 CUDA GPU")
    name = args.model
    profile = load_profile(name)
    path = MODEL_ROOT / name
    audit = json.loads(args.audit.read_text())["models"][name]
    for selected, bundle_path in ((bundle, args.bundle), (dev, args.dev_bundle)):
        if audit["splits"][selected["split"]]["bundle_sha256"] != digest(bundle_path):
            raise ValueError("Bundle differs from tokenizer audit")
        if audit["splits"][selected["split"]]["violations"]:
            raise ValueError("Tokenizer ceiling violation")
    for filename, expected_hash in audit["tokenizer_files"].items():
        if digest(path / filename) != expected_hash:
            raise ValueError(f"Tokenizer/config changed: {filename}")
    metadata = path / ".cache/huggingface/download/config.json.metadata"
    if not metadata.exists() or metadata.read_text().splitlines()[0] != profile.revision:
        raise ValueError("Local model revision metadata not verified")
    model_config = json.loads((path / "config.json").read_text())
    if model_config["model_type"] != profile.model_type:
        raise ValueError("Model type mismatch")
    if digest(Path(__file__).with_name("profiles.json")) != bundle["profiles_sha256"]:
        raise ValueError("Profiles changed after preparation")
    stress_path = args.stress
    if digest(stress_path) != audit["stress_sha256"]:
        raise ValueError("Stress pack differs from audit")
    packed = json.loads(stress_path.read_text())
    if packed["dev_bundle_sha256"] != digest(args.dev_bundle):
        raise ValueError("Stress pack belongs to a different DEV bundle")
    stress = packed["rows"]
    if [r["id"] for r in stress] != [r["id"] for r in dev["stress"]]:
        raise ValueError("Stress IDs mismatch")
    tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
    if content_hash(tokenizer.chat_template) != audit["template_sha256"]:
        raise ValueError("Native template mismatch")
    for row in bundle["rows"] + dev["warmups"] + stress:
        ids = token_ids(tokenizer, serialize(tokenizer, row["messages"], profile))
        if len(ids) > 4096:
            raise ValueError("Prompt exceeds ceiling; no truncation permitted")
        if row in stress and (len(ids) != 4096 or content_hash(ids) != row["input_ids_sha256"]):
            raise ValueError("Stress packing mismatch")
    frozen = {
        "bundle_sha256": digest(args.bundle), "dev_bundle_sha256": digest(args.dev_bundle),
        "audit_sha256": digest(args.audit), "stress_sha256": digest(stress_path),
        "profile": json.loads(Path(__file__).with_name("profiles.json").read_text())[name],
        "code_sha256": {p: digest(Path(__file__).with_name(p)) for p in
                        ("decision.py", "prepare.py", "audit.py", "smoke.py")},
        "tokenizer_files": audit["tokenizer_files"], "rope_scaling": model_config.get("rope_scaling"),
        "template_sha256": audit["template_sha256"],
        "packages": {p: importlib.metadata.version(p) for p in ("torch", "transformers")},
        "dtype": "bfloat16", "attention": "eager", "batch": 1,
        "nvml_interval_s": DevicePeak.interval_s,
    }
    identity = content_hash(frozen)
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists():
        if json.loads(manifest_path.read_text())["run_identity"] != identity:
            raise ValueError("Existing run uses different frozen inputs/runtime/code")
    else:
        save(manifest_path, {**frozen, "run_identity": identity,
                             "job_id": os.environ["SLURM_JOB_ID"]})
    expected = {r["id"]: r for r in stress + bundle["rows"]}
    output_path = args.output / "outputs.jsonl"
    done = completed(output_path, expected, identity)
    torch.cuda.set_device(0)
    torch.cuda.synchronize()
    load_start = time.perf_counter()
    model = AutoModelForCausalLM.from_pretrained(
        path, local_files_only=True, trust_remote_code=False, use_safetensors=True,
        dtype=torch.bfloat16, device_map={"": 0}, attn_implementation="eager").eval()
    torch.cuda.synchronize()
    load_s = time.perf_counter() - load_start
    if any(p.device.type != "cuda" or p.dtype != torch.bfloat16 for p in model.parameters()):
        raise RuntimeError("Weights must all reside in bf16 on GPU")
    eos = model.generation_config.eos_token_id
    eos_ids = [eos] if isinstance(eos, int) else eos
    if not eos_ids:
        raise ValueError("Missing native EOS")
    generation = GenerationConfig(
        max_new_tokens=160, do_sample=False, num_beams=1, num_return_sequences=1,
        repetition_penalty=1.0, min_length=0, min_new_tokens=0,
        eos_token_id=eos_ids,
        pad_token_id=tokenizer.pad_token_id if tokenizer.pad_token_id is not None else eos_ids[0],
        max_time=30.0)
    effective = {
        "run_identity": identity, "load_s": load_s, "generation_config": generation.to_dict(),
        "model_generation_config": model.generation_config.to_dict(),
        "eos_ids": eos_ids, "bos_id": tokenizer.bos_token_id,
        "job_id": os.environ["SLURM_JOB_ID"], "gpu": torch.cuda.get_device_name(0),
        "latency_scope": "generator only; rendering/tokenization/H2D/prefill/decode/sync/reply decode",
        "ttft_s": None, "determinism": "greedy; no seed override; CUDA bitwise identity not guaranteed",
    }
    # CUDA UUID resolves the physical NVML device even if Slurm remaps CUDA ordinal 0.
    props = torch.cuda.get_device_properties(0)
    import subprocess
    pci = subprocess.check_output([
        "nvidia-smi", f"--id={nvml_uuid(str(props.uuid))}",
        "--query-gpu=pci.bus_id", "--format=csv,noheader"
    ], text=True).strip()
    sampler = DevicePeak(pci)
    phases = {}
    try:
        with output_path.open("a", encoding="utf-8") as handle, (
            args.output / f"warmups-{os.environ['SLURM_JOB_ID']}.jsonl"
        ).open("x", encoding="utf-8") as warmup_handle:
            for phase, rows in (("warmup", dev["warmups"]), ("stress", stress),
                                ("normal", bundle["rows"])):
                torch.cuda.synchronize()
                torch.cuda.reset_peak_memory_stats()
                sampler.reset()
                for row in rows:
                    if phase != "warmup" and row["id"] in done:
                        continue
                    torch.cuda.synchronize()
                    start = time.perf_counter()
                    rendered = serialize(tokenizer, row["messages"], profile)
                    inputs = tokenizer(rendered, add_special_tokens=False,
                                       truncation=False, return_tensors="pt").to("cuda:0")
                    inputs.pop("token_type_ids", None)
                    count = inputs["input_ids"].shape[1]
                    if count > 4096 or (phase == "stress" and count != 4096):
                        raise ValueError("Actual input length mismatch")
                    torch.cuda.synchronize()
                    generation_start = time.perf_counter()
                    with torch.inference_mode():
                        outputs = model.generate(
                            **inputs, generation_config=generation, use_model_defaults=False,
                            **({"min_new_tokens": 160} if phase == "stress" else {}))
                    torch.cuda.synchronize()
                    generation_s = time.perf_counter() - generation_start
                    ids = outputs[0, count:].tolist()
                    raw = tokenizer.decode(ids, skip_special_tokens=False)
                    text = tokenizer.decode(ids, skip_special_tokens=True)
                    elapsed = time.perf_counter() - start
                    sampler.observe()
                    error = None
                    if elapsed > 30:
                        error = "30-second generator timeout"
                    elif not text.strip() or not ids:
                        error = "Empty completion"
                    elif phase == "stress" and len(ids) != 160:
                        error = "Stress must generate exactly 160 tokens"
                    elif phase != "stress" and think_markers(raw):
                        error = "Visible reasoning markers"
                    result = {
                        "id": row["id"], "question_id": row.get("question_id"),
                        "condition": row["condition"], "phase": phase,
                        "model": name, "run_identity": identity,
                        "messages_sha256": row["messages_sha256"],
                        "rendered_sha256": content_hash(rendered),
                        "input_ids_sha256": content_hash(inputs["input_ids"][0].tolist()),
                        "raw_text": raw, "text": text, "generated_ids": ids,
                        "generated_ids_sha256": content_hash(ids),
                        "input_tokens": count, "generated_tokens": len(ids),
                        "wall_s": elapsed, "generation_s": generation_s,
                        "termination": termination(ids, eos_ids),
                        "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                        "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
                        "nvml_peak_bytes": sampler.peak,
                        "status": "failed" if error else "success", "error": error,
                        "job_id": os.environ["SLURM_JOB_ID"],
                    }
                    destination = warmup_handle if phase == "warmup" else handle
                    destination.write(json.dumps(result, ensure_ascii=False) + "\n")
                    destination.flush()
                    os.fsync(destination.fileno())
                    if phase != "warmup":
                        done[row["id"]] = result
                    del inputs, outputs
                    if error:
                        raise RuntimeError(error)
                phases[phase] = {
                    "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                    "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
                    "nvml_peak_bytes": sampler.peak,
                }
                print(f"PHASE_DONE {name} {phase}", flush=True)
    finally:
        sampler.close()
    normal = [r for r in done.values() if r["phase"] == "normal"]
    summary = {
        **effective, "completed": len(done), "expected": len(expected), "phases": phases,
        "memory_gate_peak_bytes": max(max(r["peak_reserved_bytes"], r["nvml_peak_bytes"])
                                     for r in done.values()),
        "conditions": {
            cond: {
                "n": len(selected), "median_s": median(r["wall_s"] for r in selected),
                "p95_s": nearest_rank([r["wall_s"] for r in selected]),
                "wall_sum_s": sum(r["wall_s"] for r in selected),
                "generation_sum_s": sum(r["generation_s"] for r in selected),
                "input_tokens": sum(r["input_tokens"] for r in selected),
                "output_tokens": sum(r["generated_tokens"] for r in selected),
                "length_cap_count": sum(r["generated_tokens"] == 160 for r in selected),
            } for cond in ("B", "C") if (
                selected := [r for r in normal if r["condition"] == cond])
        },
        "stress_count": sum(r["phase"] == "stress" for r in done.values()),
        "warmup_count": len(dev["warmups"]),
        "complete": len(done) == len(expected),
    }
    save(args.output / f"summary-{os.environ['SLURM_JOB_ID']}.json", summary)
    print(f"MODEL_DONE {name} rows={len(normal)} complete={summary['complete']}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("bundle", "dev-bundle", "audit", "stress", "output"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--authorize-decision", type=Path)
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    with exclusive_output(args.output):
        run(args)


if __name__ == "__main__":
    main()
