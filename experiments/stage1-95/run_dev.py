"""Explicitly enabled, offline four-arm DEV generation; no submit/SSH/judges."""

from hashlib import sha256
import importlib.metadata
from inspect import signature
import json
import os
from pathlib import Path
import sys
import time

import typer

from prepare95 import CRITERIA_SHA256, HERE, ROOT, Pack


def validate_pack(pack: Pack) -> None:
    """Check frozen identities and prompt hashes before importing any runtime."""
    if pack.criteria_sha256 != CRITERIA_SHA256 or (
        sha256((HERE / "CRITERIA.md").read_bytes()).hexdigest() != CRITERIA_SHA256
    ):
        raise ValueError("Criteria mismatch")
    if len(pack.rows) != 1440 or len({r.id for r in pack.rows}) != 1440:
        raise ValueError("Requires the frozen 360-question four-arm DEV pack")
    questions = {r.question_id for r in pack.rows}
    if len(questions) != 360:
        raise ValueError("Question count mismatch")
    for qid in questions:
        group = {r.arm: r for r in pack.rows if r.question_id == qid}
        if set(group) != {"B", "C", "S", "P"} or group["S"].evidence_ids != group["B"].evidence_ids:
            raise ValueError("Invalid paired arm identities")
        if not group["S"].messages[1].content.startswith(group["B"].messages[1].content):
            raise ValueError("S changed B facts/question")
        if group["S"].messages[0] != group["B"].messages[0]:
            raise ValueError("S changed the baseline system")
        for row in group.values():
            if row.id != f"{row.question_id}:{row.arm}" or tuple(
                m.role for m in row.messages
            ) != ("system", "user"):
                raise ValueError("Invalid row identity/message roles")
            payload = "\n".join(m.model_dump_json() for m in row.messages)
            if sha256(payload.encode()).hexdigest() != row.messages_sha256:
                raise ValueError("Message hash mismatch")


def main(pack_path: Path, model_path: Path, output: Path) -> None:
    """One load, one sequential batch-one DEV run; never retries failed rows."""
    if os.environ.get("STAGE95_EXECUTE_DEV") != "YES":
        raise PermissionError("Separate DEV execution authorization is required")
    if not os.environ.get("SLURM_JOB_ID") or os.environ.get("SLURMD_NODENAME") != "DIS02":
        raise PermissionError("Requires an existing DIS02 Slurm allocation")
    if any(word in str(pack_path.resolve()).lower() for word in ("heldout", "held-out", "sealed", "decision")):
        raise ValueError("Forbidden input path")
    pack = Pack.model_validate_json(pack_path.read_text())
    validate_pack(pack)
    for relative, digest in pack.code_sha256.items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT) or sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Prepared code/profile changed")
    if output.exists():
        raise FileExistsError(output)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    # Reuse #77's native template, pinned profile, EOS and version compatibility.
    sys.path.append(str(HERE.parent / "model77"))
    from audit import serialize, token_ids
    from decision import model_default_options
    from smoke import load_profile, termination, think_markers
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig

    profile = load_profile("Qwen3-8B")
    config = json.loads((model_path / "config.json").read_text())
    metadata = model_path / ".cache/huggingface/download/config.json.metadata"
    if config["model_type"] != profile.model_type or (
        not metadata.exists() or metadata.read_text().splitlines()[0] != profile.revision
    ):
        raise ValueError("Model type/revision differs from the pinned #77 profile")
    if sha256((model_path / "tokenizer.json").read_bytes()).hexdigest() != pack.tokenizer_sha256:
        raise ValueError("Tokenizer changed since preparation")
    if torch.cuda.device_count() != 1 or not torch.cuda.is_bf16_supported():
        raise RuntimeError("Requires one visible BF16 GPU")
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=False)
    rendered = [serialize(tokenizer, [m.model_dump() for m in r.messages], profile) for r in pack.rows]
    limit = min(4096, config["max_position_embeddings"])
    if any(len(token_ids(tokenizer, text)) + 160 > limit for text in rendered):
        raise ValueError("Input + output exceeds the common context cap; no truncation")
    torch.cuda.set_device(0)
    model = AutoModelForCausalLM.from_pretrained(
        model_path, local_files_only=True, trust_remote_code=False, use_safetensors=True,
        dtype=torch.bfloat16, device_map={"": 0}, attn_implementation="eager",
    ).eval()
    if any(p.device.type != "cuda" or p.dtype != torch.bfloat16 for p in model.parameters()):
        raise RuntimeError("All model weights must be BF16 on CUDA")
    eos = model.generation_config.eos_token_id
    eos_ids = [eos] if isinstance(eos, int) else eos
    if not eos_ids:
        raise ValueError("Missing native EOS")
    decoding = GenerationConfig(
        max_new_tokens=160, do_sample=False, num_beams=1, num_return_sequences=1,
        repetition_penalty=1.0, min_length=0, min_new_tokens=0,
        eos_token_id=eos_ids, pad_token_id=tokenizer.pad_token_id or eos_ids[0],
        max_time=30.0,
    )
    options = model_default_options(signature(model.generate).parameters)
    pack_sha256 = sha256(pack_path.read_bytes()).hexdigest()
    with output.open("x", encoding="utf-8") as handle:
        for row, text in zip(pack.rows, rendered, strict=True):
            start = time.perf_counter()
            inputs = tokenizer(text, add_special_tokens=False, truncation=False,
                               return_tensors="pt").to("cuda:0")
            inputs.pop("token_type_ids", None)
            count = inputs["input_ids"].shape[1]
            torch.cuda.synchronize()
            generation_start = time.perf_counter()
            with torch.inference_mode():
                generated = model.generate(**inputs, generation_config=decoding, **options)
            torch.cuda.synchronize()
            generation_seconds = time.perf_counter() - generation_start
            ids = generated[0, count:].tolist()
            raw = tokenizer.decode(ids, skip_special_tokens=False)
            answer = tokenizer.decode(ids, skip_special_tokens=True)
            wall = time.perf_counter() - start
            valid = bool(answer.strip() and ids) and not think_markers(raw) and wall <= 30
            result = {
                "scope": "DEV_ONLY", "id": row.id, "question_id": row.question_id, "arm": row.arm,
                "model": profile.model_id, "revision": profile.revision,
                "messages_sha256": row.messages_sha256, "pack_sha256": pack_sha256,
                "criteria_sha256": CRITERIA_SHA256, "job_id": os.environ["SLURM_JOB_ID"],
                "raw_text": raw, "text": answer, "generated_ids": ids,
                "generated_ids_sha256": sha256(json.dumps(ids).encode()).hexdigest(),
                "input_tokens": count, "output_tokens": len(ids), "wall_seconds": wall,
                "generation_seconds": generation_seconds, "termination": termination(ids, eos_ids),
                "status": "success" if valid else "failed", "batch": 1, "dtype": "bfloat16",
                "packages": {p: importlib.metadata.version(p) for p in ("torch", "transformers")},
            }
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            handle.flush()
            if not valid:
                raise RuntimeError(f"Failed DEV row {row.id}; no regeneration")
    print(f"DEV_COMPLETE rows={len(pack.rows)} judges_run=0")


if __name__ == "__main__":
    typer.run(main)
