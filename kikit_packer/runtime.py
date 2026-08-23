import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PROBE = r'''
import importlib, json, os, platform, sys
result = {"python": sys.version.split()[0], "executable": sys.executable, "platform": platform.platform(), "modules": {}}
for name in ("pcbnew", "kikit", "wx", "rpack", "yaml", "shapely"):
    try:
        module = importlib.import_module(name)
        result["modules"][name] = {"ok": True, "version": str(getattr(module, "__version__", getattr(module, "VERSION", "unknown")))}
    except BaseException as exc:
        result["modules"][name] = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}
try:
    import pcbnew
    result["kicad"] = str(pcbnew.GetBuildVersion())
except BaseException:
    result["kicad"] = None
result["display"] = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or sys.platform in ("darwin", "win32"))
print(json.dumps(result, sort_keys=True))
'''


def candidate_interpreters() -> list[Path]:
    candidates = [Path(sys.executable)]
    if sys.platform == "darwin":
        applications = Path("/Applications")
        if applications.exists():
            candidates.extend(app / "Contents/Frameworks/Python.framework/Versions/Current/bin/python3" for app in applications.glob("KiCad*.app"))
    elif os.name == "nt":
        for root in filter(None, (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA"))):
            candidates.extend(Path(root).glob("KiCad/*/bin/python.exe"))
    else:
        for name in ("python3", "/usr/bin/python3", "/usr/local/bin/python3"):
            resolved = shutil.which(name)
            if resolved:
                candidates.append(Path(resolved))
    unique = []
    seen = set()
    for candidate in candidates:
        executable = Path(os.path.abspath(os.path.expanduser(str(candidate))))
        if not executable.is_file():
            continue
        identity = os.path.normcase(str(executable))
        if identity not in seen:
            seen.add(identity)
            unique.append(executable)
    return unique


def probe_runtime(interpreter: Path, timeout: float = 15.0) -> dict[str, Any]:
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.run(
                [str(interpreter), "-c", PROBE],
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=timeout,
                env=dict(os.environ),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"executable": str(interpreter), "usable": False, "error": "probe timed out"}
        stdout_file.seek(max(0, stdout_file.tell() - 1_048_576))
        stderr_file.seek(max(0, stderr_file.tell() - 1_048_576))
        stdout = stdout_file.read().decode("utf-8", errors="replace")
        stderr = stderr_file.read().decode("utf-8", errors="replace")
    try:
        result = json.loads(stdout)
    except (ValueError, TypeError):
        return {"executable": str(interpreter), "usable": False, "error": "probe returned invalid JSON", "stderr": stderr}
    required = result.get("modules", {})
    result["usable"] = process.returncode == 0 and all(required.get(name, {}).get("ok") for name in ("pcbnew", "kikit", "wx", "rpack", "yaml", "shapely"))
    if stderr:
        result["stderr"] = stderr
    return result


def discover() -> list[dict[str, Any]]:
    return [probe_runtime(candidate) for candidate in candidate_interpreters()]
