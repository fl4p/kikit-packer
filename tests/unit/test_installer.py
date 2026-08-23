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


def test_wheel_install_requires_expected_hash(tmp_path: Path):
    module = load("install_hash", ROOT / "installer/install.py")
    wheel = tmp_path / "package.whl"
    wheel.write_bytes(b"artifact")
    with pytest.raises(RuntimeError, match="source-sha256"):
        module.install(Path("python"), wheel, tmp_path / "root")
