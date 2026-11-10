"""Local, non-thinking greedy inference; no HTTP or request scheduling."""
from dataclasses import dataclass

import baseline


class ContextLimitExceeded(Exception):
    """The current question and unchanged budget do not fit."""


@dataclass(frozen=True)
class Request:
    request_id: str
    message: str
    history: tuple[tuple[str, str], ...]
    max_new_tokens: int


@dataclass(frozen=True)
class Prepared:
    token_ids: tuple[int, ...]
    used_history_turns: int
    dropped_history_turns: int


@dataclass(frozen=True)
class Generated:
    message: str
    finish_reason: str
    output_tokens: int


class Inference:
    def __init__(self, settings, context_limit: int | None = None):
        self.torch, self.generation_config, self.tokenizer, self.model = baseline.load_runtime(settings)
        self.system_prompt = settings["system_prompt"]
        limits = [self.model.config.max_position_embeddings]
        tokenizer_limit = self.tokenizer.model_max_length
        if type(tokenizer_limit) is int and tokenizer_limit > 0:
            limits.append(tokenizer_limit)
        if context_limit is not None:
            limits.append(context_limit)
        self.context_limit = min(limits)

    def prepare(self, request: Request) -> Prepared:
        for dropped in range(len(request.history) + 1):
            messages = [{"role": "system", "content": self.system_prompt}]
            for user, assistant in request.history[dropped:]:
                messages.extend([{"role": "user", "content": user},
                                 {"role": "assistant", "content": assistant}])
            messages.append({"role": "user", "content": request.message})
            ids = self.tokenizer.apply_chat_template(
                messages, tokenize=True, add_generation_prompt=True, enable_thinking=False)
            if len(ids) + request.max_new_tokens <= self.context_limit:
                return Prepared(tuple(ids), len(request.history) - dropped, dropped)
        raise ContextLimitExceeded

    def generate(self, request: Request, prepared: Prepared) -> Generated:
        torch = self.torch
        try:
            inputs = torch.tensor([prepared.token_ids], device="cuda:0")
            generation = self.generation_config(
                max_new_tokens=request.max_new_tokens, do_sample=False,
                eos_token_id=self.model.generation_config.eos_token_id,
                pad_token_id=self.tokenizer.eos_token_id)
            with torch.inference_mode():
                outputs = self.model.generate(
                    input_ids=inputs, attention_mask=torch.ones_like(inputs),
                    generation_config=generation, use_model_defaults=False)
            tokens = outputs[0, len(prepared.token_ids):].tolist()
            eos = generation.eos_token_id
            eos_ids = eos if isinstance(eos, (list, tuple)) else [eos]
            stopped = bool(tokens) and tokens[-1] in eos_ids
            reason = "length" if len(tokens) >= request.max_new_tokens and not stopped else "stop"
            return Generated(self.tokenizer.decode(tokens, skip_special_tokens=True), reason, len(tokens))
        finally:
            # Also drain already-launched kernels when Python generation fails.
            # The service may return the slot only after this worker has finished.
            torch.cuda.synchronize(0)
