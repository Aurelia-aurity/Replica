"""Synthetic builder schedule checks, stopping before tokenizer or GPU loading."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from build_arm import build
from decision import validate_bundle
from prepare import content_hash, digest, save
from prompt import template_path, variant_bundle


class TokenizerReached(RuntimeError):
    """Signal that the builder accepted inputs and reached the external adapter."""


class BuildArmTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        messages = [
            {"role": "system", "content": '<persona>\n{"register":"하게체"}\n</persona>'},
            {"role": "user", "content": "synthetic"},
        ]
        rows = [
            {"id": f"q{i}:{condition}", "question_id": f"q{i}",
             "condition": condition, "persona_id": "synthetic",
             "evidence_sha256": f"e{i}:{condition}", "messages": messages,
             "messages_sha256": content_hash(messages)}
            for i in range(240) for condition in ("B", "C")
        ]
        self.original = {
            "split": "heldout", "schedule_seed": 77, "rows": rows,
            "warmups": deepcopy(rows[:6]), "stress": deepcopy(rows[:6]),
        }
        dev = dict(deepcopy(self.original), split="dev", rows=deepcopy(rows[:6]))
        self.args = SimpleNamespace(
            arm="BASE", template_sha256=digest(template_path("r1-reg")),
            bundle=base / "heldout.json", dev_bundle=base / "dev.json",
            tokenizers=base / "tokenizers", output=base / "arm",
        )
        save(self.args.bundle, self.original)
        save(self.args.dev_bundle, dev)
        self.tokenizer = SimpleNamespace(from_pretrained=Mock(side_effect=TokenizerReached))
        self.transformers = SimpleNamespace(AutoTokenizer=self.tokenizer)

    def test_build_preserves_item_order_when_template_changes(self) -> None:
        # Given: real synthetic bundles and transformation; only tokenizer I/O is replaced.
        with patch.dict("sys.modules", transformers=self.transformers):
            # When
            with self.assertRaises(TokenizerReached):
                build(self.args)
        # Then: the real builder saved the complete ordered schedule before loading.
        built = json.loads((self.args.output / "bundle.json").read_text())
        keys = ("id", "question_id", "condition", "persona_id", "evidence_sha256")
        self.assertEqual(
            [[row[key] for key in keys] for row in built["rows"]],
            [[row[key] for key in keys] for row in self.original["rows"]],
        )
        self.assertNotEqual(built["rows"][0]["messages_sha256"],
                            self.original["rows"][0]["messages_sha256"])
        self.tokenizer.from_pretrained.assert_called_once()

    def test_build_rejects_reordered_question_pairs(self) -> None:
        # Given: a faulty transformation swaps whole B/C pairs, not their internal order.
        transformed = variant_bundle(self.original, "r1-reg")
        rows = transformed["rows"]
        rows[:4] = rows[2:4] + rows[:2]
        validate_bundle(transformed)  # Still valid B/C: only the preservation guard can reject it.
        with (
            patch.dict("sys.modules", transformers=self.transformers),
            patch("build_arm.variant_bundle", return_value=transformed),
        ):
            # When / Then
            with self.assertRaisesRegex(ValueError, "Prompt transformation changed schedule/evidence"):
                build(self.args)
        self.assertFalse(self.args.output.exists())
        self.tokenizer.from_pretrained.assert_not_called()


if __name__ == "__main__":
    unittest.main()
