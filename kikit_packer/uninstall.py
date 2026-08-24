#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

from .install_lock import installer_lock

RECEIPT_VERSION = 2
JOURNAL_VERSION = 2


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    encoded = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name("." + path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


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


def _remove_empty_managed_parents(paths, roots) -> None:
    canonical_roots = [root.resolve() for root in roots]
    for path in paths:
        parent = path.parent.resolve()
        boundaries = []
        for root in canonical_roots:
            try:
                parent.relative_to(root)
                boundaries.append(root)
            except ValueError:
                pass
        if not boundaries:
            continue
        boundary = max(boundaries, key=lambda value: len(value.parts))
        while parent != boundary:
            try:
                parent.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                break
            parent = parent.parent


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
    all_versions = [version_root, *retained_versions]
    if len({path.resolve() for path in all_versions}) != len(all_versions):
        raise RuntimeError("receipt contains duplicate version roots")
    return receipt, version_root, retained_versions, managed


def _receipt_targets(root: Path, receipt: dict) -> list[Path]:
    canonical_root = root.resolve()
    if (
        receipt.get("schema_version") != RECEIPT_VERSION
        or Path(receipt.get("install_root", "")).resolve() != canonical_root
    ):
        raise RuntimeError("journal receipt ownership mismatch")
    version_root = Path(receipt.get("version_root", "")).resolve()
    retained = [Path(value).resolve() for value in receipt.get("retained_version_roots", [])]
    for path in [version_root, *retained]:
        try:
            path.relative_to(canonical_root / "versions")
        except ValueError as exc:
            raise RuntimeError("journal receipt version path escapes install root") from exc
    managed = []
    for item in receipt.get("managed_files", []):
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise RuntimeError("journal receipt managed-file record is invalid")
        path = Path(item["path"]).resolve()
        if not _within(path, allowed_external_roots(root)):
            raise RuntimeError("journal receipt managed path is outside allowed roots")
        managed.append(path)
    targets = [
        *managed,
        version_root,
        *retained,
        canonical_root / "current.txt",
        canonical_root / "install-receipt.json",
        canonical_root / "install-receipt.sha256",
    ]
    if len(set(targets)) != len(targets):
        raise RuntimeError("journal receipt contains duplicate targets")
    return targets


def _receipt_digest(receipt: dict) -> str:
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _remove_quarantine(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _finalize_uninstall(root: Path, managed: list[Path]) -> None:
    _remove_empty_managed_parents(managed, allowed_external_roots(root))
    try:
        (root / "versions").rmdir()
        root.rmdir()
    except OSError:
        pass


def recover_journal(root: Path) -> bool:
    journal_path = root / "uninstall-journal.json"
    if not journal_path.exists():
        return False
    if not journal_path.is_file() or journal_path.is_symlink():
        raise RuntimeError("uninstall journal is unsafe")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    expected_fields = {
        "schema_version",
        "install_root",
        "transaction_id",
        "receipt",
        "receipt_sha256",
        "committed",
        "entries",
    }
    if not isinstance(journal, dict) or set(journal) != expected_fields:
        raise RuntimeError("uninstall journal fields mismatch")
    if type(journal["committed"]) is not bool:
        raise RuntimeError("uninstall journal committed flag must be boolean")
    if (
        journal.get("schema_version") != JOURNAL_VERSION
        or Path(journal.get("install_root", "")).resolve() != root.resolve()
    ):
        raise RuntimeError("uninstall journal ownership mismatch")
    receipt = journal.get("receipt")
    transaction_id = journal.get("transaction_id")
    if (
        not isinstance(receipt, dict)
        or journal.get("receipt_sha256") != _receipt_digest(receipt)
        or not isinstance(transaction_id, str)
        or len(transaction_id) != 12
        or any(character not in "0123456789abcdef" for character in transaction_id)
    ):
        raise RuntimeError("uninstall journal receipt binding is invalid")
    expected_targets = _receipt_targets(root, receipt)
    raw_entries = journal.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) != len(expected_targets):
        raise RuntimeError("uninstall journal target set is invalid")
    entries = []
    for item, expected_target in zip(raw_entries, expected_targets):
        if not isinstance(item, dict) or set(item) != {"target", "quarantine", "moved"}:
            raise RuntimeError("uninstall journal entry is invalid")
        target = Path(item["target"]).resolve()
        quarantine = Path(item["quarantine"]).resolve()
        expected_quarantine = target.with_name(f".{target.name}.uninstall-{transaction_id}")
        if target != expected_target or quarantine != expected_quarantine or type(item["moved"]) is not bool:
            raise RuntimeError("uninstall journal entry is not receipt-derived")
        entries.append((target, quarantine))
    if journal["committed"]:
        if not all(item["moved"] is True for item in raw_entries):
            raise RuntimeError("committed uninstall journal has incomplete move state")
        for _, quarantine in entries:
            try:
                _remove_quarantine(quarantine)
            except OSError:
                return True
        journal_path.unlink(missing_ok=True)
        managed = [Path(item["path"]).resolve() for item in receipt["managed_files"]]
        _finalize_uninstall(root, managed)
        return True
    errors = []
    for target, quarantine in reversed(entries):
        if quarantine.exists() or quarantine.is_symlink():
            if target.exists() or target.is_symlink():
                raise RuntimeError(
                    f"uninstall recovery target was recreated while quarantined: {target}"
                )
            try:
                os.replace(quarantine, target)
            except OSError as exc:
                errors.append(exc)
    if errors:
        raise RuntimeError("uninstall recovery was incomplete: " + "; ".join(map(str, errors)))
    journal_path.unlink(missing_ok=True)
    return True


def uninstall(root: Path) -> None:
    receipt, version_root, retained_versions, managed = load_receipt(root)
    targets = [
        *managed,
        version_root,
        *retained_versions,
        root / "current.txt",
        root / "install-receipt.json",
        root / "install-receipt.sha256",
    ]
    canonical = [path.resolve() for path in targets]
    if len(set(canonical)) != len(canonical):
        raise RuntimeError("uninstall targets overlap or repeat")
    transaction_id = uuid.uuid4().hex[:12]
    entries = [
        {
            "target": str(path),
            "quarantine": str(path.with_name(f".{path.name}.uninstall-{transaction_id}")),
            "moved": False,
        }
        for path in targets
    ]
    journal = {
        "schema_version": JOURNAL_VERSION,
        "install_root": str(root.resolve()),
        "transaction_id": transaction_id,
        "receipt": receipt,
        "receipt_sha256": _receipt_digest(receipt),
        "committed": False,
        "entries": entries,
    }
    journal_path = root / "uninstall-journal.json"
    atomic_json(journal_path, journal)
    moved = []
    try:
        for entry in entries:
            target = Path(entry["target"])
            quarantine = Path(entry["quarantine"])
            if quarantine.exists() or quarantine.is_symlink():
                raise RuntimeError(f"uninstall quarantine already exists: {quarantine}")
            os.replace(target, quarantine)
            entry["moved"] = True
            moved.append((target, quarantine))
            atomic_json(journal_path, journal)
    except BaseException as uninstall_error:
        restore_errors = []
        for target, quarantine in reversed(moved):
            try:
                os.replace(quarantine, target)
            except OSError as exc:
                restore_errors.append(exc)
        if restore_errors:
            raise RuntimeError(
                "uninstall failed and recovery was incomplete: "
                + "; ".join(map(str, restore_errors))
            ) from uninstall_error
        journal_path.unlink(missing_ok=True)
        raise
    journal["committed"] = True
    atomic_json(journal_path, journal)
    for _, quarantine in moved:
        try:
            _remove_quarantine(quarantine)
        except OSError:
            return
    journal_path.unlink(missing_ok=True)
    _finalize_uninstall(root, managed)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    journal = root / "uninstall-journal.json"
    if journal.exists():
        with installer_lock(root):
            recovered = recover_journal(root)
        if recovered and not (root / "install-receipt.json").exists():
            return 0
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
    with installer_lock(root):
        recover_journal(root)
        uninstall(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
