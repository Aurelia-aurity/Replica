"""Synthetic arm-envelope regression tests; no GPU or decision responses."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from arms import ARMS, PROVENANCE, arm_context, schedule_hash
from decision import completed
from prepare import content_hash, digest, save


def fixture(directory, arm="BASE"):
    base = Path(directory)
    model, prompt = ARMS[arm]
    bundle = {
        "arm": arm, "run_id": "decision-v3", "split": "heldout",
        "prompt_version": prompt, "register_template_sha256": "template",
        "schedule_seed": 77, "protocol_sha256": "protocol",
        "rows": [
            {"id": f"q{i}:{cond}", "question_id": f"q{i}", "condition": cond,
             "persona_id": f"p{i // 40}", "evidence_sha256": f"e{i}:{cond}",
             "arm": arm, "run_id": "decision-v3", "messages_sha256": f"m{i}:{cond}"}
            for i in range(240) for cond in ("B", "C")
        ],
    }
    args = SimpleNamespace(
        arm=arm, run_id="decision-v3", model=model, normal_only=False,
        output=base / "results/decision-v3" / arm,
        authorize_decision=base / "authorization.json",
        bundle=base / "bundle.json", dev_bundle=base / "dev.json",
        audit=base / "audit.json", stress=base / "stress.json",
    )
    save(args.bundle, bundle)
    for file in (args.dev_bundle, args.audit, args.stress):
        save(file, {"synthetic": file.name})
    (base / "CODE_COMMIT.txt").write_text("synthetic-commit\n")
    approval = {
        "authorized": True, "approver": "synthetic", "date": "2026-11-04",
        "run_id": "decision-v3", "code_commit": "synthetic-commit",
        "arm_order": list(ARMS), "schedule_sha256": schedule_hash(bundle),
        "code_sha256": {name: digest(Path(__file__).with_name(name)) for name in
                        ("decision.py", "arms.py", "prepare.py", "audit.py", "smoke.py",
                         "profiles.json")},
        "arms": {arm: {
            "model": model, "prompt_version": prompt,
            "register_template_sha256": "template",
            "protocol_sha256": "protocol", "schedule_sha256": schedule_hash(bundle),
            **{key: digest(getattr(args, attr)) for key, attr in (
                ("bundle_sha256", "bundle"), ("dev_bundle_sha256", "dev_bundle"),
                ("audit_sha256", "audit"), ("stress_sha256", "stress"))},
        }},
    }
    for key, name in PROVENANCE.items():
        file = base / "provenance" / name
        save(file, {"synthetic": name})
        approval[key] = digest(file)
    save(args.authorize_decision, approval)
    return args, bundle, approval


class ArmTests(unittest.TestCase):
    def test_arm_identity_distinguishes_two_real_8b_runs(self):
        # Given
        with tempfile.TemporaryDirectory() as directory:
            args, bundle, _ = fixture(Path(directory) / "base")
            diag_args, diag_bundle, _ = fixture(Path(directory) / "diag", "DIAG")
            # When
            base = arm_context(args, bundle)
            diag = arm_context(diag_args, diag_bundle)
            # Then
            self.assertEqual(args.model, diag_args.model)
            self.assertEqual((base["arm"], diag["arm"]), ("BASE", "DIAG"))
            self.assertNotEqual(content_hash(base), content_hash(diag))
            self.assertEqual(base["schedule_sha256"], diag["schedule_sha256"])

    def test_all_three_arms_are_authorized_individually(self):
        # Given
        for arm in ARMS:
            with self.subTest(arm=arm), tempfile.TemporaryDirectory() as directory:
                args, bundle, _ = fixture(directory, arm)
                # When
                result = arm_context(args, bundle)
                # Then
                self.assertEqual(result["arm"], arm)
                self.assertEqual(result["prompt_version"], ARMS[arm][1])

    def test_arm_model_prompt_and_output_mismatches_rejected(self):
        # Given
        for field, value in (("model", "Qwen3-14B"), ("arm", "DIAG"),
                             ("run_id", "decision-v2"),
                             ("output", Path("results/decision-v2/BASE"))):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                args, bundle, _ = fixture(directory)
                setattr(args, field, value)
                # When / Then
                with self.assertRaises(ValueError):
                    arm_context(args, bundle)

    def test_changed_provenance_rejected(self):
        # Given
        with tempfile.TemporaryDirectory() as directory:
            args, bundle, _ = fixture(directory)
            file = Path(directory) / "provenance/EXECUTION_SEAL.txt"
            file.write_text("changed synthetic execution seal")
            # When / Then
            with self.assertRaises(PermissionError):
                arm_context(args, bundle)

    def test_missing_approval_rejected(self):
        # Given
        with tempfile.TemporaryDirectory() as directory:
            args, bundle, _ = fixture(directory)
            args.authorize_decision = None
            # When / Then
            with self.assertRaises(PermissionError):
                arm_context(args, bundle)

    def test_changed_schedule_and_row_arm_rejected(self):
        # Given
        for field, value in (("evidence_sha256", "different"), ("arm", "DIAG")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                args, bundle, _ = fixture(directory)
                bundle["rows"][0][field] = value
                # When / Then
                with self.assertRaises((PermissionError, ValueError)):
                    arm_context(args, bundle)

    def test_resume_cannot_cross_arm_identity(self):
        # Given
        with tempfile.TemporaryDirectory() as directory:
            args, bundle, _ = fixture(Path(directory) / "base")
            diag_args, diag_bundle, _ = fixture(Path(directory) / "diag", "DIAG")
            base_identity = content_hash(arm_context(args, bundle))
            diag_identity = content_hash(arm_context(diag_args, diag_bundle))
            row = dict(bundle["rows"][0], run_identity=base_identity, status="success")
            output = Path(directory) / "outputs.jsonl"
            output.write_text(json.dumps(row) + "\n")
            # When / Then
            with self.assertRaises(ValueError):
                completed(output, {row["id"]: row}, diag_identity)

    def test_legacy_without_round3_arguments_preserved(self):
        # Given
        args = SimpleNamespace(arm=None, run_id=None)
        # When
        result = arm_context(args, {})
        # Then
        self.assertEqual(result, {})

    def test_resume_rejects_missing_arm_even_with_matching_identity(self):
        # Given
        with tempfile.TemporaryDirectory() as directory:
            expected = {"q:B": {"messages_sha256": "same", "arm": "BASE",
                                 "run_id": "decision-v3"}}
            row = {"id": "q:B", "messages_sha256": "same",
                   "run_identity": "same", "status": "success"}
            output = Path(directory) / "outputs.jsonl"
            output.write_text(json.dumps(row) + "\n")
            # When / Then
            with self.assertRaises(ValueError):
                completed(output, expected, "same")


if __name__ == "__main__":
    unittest.main()
