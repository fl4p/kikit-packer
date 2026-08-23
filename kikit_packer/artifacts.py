import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class ArtifactError(RuntimeError):
    pass


def manifest_path(board: Path) -> Path:
    return Path(str(board) + ".panel.json")


def managed_paths(board: Path) -> tuple[Path, Path, Path, Path]:
    return (
        board,
        board.with_suffix(".kicad_pro"),
        board.with_suffix(".kicad_dru"),
        manifest_path(board),
    )


def lock_paths(board: Path) -> tuple[Path, ...]:
    return (
        board.parent / ("~" + board.name + ".lck"),
        board.parent / ("~" + board.stem + ".kicad_pro.lck"),
    )


def assert_unlocked(board: Path) -> None:
    present = [str(path) for path in lock_paths(board) if path.exists()]
    if present:
        raise ArtifactError("output is locked by KiCad: {}".format(", ".join(present)))


@contextmanager
def transaction_lock(board: Path) -> Iterator[None]:
    lock = board.parent / ("." + board.stem + ".kikit-packer.lock")
    try:
        descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ArtifactError("another KiKit Packer transaction is active")
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.close(descriptor)
        yield
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _assert_regular_or_missing(path: Path, label: str) -> None:
    try:
        metadata = os.lstat(str(path))
    except FileNotFoundError:
        return
    if not os.path.isfile(path) or os.path.islink(path):
        raise ArtifactError(f"{label} must be a regular non-symlink file: {path}")
    if metadata.st_nlink != 1:
        raise ArtifactError(f"{label} must not have multiple hard links: {path}")


def promote(staging_board: Path, final_board: Path) -> tuple[Path, ...]:
    final_board.parent.mkdir(parents=True, exist_ok=True)
    staged = managed_paths(staging_board)
    final = managed_paths(final_board)
    for source in staged:
        _assert_regular_or_missing(source, "staged artifact")
    if not staged[0].is_file() or not staged[3].is_file():
        raise ArtifactError("staged board and manifest are required")
    moved_old: list[tuple[Path, Path]] = []
    moved_new: list[Path] = []
    with transaction_lock(final_board):
        assert_unlocked(final_board)
        for destination in final:
            _assert_regular_or_missing(destination, "existing output artifact")
        backup_root = Path(tempfile.mkdtemp(prefix="." + final_board.stem + ".backup-", dir=str(final_board.parent)))
        try:
            for destination in final:
                if destination.exists():
                    backup = backup_root / destination.name
                    os.replace(str(destination), str(backup))
                    moved_old.append((destination, backup))
            for source, destination in zip(staged[:-1], final[:-1]):
                if source.exists():
                    os.replace(str(source), str(destination))
                    moved_new.append(destination)
            os.replace(str(staged[-1]), str(final[-1]))
            moved_new.append(final[-1])
            for path in moved_new:
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
            _fsync_directory(final_board.parent)
        except BaseException:
            for path in reversed(moved_new):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            for destination, backup in reversed(moved_old):
                if backup.exists():
                    os.replace(str(backup), str(destination))
            _fsync_directory(final_board.parent)
            raise
        else:
            for _, backup in moved_old:
                backup.unlink()
            backup_root.rmdir()
            return tuple(path for path in final if path.exists())
        finally:
            if backup_root.exists() and not any(backup_root.iterdir()):
                backup_root.rmdir()
