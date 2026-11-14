"""CPU-only tensor/model doubles for exercising production timing boundaries."""
from contextlib import nullcontext
from types import SimpleNamespace

from inference import Inference
from persona_adapter import PersonaInference


class Tensor:
    def __init__(self, rows):
        self.rows = rows

    def __getitem__(self, key):
        row, columns = key
        return SimpleNamespace(tolist=lambda: self.rows[row][columns])

    def numel(self):
        return sum(len(row) for row in self.rows)


class Tokenizer:
    eos_token_id = 2

    def encode(self, text, **kwargs):
        return [ord(c) for c in text]

    def apply_chat_template(self, messages, **kwargs):
        return [ord(c) for m in messages for c in m["content"]]

    def decode(self, tokens, **kwargs):
        return "".join(chr(token) for token in tokens if token != self.eos_token_id)


class Model:
    generation_config = SimpleNamespace(eos_token_id=2)

    def generate(self, *, input_ids, attention_mask, generation_config,
                 use_model_defaults, streamer=None, logits_processor=()):
        # Synthetic completion only: this double does not simulate logits or GPU work.
        text = '{"message":"Synthetic answer.","evidence_used":[]}'
        tokens = ([ord(c) for c in text] + [2])[:generation_config.max_new_tokens]
        if streamer:
            streamer.put(input_ids)
            for token in tokens:
                streamer.put(Tensor([[token]]))
            streamer.end()
        return Tensor([list(input_ids.rows[0]) + tokens])


class DoubleEngine(Inference):
    """Use real preparation/decoding/telemetry with CPU tensor and model boundaries."""
    def __init__(self):
        self.context_limit, self.system_prompt = 100000, "Synthetic system."
        self.tokenizer, self.model = Tokenizer(), Model()
        self.generation_config = lambda **kw: SimpleNamespace(**kw)
        self.torch = SimpleNamespace(
            tensor=lambda rows, **kw: Tensor(rows), ones_like=lambda inputs: inputs,
            inference_mode=nullcontext, cuda=SimpleNamespace(synchronize=lambda device: None))


class DoublePersona(PersonaInference):
    def __init__(self):
        super().__init__(DoubleEngine(), "local-double")
