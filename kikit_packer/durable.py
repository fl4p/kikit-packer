import ctypes
import errno
import os
import shutil
import stat
import sys
from pathlib import Path

_RENAME_EXCL = 0x00000004
_RENAME_NOREPLACE = 0x00000001
_LINUX_AT_FDCWD = -100


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_makedirs(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    path.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        fsync_directory(created)
        fsync_directory(created.parent)


def durable_replace(source: Path, target: Path) -> None:
    os.replace(source, target)
    fsync_directory(target.parent)
    if source.parent != target.parent:
        fsync_directory(source.parent)


def _rename_exclusive(source: Path, target: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source_bytes, target_bytes, _RENAME_EXCL)
    elif sys.platform.startswith("linux"):
        try:
            rename = library.renameat2
        except AttributeError as exc:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable") from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            _LINUX_AT_FDCWD,
            source_bytes,
            _LINUX_AT_FDCWD,
            target_bytes,
            _RENAME_NOREPLACE,
        )
    elif os.name == "nt":
        os.rename(source, target)
        return
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def durable_rename_exclusive(source: Path, target: Path) -> None:
    _rename_exclusive(source, target)
    fsync_directory(target.parent)
    if source.parent != target.parent:
        fsync_directory(source.parent)


def durable_unlink(path: Path, missing_ok: bool = False) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    fsync_directory(path.parent)


def durable_rmtree(path: Path) -> None:
    shutil.rmtree(path)
    fsync_directory(path.parent)


def durable_rmdir(path: Path) -> None:
    path.rmdir()
    fsync_directory(path.parent)


def _fsync_regular_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_tree(root: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError(f"durability barrier root is missing or unsafe: {root}")
    for directory, _directories, names in os.walk(root, topdown=False, followlinks=False):
        current = Path(directory)
        for name in names:
            path = current / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError(f"unsupported staged filesystem entry: {path}")
            _fsync_regular_file(path)
        fsync_directory(current)
