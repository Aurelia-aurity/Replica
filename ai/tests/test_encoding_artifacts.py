"""Reject unsafe local weight layouts before entering the optional SDK."""
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from encoding_types import EncodingError
from record_encoding import LocalEncoder
from record_encoding_doubles import ModelBoundary


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        (self.path / "modules.json").write_text(json.dumps([
            {"type": "sentence_transformers.models.Transformer", "path": ""}]))
        self.runtime = ModuleType("sentence_transformers")
        self.runtime.__version__ = "5.1.2"
        self.runtime.SentenceTransformer = Mock(return_value=ModelBoundary())
        runtime = patch.dict(sys.modules, {"sentence_transformers": self.runtime})
        runtime.start()
        self.addCleanup(runtime.stop)

    def test_bin_shard_in_safetensors_index_never_enters_dependency(self):
        (self.path / "model.safetensors.index.json").write_text(json.dumps({
            "metadata": {}, "weight_map": {"weight": "payload.bin"}}))
        (self.path / "payload.bin").touch()
        (self.path / "sentence_bert_config.json").write_text(json.dumps({
            "model_args": {"weights_only": False}}))
        with self.assertRaisesRegex(EncodingError, "unsafe_local_weights"):
            LocalEncoder(self.path)
        self.runtime.SentenceTransformer.assert_not_called()

    def test_adapter_directory_never_enters_dependency(self):
        (self.path / "adapter_config.json").write_text("{}")
        with self.assertRaisesRegex(EncodingError, "unsafe_local_weights"):
            LocalEncoder(self.path)
        self.runtime.SentenceTransformer.assert_not_called()

    def test_shards_require_present_contained_safetensors_files(self):
        for shard in ("../outside.safetensors", "/outside.safetensors",
                      "missing.safetensors", 1):
            with self.subTest(shard=shard):
                (self.path / "model.safetensors.index.json").write_text(json.dumps({
                    "weight_map": {"weight": shard}}))
                with self.assertRaisesRegex(EncodingError, "unsafe_local_weights"):
                    LocalEncoder(self.path)
        self.runtime.SentenceTransformer.assert_not_called()

    def test_valid_shards_load_with_weights_only_override(self):
        (self.path / "part.safetensors").touch()
        (self.path / "model.safetensors.index.json").write_text(json.dumps({
            "metadata": {}, "weight_map": {"weight": "part.safetensors"}}))
        (self.path / "sentence_bert_config.json").write_text(json.dumps({
            "model_args": {"weights_only": False}}))
        LocalEncoder(self.path)
        self.assertIs(
            self.runtime.SentenceTransformer.call_args.kwargs["model_kwargs"].get("weights_only"),
            True)
