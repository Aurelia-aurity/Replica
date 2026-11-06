"""Offline supervised adapter loop over reviewed, pretokenized synthetic DEV."""

import argparse
from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import itertools
import json
from pathlib import Path
from typing import Final

import torch
from torch import Tensor, nn

from .adapter import AdapterSpec, adapters, inject, save_adapter

BASE_MODEL: Final = "Qwen/Qwen3-8B"
REVISION: Final = "b968826d9c46dd6066d109eabc6255188de91218"
TARGETS: Final = tuple(
    f"model.layers.{layer}.self_attn.{projection}"
    for layer in range(36) for projection in ("q_proj", "v_proj")
)


class TrainingError(ValueError):
    """Invalid training input or unsafe numerical/training state."""


@dataclass(frozen=True, slots=True)
class TrainConfig:
    epochs: int = 2
    accumulation: int = 8
    max_sequence: int = 4096
    learning_rate: float = 1e-4
    seed: int = 88

    def __post_init__(self) -> None:
        if (self.epochs not in (1, 2) or self.accumulation < 1
                or self.max_sequence < 2 or self.max_sequence > 4096
                or not 0 < self.learning_rate < float("inf")):
            raise TrainingError("Invalid bounded pilot training configuration")


@dataclass(frozen=True, slots=True)
class Batch:
    """One microbatch; labels mask prompt/padding with -100."""

    input_ids: Tensor
    attention_mask: Tensor
    labels: Tensor

    def validate(self, max_sequence: int) -> None:
        if (self.input_ids.ndim != 2 or self.input_ids.shape[0] != 1
                or not 2 <= self.input_ids.shape[1] <= max_sequence
                or self.attention_mask.shape != self.input_ids.shape
                or self.labels.shape != self.input_ids.shape):
            raise TrainingError("Expected matching batch-one 2D tensors within sequence cap")
        if any(t.dtype != torch.long for t in
               (self.input_ids, self.attention_mask, self.labels)):
            raise TrainingError("Token, attention and label tensors must be int64")
        if (self.input_ids < 0).any() or not (
                (self.attention_mask == 0) | (self.attention_mask == 1)).all():
            raise TrainingError("Invalid token IDs or binary attention mask")
        visible = self.labels != -100
        if ((visible & (self.attention_mask == 0)).any()
                or not torch.equal(self.labels[visible], self.input_ids[visible])
                or not (self.labels[:, 1:] != -100).any()
                or (self.labels[:, 0] != -100).any()):
            raise TrainingError("Invalid shifted assistant labels or unmasked padding/prompt")


@dataclass(frozen=True, slots=True)
class EpochResult:
    epoch: int
    optimizer_steps: int
    supervised_tokens: int
    mean_loss: float


def train_epochs(
    model: nn.Module, batches: tuple[Batch, ...], config: TrainConfig,
    on_epoch: Callable[[EpochResult], None] | None = None,
    on_step: Callable[[int, int], None] | None = None,
    on_metrics: Callable[[int, float, float], None] | None = None,
) -> tuple[EpochResult, ...]:
    """Train adapters only; token-weight accumulated loss handles short groups."""
    if not batches or not adapters(model):
        raise TrainingError("Nonempty reviewed batches and injected adapters required")
    for batch in batches:
        batch.validate(config.max_sequence)
    allowed = {id(p) for module in adapters(model)
               for p in (module.lora_A, module.lora_B)}
    trainable = [p for p in model.parameters() if p.requires_grad]
    if {id(p) for p in trainable} != allowed:
        raise TrainingError("Only all declared LoRA parameters may be trainable")
    if any(module.merged for module in adapters(model)):
        raise TrainingError("Unmerge adapters before training")
    torch.manual_seed(config.seed)
    model.train()
    optimizer = torch.optim.AdamW(
        trainable, lr=config.learning_rate, betas=(0.9, 0.999),
        eps=1e-8, weight_decay=0.0, foreach=False,
    )
    device = trainable[0].device
    results: list[EpochResult] = []
    for epoch in range(1, config.epochs + 1):
        # A local generator leaves unrelated caller RNG streams alone.
        order = torch.randperm(len(batches), generator=torch.Generator().manual_seed(
            config.seed + epoch)).tolist()
        iterator = iter(order)
        steps, count, total_loss = 0, 0, 0.0
        while group := tuple(itertools.islice(iterator, config.accumulation)):
            tokens = [int((batches[index].labels[:, 1:] != -100).sum()) for index in group]
            denominator = sum(tokens)
            group_loss = 0.0
            optimizer.zero_grad(set_to_none=True)
            for index, token_count in zip(group, tokens, strict=True):
                batch = batches[index]
                output = model(
                    input_ids=batch.input_ids.to(device),
                    attention_mask=batch.attention_mask.to(device),
                    labels=batch.labels.to(device), use_cache=False,
                )
                loss = output.loss
                if loss.ndim != 0 or not torch.isfinite(loss):
                    raise TrainingError("Loss must be a finite scalar")
                (loss * (token_count / denominator)).backward()
                total_loss += float(loss.detach()) * token_count
                group_loss += float(loss.detach()) * token_count
                count += token_count
            grad_norm = torch.nn.utils.clip_grad_norm_(
                trainable, max_norm=1.0, error_if_nonfinite=True)
            optimizer.step()
            steps += 1
            if on_metrics is not None:
                on_metrics(steps, group_loss / denominator, float(grad_norm))
            if on_step is not None:
                on_step(steps, count)
        results.append(EpochResult(epoch, steps, count, total_loss / count))
        if on_epoch is not None:
            on_epoch(results[-1])
    return tuple(results)


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def main() -> None:
    """Require a user-approved frozen local protocol; never download a model."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--protocol-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sha256(args.protocol) != args.protocol_sha256:
        raise TrainingError("Approved protocol hash differs")
    protocol = json.loads(args.protocol.read_text())
    config = TrainConfig(**protocol["training"])
    spec = AdapterSpec(BASE_MODEL, REVISION, TARGETS)
    if (protocol["status"] != "APPROVED_FROZEN" or not protocol["approved_by"]
            or not protocol["approved_at"] or protocol["data_sha256"] != sha256(args.data)
            or protocol["adapter"] != json.loads(json.dumps(asdict(spec)))
            or protocol["model_path"] != str(args.model_path.resolve())):
        raise TrainingError("Protocol must bind reviewed data, adapter and local model")
    # This is a review attestation, not an authentication or signature system.
    if args.output.exists():
        raise TrainingError("Output must be new; do not overwrite experiment artifacts")
    raw = torch.load(args.data, map_location="cpu", weights_only=True)
    if raw["split"] != "train" or raw["provenance"] != "reviewed-synthetic-dev":
        raise TrainingError("Only reviewed synthetic DEV training data is eligible")
    batches = tuple(Batch(**row) for row in raw["batches"])
    for batch in batches:
        batch.validate(config.max_sequence)
    # Import only after the argument/protocol boundary, including for --help.
    from transformers import AutoModelForCausalLM

    torch.manual_seed(config.seed)
    model = AutoModelForCausalLM.from_pretrained(
        args.model_path, local_files_only=True, trust_remote_code=False,
        use_safetensors=True, torch_dtype=torch.bfloat16, attn_implementation="eager",
    )
    if model.config.model_type != "qwen3" or model.config.num_hidden_layers != 36:
        raise TrainingError("Expected pinned Qwen3-8B architecture")
    model.config.use_cache = False
    model.gradient_checkpointing_enable(
        gradient_checkpointing_kwargs={"use_reentrant": False})
    inject(model, spec)
    model.to("cuda")
    args.output.mkdir(parents=True)
    # Preserve AdamW moments across epochs; selection is a separate DEV step.
    def checkpoint(result: EpochResult) -> None:
        save_adapter(model, spec, args.output / f"epoch-{result.epoch}.pt")
        with (args.output / f"epoch-{result.epoch}.json").open("x") as handle:
            json.dump(asdict(result), handle)

    train_epochs(model, batches, config, on_epoch=checkpoint)
    with (args.output / "manifest.json").open("x") as handle:
        json.dump({
            "protocol_sha256": args.protocol_sha256, "data_sha256": sha256(args.data),
            "adapter": asdict(spec), "training": asdict(config),
            "torch": torch.__version__,
        }, handle, indent=2)


if __name__ == "__main__":
    main()
