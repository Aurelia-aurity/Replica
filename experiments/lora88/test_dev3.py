"""DEV3 data and real CPU PEFT parity without network or pretrained weights."""

import copy
from pathlib import Path

import torch

from experiments.lora88.adapter import AdapterSpec, inject
from experiments.lora88.dev3_data import reinforce
from experiments.lora88.train import TrainConfig, train_epochs


def test_reinforcement_preserves_other_rows_and_input_facts() -> None:
    # Given
    rows = [{"type": kind, "family_cluster": "l88-dev2-train-036", "register": register,
             "text": "unknown", "source_answer": "unknown", "evidence": [], "slots": []}
            for kind in ("unanswerable", "multi") for register in ("반말", "해요체", "하게체")]
    original = copy.deepcopy(rows)
    # When
    changed = reinforce(rows)
    # Then
    assert rows == original
    assert changed[3:] == original[3:]
    assert all(a["text"].startswith(b["text"]) and len(a["text"]) > len(b["text"])
               for a, b in zip(changed[:3], original[:3], strict=True))
    assert all(a["evidence"] == b["evidence"] and a["slots"] == b["slots"]
               for a, b in zip(changed, original, strict=True))


def peft_cpu_parity(checkpoint: Path) -> None:
    """Real Qwen forward/backward, initialized tensors, reload and merge."""
    from peft import PeftModel
    from transformers import Qwen3Config, Qwen3ForCausalLM
    from experiments.lora88.peft_backend import inject_peft, save_peft
    from experiments.lora88.train import Batch

    torch.set_num_threads(1)
    torch.manual_seed(12)
    base = Qwen3ForCausalLM(Qwen3Config(
        vocab_size=32, hidden_size=16, intermediate_size=32, num_hidden_layers=1,
        num_attention_heads=2, num_key_value_heads=1, head_dim=8,
        attention_dropout=0.0, tie_word_embeddings=False))
    old, new = copy.deepcopy(base), copy.deepcopy(base)
    spec = AdapterSpec("tiny-qwen", "random12",
                       ("model.layers.0.self_attn.q_proj", "model.layers.0.self_attn.v_proj"), 2, 4)
    torch.manual_seed(88)
    inject(old, spec)
    torch.manual_seed(88)
    new = inject_peft(new, spec)
    for name in spec.targets:
        legacy = old.get_submodule(name)
        peft = new.base_model.model.get_submodule(name)
        torch.testing.assert_close(legacy.lora_A, peft.lora_A["default"].weight, atol=0, rtol=0)
        torch.testing.assert_close(legacy.lora_B, peft.lora_B["default"].weight, atol=0, rtol=0)
    batches = tuple(Batch(
        torch.tensor([[1, 2, 3, end]]), torch.ones(1, 4, dtype=torch.long),
        torch.tensor([[-100, -100, 3, end]])) for end in (4, 5, 6))
    config = TrainConfig(epochs=1, accumulation=2, learning_rate=2e-5)
    first = train_epochs(old, batches, config)
    second = train_epochs(new, batches, config, peft_backend=True)
    assert abs(first[0].mean_loss - second[0].mean_loss) < 1e-6
    old.eval()
    new.eval()
    ids = batches[0].input_ids
    expected = old(input_ids=ids).logits
    torch.testing.assert_close(expected, new(input_ids=ids).logits, atol=1e-6, rtol=1e-6)
    save_peft(new, checkpoint)
    loaded = PeftModel.from_pretrained(copy.deepcopy(base), checkpoint).eval()
    torch.testing.assert_close(expected, loaded(input_ids=ids).logits, atol=1e-6, rtol=1e-6)
    merged = loaded.merge_and_unload(safe_merge=True).eval()
    torch.testing.assert_close(expected, merged(input_ids=ids).logits, atol=1e-6, rtol=1e-6)
