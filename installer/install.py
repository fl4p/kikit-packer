#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

RECEIPT_VERSION = 2


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


def probe(interpreter: Path, timeout: float = 15) -> bool:
    code = "import pcbnew, wx; print(pcbnew.GetBuildVersion(), wx.version())"
    try:
        return subprocess.run(
            [str(interpreter), "-c", code], capture_output=True, timeout=timeout, check=False
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
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


def external_payloads(root: Path):
    payloads = []
    if os.name == "nt":
        launcher = root / "bin/kikit-packer.cmd"
        command = (
            '@for /f "usebackq delims=" %%i in ("{}") do @set "CURRENT=%%i"\r\n'
            '@"%CURRENT%\\venv\\Scripts\\python.exe" -m kikit_packer %*\r\n'
        ).format(root / "current.txt")
        payloads.append((launcher, command.encode("utf-8"), 0o644))
        start_menu = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"
        gui = start_menu / "KiKit Packer.cmd"
        gui_command = command.replace("kikit_packer %*", "kikit_packer gui %*")
        payloads.append((gui, gui_command.encode("utf-8"), 0o644))
    else:
        launcher = Path.home() / ".local/bin/kikit-packer"
        payloads.append((launcher, shell_launcher(root), 0o755))
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


def install(
    interpreter: Path,
    source: Path,
    root: Path,
    expected_source_sha256: str = "",
) -> Path:
    if source.is_file():
        actual_source_hash = file_hash(source)
        if not expected_source_sha256:
            raise RuntimeError("--source-sha256 is required for wheel installation")
        if actual_source_hash != expected_source_sha256.lower():
            raise RuntimeError("source artifact SHA-256 mismatch")
    else:
        actual_source_hash = "development-directory"
    root.mkdir(parents=True, exist_ok=True)
    versions = root / "versions"
    versions.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".install-", dir=str(versions)))
    environment = staging / "venv"
    final = None
    backups = {}
    try:
        subprocess.run(
            [str(interpreter), "-m", "venv", "--system-site-packages", str(environment)], check=True
        )
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        lock = dependency_lock(interpreter)
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--require-hashes",
                "--no-deps",
                "--no-build-isolation",
                "-r",
                str(lock),
            ],
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
            check=True
        )
        doctor = subprocess.run(
            [str(python), "-m", "kikit_packer", "doctor", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if doctor.returncode not in (0, 1):
            raise RuntimeError(
                f"installed environment failed doctor (exit {doctor.returncode}): stdout={doctor.stdout[-4000:]} stderr={doctor.stderr[-4000:]}"
            )
        version = subprocess.run(
            [str(python), "-c", "import kikit_packer; print(kikit_packer.__version__)"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        install_id = uuid.uuid4().hex
        final = versions / (version + "-" + install_id[:12])
        os.replace(str(staging), str(final))
        payloads = external_payloads(root)
        current = root / "current.txt"
        receipt_path = root / "install-receipt.json"
        receipt_hash_path = root / "install-receipt.sha256"
        external = [current, receipt_path, receipt_hash_path] + [path for path, _, _ in payloads]
        for path in external:
            if path.exists() and path.is_file() and not path.is_symlink():
                backups[path] = (path.read_bytes(), path.stat().st_mode & 0o777)
            elif path.exists() or path.is_symlink():
                raise RuntimeError(f"installer-managed path is not a regular file: {path}")
            else:
                backups[path] = None
        atomic_write(current, (str(final.resolve()) + "\n").encode("utf-8"), 0o600)
        for path, data, mode in payloads:
            atomic_write(path, data, mode)
        package_versions = json.loads(
            subprocess.run(
                [str(final / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")), "-m", "pip", "list", "--format=json"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        retained_versions = []
        for candidate in versions.iterdir():
            if candidate != final:
                if not candidate.is_dir() or candidate.is_symlink():
                    raise RuntimeError(f"unexpected entry in version store: {candidate}")
                retained_versions.append(str(candidate.resolve()))
        receipt = {
            "schema_version": RECEIPT_VERSION,
            "install_id": install_id,
            "install_root": str(root.resolve()),
            "version_root": str(final.resolve()),
            "retained_version_roots": sorted(retained_versions),
            "runtime": str(interpreter.absolute()),
            "source": str(source.resolve()),
            "source_sha256": actual_source_hash,
            "dependency_lock_sha256": file_hash(lock),
            "packages": package_versions,
            "managed_files": [
                {"path": str(path.resolve()), "sha256": file_hash(path)}
                for path, _, _ in payloads
            ],
        }
        receipt_bytes = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode("utf-8")
        atomic_write(receipt_path, receipt_bytes, 0o600)
        atomic_write(receipt_hash_path, (hashlib.sha256(receipt_bytes).hexdigest() + "\n").encode("ascii"), 0o600)
        return payloads[0][0]
    except BaseException:
        for path, backup in reversed(list(backups.items())):
            try:
                if backup is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, backup[0], backup[1])
            except OSError:
                pass
        if final is not None:
            shutil.rmtree(final, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
        raise


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
