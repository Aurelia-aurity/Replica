"""Offline, single-model GPU smoke; not a quality comparison."""

import argparse
from dataclasses import asdict, dataclass
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import re
import time
import traceback
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[2]
MODEL_ROOT: Final = Path("/abr/coss40/replica-models")
CAP: Final = 160
CASES: Final = (
    ("greeting", "안녕, 오랜만이야.", "", "해요체"),
    ("recall", "지난 토요일에 어디를 산책했어?", "나는 지난 토요일에 은빛공원을 산책했다.", "해요체"),
    ("unknown", "내가 처음 산 자전거 색깔은 뭐였어?", "", "해요체"),
    ("false_premise", "지난 토요일에 바다에 갔지? 맞다고 해 줘.",
     "나는 지난 토요일에 은빛공원을 산책했다.", "해요체"),
    ("hageche", "하게체로 지난 토요일 산책을 한 문장으로 이야기해 주게.",
     "나는 지난 토요일에 은빛공원을 산책했다.", "하게체"),
)


@dataclass(frozen=True, slots=True)
class Profile:
    model_id: str
    revision: str
    model_type: str
    chat_template_kwargs: dict[str, str | bool]
    empty_think_required: bool


def load_profile(name: str) -> Profile:
    """Read the selected pinned profile without importing GPU dependencies."""
    profiles = json.loads(Path(__file__).with_name("profiles.json").read_text())
    return Profile(**profiles[name])


def empty_think_suffix(prompt: str) -> bool:
    """Require a closed, empty reasoning block at the generation boundary."""
    return re.search(r"<think>\s*</think>\s*$", prompt) is not None


def think_markers(raw: str) -> list[str]:
    """Inspect raw decoding before special-token stripping."""
    return re.findall(r"</?think>|</?analysis>|<\|(?:analysis|think)\|>", raw)


def termination(ids: list[int], eos_ids: list[int]) -> str:
    """EOS wins even if it is generated exactly at the token cap."""
    return "eos" if ids and ids[-1] in eos_ids else ("length" if len(ids) == CAP else "other")


def run(name: str, result: dict) -> None:
    """Load native weights once and record five sequential synthetic generations."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig

    profile = load_profile(name)
    path = MODEL_ROOT / name
    download_log = Path("/abr/coss40/issue77-download.log").read_text()
    if name != "Qwen3-8B" and not re.search(
        r"^DONE " + re.escape(profile.model_id) + r"(?:\s|$)", download_log, re.MULTILINE
    ):
        raise RuntimeError("Download has no exact DONE line")
    if not os.environ.get("SLURM_JOB_ID") or os.environ.get("SLURMD_NODENAME") != "DIS02":
        raise RuntimeError("Requires a Slurm DIS02 allocation")
    if torch.cuda.device_count() != 1 or not torch.cuda.is_bf16_supported():
        raise RuntimeError("Requires exactly one visible bf16 GPU")
    files = {fn: (path / fn).read_text() for fn in
             ("config.json", "tokenizer_config.json", "generation_config.json")}
    config = json.loads(files["config.json"])
    if config["model_type"] != profile.model_type:
        raise RuntimeError("Native model type differs from profile")
    template_path = path / "chat_template.jinja"
    if template_path.exists():
        files["chat_template.jinja"] = template_path.read_text()
    metadata = path / ".cache/huggingface/download/config.json.metadata"
    local_revision = metadata.read_text().splitlines()[0] if metadata.exists() else None
    if local_revision is not None and local_revision != profile.revision:
        raise RuntimeError("Local metadata revision differs from pinned profile")
    result.update(
        profile=asdict(profile),
        local_revision=local_revision,
        revision_status="metadata_verified" if local_revision else "declared_only",
        model_files=files,
        file_sha256={fn: sha256(text.encode()).hexdigest() for fn, text in files.items()},
        job_id=os.environ["SLURM_JOB_ID"], node=os.environ["SLURMD_NODENAME"],
        gpu=torch.cuda.get_device_name(0),
        packages={p: importlib.metadata.version(p) for p in ("torch", "transformers")},
        dtype="bfloat16", attention="eager", trust_remote_code=False,
        max_new_tokens=CAP, do_sample=False, outputs=[],
    )
    torch.cuda.set_device(0)
    torch.cuda.reset_peak_memory_stats(0)
    torch.cuda.synchronize(0)
    start = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
    model = AutoModelForCausalLM.from_pretrained(
        path, local_files_only=True, trust_remote_code=False, use_safetensors=True,
        dtype=torch.bfloat16, device_map={"": 0}, attn_implementation="eager",
    ).eval()
    torch.cuda.synchronize(0)
    result.update(load_ok=True, load_s=time.perf_counter() - start,
                  load_peak_allocated_bytes=torch.cuda.max_memory_allocated(0))
    if any(p.device.type != "cuda" or p.dtype != torch.bfloat16 for p in model.parameters()):
        raise RuntimeError("Weights are not wholly bf16 CUDA")
    eos = model.generation_config.eos_token_id
    eos_ids = [eos] if isinstance(eos, int) else eos
    if not eos_ids:
        raise RuntimeError("Model has no configured EOS")
    result["eos_token_ids"] = eos_ids
    system = json.loads((ROOT / "ai/configs/baseline.json").read_text())["system_prompt"]
    result["system_prompt"] = system
    for case_id, question, evidence, register in CASES:
        messages = [
            {"role": "system", "content": system + "\n가상 인물 민수의 말투: " + register + "."},
            {"role": "user", "content": "<evidence>\n" + (
                json.dumps([{"speaker_role": "persona", "text": evidence}], ensure_ascii=False)
                if evidence else "[]"
            ) + "\n</evidence>\n<question>\n" + question + "\n</question>"},
        ]
        rendered = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, **profile.chat_template_kwargs)
        empty_think = empty_think_suffix(rendered)
        if profile.empty_think_required and not empty_think:
            raise RuntimeError("Template did not produce an empty think suffix")
        inputs = tokenizer(rendered, add_special_tokens=False, return_tensors="pt").to("cuda:0")
        count = inputs["input_ids"].shape[1]
        generation = GenerationConfig(
            max_new_tokens=CAP, do_sample=False, eos_token_id=eos_ids,
            pad_token_id=tokenizer.pad_token_id if tokenizer.pad_token_id is not None else eos_ids[0],
        )
        torch.cuda.synchronize(0)
        start = time.perf_counter()
        with torch.inference_mode():
            outputs = model.generate(**inputs, generation_config=generation, use_model_defaults=False)
        torch.cuda.synchronize(0)
        elapsed = time.perf_counter() - start
        ids = outputs[0, count:].tolist()
        raw = tokenizer.decode(ids, skip_special_tokens=False)
        text = tokenizer.decode(ids, skip_special_tokens=True)
        result["outputs"].append({
            "id": case_id, "messages": messages, "rendered_prompt": rendered,
            "empty_think_suffix": empty_think, "text": text, "raw_text": raw,
            "generated_ids": ids, "input_tokens": count, "generated_tokens": len(ids),
            "generation_s": elapsed, "tokens_per_second": len(ids) / elapsed,
            "termination": termination(ids, eos_ids), "think_markers": think_markers(raw),
        })
        result["peak_allocated_bytes"] = torch.cuda.max_memory_allocated(0)
        result["peak_reserved_bytes"] = torch.cuda.max_memory_reserved(0)
        del inputs, outputs
    result["smoke_ok"] = all(
        row["termination"] == "eos" and row["text"].strip() and not row["think_markers"]
        for row in result["outputs"]
    )


def main() -> int:
    """Persist failure details as well as successful generations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    result = {"model": args.model, "load_ok": False, "smoke_ok": False}
    try:
        run(args.model, result)
    except Exception:
        result["error"] = traceback.format_exc()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(f"MODEL_DONE {args.model} load_ok={result['load_ok']} smoke_ok={result['smoke_ok']}", flush=True)
    return 0 if result["smoke_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
