from pathlib import Path

import pytest

from kikit_packer.artifacts import (
    ArtifactError,
    assert_unlocked,
    lock_paths,
    manifest_path,
    promote,
)


def test_promote_replaces_managed_set(tmp_path: Path):
    staging = tmp_path / "staging"
    final = tmp_path / "final" / "panel.kicad_pcb"
    staging.mkdir()
    final.parent.mkdir()
    staged_board = staging / "panel.kicad_pcb"
    staged_board.write_bytes(b"new")
    manifest_path(staged_board).write_bytes(b"manifest")
    final.write_bytes(b"old")
    promoted = promote(staged_board, final)
    assert final.read_bytes() == b"new"
    assert manifest_path(final).read_bytes() == b"manifest"
    assert final in promoted


def test_directory_output_is_rejected_without_data_loss(tmp_path: Path):
    staging = tmp_path / "staging"
    staging.mkdir()
    staged = staging / "panel.kicad_pcb"
    staged.write_bytes(b"new")
    manifest_path(staged).write_bytes(b"manifest")
    final = tmp_path / "panel.kicad_pcb"
    final.mkdir()
    (final / "user-data").write_bytes(b"keep")
    with pytest.raises(ArtifactError):
        promote(staged, final)
    assert (final / "user-data").read_bytes() == b"keep"
    assert not list(tmp_path.glob(".panel.backup-*"))


def test_lock_prevents_output_mutation(tmp_path: Path):
    board = tmp_path / "panel.kicad_pcb"
    board.write_bytes(b"old")
    lock_paths(board)[1].write_text("locked")
    with pytest.raises(ArtifactError):
        assert_unlocked(board)
    assert board.read_bytes() == b"old"


def test_promotion_failure_restores_old_output(tmp_path: Path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    staged = staging / "panel.kicad_pcb"
    staged.write_bytes(b"new")
    manifest_path(staged).write_bytes(b"new manifest")
    final = tmp_path / "panel.kicad_pcb"
    final.write_bytes(b"old")
    original_replace = __import__("os").replace

    def replace(source, destination):
        if str(source).endswith(".panel.json"):
            raise OSError("injected manifest promotion failure")
        return original_replace(source, destination)

    monkeypatch.setattr("kikit_packer.artifacts.os.replace", replace)
    with pytest.raises(OSError):
        promote(staged, final)
    assert final.read_bytes() == b"old"


def test_missing_manifest_preserves_old_output(tmp_path: Path):
    staging = tmp_path / "staging"
    staging.mkdir()
    staged = staging / "panel.kicad_pcb"
    staged.write_bytes(b"new")
    final = tmp_path / "panel.kicad_pcb"
    final.write_bytes(b"old")
    with pytest.raises(ArtifactError):
        promote(staged, final)
    assert final.read_bytes() == b"old"
