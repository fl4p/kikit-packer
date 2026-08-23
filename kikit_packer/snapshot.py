from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .protocol import file_sha256


@dataclass(frozen=True)
class SnapshotFile:
    original: Path
    relative: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class SnapshotSource:
    source_id: str
    board: SnapshotFile
    kicad_pro: SnapshotFile | None
    kicad_dru: SnapshotFile | None
    ignored_companions: tuple[Path, ...]


def _copy_verified(original: Path, destination: Path, root: Path) -> SnapshotFile:
    before = file_sha256(original)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(original), str(destination))
    after = file_sha256(destination)
    if before != after:
        raise RuntimeError(f"source changed while snapshotting: {original}")
    os.chmod(destination, 0o444)
    return SnapshotFile(original, destination.relative_to(root), after, destination.stat().st_size)


def snapshot_sources(
    paths: Iterable[Path],
    authority: Path,
    staging_root: Path,
) -> tuple[SnapshotSource, ...]:
    inputs = staging_root / "inputs"
    unique: dict[Path, str] = {}
    ordered = []
    for path in paths:
        canonical = path.resolve(strict=True)
        if canonical not in unique:
            source_id = f"source-{len(unique) + 1:04d}"
            unique[canonical] = source_id
            ordered.append(canonical)
    authority = authority.resolve(strict=True)
    if authority not in unique:
        unique[authority] = f"source-{len(unique) + 1:04d}"
        ordered.append(authority)
    output = []
    for original in ordered:
        source_id = unique[original]
        directory = inputs / source_id
        board = _copy_verified(original, directory / original.name, staging_root)
        companions = {}
        ignored = []
        for suffix in (".kicad_pro", ".kicad_dru"):
            companion = original.with_suffix(suffix)
            if not companion.exists():
                companions[suffix] = None
            elif original == authority:
                companions[suffix] = _copy_verified(companion, directory / companion.name, staging_root)
            else:
                companions[suffix] = None
                ignored.append(companion)
        output.append(SnapshotSource(
            source_id,
            board,
            companions[".kicad_pro"],
            companions[".kicad_dru"],
            tuple(ignored),
        ))
    return tuple(output)


def verify_snapshots_from_plan(root: Path, plan) -> None:
    for source in plan["sources"]:
        paths = [(source["snapshot_path"], source["sha256"])]
        for companion in source.get("companions", {}).values():
            if companion.get("present"):
                paths.append((companion["snapshot_path"], companion["sha256"]))
        for relative, expected in paths:
            path = (root / relative).resolve()
            try:
                path.relative_to(root.resolve())
            except ValueError:
                raise RuntimeError("snapshot path escapes staging root")
            if not path.is_file() or file_sha256(path) != expected:
                raise RuntimeError(f"snapshot hash mismatch: {relative}")


def verify_snapshots(root: Path, sources: Iterable[SnapshotSource]) -> None:
    for source in sources:
        for item in (source.board, source.kicad_pro, source.kicad_dru):
            if item is None:
                continue
            path = root / item.relative
            if file_sha256(path) != item.sha256:
                raise RuntimeError(f"snapshot hash mismatch: {item.relative}")
