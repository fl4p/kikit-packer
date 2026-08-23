import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "example"


def test_path_loaded_plugin_from_example_directory(tmp_path: Path):
    pytest.importorskip("pcbnew")
    output = tmp_path / "raw.kicad_pcb"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "kikit.ui",
            "panelize",
            "--layout",
            "plugin; code: ../kikit-packer.py.Plugin; input:merge.yaml",
            "--tabs",
            "fixed; hwidth: 2mm; vwidth: 2mm",
            "--cuts",
            "mousebites",
            "--post",
            "millradius: 1mm",
            "main.kicad_pcb",
            str(output),
        ],
        cwd=str(EXAMPLE),
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    assert output.is_file()


def test_raw_plugin_rejects_versioned_project(tmp_path: Path):
    pytest.importorskip("pcbnew")
    project = tmp_path / "project.yaml"
    project.write_text("version: 1\nboards:\n  - board: {}\n".format(EXAMPLE / "main.kicad_pcb"))
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "kikit.ui",
            "panelize",
            "--layout",
            "plugin; code: {}.Plugin; input:{}".format(ROOT / "kikit-packer.py", project),
            str(EXAMPLE / "main.kicad_pcb"),
            str(tmp_path / "output.kicad_pcb"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode != 0
    assert "versioned projects must be generated" in process.stderr
