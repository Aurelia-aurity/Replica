"""The DEV CLI must reject mutated sealed inputs before model loading."""

from pathlib import Path

import pytest

from experiments.lora88.execution import PilotError, file_hash, save, verify_seal


def test_seal_accepts_matching_input_when_unchanged(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "input.json"
    save(path, {"scope": "dev"})
    expected = {"files": {"input.json": file_hash(path)}}
    save(tmp_path / "EXECUTION_SEAL.json", expected)
    # When
    actual = verify_seal(tmp_path)
    # Then
    assert actual == expected


def test_seal_rejects_mutation_when_input_differs(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "input.json"
    save(path, {"scope": "dev"})
    save(tmp_path / "EXECUTION_SEAL.json", {"files": {"input.json": "wrong"}})
    # When / Then
    with pytest.raises(PilotError):
        verify_seal(tmp_path)


def test_seal_rejects_outside_path_when_manifest_escapes_root(tmp_path: Path) -> None:
    # Given
    save(tmp_path / "EXECUTION_SEAL.json", {"files": {"../outside": "irrelevant"}})
    # When / Then
    with pytest.raises(PilotError):
        verify_seal(tmp_path)
