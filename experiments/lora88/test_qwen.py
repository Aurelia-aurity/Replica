"""Tiny random Qwen3 integration using the existing transformers installation."""

from pathlib import Path

import torch
from transformers import Qwen3Config, Qwen3ForCausalLM

from experiments.lora88.adapter import (
    AdapterSpec, adapters, inject, load_adapter, save_adapter,
)
from experiments.lora88.train import Batch, TrainConfig, train_epochs


def tiny_qwen() -> Qwen3ForCausalLM:
    torch.manual_seed(88)
    return Qwen3ForCausalLM(Qwen3Config(
        vocab_size=32, hidden_size=32, intermediate_size=64, num_hidden_layers=2,
        num_attention_heads=4, num_key_value_heads=2, head_dim=8,
        attention_dropout=0.0, use_cache=False,
    ))


def test_qwen_checkpointed_training_and_reload_when_base_frozen(tmp_path: Path) -> None:
    # Given: same architecture/API, randomly initialized tiny weights, no downloads.
    model = tiny_qwen()
    spec = AdapterSpec("tiny-qwen3-random", "seed88", tuple(
        f"model.layers.{layer}.self_attn.{projection}"
        for layer in range(2) for projection in ("q_proj", "v_proj")
    ), 2, 4)
    model.gradient_checkpointing_enable(
        gradient_checkpointing_kwargs={"use_reentrant": False})
    inject(model, spec)
    frozen = {name: p.clone() for name, p in model.named_parameters()
              if not p.requires_grad}
    ids = torch.tensor([[1, 2, 3, 4, 5]])
    batch = Batch(ids, torch.ones_like(ids), torch.tensor([[-100, -100, 3, 4, 5]]))
    # When
    results = train_epochs(model, (batch,), TrainConfig(epochs=1))
    # Then: real Transformers checkpointing/backward changes adapters, not base.
    assert results[0].optimizer_steps == 1
    assert any(torch.count_nonzero(module.lora_B) > 0 for module in adapters(model))
    for name, p in model.named_parameters():
        if not p.requires_grad:
            assert p.grad is None
            torch.testing.assert_close(p, frozen[name], rtol=0, atol=0)
    model.eval()
    expected = model(ids).logits.detach()
    checkpoint = tmp_path / "adapter.pt"
    save_adapter(model, spec, checkpoint)
    fresh = tiny_qwen()
    inject(fresh, spec)
    load_adapter(fresh, spec, checkpoint)
    fresh.eval()
    torch.testing.assert_close(fresh(ids).logits, expected, rtol=0, atol=0)
    for module in adapters(fresh):
        module.merge()
    torch.testing.assert_close(fresh(ids).logits, expected, rtol=1e-5, atol=1e-6)
