"""Read-only allocation preflight: frozen files, installed runtime, local weights."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[2]
REVISION = "b968826d9c46dd6066d109eabc6255188de91218"


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main():
    manifest = json.loads((ROOT / "run-inputs/manifest.json").read_text())
    for name, expected in manifest["files"].items():
        if digest(ROOT / name) != expected:
            raise ValueError("input_hash_mismatch")
    with tarfile.open(ROOT / "run-inputs/source-2af5b97a.tar") as archive:
        for member in archive.getmembers():
            if member.isfile():
                archived = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
                if digest(ROOT / member.name) != archived:
                    raise ValueError("source_archive_mismatch")
    model = Path(os.environ["REPLICA_MODEL_PATH"])
    index = json.loads((model / "model.safetensors.index.json").read_text())
    shards = sorted(set(index["weight_map"].values()))
    if len(shards) != 5:
        raise ValueError("shard_count")
    inventory = {}
    for name in shards:
        metadata = model / ".cache/huggingface/download" / (name + ".metadata")
        revision, expected = metadata.read_text().splitlines()[:2]
        actual = digest(model / name)
        if revision != REVISION or actual != expected:
            raise ValueError("model_hash_mismatch")
        inventory[name] = actual
    for name in ("config.json", "tokenizer.json", "tokenizer_config.json",
                 "generation_config.json", "model.safetensors.index.json"):
        metadata = model / ".cache/huggingface/download" / (name + ".metadata")
        if metadata.read_text().splitlines()[0] != REVISION:
            raise ValueError("artifact_revision_mismatch")
        inventory[name] = digest(model / name)
    if sys.version_info[:2] != (3, 12) or sys.prefix == sys.base_prefix:
        raise ValueError("python_environment")
    import torch
    import transformers
    import accelerate
    import safetensors
    if not torch.cuda.is_available():
        raise ValueError("cuda_unavailable")
    result = {
        "event": "preflight", "outcome": "verified", "utc_epoch": time.time(),
        "job_id": os.environ["SLURM_JOB_ID"], "python": sys.version.split()[0],
        "packages": {name: importlib.metadata.version(name)
                     for name in ("torch", "transformers", "accelerate", "safetensors")},
        "gpu_name": torch.cuda.get_device_name(0),
        "gpu_memory_bytes": torch.cuda.get_device_properties(0).total_memory,
        "gpu_capability": list(torch.cuda.get_device_capability(0)),
        "cuda_runtime": torch.version.cuda,
        "model_id": "Qwen/Qwen3-8B", "model_revision": REVISION,
        "model_files": inventory, "manifest_sha256": digest(ROOT / "run-inputs/manifest.json"),
        "criteria_sha256": manifest["files"]["run-inputs/CRITERIA.md"],
    }
    print(json.dumps(result, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"event": "preflight", "outcome": "failed",
                          "exception_type": type(error).__name__}), flush=True)
        raise SystemExit(1)
