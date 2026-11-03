"""Stdlib tests for structural template and generation policy."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from smoke import CAP, empty_think_suffix, load_profile, run, termination, think_markers


class SmokeTests(unittest.TestCase):
    def test_empty_block_at_generation_boundary(self):
        self.assertTrue(empty_think_suffix("assistant\n<think>\n\n</think>\n"))

    def test_nonempty_block_rejected(self):
        self.assertFalse(empty_think_suffix("<think>x</think>"))

    def test_open_block_rejected(self):
        self.assertFalse(empty_think_suffix("<think>\n"))

    def test_block_before_answer_rejected(self):
        self.assertFalse(empty_think_suffix("<think></think>x"))

    def test_raw_special_markers_detected(self):
        self.assertEqual(think_markers("<think>x</think>"), ["<think>", "</think>"])

    def test_plain_answer_has_no_markers(self):
        self.assertEqual(think_markers("abc"), [])

    def test_eos_list_uses_all_ids_at_cap(self):
        self.assertEqual(termination([0] * (CAP - 1) + [9], [8, 9]), "eos")

    def test_length_without_eos(self):
        self.assertEqual(termination([0] * CAP, [8, 9]), "length")

    def test_short_generation_without_eos(self):
        self.assertEqual(termination([0], [8]), "other")

    def test_kanana_profile_selects_own_template_switch(self):
        profile = load_profile("kanana-2-3b-instruct")
        self.assertEqual(profile.chat_template_kwargs, {"thinking_mode": "no_think"})

    def test_unknown_profile_fails_closed(self):
        with self.assertRaises(KeyError):
            load_profile("unknown")

    def test_generate_excludes_token_type_ids_for_all_profiles(self):
        # Given: tokenizer output may include segment IDs unsupported by causal LMs.
        class Batch(dict):
            def to(self, device):
                return self

        class GenerationReached(Exception):
            pass

        names = ("Qwen3-8B", "Qwen3-14B", "kanana-2-3b-instruct",
                 "kanana-1.5-15.7b-a3b-instruct", "Midm-2.0-Base-Instruct")
        for name in names:
            for with_segment_ids in (True, False):
                with self.subTest(model=name, with_segment_ids=with_segment_ids):
                    profile = load_profile(name)
                    ids = SimpleNamespace(shape=(1, 2))
                    mask = SimpleNamespace(shape=(1, 2))
                    batch = Batch(input_ids=ids, attention_mask=mask)
                    if with_segment_ids:
                        batch["token_type_ids"] = SimpleNamespace(shape=(1, 2))
                    tokenizer = MagicMock(return_value=batch)
                    tokenizer.apply_chat_template.return_value = "<think></think>"
                    model = MagicMock()
                    model.parameters.return_value = iter(())
                    model.generation_config.eos_token_id = 2

                    def generate(**kwargs):
                        # Then: the actual generate boundary receives IDs and mask only.
                        self.assertNotIn("token_type_ids", kwargs)
                        self.assertIs(kwargs["input_ids"], ids)
                        self.assertIs(kwargs["attention_mask"], mask)
                        raise GenerationReached

                    model.generate.side_effect = generate
                    torch = MagicMock()
                    torch.cuda.device_count.return_value = 1
                    transformers = SimpleNamespace(
                        AutoTokenizer=SimpleNamespace(from_pretrained=lambda *a, **k: tokenizer),
                        AutoModelForCausalLM=SimpleNamespace(
                            from_pretrained=lambda *a, **k: SimpleNamespace(eval=lambda: model)),
                        GenerationConfig=lambda **kwargs: kwargs,
                    )

                    def read_text(path):
                        if path.name == "issue77-download.log":
                            return "DONE " + profile.model_id
                        if path.name == "config.json":
                            return '{"model_type": "' + profile.model_type + '"}'
                        if path.name == "baseline.json":
                            return '{"system_prompt": ""}'
                        return "{}"

                    # When: execute the real runner up to its generation call, without GPU I/O.
                    with (
                        patch.dict("sys.modules", torch=torch, transformers=transformers),
                        patch.dict("os.environ", SLURM_JOB_ID="test", SLURMD_NODENAME="DIS02"),
                        patch("smoke.load_profile", return_value=profile),
                        patch("smoke.importlib.metadata.version", return_value="test"),
                        patch.object(Path, "exists", return_value=False),
                        patch.object(Path, "read_text", autospec=True, side_effect=read_text),
                        self.assertRaises(GenerationReached),
                    ):
                        run(name, {})


if __name__ == "__main__":
    unittest.main()
