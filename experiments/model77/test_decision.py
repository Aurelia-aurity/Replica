"""Mechanical protocol tests; no prompt-prose assertions or model dependencies."""

from copy import deepcopy
import json
from inspect import signature
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from audit import pack_stress
from decision import additional_dev_profile, authorize, completed, model_default_options, nearest_rank, nvml_uuid, validate_bundle
from prepare import content_hash, digest, save


def fixture():
    messages = [{"role": "system", "content": ""}, {"role": "user", "content": ""}]
    rows = [{"id": f"q:{c}", "question_id": "q", "condition": c,
             "messages": messages, "messages_sha256": content_hash(messages)}
            for c in ("B", "C")]
    return {
        "split": "dev", "protocol_sha256": "a", "rows": rows,
        "warmups": [{**row, "id": f"warmup:{i}:{row['id']}"} for i in range(3) for row in rows],
        "stress": [{"id": str(i)} for i in range(6)],
    }


def save_row(path, row):
    with path.open("x") as handle:
        handle.write(json.dumps(row) + "\n")


class ScheduleTests(unittest.TestCase):
    def test_bare_uuid_is_normalized(self):
        # Given
        value = "e70efa8a-37ca-255a-6dfd-a21aadbff558"
        # When
        result = nvml_uuid(value)
        # Then
        self.assertEqual(result, "GPU-" + value)

    def test_prefixed_uuid_is_not_doubled(self):
        # Given
        value = "GPU-e70efa8a-37ca-255a-6dfd-a21aadbff558"
        # When
        result = nvml_uuid(value)
        # Then
        self.assertEqual(result, value)

    def test_valid_interleaved_schedule(self):
        # Given
        bundle = fixture()
        # When / Then
        self.assertIsNone(validate_bundle(bundle))

    def test_message_changes_fail(self):
        # Given
        bundle = deepcopy(fixture())
        bundle["rows"][0]["messages"][1]["content"] = "changed"
        # When / Then
        with self.assertRaises(ValueError):
            validate_bundle(bundle)

    def test_condition_reordering_fails(self):
        # Given
        bundle = fixture()
        bundle["rows"].reverse()
        # When / Then
        with self.assertRaises(ValueError):
            validate_bundle(bundle)

    def test_duplicate_ids_fail(self):
        # Given
        bundle = fixture()
        bundle["rows"][1]["id"] = bundle["rows"][0]["id"]
        # When / Then
        with self.assertRaises(ValueError):
            validate_bundle(bundle)

    def test_warmup_count_fails(self):
        # Given
        bundle = fixture()
        bundle["warmups"].pop()
        # When / Then
        with self.assertRaises(ValueError):
            validate_bundle(bundle)

    def test_nearest_rank_240(self):
        # Given
        values = list(range(240, 0, -1))
        # When
        result = nearest_rank(values)
        # Then
        self.assertEqual(result, 228)

    def test_nearest_rank_small(self):
        # Given
        values = [2, 1]
        # When
        result = nearest_rank(values)
        # Then
        self.assertEqual(result, 2)

    def test_empty_observations_fail(self):
        # Given / When / Then
        with self.assertRaises(ValueError):
            nearest_rank([])


class ArtifactTests(unittest.TestCase):
    def test_removed_generation_option_is_not_forwarded_to_current_model(self):
        # Given: the observed 5.22 generate signature has kwargs but no defaults option.
        def generate(*, generation_config, **model_kwargs):
            if "use_model_defaults" in model_kwargs:
                raise ValueError("unused model argument")
            return generation_config

        # When
        result = generate(generation_config="synthetic-config",
                          **model_default_options(signature(generate).parameters))
        # Then
        self.assertEqual(result, "synthetic-config")

    def test_supported_generation_option_preserves_legacy_false_override(self):
        # Given: the 4.57 API explicitly accepts this option, defaulting it to true.
        def generate(*, generation_config, use_model_defaults=True, **model_kwargs):
            return generation_config, use_model_defaults

        # When
        result = generate(generation_config="synthetic-config",
                          **model_default_options(signature(generate).parameters))
        # Then
        self.assertEqual(result, ("synthetic-config", False))

    def test_additional_profile_rejects_decision_and_stress_before_reading(self):
        # Given: no real corpus or profile is read.
        path = Path("nonexistent-synthetic-profile.json")
        for normal_only, split, dev_split in (
            (True, "heldout", "dev"), (True, "dev", "heldout"), (False, "dev", "dev"),
        ):
            with self.subTest(normal_only=normal_only, split=split, dev_split=dev_split):
                # When / Then
                with self.assertRaises(ValueError):
                    additional_dev_profile(path, normal_only, split, dev_split)

    def test_additional_profile_reads_exact_pinned_dev_configuration(self):
        # Given
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            value = {"model_id": "synthetic/model", "revision": "synthetic-revision",
                     "model_type": "synthetic", "chat_template_kwargs": {"enable_thinking": False},
                     "empty_think_required": True}
            save(path, value)
            # When
            result = additional_dev_profile(path, True, "dev", "dev")
            # Then
            self.assertEqual(result.model_id, value["model_id"])
            self.assertEqual(result.revision, value["revision"])
            self.assertEqual(result.chat_template_kwargs, value["chat_template_kwargs"])

    def test_stress_packing_handles_token_count_gaps(self):
        # Given: a tokenizer with two-token character steps except for a tab.
        class GappedTokenizer:
            def encode(self, text, **kwargs):
                return [1] * sum(1 if char == "\t" else 2 for char in text)

            def apply_chat_template(self, messages, **kwargs):
                return "\n".join(message["content"] for message in messages)

        tokenizer = GappedTokenizer()
        row = {
            "id": "stress", "condition": "B", "persona_id": "p",
            "messages": [{"role": "system", "content": "s"},
                         {"role": "user", "content": "u"}],
            "context_cycle": "abc", "target_input_tokens": 101,
        }
        profile = SimpleNamespace(chat_template_kwargs={}, empty_think_required=False)
        # When
        result = pack_stress(tokenizer, row, profile)
        # Then
        self.assertEqual(result["input_tokens"], 101)
        self.assertEqual(result["messages"][0], row["messages"][0])
        self.assertTrue(result["messages"][1]["content"].startswith("u\n<dev_memory_context>\n"))

    def test_no_heldout_generation_without_authorization(self):
        # Given
        bundle = {**fixture(), "split": "heldout"}
        # When / Then
        with self.assertRaises(PermissionError):
            authorize(bundle, None, Path("unused"))

    def test_authorization_binds_bundle_hash(self):
        # Given
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            bundle = {**fixture(), "split": "heldout"}
            save(path / "bundle.json", bundle)
            save(path / "approval.json", {
                "authorized": True, "approver": "test", "date": "2026-11-03",
                "bundle_sha256": digest(path / "bundle.json"), "protocol_sha256": "a",
            })
            # When / Then
            self.assertIsNone(authorize(bundle, path / "approval.json", path / "bundle.json"))

    def test_artifacts_are_not_overwritten(self):
        # Given
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact"
            save(path, {"n": 1})
            # When / Then
            with self.assertRaises(FileExistsError):
                save(path, {"n": 2})

    def test_resume_returns_completed_ids_only(self):
        # Given
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows"
            expected = {"q:B": {"messages_sha256": "x"}, "q:C": {"messages_sha256": "y"}}
            row = {"id": "q:B", "messages_sha256": "x", "run_identity": "a", "status": "success"}
            save_row(path, row)
            # When
            result = completed(path, expected, "a")
            # Then
            self.assertEqual(set(result), {"q:B"})

    def test_resume_rejects_changed_input(self):
        # Given
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows"
            save_row(path, {"id": "q:B", "messages_sha256": "old",
                        "run_identity": "a", "status": "success"})
            # When / Then
            with self.assertRaises(ValueError):
                completed(path, {"q:B": {"messages_sha256": "new"}}, "a")

    def test_resume_rejects_failure_without_regeneration(self):
        # Given
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows"
            save_row(path, {"id": "q:B", "messages_sha256": "x",
                        "run_identity": "a", "status": "failed"})
            # When / Then
            with self.assertRaises(RuntimeError):
                completed(path, {"q:B": {"messages_sha256": "x"}}, "a")


if __name__ == "__main__":
    unittest.main()
