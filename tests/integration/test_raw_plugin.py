import subprocess
import sys
from pathlib import Path
from typing import Any, cast

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


@pytest.mark.parametrize(
    ("bounds", "start", "end", "expected_cuts"),
    [
        (
            [(0, 0, 8, 15), (10, 0, 18, 15)],
            (8, 7.5),
            (10, 7.5),
            [[(8, 6.5), (8, 8.5)], [(10, 8.5), (10, 6.5)]],
        ),
        (
            [(10, 0, 18, 15), (0, 0, 8, 15)],
            (10, 7.5),
            (8, 7.5),
            [[(10, 8.5), (10, 6.5)], [(8, 6.5), (8, 8.5)]],
        ),
        (
            [(0, 0, 15, 8), (0, 10, 15, 18)],
            (7.5, 8),
            (7.5, 10),
            [[(8.5, 8), (6.5, 8)], [(6.5, 10), (8.5, 10)]],
        ),
        (
            [(0, 10, 15, 18), (0, 0, 15, 8)],
            (7.5, 10),
            (7.5, 8),
            [[(6.5, 10), (8.5, 10)], [(8.5, 8), (6.5, 8)]],
        ),
    ],
    ids=["right", "left", "down", "up"],
)
def test_flat_edge_tabs_build_material_without_partition_lines(
    bounds, start, end, expected_cuts
):
    pytest.importorskip("pcbnew")
    from shapely.geometry import LineString, box
    from shapely.ops import unary_union

    from kikit_packer.plugin import FlatEdgeTabs

    class Substrate:
        def __init__(self, geometry):
            self.substrates = geometry
            self.annotations = []

        def bounds(self):
            return self.substrates.bounds

    class BoardSubstrate:
        def __init__(self, geometries):
            self.geometry = unary_union(geometries)

        def union(self, geometries):
            self.geometry = unary_union([self.geometry, *geometries])

    class Panel:
        def __init__(self):
            self.substrates = [
                Substrate(box(*(coordinate * unit for coordinate in item)))
                for item in bounds
            ]
            self.forwardTabs = []
            self.boardSubstrate = BoardSubstrate(
                [substrate.substrates for substrate in self.substrates]
            )

        def clearTabsAnnotations(self):
            for substrate in self.substrates:
                substrate.annotations = []

    unit = 1_000_000

    def scaled(point):
        return tuple(coordinate * unit for coordinate in point)

    panel = Panel()
    cuts = FlatEdgeTabs({}, "2").buildTabs(cast(Any, panel))

    assert [list(cut.coords) for cut in cuts] == [
        [scaled(point) for point in cut] for cut in expected_cuts
    ]
    assert all(cut.length == 2_000_000 for cut in cuts)
    assert len(panel.forwardTabs) == 1
    assert panel.boardSubstrate.geometry.geom_type == "Polygon"
    assert panel.forwardTabs[0].buffer(5000).covers(
        LineString([scaled(start), scaled(end)])
    )
    assert cast(Any, panel)._kikit_packer_tab_connections == [
        {
            "left": 0,
            "right": 1,
            "start": scaled(start),
            "end": scaled(end),
        }
    ]


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
