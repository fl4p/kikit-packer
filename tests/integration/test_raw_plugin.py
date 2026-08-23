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
    import pcbnew

    board = pcbnew.LoadBoard(str(output))
    bounds = board.GetBoardEdgesBoundingBox()
    assert [bounds.GetX(), bounds.GetY(), bounds.GetWidth(), bounds.GetHeight()] == [
        111_950_270,
        19_950_376,
        73_099_435,
        135_099_624,
    ]
    assert {
        "drawings": len(board.GetDrawings()),
        "footprints": len(board.GetFootprints()),
        "layers": board.GetCopperLayerCount(),
        "thickness": board.GetDesignSettings().GetBoardThickness(),
        "tracks": len(board.GetTracks()),
        "zones": len(board.Zones()),
    } == {
        "drawings": 946,
        "footprints": 42,
        "layers": 2,
        "thickness": 1_600_000,
        "tracks": 0,
        "zones": 5,
    }


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
