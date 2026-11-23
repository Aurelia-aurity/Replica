"""Tokenizer/model test boundary; never a production encoder or KURE result."""
from types import SimpleNamespace

from encoding_types import EncodingError


class ModelBoundary:
    def __init__(self):
        self.max_seq_length = 999
        self.do_lower_case = False
        self.row_override = None
        self.seen = []
        self.tokenizer = self.tokenize

    def tokenize(self, text, **kwargs):
        if kwargs != dict(add_special_tokens=True, truncation=False, padding=False):
            raise EncodingError("unsafe_tokenizer_policy")
        return {"input_ids": list(range(len(text) + 2))}

    def __getitem__(self, index):
        return self

    def get_sentence_embedding_dimension(self):
        return 1024

    def encode(self, sentences, **kwargs):
        if kwargs != dict(batch_size=8, device="cpu", prompt="", normalize_embeddings=False,
                          convert_to_numpy=False, convert_to_tensor=True,
                          show_progress_bar=False, precision="float32"):
            raise EncodingError("unsafe_encoding_policy")
        self.seen.extend(sentences)
        rows = self.row_override
        if rows is None:
            rows = [[3.0, float(i + 4)] + [0.0] * 1022 for i in range(len(sentences))]
        return SimpleNamespace(tolist=lambda: rows)
