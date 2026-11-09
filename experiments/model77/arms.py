"""ROUND 3 execution envelope; actual model IDs remain unchanged."""

import json
import os
from pathlib import Path
from typing import Final, TypedDict

from prepare import content_hash, digest

ARMS: Final = {
    "BASE": ("Qwen3-8B", "r1-reg"),
    "CAND": ("Qwen3-14B", "r1-reg-14b"),
    "DIAG": ("Qwen3-8B", "r1-reg-14b"),
}
PROVENANCE: Final = {
    "criteria_v3_sha256": "ACCEPTANCE_model77_v3.md",
    "criteria_freeze_record_sha256": "FROZEN_CRITERIA_v3.txt",
    "content_seal_sha256": "SEAL.txt",
    "execution_seal_sha256": "EXECUTION_SEAL.txt",
    "evidence_sha256": "evidence.json",
    "sealed_r1_bundle_sha256": "r1-bundle.json",
}


class ArmContext(TypedDict, total=False):
    arm: str
    run_id: str
    prompt_version: str
    register_template_sha256: str
    authorization_sha256: str
    schedule_sha256: str
    code_commit: str


def schedule_hash(bundle) -> str:
    """Bind the common seeded order, conditions and evidence, not prompt prose."""
    return content_hash([
        {k: row[k] for k in ("id", "question_id", "condition", "persona_id", "evidence_sha256")}
        for row in bundle["rows"]
    ])


def arm_context(args, bundle) -> ArmContext:
    """Validate the separately frozen arm authorization before any GPU loading."""
    if args.arm is None and args.run_id is None:
        if os.environ.get("MODEL77_RUN_ID") == "decision-v3":
            raise PermissionError("decision-v3 requires --run-id and --arm")
        return {}
    if args.run_id != "decision-v3" or args.arm not in ARMS:
        raise ValueError("ROUND 3 requires decision-v3 and a known arm")
    model, prompt = ARMS[args.arm]
    if (args.model, bundle.get("prompt_version")) != (model, prompt):
        raise ValueError("Arm model/prompt mismatch")
    if args.output.name != args.arm or args.output.parent.name != "decision-v3":
        raise ValueError("ROUND 3 output must be results/decision-v3/<arm>")
    if args.normal_only or bundle["split"] != "heldout":
        raise ValueError("ROUND 3 arms require full heldout execution")
    if bundle.get("arm") != args.arm or bundle.get("run_id") != args.run_id:
        raise ValueError("ROUND 3 bundle envelope mismatch")
    if any(row.get("arm") != args.arm or row.get("run_id") != args.run_id
           for row in bundle["rows"]):
        raise ValueError("ROUND 3 row envelope mismatch")
    if args.authorize_decision is None:
        raise PermissionError("ROUND 3 authorization is required")
    if args.stress is None:
        raise ValueError("ROUND 3 requires full DEV stress instrumentation")
    approval = json.loads(args.authorize_decision.read_text())
    base = args.authorize_decision.parent
    if not (approval.get("authorized") is True and approval.get("approver")
            and approval.get("date") and approval.get("run_id") == args.run_id):
        raise PermissionError("Invalid ROUND 3 authorization")
    if approval.get("arm_order") != list(ARMS):
        raise PermissionError("ROUND 3 arm order mismatch")
    for key, filename in PROVENANCE.items():
        if approval.get(key) != digest(base / "provenance" / filename):
            raise PermissionError(f"ROUND 3 provenance mismatch: {key}")
    commit = (base / "CODE_COMMIT.txt").read_text().strip()
    if approval.get("code_commit") != commit:
        raise PermissionError("ROUND 3 code commit mismatch")
    source = Path(__file__).parent
    code = approval["code_sha256"]
    required = ("decision.py", "arms.py", "prepare.py", "audit.py", "smoke.py", "profiles.json")
    if any(code.get(name) != digest(source / name) for name in required):
        raise PermissionError("ROUND 3 code hash mismatch")
    arm = approval["arms"][args.arm]
    expected = {
        "model": model, "prompt_version": prompt,
        "register_template_sha256": bundle["register_template_sha256"],
        "bundle_sha256": digest(args.bundle),
        "dev_bundle_sha256": digest(args.dev_bundle),
        "audit_sha256": digest(args.audit), "stress_sha256": digest(args.stress),
        "protocol_sha256": bundle["protocol_sha256"],
        "schedule_sha256": schedule_hash(bundle),
    }
    if any(arm.get(key) != value for key, value in expected.items()):
        raise PermissionError("ROUND 3 arm authorization mismatch")
    if arm["schedule_sha256"] != approval["schedule_sha256"]:
        raise PermissionError("ROUND 3 common schedule mismatch")
    if bundle["schedule_seed"] != 77 or len(bundle["rows"]) != 480:
        raise ValueError("ROUND 3 requires the frozen seed-77 480-row schedule")
    return {
        "arm": args.arm, "run_id": args.run_id, "prompt_version": prompt,
        "register_template_sha256": bundle["register_template_sha256"],
        "authorization_sha256": digest(args.authorize_decision),
        "schedule_sha256": arm["schedule_sha256"], "code_commit": commit,
    }
