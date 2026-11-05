"""Exclusive result writes and immutable input verification for the DEV job."""

import hashlib
import json
from pathlib import Path
from typing import TypedDict

type JSON = str | int | float | bool | None | list[JSON] | dict[str, JSON]


class PilotError(RuntimeError):
    """Invalid frozen execution input, resource limit or runtime invariant."""


class ExecutionSeal(TypedDict):
    files: dict[str, str]


def save(path: Path, value: JSON) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify_seal(root: Path) -> ExecutionSeal:
    """Reject input mutations and paths escaping the dedicated execution root."""
    seal = json.loads((root / "EXECUTION_SEAL.json").read_text())
    for relative, expected in seal["files"].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or file_hash(path) != expected:
            raise PilotError(f"Execution input differs from seal: {relative}")
    return seal
