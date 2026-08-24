import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_receipt(root: Path, receipt: dict):
    encoded = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode()
    (root / "install-receipt.json").write_bytes(encoded)
    (root / "install-receipt.sha256").write_text(hashlib.sha256(encoded).hexdigest() + "\n")


def test_uninstall_rejects_receipt_escape(tmp_path: Path):
    module = load("uninstall_escape", ROOT / "installer/uninstall.py")
    root = tmp_path / "install"
    root.mkdir()
    (root / "current.txt").write_text(str(tmp_path / "escape") + "\n")
    write_receipt(
        root,
        {
            "schema_version": 2,
            "install_root": str(root),
            "version_root": str(tmp_path / "escape"),
            "managed_files": [],
        },
    )
    with pytest.raises(RuntimeError, match="escapes"):
        module.load_receipt(root)


def test_uninstall_rejects_modified_managed_file_before_mutation(tmp_path: Path, monkeypatch):
    module = load("uninstall_modified", ROOT / "installer/uninstall.py")
    root = tmp_path / "install"
    version = root / "versions/0.1.0-id"
    version.mkdir(parents=True)
    managed = tmp_path / "launcher"
    managed.write_text("modified")
    (root / "current.txt").write_text(str(version) + "\n")
    monkeypatch.setattr(module, "allowed_external_roots", lambda _root: [tmp_path])
    write_receipt(
        root,
        {
            "schema_version": 2,
            "install_root": str(root),
            "version_root": str(version),
            "managed_files": [{"path": str(managed), "sha256": "0" * 64}],
        },
    )
    with pytest.raises(RuntimeError, match="modified"):
        module.load_receipt(root)
    assert managed.read_text() == "modified"
    assert version.is_dir()


def test_uninstall_rename_failure_restores_every_target(tmp_path: Path, monkeypatch):
    module = load("uninstall_transaction", ROOT / "installer/uninstall.py")
    root = tmp_path / "install"
    version = root / "versions/0.1.0-id"
    version.mkdir(parents=True)
    managed = [tmp_path / "launcher-a", tmp_path / "launcher-b"]
    for path in managed:
        path.write_text(path.name)
    (root / "current.txt").write_text(str(version) + "\n")
    monkeypatch.setattr(module, "allowed_external_roots", lambda _root: [tmp_path])
    write_receipt(
        root,
        {
            "schema_version": 2,
            "install_root": str(root),
            "version_root": str(version),
            "retained_version_roots": [],
            "managed_files": [
                {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in managed
            ],
        },
    )
    original_replace = module.os.replace
    failed = False

    def replace(source, destination):
        nonlocal failed
        if Path(source) == managed[1] and not failed:
            failed = True
            raise OSError("injected quarantine failure")
        return original_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", replace)
    with pytest.raises(OSError, match="injected"):
        module.uninstall(root)
    assert [path.read_text() for path in managed] == ["launcher-a", "launcher-b"]
    assert version.is_dir()
    assert (root / "install-receipt.json").is_file()
    assert not (root / "uninstall-journal.json").exists()


def test_uninstall_recovery_rejects_non_receipt_target(tmp_path: Path, monkeypatch):
    module = load("uninstall_corrupt_journal", ROOT / "installer/uninstall.py")
    root = tmp_path / "install"
    root.mkdir()
    version = root / "versions/0.1.0-id"
    managed = tmp_path / "launcher"
    victim = tmp_path / "victim"
    victim.write_text("keep")
    monkeypatch.setattr(module, "allowed_external_roots", lambda _root: [tmp_path])
    receipt = {
        "schema_version": 2,
        "install_root": str(root),
        "version_root": str(version),
        "retained_version_roots": [],
        "managed_files": [{"path": str(managed), "sha256": "a" * 64}],
    }
    transaction_id = "0123456789ab"
    targets = module._receipt_targets(root, receipt)
    entries = [
        {
            "target": str(path),
            "quarantine": str(path.with_name(f".{path.name}.uninstall-{transaction_id}")),
            "moved": False,
        }
        for path in targets
    ]
    entries[0]["target"] = str(victim)
    module.atomic_json(root / "uninstall-journal.json", {
        "schema_version": module.JOURNAL_VERSION,
        "install_root": str(root),
        "transaction_id": transaction_id,
        "receipt": receipt,
        "receipt_sha256": module._receipt_digest(receipt),
        "committed": False,
        "entries": entries,
    })
    with pytest.raises(RuntimeError, match="receipt-derived"):
        module.recover_journal(root)
    assert victim.read_text() == "keep"


def test_uninstall_recovery_rejects_string_committed_without_deleting(tmp_path: Path, monkeypatch):
    module = load("uninstall_wrong_committed", ROOT / "installer/uninstall.py")
    root = tmp_path / "install"
    root.mkdir()
    version = root / "versions/0.1.0-id"
    managed = tmp_path / "launcher"
    monkeypatch.setattr(module, "allowed_external_roots", lambda _root: [tmp_path])
    receipt = {
        "schema_version": 2,
        "install_root": str(root),
        "version_root": str(version),
        "retained_version_roots": [],
        "managed_files": [{"path": str(managed), "sha256": "a" * 64}],
    }
    transaction_id = "abcdef012345"
    targets = module._receipt_targets(root, receipt)
    entries = []
    for target in targets:
        quarantine = target.with_name(f".{target.name}.uninstall-{transaction_id}")
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        quarantine.write_text("preserve")
        entries.append({"target": str(target), "quarantine": str(quarantine), "moved": True})
    module.atomic_json(root / "uninstall-journal.json", {
        "schema_version": module.JOURNAL_VERSION,
        "install_root": str(root),
        "transaction_id": transaction_id,
        "receipt": receipt,
        "receipt_sha256": module._receipt_digest(receipt),
        "committed": "false",
        "entries": entries,
    })
    with pytest.raises(RuntimeError, match="must be boolean"):
        module.recover_journal(root)
    assert all(Path(item["quarantine"]).read_text() == "preserve" for item in entries)


def test_uninstall_removes_empty_managed_app_bundle(tmp_path: Path):
    module = load("uninstall_empty_parents", ROOT / "installer/uninstall.py")
    applications = tmp_path / "Applications"
    executable = applications / "KiKit Packer.app/Contents/MacOS/kikit-packer"
    plist = applications / "KiKit Packer.app/Contents/Info.plist"
    executable.parent.mkdir(parents=True)
    executable.write_text("launcher")
    plist.write_text("plist")
    executable.unlink()
    plist.unlink()
    module._remove_empty_managed_parents([executable, plist], [applications])
    assert applications.is_dir()
    assert not (applications / "KiKit Packer.app").exists()


def test_install_rejects_unowned_version_store(tmp_path: Path):
    module = load("install_ownership", ROOT / "installer/install.py")
    root = tmp_path / "install"
    (root / "versions/unowned").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="without an ownership receipt"):
        module._owned_receipt(root, [])


def test_source_identity_includes_package_json(tmp_path: Path):
    module = load("install_source_identity", ROOT / "installer/install.py")
    source = tmp_path / "source"
    (source / "kikit_packer").mkdir(parents=True)
    preset = source / "kikit_packer/kikit_181_preset.json"
    preset.write_text('{"value":1}')
    before = module.source_hash(source)
    preset.write_text('{"value":2}')
    assert module.source_hash(source) != before


def test_wheel_install_requires_expected_hash(tmp_path: Path):
    module = load("install_hash", ROOT / "installer/install.py")
    wheel = tmp_path / "package.whl"
    wheel.write_bytes(b"artifact")
    with pytest.raises(RuntimeError, match="source-sha256"):
        module.install(Path("python"), wheel, tmp_path / "root")
