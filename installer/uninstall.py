#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

RECEIPT_VERSION = 2


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def allowed_external_roots(root: Path):
    if os.name == "nt":
        return [
            (root / "bin").resolve(),
            (Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs").resolve(),
        ]
    roots = [(Path.home() / ".local/bin").resolve()]
    if sys.platform == "darwin":
        roots.append((Path.home() / "Applications").resolve())
    else:
        roots.append((Path.home() / ".local/share/applications").resolve())
    return roots


def _within(path: Path, roots) -> bool:
    canonical = path.resolve()
    for root in roots:
        try:
            canonical.relative_to(root)
            return True
        except ValueError:
            pass
    return False


def load_receipt(root: Path):
    receipt_path = root / "install-receipt.json"
    receipt_hash_path = root / "install-receipt.sha256"
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise RuntimeError("install receipt is missing or unsafe")
    if not receipt_hash_path.is_file() or receipt_hash_path.is_symlink():
        raise RuntimeError("install receipt hash is missing or unsafe")
    expected_hash = receipt_hash_path.read_text(encoding="ascii").strip()
    if file_hash(receipt_path) != expected_hash:
        raise RuntimeError("install receipt hash mismatch")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("schema_version") != RECEIPT_VERSION:
        raise RuntimeError("unsupported receipt")
    canonical_root = root.resolve()
    if Path(receipt["install_root"]).resolve() != canonical_root:
        raise RuntimeError("receipt install root mismatch")
    version_root = Path(receipt["version_root"]).resolve()
    try:
        version_root.relative_to(canonical_root / "versions")
    except ValueError:
        raise RuntimeError("receipt version path escapes install root")
    current = root / "current.txt"
    if not current.is_file() or current.is_symlink():
        raise RuntimeError("current-version pointer is missing or unsafe")
    if Path(current.read_text(encoding="utf-8").strip()).resolve() != version_root:
        raise RuntimeError("receipt is not the current installed version")
    retained_versions = []
    for value in receipt.get("retained_version_roots", []):
        retained = Path(value).resolve()
        try:
            retained.relative_to(canonical_root / "versions")
        except ValueError:
            raise RuntimeError("retained version path escapes install root")
        if retained == version_root or not retained.is_dir() or retained.is_symlink():
            raise RuntimeError("retained version path is missing or unsafe")
        retained_versions.append(retained)
    managed = []
    roots = allowed_external_roots(root)
    for item in receipt.get("managed_files", []):
        path = Path(item["path"])
        if not _within(path, roots):
            raise RuntimeError("receipt managed path is outside allowed roots")
        if not path.is_file() or path.is_symlink() or file_hash(path) != item["sha256"]:
            raise RuntimeError(f"installer-managed file was modified; uninstall aborted: {path}")
        managed.append(path)
    if not version_root.is_dir() or version_root.is_symlink():
        raise RuntimeError("installed environment is missing or unsafe")
    return receipt, version_root, retained_versions, managed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    _, version_root, retained_versions, managed = load_receipt(root)
    targets = managed + [
        version_root,
        *retained_versions,
        root / "current.txt",
        root / "install-receipt.json",
        root / "install-receipt.sha256",
    ]
    for target in targets:
        print(target)
    if not args.yes:
        print("Dry run only; pass --yes to remove these paths")
        return 0
    for path in managed:
        path.unlink()
    if sys.platform == "darwin":
        app = Path.home() / "Applications/KiKit Packer.app"
        for directory in (app / "Contents/MacOS", app / "Contents", app):
            try:
                directory.rmdir()
            except OSError:
                pass
    shutil.rmtree(version_root)
    for retained in retained_versions:
        shutil.rmtree(retained)
    (root / "current.txt").unlink()
    (root / "install-receipt.json").unlink()
    (root / "install-receipt.sha256").unlink()
    try:
        (root / "versions").rmdir()
        root.rmdir()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
