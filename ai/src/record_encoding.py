"""Optional CPU-only local encoding; no persistence, network or ID issuance."""
from collections.abc import Sequence
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import struct
from typing import Protocol, TypedDict

from encoding_types import (
    EncodedRows, EncodingError, EncodingLimits, EncodingProvenance, SourceRecord,
)
from record_chunking import chunk_record


@dataclass(frozen=True, slots=True)
class EncodingCandidate:
    purpose: str = "offline-local-candidate"
    model_id: str = "nlpai-lab/KURE-v1"
    intended_revision: str = "8b418a58414668e75532ed045c22d9ca018ae2b2"
    dimensions: int = 1024
    max_tokens: int = 512
    query_prefix: str = ""
    passage_prefix: str = ""
    device: str = "cpu"
    batch_size: int = 8
    normalization: str = "l2-float32"
    chunker_version: str = "lossless-unit-spans-v1"


def read_candidate(path: Path) -> EncodingCandidate:
    """Accept only the explicit reproducibility candidate, not deployment config."""
    candidate = EncodingCandidate()
    try:
        configured = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise EncodingError("candidate_configuration_unavailable") from None
    if configured != asdict(candidate):
        raise EncodingError("unsupported_encoding_candidate")
    return candidate


class Tokenized(TypedDict):
    input_ids: list[int]


class LocalTokenizer(Protocol):
    def __call__(self, text: str, *, add_special_tokens: bool,
                 truncation: bool, padding: bool) -> Tokenized: ...


class TransformerBoundary(Protocol):
    do_lower_case: bool
    tokenizer: LocalTokenizer


class TensorRows(Protocol):
    def tolist(self) -> list[list[float]]: ...


class LocalModel(Protocol):
    max_seq_length: int

    def __getitem__(self, index: int) -> TransformerBoundary: ...

    def get_sentence_embedding_dimension(self) -> int: ...

    def encode(self, sentences: list[str], *, batch_size: int, device: str,
               prompt: str, normalize_embeddings: bool, convert_to_numpy: bool,
               convert_to_tensor: bool, show_progress_bar: bool,
               precision: str) -> TensorRows: ...


def _float32(value: float) -> float:
    try:
        result = struct.unpack("f", struct.pack("f", value))[0]
    except (OverflowError, struct.error):
        raise EncodingError("float32_overflow") from None
    if not math.isfinite(result):
        raise EncodingError("nonfinite_vector")
    return result


class LocalEncoder:
    """Own one loaded optional model. Constructor always loads a real local path."""

    def __init__(self, model_path: Path, candidate: EncodingCandidate = EncodingCandidate()):
        if candidate != EncodingCandidate():
            raise EncodingError("unsupported_encoding_candidate")
        path = model_path.resolve()
        if not path.is_dir():
            raise EncodingError("local_model_directory_required")
        # Standard ST modules only; never fall back to inferred mean pooling.
        try:
            modules = json.loads((path / "modules.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise EncodingError("local_module_manifest_required") from None
        allowed = {
            "sentence_transformers.models.Transformer",
            "sentence_transformers.models.Pooling",
            "sentence_transformers.models.Normalize",
        }
        if (not isinstance(modules, list) or not modules
                or not all(isinstance(module, dict) for module in modules)
                or modules[0].get("type") != "sentence_transformers.models.Transformer"):
            raise EncodingError("unsupported_local_modules")
        for module in modules:
            if (module.get("type") not in allowed or not isinstance(module.get("path"), str)
                    or not (path / module["path"]).resolve().is_relative_to(path)):
                raise EncodingError("unsupported_local_modules")
        model_config = path / "config_sentence_transformers.json"
        if model_config.exists():
            try:
                configured = json.loads(model_config.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise EncodingError("invalid_local_model_type") from None
            if (not isinstance(configured, dict)
                    or configured.get("model_type", "SentenceTransformer") != "SentenceTransformer"):
                raise EncodingError("invalid_local_model_type")
        try:
            from sentence_transformers import SentenceTransformer, __version__
        except ImportError:
            raise EncodingError("encoding_dependencies_unavailable") from None
        if __version__ != "5.1.2":
            raise EncodingError("unsupported_encoding_runtime")
        model: LocalModel = SentenceTransformer(
            str(path), device="cpu", local_files_only=True, trust_remote_code=False,
            token=False, backend="torch",
            model_kwargs={"local_files_only": True, "trust_remote_code": False,
                          "use_safetensors": True},
            tokenizer_kwargs={"local_files_only": True, "trust_remote_code": False},
            config_kwargs={"local_files_only": True, "trust_remote_code": False},
        )
        if model.get_sentence_embedding_dimension() != candidate.dimensions:
            raise EncodingError("model_dimension_mismatch")
        model.max_seq_length = candidate.max_tokens
        self._model = model
        self.candidate = candidate
        self.provenance = EncodingProvenance(candidate.model_id, candidate.intended_revision)

    def count_tokens(self, rendered: str) -> int:
        """Count full rendering and ST5.1.2's strip/lower preprocessing, untruncated."""
        transformer = self._model[0]
        processed = rendered.strip()
        if transformer.do_lower_case:
            processed = processed.lower()
        return max(len(transformer.tokenizer(text, add_special_tokens=True,
                                             truncation=False, padding=False)["input_ids"])
                   for text in (rendered, processed))

    def _encode(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        for text in texts:
            if self.count_tokens(text) > self.candidate.max_tokens:
                raise EncodingError("encoder_token_limit")
        tensor = self._model.encode(
            list(texts), batch_size=self.candidate.batch_size, device="cpu", prompt="",
            normalize_embeddings=False, convert_to_numpy=False, convert_to_tensor=True,
            show_progress_bar=False, precision="float32",
        )
        raw = tensor.tolist()
        if not isinstance(raw, list) or len(raw) != len(texts):
            raise EncodingError("embedding_row_count")
        rows = []
        for row in raw:
            if not isinstance(row, list) or len(row) != self.candidate.dimensions:
                raise EncodingError("embedding_dimension")
            if any(type(value) not in (int, float) for value in row):
                raise EncodingError("invalid_vector_number")
            finite = tuple(_float32(value) for value in row)
            norm = math.hypot(*finite)
            if norm == 0:
                raise EncodingError("zero_vector")
            normalized = tuple(_float32(value / norm) for value in finite)
            if not math.isclose(math.hypot(*normalized), 1.0, rel_tol=1e-6):
                raise EncodingError("vector_normalization")
            rows.append(normalized)
        return tuple(rows)

    def encode_records(
        self, records: tuple[SourceRecord, ...], limits: EncodingLimits = EncodingLimits(),
    ) -> EncodedRows:
        """Produce only chunk drafts/vectors; no DB freshness or index readiness claim."""
        if not records:
            raise EncodingError("empty_sources")
        if (sum(len(r.units) for r in records) > limits.max_units
                or sum(len(u.text) for r in records for u in r.units) > limits.max_codepoints):
            raise EncodingError("input_budget")
        drafts = tuple(d for record in records
                       for d in chunk_record(record, self.count_tokens, limits))
        if len(drafts) > limits.max_chunks:
            raise EncodingError("chunk_budget")
        return EncodedRows(self._encode(tuple(d.rendered() for d in drafts)),
                           self.provenance, drafts)

    def encode_queries(
        self, queries: tuple[str, ...], limits: EncodingLimits = EncodingLimits(),
    ) -> EncodedRows:
        """Encode explicit queries under the same candidate, without truncation."""
        if not queries or any(not text for text in queries):
            raise EncodingError("empty_query")
        if len(queries) > limits.max_units or sum(map(len, queries)) > limits.max_codepoints:
            raise EncodingError("input_budget")
        return EncodedRows(self._encode(queries), self.provenance)
