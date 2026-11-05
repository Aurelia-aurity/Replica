"""CPU loop checks on a tiny randomly initialized causal model."""

from dataclasses import dataclass, replace
from typing import Literal, assert_never

import pytest
import torch
from torch import Tensor, nn
from torch.nn import functional as F

from experiments.lora88.adapter import AdapterSpec, inject
from experiments.lora88.train import Batch, TrainConfig, TrainingError, train_epochs


@dataclass(frozen=True, slots=True)
class Output:
    loss: Tensor


class TinyLM(nn.Module):
    """Real differentiable random model; no mocked loss or optimizer."""

    def __init__(self) -> None:
        super().__init__()
        self.embedding = nn.Embedding(16, 8)
        self.q_proj = nn.Linear(8, 8)
        self.v_proj = nn.Linear(8, 8)
        self.head = nn.Linear(8, 16)

    def forward(
        self, input_ids: Tensor, attention_mask: Tensor,
        labels: Tensor, use_cache: bool,
    ) -> Output:
        hidden = self.embedding(input_ids) * attention_mask.unsqueeze(-1)
        logits = self.head(torch.tanh(self.q_proj(hidden) + self.v_proj(hidden)))
        return Output(F.cross_entropy(
            logits[:, :-1].reshape(-1, 16), labels[:, 1:].reshape(-1), ignore_index=-100))


def setup() -> tuple[TinyLM, tuple[Batch, ...]]:
    torch.manual_seed(88)
    model = TinyLM()
    inject(model, AdapterSpec("tiny-random", "seed88", ("q_proj", "v_proj"), 2, 4))
    batches = tuple(
        Batch(torch.tensor([[1, 2, 3, end]]), torch.ones(1, 4, dtype=torch.long),
              torch.tensor([[-100, -100, 3, end]]))
        for end in (4, 5, 6)
    )
    return model, batches


def test_loop_updates_adapters_and_keeps_base_when_accumulation_has_remainder() -> None:
    # Given
    model, batches = setup()
    before = {name: p.clone() for name, p in model.named_parameters()}
    recorded = []
    # When
    results = train_epochs(
        model, batches, TrainConfig(accumulation=2), on_epoch=recorded.append)
    # Then
    assert results == tuple(recorded)
    assert [r.optimizer_steps for r in results] == [2, 2]
    assert [r.supervised_tokens for r in results] == [6, 6]
    assert all(0 < r.mean_loss < float("inf") for r in results)
    for name, p in model.named_parameters():
        if not p.requires_grad:
            assert p.grad is None
            torch.testing.assert_close(p, before[name], rtol=0, atol=0)
    assert not torch.equal(model.q_proj.lora_B, before["q_proj.lora_B"])


def test_loop_is_deterministic_when_seed_and_inputs_match() -> None:
    # Given
    one, batches = setup()
    two, _ = setup()
    # When
    first = train_epochs(one, batches, TrainConfig(accumulation=2))
    second = train_epochs(two, batches, TrainConfig(accumulation=2))
    # Then
    assert first == second
    for a, b in zip(one.parameters(), two.parameters(), strict=True):
        torch.testing.assert_close(a, b, rtol=0, atol=0)


@pytest.mark.parametrize("defect", ["all_masked", "padding", "label", "first", "length"])
def test_loop_rejects_invalid_labels_before_update_when_data_bad(
    defect: Literal["all_masked", "padding", "label", "first", "length"],
) -> None:
    # Given
    model, batches = setup()
    batch = batches[0]
    labels = batch.labels.clone()
    mask = batch.attention_mask.clone()
    cap = 1024
    match defect:
        case "all_masked":
            labels.fill_(-100)
        case "padding":
            mask[0, -1] = 0
        case "label":
            labels[0, -1] = 15
        case "first":
            labels[0, 0] = 1
        case "length":
            cap = 3
        case unexpected:
            assert_never(unexpected)
    bad = replace(batch, labels=labels, attention_mask=mask)
    before = [p.clone() for p in model.parameters()]
    # When
    with pytest.raises(TrainingError):
        train_epochs(model, (bad,), TrainConfig(max_sequence=cap))
    # Then
    for p, original in zip(model.parameters(), before, strict=True):
        torch.testing.assert_close(p, original, rtol=0, atol=0)


def test_loop_rejects_unfrozen_base_when_trainable_set_wrong() -> None:
    # Given
    model, batches = setup()
    model.embedding.weight.requires_grad_(True)
    # When / Then
    with pytest.raises(TrainingError, match="LoRA"):
        train_epochs(model, batches, TrainConfig())


def test_token_weighted_accumulation_matches_single_group_when_lengths_differ() -> None:
    # Given
    model, batches = setup()
    reference, _ = setup()
    short = replace(batches[0], labels=torch.tensor([[-100, -100, -100, 4]]))
    batch_pair = (short, batches[1])
    optimizer = torch.optim.AdamW(
        [p for p in reference.parameters() if p.requires_grad], lr=1e-4,
        weight_decay=0.0, foreach=False)
    losses = [reference(b.input_ids, b.attention_mask, b.labels, False).loss
              for b in batch_pair]
    (losses[0] / 3 + losses[1] * 2 / 3).backward()
    torch.nn.utils.clip_grad_norm_(
        [p for p in reference.parameters() if p.requires_grad], 1.0)
    optimizer.step()
    # When
    train_epochs(model, batch_pair, TrainConfig(epochs=1, accumulation=8))
    # Then
    for actual, expected in zip(model.parameters(), reference.parameters(), strict=True):
        torch.testing.assert_close(actual, expected, atol=1e-7, rtol=1e-6)
