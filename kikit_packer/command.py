from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def child_argv(interpreter: Path, contract: Path) -> list[str]:
    return [str(interpreter), "-m", "kikit_packer.plugin_child", str(contract)]


def open_argv(path: Path, platform: str | None = None) -> list[str]:
    platform = platform or sys.platform
    if platform == "darwin":
        return ["open", str(path)]
    if platform.startswith("win"):
        return ["cmd", "/c", "start", "", str(path)]
    return ["xdg-open", str(path)]


def open_board(path: Path) -> int:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return 0
    return subprocess.run(open_argv(path), check=False).returncode
