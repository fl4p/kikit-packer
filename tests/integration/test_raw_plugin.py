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


def _write_reference_fixture(path: Path) -> dict[str, dict[str, Any]]:
    import pcbnew

    board = pcbnew.BOARD()
    mm = pcbnew.FromMM
    corners = [(0, 0), (30, 0), (30, 20), (0, 20)]
    for (x0, y0), (x1, y1) in zip(corners, corners[1:] + corners[:1]):
        edge = pcbnew.PCB_SHAPE(board)
        edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
        edge.SetLayer(pcbnew.Edge_Cuts)
        edge.SetStart(pcbnew.VECTOR2I(mm(x0), mm(y0)))
        edge.SetEnd(pcbnew.VECTOR2I(mm(x1), mm(y1)))
        edge.SetWidth(mm(0.1))
        board.Add(edge)

    specs = {
        "R1": {"pos": (5, 5), "angle": 0, "layer": pcbnew.F_SilkS, "visible": True, "upright": False, "mirrored": False},
        "R2": {"pos": (15, 5), "angle": 0, "layer": pcbnew.F_SilkS, "visible": False, "upright": False, "mirrored": False},
        "C1": {"pos": (5, 15), "angle": 180, "layer": pcbnew.F_SilkS, "visible": True, "upright": True, "mirrored": False},
        "U1": {"pos": (20, 12), "angle": 90, "layer": pcbnew.B_SilkS, "visible": True, "upright": False, "mirrored": True},
    }
    for reference, spec in specs.items():
        footprint = pcbnew.FOOTPRINT(board)
        footprint.SetReference(reference)
        footprint.SetPosition(pcbnew.VECTOR2I(mm(spec["pos"][0]), mm(spec["pos"][1])))
        field = footprint.Reference()
        field.SetLayer(spec["layer"])
        field.SetVisible(spec["visible"])
        field.SetTextSize(pcbnew.VECTOR2I(mm(1), mm(1)))
        field.SetTextThickness(mm(0.15))
        field.SetTextAngleDegrees(spec["angle"])
        field.SetKeepUpright(spec["upright"])
        field.SetMirrored(spec["mirrored"])
        field.SetPosition(pcbnew.VECTOR2I(mm(spec["pos"][0]), mm(spec["pos"][1] - 2)))
        footprint.Value().SetVisible(False)
        board.Add(footprint)
    board.Save(str(path))
    return specs


def _text_polygon(item):
    import pcbnew

    polygon = pcbnew.SHAPE_POLY_SET()
    item.TransformShapeToPolygon(polygon, item.GetLayer(), 0, pcbnew.FromMM(0.001), pcbnew.ERROR_INSIDE)
    polygon.Simplify()
    return polygon


def test_renamed_references_keep_original_text_on_silk(tmp_path: Path):
    pytest.importorskip("pcbnew")
    import pcbnew

    source = tmp_path / "refs.kicad_pcb"
    specs = _write_reference_fixture(source)
    (tmp_path / "merge.yaml").write_text(
        "boards:\n- board: refs.kicad_pcb\n  qty: 2\n  margin_mm: 2\n"
    )
    output = tmp_path / "panel.kicad_pcb"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "kikit.ui",
            "panelize",
            "--layout",
            "plugin; code: {}.Plugin; input: merge.yaml; renameref: B{{n}}-{{orig}}".format(ROOT / "kikit-packer.py"),
            "--tabs",
            "fixed; hwidth: 2mm; vwidth: 2mm",
            str(source),
            str(output),
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr

    board = pcbnew.LoadBoard(str(output))
    # Baked text belongs to its footprint; KiKit's bakeref adds free board text.
    assert not [d for d in board.GetDrawings() if isinstance(d, pcbnew.PCB_TEXT)]

    designators = sorted(footprint.GetReference() for footprint in board.GetFootprints())
    assert designators == sorted(
        f"B{instance}-{reference}" for instance in (0, 1) for reference in specs
    )

    for footprint in board.GetFootprints():
        instance, original = footprint.GetReference().split("-", 1)
        spec = specs[original]
        field = footprint.Reference()
        texts = [item for item in footprint.GraphicalItems() if isinstance(item, pcbnew.PCB_TEXT)]
        # The designator field never shows the renamed text.
        assert not field.IsVisible(), footprint.GetReference()
        if not spec["visible"]:
            # A hidden reference stays hidden; nothing new becomes visible.
            assert texts == [], footprint.GetReference()
            continue
        assert [text.GetText() for text in texts] == [original]
        text = texts[0]
        assert text.GetLayer() == spec["layer"]
        assert text.IsMirrored() == spec["mirrored"]

        # The baked text renders exactly where and how the original field did.
        field.SetText(original)
        field.SetVisible(True)
        expected = _text_polygon(field)
        actual = _text_polygon(text)
        assert actual.BBox().GetPosition() == expected.BBox().GetPosition(), footprint.GetReference()
        assert actual.BBox().GetSize() == expected.BBox().GetSize(), footprint.GetReference()
        difference = pcbnew.SHAPE_POLY_SET(expected)
        difference.BooleanXor(actual)
        assert difference.Area() == 0, footprint.GetReference()
