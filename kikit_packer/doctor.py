from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .runtime import discover


def report(project: Path | None = None) -> dict[str, Any]:
    runtimes = discover()
    selected = next((item for item in runtimes if item.get("usable")), None)
    warnings = []
    paths: dict[str, Any] = {
        "package_directory": str(Path(__file__).resolve().parent),
        "package_readable": os.access(str(Path(__file__).resolve().parent), os.R_OK),
    }
    if project is not None:
        project = project.expanduser().resolve()
        paths.update({
            "project": str(project),
            "project_readable": project.is_file() and os.access(str(project), os.R_OK),
            "directory_writable": os.access(str(project.parent), os.W_OK),
        })
        try:
            from .config import load_project

            loaded = load_project(project)
            output = loaded.panel.output
            if output is not None:
                paths["output"] = str(output)
                paths["output_directory_writable"] = os.access(str(output.parent), os.W_OK)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"project validation failed: {exc}")
    kicad_cli = shutil.which("kicad-cli")
    if not kicad_cli:
        warnings.append("kicad-cli is not available")
    if selected is not None and not selected.get("display"):
        warnings.append("wxPython imports, but no interactive display is available")
    return {
        "kind": "kikit-packer.doctor",
        "schema_version": 1,
        "runtimes": runtimes,
        "selected": selected,
        "kicad_cli": kicad_cli,
        "paths": paths,
        "warnings": warnings,
        "status": "unusable" if selected is None else ("usable_with_warnings" if warnings else "usable"),
    }


def run(project: Path | None = None, json_output: bool = False) -> int:
    value = report(project)
    usable = value["selected"] is not None
    if json_output:
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    else:
        print("KiKit Packer doctor")
        for runtime in value["runtimes"]:
            marker = "ok" if runtime.get("usable") else "unusable"
            print("- {}: {}".format(runtime.get("executable"), marker))
            if runtime.get("kicad"):
                print("  KiCad {}".format(runtime["kicad"]))
        print("kicad-cli: {}".format(value["kicad_cli"] or "not found"))
    if not usable:
        return 7
    return 1 if value["warnings"] else 0
