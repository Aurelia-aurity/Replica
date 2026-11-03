"""Mechanical protocol tests; no prompt-prose assertions or model dependencies."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from decision import authorize, completed, nearest_rank, validate_bundle
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
