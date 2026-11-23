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

    def test_variant_bin_shard_never_enters_dependency(self):
        (self.path / "sentence_bert_config.json").write_text(json.dumps({
            "model_args": {"variant": "x"}}))
        (self.path / "model.safetensors.index.x.json").write_text(json.dumps({
            "metadata": {}, "weight_map": {"weight": "payload.bin"}}))
        (self.path / "payload.bin").touch()
        with self.assertRaisesRegex(EncodingError, "unsafe_local_weights"):
            LocalEncoder(self.path)
        self.runtime.SentenceTransformer.assert_not_called()

    def test_mixed_case_bin_shard_indexes_never_enter_dependency(self):
        (self.path / "payload.bin").touch()
        for name in ("MODEL.SAFETENSORS.INDEX.JSON", "Model.Safetensors.Index.X.Json"):
            with self.subTest(index=name):
                index = self.path / name
                index.write_text(json.dumps({
                    "metadata": {}, "weight_map": {"weight": "payload.bin"}}))
                try:
                    with self.assertRaisesRegex(EncodingError, "unsafe_local_weights"):
                        LocalEncoder(self.path)
                finally:
                    index.unlink()
        self.runtime.SentenceTransformer.assert_not_called()

    def test_config_weight_selector_never_enters_dependency(self):
        (self.path / "config.json").write_text(json.dumps({
            "transformers_weights": "other.safetensors.index.json"}))
        (self.path / "other.safetensors.index.json").write_text(json.dumps({
            "metadata": {}, "weight_map": {"weight": "payload.bin"}}))
        (self.path / "payload.bin").touch()
        with self.assertRaisesRegex(EncodingError, "unsafe_local_weights"):
            LocalEncoder(self.path)
        self.runtime.SentenceTransformer.assert_not_called()

    def test_module_config_redirect_never_enters_dependency(self):
        (self.path / "sentence_bert_config.json").write_text(json.dumps({
            "config_args": {"_configuration_file": "weights.json"}}))
        (self.path / "weights.json").write_text(json.dumps({
            "model_type": "bert", "transformers_weights": "other.safetensors.index.json"}))
        (self.path / "other.safetensors.index.json").write_text(json.dumps({
            "weight_map": {"weight": "payload.bin"}}))
        (self.path / "payload.bin").touch()
        with self.assertRaisesRegex(EncodingError, "unsafe_local_weights"):
            LocalEncoder(self.path)
        self.runtime.SentenceTransformer.assert_not_called()

    def test_versioned_config_redirect_never_enters_dependency(self):
        (self.path / "config.json").write_text(json.dumps({
            "model_type": "bert", "configuration_files": ["config.4.0.0.json"]}))
        (self.path / "config.4.0.0.json").write_text(json.dumps({
            "model_type": "bert", "transformers_weights": "other.safetensors.index.json"}))
        (self.path / "other.safetensors.index.json").write_text(json.dumps({
            "weight_map": {"weight": "payload.bin"}}))
        (self.path / "payload.bin").touch()
        with self.assertRaisesRegex(EncodingError, "unsafe_local_weights"):
            LocalEncoder(self.path)
        self.runtime.SentenceTransformer.assert_not_called()

    def test_unselected_nested_pickle_weights_never_enter_dependency(self):
        nested = self.path / "unused"
        nested.mkdir()
        for extension in (".bin", ".PT", ".pth", ".PKL", ".pickle", ".CKPT"):
            with self.subTest(extension=extension):
                weight = nested / ("weights" + extension)
                weight.touch()
                try:
                    with self.assertRaisesRegex(EncodingError, "unsafe_local_weights"):
                        LocalEncoder(self.path)
                finally:
                    weight.unlink()
        self.runtime.SentenceTransformer.assert_not_called()

    def test_unselected_index_and_escaping_symlinks_never_enter_dependency(self):
        nested = self.path / "unused"
        nested.mkdir()
        index = nested / "other.safetensors.index.json"
        index.write_text(json.dumps({"weight_map": {"weight": "missing.bin"}}))
        with self.assertRaisesRegex(EncodingError, "unsafe_local_weights"):
            LocalEncoder(self.path)
        index.unlink()
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "part.safetensors"
            target.touch()
            for destination in (target, Path(outside)):
                with self.subTest(destination=destination.name):
                    link = nested / "linked"
                    link.symlink_to(destination)
                    try:
                        with self.assertRaisesRegex(EncodingError, "unsafe_local_weights"):
                            LocalEncoder(self.path)
                    finally:
                        link.unlink()
        self.runtime.SentenceTransformer.assert_not_called()

    def test_existing_shard_escapes_never_enter_dependency(self):
        root = self.path / "model"
        root.mkdir()
        (root / "modules.json").write_text((self.path / "modules.json").read_text())
        outside = self.path / "outside.safetensors"
        outside.touch()
        (root / "linked.safetensors").symlink_to(outside)
        for shard in ("../outside.safetensors", "linked.safetensors"):
            with self.subTest(shard=shard):
                (root / "model.safetensors.index.json").write_text(json.dumps({
                    "weight_map": {"weight": shard}}))
                self.assertTrue((root / shard).is_file())
                with self.assertRaisesRegex(EncodingError, "unsafe_local_weights"):
                    LocalEncoder(root)
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
        (self.path / "config.json").write_text(json.dumps({"model_type": "bert"}))
        (self.path / "part.safetensors").touch()
        (self.path / "model.safetensors.index.json").write_text(json.dumps({
            "metadata": {}, "weight_map": {"weight": "part.safetensors"}}))
        (self.path / "sentence_bert_config.json").write_text(json.dumps({
            "model_args": {"weights_only": False, "variant": "x"}}))
        (self.path / "model.safetensors.index.x.json").write_text(json.dumps({
            "weight_map": {"weight": "part.safetensors"}}))
        LocalEncoder(self.path)
        kwargs = self.runtime.SentenceTransformer.call_args.kwargs["model_kwargs"]
        self.assertIn("variant", kwargs)
        self.assertIsNone(kwargs["variant"])
        self.assertIs(
            self.runtime.SentenceTransformer.call_args.kwargs["model_kwargs"].get("weights_only"),
            True)
