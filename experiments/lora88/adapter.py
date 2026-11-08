"""Small explicit-target LoRA implementation, independent of PEFT."""

from dataclasses import asdict, dataclass
import math
from pathlib import Path
from typing import Final

import torch
from torch import Tensor, nn
from torch.nn import functional as F

FORMAT: Final = "replica-lora88-v1"


class AdapterError(ValueError):
    """Invalid adapter configuration, topology or checkpoint."""


@dataclass(frozen=True, slots=True)
class AdapterSpec:
    """Bind adapter tensors to an exact declared base and module topology."""

    base_model: str
    revision: str
    targets: tuple[str, ...]
    rank: int = 8
    alpha: float = 16.0

    def __post_init__(self) -> None:
        if (not self.base_model or not self.revision or not self.targets
                or len(set(self.targets)) != len(self.targets)
                or self.rank <= 0 or not math.isfinite(self.alpha) or self.alpha <= 0):
            raise AdapterError("Nonempty identity, unique targets and positive rank/alpha required")


class LoRALinear(nn.Module):
    """Mutable adapter weights and reversible evaluation merge over frozen W0."""

    def __init__(self, base: nn.Linear, rank: int, alpha: float) -> None:
        super().__init__()
        if rank <= 0 or not math.isfinite(alpha) or alpha <= 0:
            raise AdapterError("Rank and alpha must be positive and finite")
        if not base.weight.is_floating_point():
            raise AdapterError("Only floating-point Linear weights are supported")
        self.base = base
        self.base.requires_grad_(False)
        self.scale = alpha / rank
        self.lora_A = nn.Parameter(torch.empty(
            rank, base.in_features, device=base.weight.device, dtype=torch.float32))
        self.lora_B = nn.Parameter(torch.zeros(
            base.out_features, rank, device=base.weight.device, dtype=torch.float32))
        nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))
        self.register_buffer("_unmerged_weight", None, persistent=False)
        self.merged = False

    def forward(self, inputs: Tensor) -> Tensor:
        result = self.base(inputs)
        if self.merged:
            return result
        update = F.linear(F.linear(inputs.float(), self.lora_A), self.lora_B)
        return result + (update * self.scale).to(result.dtype)

    @torch.no_grad()
    def merge(self) -> None:
        """Merge once in eval mode, preserving exact base bytes for unmerge."""
        if self.training:
            raise AdapterError("Merging requires eval mode")
        if not self.merged:
            self._unmerged_weight = self.base.weight.detach().clone()
            delta = self.lora_B @ self.lora_A * self.scale
            self.base.weight.copy_((self.base.weight.float() + delta).to(self.base.weight.dtype))
            self.merged = True

    @torch.no_grad()
    def unmerge(self) -> None:
        """Restore the original base exactly, including bf16 roundoff."""
        if self.merged:
            self.base.weight.copy_(self._unmerged_weight)
            self._unmerged_weight = None
            self.merged = False

    def train(self, mode: bool = True) -> "LoRALinear":
        if mode:
            self.unmerge()
        return super().train(mode)


def inject(model: nn.Module, spec: AdapterSpec) -> None:
    """Preflight all exact Linear paths, then freeze base and inject adapters."""
    selected: list[tuple[str, nn.Linear]] = []
    identities: set[int] = set()
    for name in spec.targets:
        try:
            module = model.get_submodule(name)
        except AttributeError as error:
            raise AdapterError(f"Missing target: {name}") from error
        if type(module) is not nn.Linear or id(module) in identities:
            raise AdapterError(f"Expected a unique plain Linear target: {name}")
        if not module.weight.is_floating_point():
            raise AdapterError(f"Only floating-point targets are supported: {name}")
        if spec.rank > min(module.in_features, module.out_features):
            raise AdapterError(f"Rank exceeds target dimensions: {name}")
        selected.append((name, module))
        identities.add(id(module))
    model.requires_grad_(False)
    for name, module in selected:
        model.set_submodule(name, LoRALinear(module, spec.rank, spec.alpha))


def adapters(model: nn.Module) -> tuple[LoRALinear, ...]:
    """Return wrappers without including any frozen base tensors."""
    return tuple(module for module in model.modules() if isinstance(module, LoRALinear))


def save_adapter(model: nn.Module, spec: AdapterSpec, path: Path) -> None:
    """Save only LoRA tensors and base identity, never original model weights."""
    weights: dict[str, Tensor] = {}
    names = tuple(name for name, module in model.named_modules()
                  if isinstance(module, LoRALinear))
    if set(names) != set(spec.targets):
        raise AdapterError("Model target topology differs from spec")
    for name in spec.targets:
        module = model.get_submodule(name)
        if not isinstance(module, LoRALinear):
            raise AdapterError(f"Not an injected adapter: {name}")
        if module.lora_A.shape[0] != spec.rank or module.scale != spec.alpha / spec.rank:
            raise AdapterError("Model adapter configuration differs from spec")
        for key in ("lora_A", "lora_B"):
            weights[f"{name}.{key}"] = getattr(module, key).detach().cpu().clone()
    with path.open("xb") as handle:
        torch.save({"format": FORMAT, "spec": asdict(spec), "weights": weights}, handle)


def load_adapter(model: nn.Module, spec: AdapterSpec, path: Path) -> None:
    """Strict atomic validation before any parameter copy; use weights_only."""
    payload = torch.load(path, map_location="cpu", weights_only=True)
    if (set(payload) != {"format", "spec", "weights"}
            or payload["format"] != FORMAT or payload["spec"] != asdict(spec)):
        raise AdapterError("Checkpoint format or base/adapter identity mismatch")
    expected: dict[str, Tensor] = {}
    names = tuple(name for name, module in model.named_modules()
                  if isinstance(module, LoRALinear))
    if set(names) != set(spec.targets):
        raise AdapterError("Model target topology differs from spec")
    for name in spec.targets:
        module = model.get_submodule(name)
        if not isinstance(module, LoRALinear) or module.merged:
            raise AdapterError("Load requires unmerged injected targets")
        if module.scale != spec.alpha / spec.rank:
            raise AdapterError("Model adapter scale differs from spec")
        expected[f"{name}.lora_A"] = module.lora_A
        expected[f"{name}.lora_B"] = module.lora_B
    weights = payload["weights"]
    if set(weights) != set(expected):
        raise AdapterError("Checkpoint tensor keys differ")
    for name, target in expected.items():
        source = weights[name]
        if (not isinstance(source, Tensor) or source.shape != target.shape
                or source.dtype != torch.float32 or not torch.isfinite(source).all()):
            raise AdapterError(f"Invalid checkpoint tensor: {name}")
    with torch.no_grad():
        for name, target in expected.items():
            target.copy_(weights[name].to(target.device))
