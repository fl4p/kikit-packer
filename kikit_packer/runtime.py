import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SUPPORTED_CELL = {
    "system": "Darwin",
    "machine": "arm64",
    "python_major_minor": "3.9",
    "kicad": "10.0.5",
    "kikit": "1.8.1",
    "rectangle_packer": "2.0.2",
    "shapely": "2.0.7",
}

PROBE = r'''
import importlib, importlib.metadata, json, os, platform, sys
result = {
    "python": platform.python_version(),
    "python_major_minor": "%s.%s" % (sys.version_info[0], sys.version_info[1]),
    "implementation": platform.python_implementation(),
    "executable": sys.executable,
    "system": platform.system(),
    "machine": platform.machine(),
    "platform": platform.platform(),
    "modules": {},
    "capabilities": {},
}
distributions = {
    "kikit": "KiKit",
    "rpack": "rectangle-packer",
    "yaml": "PyYAML",
    "shapely": "Shapely",
}
for name in ("pcbnew", "kikit", "wx", "rpack", "yaml", "shapely"):
    try:
        module = importlib.import_module(name)
        version = None
        if name in distributions:
            version = importlib.metadata.version(distributions[name])
        if version is None:
            version = str(getattr(module, "__version__", getattr(module, "VERSION", "unknown")))
        result["modules"][name] = {
            "ok": True,
            "version": version,
            "origin": str(getattr(module, "__file__", "")),
        }
    except BaseException as exc:
        result["modules"][name] = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}
try:
    import pcbnew
    result["kicad"] = str(pcbnew.GetBuildVersion())
    board = pcbnew.BOARD()
    result["capabilities"]["board_create"] = board.GetCopperLayerCount() >= 2
    result["capabilities"]["board_thickness"] = hasattr(board.GetDesignSettings(), "GetBoardThickness")
except BaseException as exc:
    result["kicad"] = None
    result["capabilities"]["pcbnew"] = type(exc).__name__ + ": " + str(exc)
result["display"] = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or sys.platform in ("darwin", "win32"))
print(json.dumps(result, sort_keys=True))
'''


def candidate_interpreters() -> list[Path]:
    candidates = [Path(sys.executable)]
    if sys.platform == "darwin":
        standard = Path(
            "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
        )
        candidates.append(standard)
        applications = Path("/Applications")
        if applications.exists():
            candidates.extend(
                app / "Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
                for app in applications.glob("KiCad*.app")
            )
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


def _support_problems(result: dict[str, Any]) -> list[str]:
    problems = []
    required = result.get("modules", {})
    for name in ("pcbnew", "kikit", "wx", "rpack", "yaml", "shapely"):
        if not required.get(name, {}).get("ok"):
            problems.append(f"missing module: {name}")
    checks = {
        "system": result.get("system"),
        "machine": result.get("machine"),
        "python_major_minor": result.get("python_major_minor"),
        "kicad": result.get("kicad"),
        "kikit": required.get("kikit", {}).get("version"),
        "rectangle_packer": required.get("rpack", {}).get("version"),
        "shapely": required.get("shapely", {}).get("version"),
    }
    for field, expected in SUPPORTED_CELL.items():
        if checks.get(field) != expected:
            problems.append(f"unsupported {field}: {checks.get(field)} (expected {expected})")
    capabilities = result.get("capabilities", {})
    for capability in ("board_create", "board_thickness"):
        if capabilities.get(capability) is not True:
            problems.append(f"missing capability: {capability}")
    if result.get("implementation") != "CPython":
        problems.append("unsupported Python implementation")
    for name in ("pcbnew", "wx"):
        origin = required.get(name, {}).get("origin", "")
        if "/Applications/KiCad/" not in origin:
            problems.append(f"protected module has unexpected origin: {name}={origin}")
    return problems


def probe_runtime(interpreter: Path, timeout: float = 15.0) -> dict[str, Any]:
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.run(
                [str(interpreter), "-s", "-c", PROBE],
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=timeout,
                env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONPATH": ""},
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
        return {
            "executable": str(interpreter),
            "usable": False,
            "error": "probe returned invalid JSON",
            "stderr": stderr,
        }
    problems = _support_problems(result)
    result["support_cell"] = dict(SUPPORTED_CELL)
    result["support_problems"] = problems
    result["capable"] = process.returncode == 0 and not any(
        problem.startswith("missing") for problem in problems
    )
    result["usable"] = process.returncode == 0 and not problems
    if stderr:
        result["stderr"] = stderr
    return result


def discover() -> list[dict[str, Any]]:
    return [probe_runtime(candidate) for candidate in candidate_interpreters()]
