#!/usr/bin/env python3
import argparse
import base64
import hashlib
import importlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

installer_lock = importlib.import_module("kikit_packer.install_lock").installer_lock

RECEIPT_VERSION = 2
INSTALL_JOURNAL_VERSION = 1


def default_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/KiKit Packer"
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "KiKit Packer"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "kikit-packer"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_hash(path: Path) -> str:
    if path.is_file():
        return file_hash(path)
    digest = hashlib.sha256()
    included = []
    for candidate in path.rglob("*"):
        relative = candidate.relative_to(path)
        if (
            candidate.is_file()
            and not candidate.is_symlink()
            and not any(part.startswith(".") or part in {"build", "dist", "venv", "venv-ki", "__pycache__"} for part in relative.parts)
        ):
            included.append((relative, candidate))
    for relative, candidate in sorted(included):
        digest.update(str(relative).encode("utf-8"))
        digest.update(b"\0")
        digest.update(candidate.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def source_version(path: Path) -> str:
    if path.is_file() and path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
            metadata = archive.read(metadata_name).decode("utf-8")
        match = re.search(r"^Version: (.+)$", metadata, re.MULTILINE)
    else:
        match = re.search(
            r'^__version__\s*=\s*["\']([^"\']+)["\']',
            (path / "kikit_packer/__init__.py").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    if match is None:
        raise RuntimeError("cannot determine package version from source")
    return match.group(1).strip()


def runtime_identity(interpreter: Path, timeout: float = 15) -> dict:
    code = r'''import json,platform,sys
import pcbnew,wx
print(json.dumps({
 "python": platform.python_version(),
 "python_major_minor": "%s.%s" % (sys.version_info[0], sys.version_info[1]),
 "system": platform.system(),
 "machine": platform.machine(),
 "kicad": str(pcbnew.GetBuildVersion()),
 "pcbnew_origin": str(pcbnew.__file__),
 "wx_origin": str(wx.__file__),
}, sort_keys=True))'''
    process = subprocess.run(
        [str(interpreter), "-s", "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONPATH": ""},
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(f"KiCad runtime probe failed: {process.stderr[-2000:]}")
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("KiCad runtime probe returned invalid JSON") from exc
    expected = {
        "python_major_minor": "3.9",
        "system": "Darwin",
        "machine": "arm64",
        "kicad": "10.0.5",
    }
    problems = [f"{key}={result.get(key)} expected {value}" for key, value in expected.items() if result.get(key) != value]
    if problems:
        raise RuntimeError("unsupported KiCad runtime: " + "; ".join(problems))
    for name in ("pcbnew_origin", "wx_origin"):
        if "/Applications/KiCad/" not in result.get(name, ""):
            raise RuntimeError(f"protected module has unexpected origin: {name}={result.get(name)}")
    result["support_cell"] = "darwin-arm64-py39-kicad-10.0.5"
    return result


def _owned_receipt(root: Path, payload_paths: list[Path]):
    receipt_path = root / "install-receipt.json"
    receipt_hash_path = root / "install-receipt.sha256"
    current = root / "current.txt"
    ownership_files = (receipt_path, receipt_hash_path, current)
    present = [path.exists() or path.is_symlink() for path in ownership_files]
    if not any(present):
        if any(path.exists() or path.is_symlink() for path in payload_paths):
            raise RuntimeError("an external launcher exists without a receipt owned by this install root")
        versions = root / "versions"
        if versions.exists() and any(versions.iterdir()):
            raise RuntimeError("version store contains entries without an ownership receipt")
        return None
    if not all(present):
        raise RuntimeError("install ownership metadata is incomplete")
    if any(not path.is_file() or path.is_symlink() for path in ownership_files):
        raise RuntimeError("install ownership metadata is unsafe")
    encoded = receipt_path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != receipt_hash_path.read_text(encoding="ascii").strip():
        raise RuntimeError("install receipt hash mismatch")
    receipt = json.loads(encoded)
    if receipt.get("schema_version") != RECEIPT_VERSION or Path(receipt.get("install_root", "")).resolve() != root.resolve():
        raise RuntimeError("install receipt ownership mismatch")
    expected_paths = {str(path.resolve()) for path in payload_paths}
    managed = receipt.get("managed_files", [])
    managed_paths = {item.get("path") for item in managed}
    if not managed_paths.issubset(expected_paths):
        raise RuntimeError("install receipt launcher set mismatch")
    for missing in expected_paths - managed_paths:
        path = Path(missing)
        if path.exists() or path.is_symlink():
            raise RuntimeError("an added launcher path exists without receipt ownership")
    for item in managed:
        path = Path(item["path"])
        if not path.is_file() or path.is_symlink() or file_hash(path) != item["sha256"]:
            raise RuntimeError(f"installer-managed launcher was modified: {path}")
    version_roots = [Path(receipt["version_root"])] + [Path(path) for path in receipt.get("retained_version_roots", [])]
    if Path(current.read_text(encoding="utf-8").strip()).resolve() != version_roots[0].resolve():
        raise RuntimeError("current pointer differs from install receipt")
    for version_root in version_roots:
        try:
            version_root.resolve().relative_to((root / "versions").resolve())
        except ValueError as exc:
            raise RuntimeError("receipt version root escapes installer root") from exc
        if not version_root.is_dir() or version_root.is_symlink():
            raise RuntimeError("receipt version root is missing or unsafe")
    entries = {path.resolve() for path in (root / "versions").iterdir()}
    if entries != {path.resolve() for path in version_roots}:
        raise RuntimeError("version store contains unowned entries")
    return receipt


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _remove_transaction_path(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"install transaction path is a symlink: {path}")
    if path.exists():
        if not path.is_dir():
            raise RuntimeError(f"install transaction path is not a directory: {path}")
        shutil.rmtree(path)


def _backup_record(path: Path, replacement: bytes) -> dict:
    if path.exists() or path.is_symlink():
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"installer-managed path is not a regular file: {path}")
        data = path.read_bytes()
        return {
            "path": str(path.resolve()),
            "existed": True,
            "data": base64.b64encode(data).decode("ascii"),
            "mode": path.stat().st_mode & 0o777,
            "sha256": hashlib.sha256(data).hexdigest(),
            "replacement_sha256": hashlib.sha256(replacement).hexdigest(),
        }
    return {
        "path": str(path.resolve()),
        "existed": False,
        "data": None,
        "mode": None,
        "sha256": None,
        "replacement_sha256": hashlib.sha256(replacement).hexdigest(),
    }


def _restore_install_backup(record: dict) -> None:
    if set(record) != {
        "path", "existed", "data", "mode", "sha256", "replacement_sha256"
    }:
        raise RuntimeError("install journal backup schema is invalid")
    path = Path(record["path"])
    if type(record["existed"]) is not bool:
        raise RuntimeError("install journal backup existed flag is invalid")
    current_hash = None
    if path.exists() or path.is_symlink():
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"installer recovery target is unsafe: {path}")
        current_hash = file_hash(path)
    allowed = {record["replacement_sha256"]}
    if record["existed"]:
        if type(record["data"]) is not str or type(record["mode"]) is not int:
            raise RuntimeError("install journal backup payload is invalid")
        try:
            data = base64.b64decode(record["data"], validate=True)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("install journal backup encoding is invalid") from exc
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise RuntimeError("install journal backup hash mismatch")
        allowed.add(record["sha256"])
        if current_hash not in allowed and current_hash is not None:
            raise RuntimeError(f"installer recovery target changed after interruption: {path}")
        if current_hash != record["sha256"]:
            atomic_write(path, data, record["mode"])
    else:
        if record["data"] is not None or record["mode"] is not None or record["sha256"] is not None:
            raise RuntimeError("install journal absent backup is invalid")
        if current_hash is not None and current_hash not in allowed:
            raise RuntimeError(f"installer recovery target changed after interruption: {path}")
        if current_hash is not None:
            path.unlink()


def recover_install_journal(root: Path, payload_paths: list[Path]) -> bool:
    journal_path = root / "install-journal.json"
    if not journal_path.exists():
        return False
    if not journal_path.is_file() or journal_path.is_symlink():
        raise RuntimeError("install journal is unsafe")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if set(journal) != {
        "schema_version", "install_root", "identity_sha256", "staging", "final",
        "committed", "backups",
    }:
        raise RuntimeError("install journal schema is invalid")
    if journal["schema_version"] != INSTALL_JOURNAL_VERSION:
        raise RuntimeError("unsupported install journal version")
    if Path(journal["install_root"]).resolve() != root.resolve():
        raise RuntimeError("install journal root mismatch")
    if type(journal["identity_sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", journal["identity_sha256"]):
        raise RuntimeError("install journal identity is invalid")
    if type(journal["committed"]) is not bool or not isinstance(journal["backups"], list):
        raise RuntimeError("install journal state is invalid")
    versions = (root / "versions").resolve()
    staging = Path(journal["staging"])
    final = Path(journal["final"])
    for path, prefix in ((staging, ".install-"), (final, "")):
        try:
            path.resolve().relative_to(versions)
        except ValueError as exc:
            raise RuntimeError("install journal path escapes version store") from exc
        invalid_name = path.name.startswith(".") if prefix == "" else not path.name.startswith(prefix)
        if path.parent.resolve() != versions or invalid_name:
            raise RuntimeError("install journal transaction path is invalid")
    expected_paths = {
        str(path.resolve())
        for path in [
            root / "current.txt",
            root / "install-receipt.json",
            root / "install-receipt.sha256",
            *payload_paths,
        ]
    }
    backups = journal["backups"]
    if backups and {record.get("path") for record in backups if isinstance(record, dict)} != expected_paths:
        raise RuntimeError("install journal backup target set mismatch")
    if journal["committed"]:
        _remove_transaction_path(staging)
        journal_path.unlink()
        return True
    for record in reversed(backups):
        if not isinstance(record, dict):
            raise RuntimeError("install journal backup record is invalid")
        _restore_install_backup(record)
    _remove_transaction_path(staging)
    _remove_transaction_path(final)
    journal_path.unlink()
    try:
        (root / "versions").rmdir()
    except OSError:
        pass
    return True


def probe(interpreter: Path, timeout: float = 15) -> bool:
    try:
        runtime_identity(interpreter, timeout)
        return True
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        return False


def discover():
    candidates = [Path(sys.executable)]
    if sys.platform == "darwin":
        candidates.append(
            Path("/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3")
        )
        candidates.extend(
            Path("/Applications").glob(
                "KiCad*.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
            )
        )
    elif os.name == "nt":
        for root in filter(None, [os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")]):
            candidates.extend(Path(root).glob("KiCad/*/bin/python.exe"))
    else:
        for name in ("python3", "/usr/bin/python3", "/usr/local/bin/python3"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
    output = []
    seen = set()
    for candidate in candidates:
        candidate = Path(os.path.abspath(os.path.expanduser(str(candidate))))
        identity = os.path.normcase(str(candidate))
        if identity not in seen and candidate.is_file() and probe(candidate):
            seen.add(identity)
            output.append(candidate)
    return output


def dependency_lock(interpreter: Path) -> Path:
    version = subprocess.run(
        [str(interpreter), "-c", "import platform,sys; print('%s-%s-%s' % (sys.version_info[0], sys.version_info[1], platform.machine()))"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if sys.platform == "darwin" and version == "3-9-arm64":
        return Path(__file__).with_name("requirements-macos-arm64-py39.txt")
    raise RuntimeError(
        f"no hash-locked dependency set exists for {version} on {sys.platform}; this platform remains provisional"
    )


def build_dependency_lock(lock: Path) -> Path:
    build_lock = lock.with_name(lock.name.replace("requirements-", "requirements-build-"))
    if not build_lock.is_file():
        raise RuntimeError(f"hash-locked build dependency set is missing: {build_lock}")
    return build_lock


def locked_distribution_names(*locks: Path) -> list[str]:
    names = []
    for lock in locks:
        for line in lock.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^([A-Za-z0-9_.-]+)==", line)
            if match is not None:
                names.append(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return sorted(set(names))


def verify_locked_distribution_origins(python: Path, environment: Path, locks: list[Path], cwd: Path) -> None:
    code = r'''import importlib.metadata,json,pathlib,re,sysconfig
root=pathlib.Path(sysconfig.get_paths()["purelib"]).resolve()
def normalized(value): return re.sub(r"[-_.]+", "-", value).lower()
found={normalized(item.metadata["Name"]): str(pathlib.Path(item.locate_file("")).resolve()) for item in importlib.metadata.distributions(path=[str(root)])}
print(json.dumps({"root": str(root), "found": found}, sort_keys=True))'''
    process = subprocess.run(
        [str(python), "-c", code],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(process.stdout)
    purelib = Path(result["root"]).resolve()
    try:
        purelib.relative_to(environment.resolve())
    except ValueError as exc:
        raise RuntimeError("staged purelib escapes managed environment") from exc
    found = result["found"]
    for name in locked_distribution_names(*locks):
        origin = found.get(name)
        if origin is None:
            raise RuntimeError(f"locked distribution is missing from managed environment: {name}")
        try:
            Path(origin).resolve().relative_to(purelib)
        except ValueError as exc:
            raise RuntimeError(f"locked distribution escaped managed environment: {name}={origin}") from exc


def shell_launcher(root: Path, gui: bool = False) -> bytes:
    command = "gui " if gui else ""
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        "CURRENT=$(cat {})\n"
        "exec \"$CURRENT/venv/bin/python\" -m kikit_packer {}\"$@\"\n".format(
            shlex.quote(str(root / "current.txt")), command
        )
    ).encode("utf-8")


def shell_uninstaller(root: Path) -> bytes:
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        "CURRENT=$(cat {})\n"
        "exec \"$CURRENT/venv/bin/python\" -m kikit_packer.uninstall --root {} \"$@\"\n".format(
            shlex.quote(str(root / "current.txt")),
            shlex.quote(str(root)),
        )
    ).encode("utf-8")


def external_payloads(root: Path):
    payloads = []
    if os.name == "nt":
        launcher = root / "bin/kikit-packer.cmd"
        command = (
            '@for /f "usebackq delims=" %%i in ("{}") do @set "CURRENT=%%i"\r\n'
            '@"%CURRENT%\\venv\\Scripts\\python.exe" -m kikit_packer %*\r\n'
        ).format(root / "current.txt")
        payloads.append((launcher, command.encode("utf-8"), 0o644))
        uninstaller = root / "bin/kikit-packer-uninstall.cmd"
        uninstall_command = (
            '@for /f "usebackq delims=" %%i in ("{}") do @set "CURRENT=%%i"\r\n'
            '@"%CURRENT%\\venv\\Scripts\\python.exe" -m kikit_packer.uninstall --root "{}" %*\r\n'
        ).format(root / "current.txt", root)
        payloads.append((uninstaller, uninstall_command.encode("utf-8"), 0o644))
        start_menu = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"
        gui = start_menu / "KiKit Packer.cmd"
        gui_command = command.replace("kikit_packer %*", "kikit_packer gui %*")
        payloads.append((gui, gui_command.encode("utf-8"), 0o644))
    else:
        launcher = Path.home() / ".local/bin/kikit-packer"
        payloads.append((launcher, shell_launcher(root), 0o755))
        uninstaller = Path.home() / ".local/bin/kikit-packer-uninstall"
        payloads.append((uninstaller, shell_uninstaller(root), 0o755))
        if sys.platform == "darwin":
            contents = Path.home() / "Applications/KiKit Packer.app/Contents"
            payloads.append((contents / "MacOS/kikit-packer", shell_launcher(root, gui=True), 0o755))
            plist = b'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>kikit-packer</string>
<key>CFBundleIdentifier</key><string>io.github.fl4p.kikit-packer</string>
<key>CFBundleName</key><string>KiKit Packer</string>
<key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>
'''
            payloads.append((contents / "Info.plist", plist, 0o644))
        else:
            desktop = Path.home() / ".local/share/applications/kikit-packer.desktop"
            data = (
                "[Desktop Entry]\nType=Application\nName=KiKit Packer\n"
                f"Exec={launcher} gui %F\nTerminal=false\nCategories=Development;Electronics;\n"
            ).encode()
            payloads.append((desktop, data, 0o644))
    return payloads


def _install_locked(
    interpreter: Path,
    source: Path,
    root: Path,
    expected_source_sha256: str = "",
) -> Path:
    actual_source_hash = source_hash(source)
    if source.is_file() and not expected_source_sha256:
        raise RuntimeError("--source-sha256 is required for wheel installation")
    if source.is_file() and actual_source_hash != expected_source_sha256.lower():
        raise RuntimeError("source artifact SHA-256 mismatch")
    payloads = external_payloads(root)
    payload_paths = [path for path, _, _ in payloads]
    recover_install_journal(root, payload_paths)
    prior = _owned_receipt(root, payload_paths)
    runtime = runtime_identity(interpreter)
    lock = dependency_lock(interpreter)
    build_lock = build_dependency_lock(lock)
    version = source_version(source)
    identity = {
        "version": version,
        "source_sha256": actual_source_hash,
        "dependency_lock_sha256": file_hash(lock),
        "build_dependency_lock_sha256": file_hash(build_lock),
        "runtime": runtime,
    }
    identity_sha256 = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    install_id = identity_sha256[:24]
    if prior is not None and prior.get("identity_sha256") == identity_sha256:
        return payloads[0][0]
    versions = root / "versions"
    versions.mkdir(exist_ok=True)
    staging = versions / f".install-{install_id}"
    final = versions / (version + "-" + install_id)
    if staging.exists() or staging.is_symlink() or final.exists() or final.is_symlink():
        raise RuntimeError("deterministic install transaction path already exists")
    journal_path = root / "install-journal.json"
    journal = {
        "schema_version": INSTALL_JOURNAL_VERSION,
        "install_root": str(root.resolve()),
        "identity_sha256": identity_sha256,
        "staging": str(staging.resolve()),
        "final": str(final.resolve()),
        "committed": False,
        "backups": [],
    }
    staging.mkdir()
    atomic_write(
        journal_path,
        (json.dumps(journal, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        0o600,
    )
    environment = staging / "venv"
    backups = []
    try:
        subprocess.run(
            [str(interpreter), "-m", "venv", "--system-site-packages", str(environment)], check=True
        )
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        for requirement_lock in (build_lock, lock):
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--require-hashes",
                    "--ignore-installed",
                    "--no-deps",
                    "--no-build-isolation",
                    "-r",
                    str(requirement_lock),
                ],
                cwd=staging,
                check=True,
            )
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-build-isolation",
                str(source),
            ],
            cwd=staging,
            check=True,
        )
        verify_locked_distribution_origins(
            python,
            environment,
            [build_lock, lock],
            staging,
        )
        doctor = subprocess.run(
            [str(python), "-m", "kikit_packer", "doctor", "--json"],
            cwd=staging,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if doctor.returncode not in (0, 1):
            raise RuntimeError(
                f"installed environment failed doctor (exit {doctor.returncode}): stdout={doctor.stdout[-4000:]} stderr={doctor.stderr[-4000:]}"
            )
        try:
            doctor_data = json.loads(doctor.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("installed environment doctor returned invalid JSON") from exc
        selected = doctor_data.get("selected")
        if selected is None or Path(selected.get("executable", "")).resolve() != python.resolve():
            raise RuntimeError("doctor did not select the staged interpreter")
        selected_modules = selected.get("modules", {})
        if selected_modules.get("pcbnew", {}).get("origin") != runtime["pcbnew_origin"]:
            raise RuntimeError("staged environment changed protected pcbnew provenance")
        if selected_modules.get("wx", {}).get("origin") != runtime["wx_origin"]:
            raise RuntimeError("staged environment changed protected wx provenance")
        smoke_root = staging / "smoke"
        smoke_root.mkdir()
        resource = subprocess.run(
            [
                str(python),
                "-c",
                "from importlib.resources import files; print(files('kikit_packer').joinpath('resources/smoke.kicad_pcb'))",
            ],
            cwd=staging,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        board_probe = subprocess.run(
            [
                str(python),
                "-c",
                "import pcbnew,sys; b=pcbnew.LoadBoard(sys.argv[1]); raise SystemExit(0 if b and b.GetCopperLayerCount()==2 else 1)",
                resource,
            ],
            cwd=staging,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if board_probe.returncode != 0:
            raise RuntimeError("staged environment failed board-load smoke")
        smoke_output = smoke_root / "panel.kicad_pcb"
        smoke_project = smoke_root / "project.json"
        smoke_project.write_text(json.dumps({
            "version": 1,
            "panel": {
                "authority": {"board": resource, "reference_only": False},
                "output": str(smoke_output),
                "max_width_mm": 40,
                "max_height_mm": 50,
                "tabs": {"mode": "flat-edge", "width_mm": 2},
                "cuts": {"mode": "none"},
                "post": {"mill_radius_mm": 0, "verify_refill_areas": True},
            },
            "boards": [{"board": resource, "qty": 1, "margin_mm": 1}],
        }))
        generation = subprocess.run(
            [str(python), "-m", "kikit_packer", "pack", str(smoke_project)],
            cwd=smoke_root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if generation.returncode != 0 or not smoke_output.is_file():
            raise RuntimeError(
                f"staged environment failed generation smoke: {generation.stderr[-4000:]}"
            )
        gui_import = subprocess.run(
            [str(python), "-c", "import wx; from kikit_packer.gui.frame import MainFrame"],
            cwd=staging,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if gui_import.returncode != 0:
            raise RuntimeError(f"staged environment failed GUI import smoke: {gui_import.stderr[-2000:]}")
        shutil.rmtree(smoke_root)
        installed_version = subprocess.run(
            [str(python), "-c", "import kikit_packer; print(kikit_packer.__version__)"],
            cwd=staging,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if installed_version != version:
            raise RuntimeError("installed package version differs from source metadata")
        package_versions = json.loads(
            subprocess.run(
                [str(python), "-m", "pip", "list", "--format=json"],
                cwd=staging,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        retained_versions = []
        if prior is not None:
            retained_versions = [
                str(Path(prior["version_root"]).resolve()),
                *[str(Path(path).resolve()) for path in prior.get("retained_version_roots", [])],
            ]
        receipt = {
            "schema_version": RECEIPT_VERSION,
            "install_id": install_id,
            "identity_sha256": identity_sha256,
            "install_root": str(root.resolve()),
            "version_root": str(final.resolve()),
            "retained_version_roots": sorted(retained_versions),
            "runtime": runtime,
            "source": str(source.resolve()),
            "source_sha256": actual_source_hash,
            "dependency_lock_sha256": file_hash(lock),
            "build_dependency_lock_sha256": file_hash(build_lock),
            "packages": package_versions,
            "managed_files": [
                {
                    "path": str(path.resolve()),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                for path, data, _ in payloads
            ],
        }
        receipt_bytes = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode("utf-8")
        receipt_hash_bytes = (hashlib.sha256(receipt_bytes).hexdigest() + "\n").encode("ascii")
        current = root / "current.txt"
        receipt_path = root / "install-receipt.json"
        receipt_hash_path = root / "install-receipt.sha256"
        writes = [
            (current, (str(final.resolve()) + "\n").encode("utf-8"), 0o600),
            *payloads,
            (receipt_path, receipt_bytes, 0o600),
            (receipt_hash_path, receipt_hash_bytes, 0o600),
        ]
        backups = [_backup_record(path, data) for path, data, _ in writes]
        journal["backups"] = backups
        atomic_write(
            journal_path,
            (json.dumps(journal, sort_keys=True, indent=2) + "\n").encode("utf-8"),
            0o600,
        )
        os.replace(str(staging), str(final))
        for path, data, mode in writes:
            atomic_write(path, data, mode)
        journal["committed"] = True
        atomic_write(
            journal_path,
            (json.dumps(journal, sort_keys=True, indent=2) + "\n").encode("utf-8"),
            0o600,
        )
        journal_path.unlink()
        return payloads[0][0]
    except BaseException as install_error:
        try:
            recover_install_journal(root, payload_paths)
        except BaseException as recovery_error:
            raise RuntimeError(
                f"installation failed and recovery was incomplete: {recovery_error}"
            ) from install_error
        raise


def install(
    interpreter: Path,
    source: Path,
    root: Path,
    expected_source_sha256: str = "",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    with installer_lock(root):
        return _install_locked(interpreter, source, root, expected_source_sha256)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source-sha256", default="")
    parser.add_argument("--root", type=Path, default=default_root())
    args = parser.parse_args(argv)
    candidates = [args.python.absolute()] if args.python else discover()
    if not candidates:
        print("No KiCad Python with pcbnew and wxPython was found", file=sys.stderr)
        return 7
    launcher = install(
        candidates[0],
        args.source.resolve(),
        args.root.expanduser().resolve(),
        args.source_sha256,
    )
    print(f"Installed KiKit Packer: {launcher}")
    if str(launcher.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"Add {launcher.parent} to PATH to use the bare kikit-packer command")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
