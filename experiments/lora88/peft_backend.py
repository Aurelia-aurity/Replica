"""PEFT adapters with the original initialization and pretokenized supervision."""

import math
from pathlib import Path

import torch
from torch import nn

from .adapter import AdapterSpec
from .train import Batch, TrainingError


def inject_peft(model: nn.Module, spec: AdapterSpec) -> nn.Module:
    """Preserve target order, FP32 tensors and old RNG draws despite PEFT setup."""
    from peft import LoraConfig, get_peft_model

    cpu_rng = torch.get_rng_state()
    cuda_rng = torch.cuda.get_rng_state_all() if torch.cuda.is_initialized() else []
    wrapped = get_peft_model(model, LoraConfig(
        r=spec.rank, lora_alpha=spec.alpha, lora_dropout=0.0,
        target_modules=list(spec.targets), bias="none", init_lora_weights=True,
        task_type="CAUSAL_LM",
    ))
    torch.set_rng_state(cpu_rng)
    if cuda_rng:
        torch.cuda.set_rng_state_all(cuda_rng)
    with torch.no_grad():
        for name in spec.targets:
            layer = model.get_submodule(name)
            layer.lora_A["default"].to(dtype=torch.float32)
            layer.lora_B["default"].to(dtype=torch.float32)
            nn.init.kaiming_uniform_(layer.lora_A["default"].weight, a=math.sqrt(5))
            layer.lora_B["default"].weight.zero_()
    return wrapped


def parameters(model: nn.Module) -> tuple[nn.Parameter, ...]:
    """Enumerate only active unmerged default q/v adapter weights."""
    from peft.tuners.lora import LoraLayer

    selected = []
    for layer in model.modules():
        if isinstance(layer, LoraLayer):
            if layer.merged or layer.active_adapters != ["default"]:
                raise TrainingError("Expected unmerged default PEFT adapters")
            selected.extend((layer.lora_A["default"].weight, layer.lora_B["default"].weight))
    if not selected or any(p.dtype != torch.float32 for p in selected):
        raise TrainingError("Expected nonempty FP32 PEFT adapter parameters")
    return tuple(selected)


def supervised_batch(tokenizer, row) -> Batch:
    """Identical old native-prefix/EOS construction; no TRL collator or packing."""
    prefix = tokenizer.apply_chat_template(
        row["messages"], tokenize=False, add_generation_prompt=True, enable_thinking=False)
    prefix_ids = tokenizer.encode(prefix, add_special_tokens=False)
    full = tokenizer.encode(prefix + row["text"] + "<|im_end|>", add_special_tokens=False)
    if prefix_ids != row["input_ids"] or full[:len(prefix_ids)] != prefix_ids or full[-1] != 151645:
        raise TrainingError("Native supervision boundary/tokenization differs")
    batch = Batch(
        torch.tensor([full]), torch.ones(1, len(full), dtype=torch.long),
        torch.tensor([[-100] * len(prefix_ids) + full[len(prefix_ids):]]))
    batch.validate(4096)
    return batch


def save_peft(model, path: Path) -> None:
    """Write standard adapter-only safetensors; never full base weights."""
    model.save_pretrained(path, safe_serialization=True, save_embedding_layers=False)
