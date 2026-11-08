"""Deterministic CPU tests with random tiny weights, no network or checkpoints."""

from dataclasses import replace
from pathlib import Path
from typing import Literal, assert_never

import pytest
import torch
from torch import nn

from experiments.lora88.adapter import (
    AdapterError, AdapterSpec, LoRALinear, adapters, inject, load_adapter, save_adapter,
)


def fixture_model() -> tuple[nn.Sequential, AdapterSpec]:
    torch.manual_seed(88)
    model = nn.Sequential(nn.Linear(6, 8), nn.Tanh(), nn.Linear(8, 4))
    return model, AdapterSpec("tiny-random", "seed88", ("0", "2"), rank=2, alpha=4)


def nonzero(model: nn.Module) -> None:
    with torch.no_grad():
        for module in adapters(model):
            module.lora_B.fill_(0.12)


def test_injection_preserves_output_when_b_is_zero() -> None:
    # Given
    model, spec = fixture_model()
    inputs = torch.randn(3, 6)
    expected = model(inputs).detach()
    # When
    inject(model, spec)
    # Then
    torch.testing.assert_close(model(inputs), expected, rtol=0, atol=0)
    assert len(adapters(model)) == 2


def test_only_lora_parameters_are_trainable_when_injected() -> None:
    # Given
    model, spec = fixture_model()
    # When
    inject(model, spec)
    # Then
    assert {name for name, p in model.named_parameters() if p.requires_grad} == {
        "0.lora_A", "0.lora_B", "2.lora_A", "2.lora_B",
    }
    assert sum(p.numel() for p in model.parameters() if p.requires_grad) == 52


@pytest.mark.parametrize("targets", [("0", "missing"), ("0", "1"), ("0", "0")])
def test_bad_targets_leave_model_unchanged_when_preflight_fails(targets: tuple[str, ...]) -> None:
    # Given
    model, spec = fixture_model()
    original = tuple(model.parameters())
    # When
    with pytest.raises(AdapterError):
        inject(model, replace(spec, targets=targets))
    # Then
    assert not adapters(model)
    assert all(p.requires_grad for p in original)
    assert tuple(model.parameters()) == original


def test_base_is_frozen_when_optimizer_updates_adapter() -> None:
    # Given
    model, spec = fixture_model()
    inject(model, spec)
    frozen = {name: p.detach().clone() for name, p in model.named_parameters()
              if not p.requires_grad}
    before = model(torch.ones(2, 6)).detach()
    optimizer = torch.optim.SGD([p for p in model.parameters() if p.requires_grad], lr=0.1)
    # When
    model(torch.ones(2, 6)).square().sum().backward()
    optimizer.step()
    # Then
    for name, p in model.named_parameters():
        if not p.requires_grad:
            assert p.grad is None
            torch.testing.assert_close(p, frozen[name], rtol=0, atol=0)
    assert not torch.equal(model(torch.ones(2, 6)), before)


def test_nonfloating_target_is_rejected_before_base_freeze() -> None:
    # Given
    model, spec = fixture_model()
    model[2].weight = nn.Parameter(model[2].weight.to(torch.int64), requires_grad=False)
    # When
    with pytest.raises(AdapterError, match="floating"):
        inject(model, spec)
    # Then
    assert not adapters(model)
    assert model[0].weight.requires_grad


@pytest.mark.parametrize("dtype", [torch.float32, torch.bfloat16])
def test_merge_equivalence_and_exact_unmerge_when_nonzero(dtype: torch.dtype) -> None:
    # Given
    model, spec = fixture_model()
    model.to(dtype=dtype)
    inject(model, spec)
    nonzero(model)
    model.eval()
    inputs = torch.randn(3, 6).to(dtype)
    expected = model(inputs).detach()
    originals = [module.base.weight.clone() for module in adapters(model)]
    # When
    for module in adapters(model):
        module.merge()
        module.merge()  # Idempotence avoids a doubled delta.
    merged = model(inputs).detach()
    for module in adapters(model):
        module.unmerge()
        module.unmerge()
    # Then
    tolerance = 0.02 if dtype == torch.bfloat16 else 1e-6
    torch.testing.assert_close(merged, expected, rtol=tolerance, atol=tolerance)
    torch.testing.assert_close(model(inputs), expected, rtol=0, atol=0)
    for module, original in zip(adapters(model), originals, strict=True):
        torch.testing.assert_close(module.base.weight, original, rtol=0, atol=0)
        assert module.lora_A.dtype == torch.float32
    assert not any("_unmerged_weight" in key for key in model.state_dict())


def test_merge_rejected_when_training() -> None:
    # Given
    module = LoRALinear(nn.Linear(6, 8), 2, 4)
    # When / Then
    with pytest.raises(AdapterError, match="eval"):
        module.merge()


def test_train_automatically_unmerges_when_eval_weights_were_merged() -> None:
    # Given
    model, spec = fixture_model()
    inject(model, spec)
    nonzero(model)
    model.eval()
    originals = [module.base.weight.clone() for module in adapters(model)]
    for module in adapters(model):
        module.merge()
    # When
    model.train()
    # Then
    for module, original in zip(adapters(model), originals, strict=True):
        assert not module.merged
        torch.testing.assert_close(module.base.weight, original, rtol=0, atol=0)


def test_save_load_matches_output_when_same_base(tmp_path: Path) -> None:
    # Given
    model, spec = fixture_model()
    inject(model, spec)
    nonzero(model)
    other, _ = fixture_model()
    inject(other, spec)
    inputs = torch.randn(3, 6)
    path = tmp_path / "adapter.pt"
    save_adapter(model, spec, path)
    # When
    load_adapter(other, spec, path)
    # Then
    torch.testing.assert_close(other(inputs), model(inputs), rtol=0, atol=0)
    payload = torch.load(path, weights_only=True)
    assert set(payload["weights"]) == {
        f"{target}.{key}" for target in spec.targets for key in ("lora_A", "lora_B")
    }
    with pytest.raises(FileExistsError):
        save_adapter(model, spec, path)


def test_load_rejects_identity_when_base_differs(tmp_path: Path) -> None:
    # Given
    model, spec = fixture_model()
    inject(model, spec)
    path = tmp_path / "adapter.pt"
    save_adapter(model, spec, path)
    # When / Then
    with pytest.raises(AdapterError, match="identity"):
        load_adapter(model, replace(spec, revision="different"), path)


@pytest.mark.parametrize("defect", ["shape", "missing", "nan", "dtype"])
def test_load_is_atomic_when_checkpoint_invalid(
    tmp_path: Path, defect: Literal["shape", "missing", "nan", "dtype"],
) -> None:
    # Given
    model, spec = fixture_model()
    inject(model, spec)
    path = tmp_path / "adapter.pt"
    save_adapter(model, spec, path)
    payload = torch.load(path, weights_only=True)
    before = {name: p.clone() for name, p in model.named_parameters()}
    payload["weights"]["0.lora_B"].fill_(1)
    match defect:
        case "shape":
            payload["weights"]["2.lora_B"] = torch.zeros(1)
        case "missing":
            del payload["weights"]["2.lora_B"]
        case "nan":
            payload["weights"]["2.lora_B"].fill_(float("nan"))
        case "dtype":
            payload["weights"]["2.lora_B"] = payload["weights"]["2.lora_B"].to(torch.bfloat16)
        case unexpected:
            assert_never(unexpected)
    bad = tmp_path / "bad.pt"
    torch.save(payload, bad)
    # When
    with pytest.raises(AdapterError):
        load_adapter(model, spec, bad)
    # Then
    for name, p in model.named_parameters():
        torch.testing.assert_close(p, before[name], rtol=0, atol=0)
