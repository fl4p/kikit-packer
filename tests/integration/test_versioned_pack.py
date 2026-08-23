import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "example"


def remove_refilled_internal_cutout(pcbnew, board_path: Path):
    board = pcbnew.LoadBoard(str(board_path))
    center = board.GetBoardEdgesBoundingBox().GetCenter()
    cutout = pcbnew.PCB_SHAPE(board)
    cutout.SetShape(pcbnew.SHAPE_T_CIRCLE)
    cutout.SetLayer(pcbnew.Edge_Cuts)
    cutout.SetCenter(center)
    cutout.SetEnd(pcbnew.VECTOR2I(center.x + pcbnew.FromMM(1), center.y))
    cutout.SetWidth(pcbnew.FromMM(0.05))
    board.Add(cutout)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.Save(str(board_path))
    board.Remove(cutout)
    board.Save(str(board_path))


def project(output: Path, single=False):
    boards = [{"board": str(EXAMPLE / "main.kicad_pcb"), "qty": 1, "margin_mm": 2}]
    if not single:
        boards.append({"board": str(EXAMPLE / "long.kicad_pcb"), "qty": 2, "margin_mm": 1})
    return {
        "version": 1,
        "panel": {
            "authority": {"board": str(EXAMPLE / "main.kicad_pcb"), "reference_only": False},
            "output": str(output),
            "max_width_mm": 100,
            "max_height_mm": 1000,
            "tabs": {"mode": "flat-edge" if single else "fixed", "width_mm": 2},
            "cuts": {"mode": "none"},
            "post": {
                "mill_radius_mm": 0,
                "verify_refill_areas": single,
            },
        },
        "boards": boards,
    }


@pytest.mark.parametrize("single", [False, True])
def test_versioned_pack_and_manifest(tmp_path: Path, single: bool):
    pytest.importorskip("pcbnew")
    output = tmp_path / "panel.kicad_pcb"
    project_path = tmp_path / "project.yaml"
    project_path.write_text(cast(str, yaml.safe_dump(project(output, single), sort_keys=False)))
    process = subprocess.run(
        [sys.executable, "-m", "kikit_packer", "pack", str(project_path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    manifest_path = Path(str(output) + ".panel.json")
    manifest = json.loads(manifest_path.read_text())
    assert manifest["kind"] == "kikit-packer.manifest"
    assert len(manifest["instances"]) == (1 if single else 3)
    refill_check = manifest["plugin_result"]["refill_area_check"]
    assert refill_check["enabled"] is single
    assert refill_check["status"] == ("passed" if single else "skipped")
    if single:
        board_artifact = next(
            item for item in manifest["plugin_result"]["artifacts"] if item["kind"] == "board"
        )
        assert refill_check["board_sha256"] == board_artifact["sha256"]
        assert set(refill_check["input_sha256"]) == {"board", "kicad_pro", "kicad_dru"}
    assert output.is_file()


def test_project_digest_binds_quantities_padding_and_limits(tmp_path: Path):
    pytest.importorskip("pcbnew")
    from kikit_packer.config import load_project
    from kikit_packer.runner import prepare_run

    first_data = project(tmp_path / "first.kicad_pcb")
    second_data = project(tmp_path / "second.kicad_pcb")
    second_data["boards"][0]["qty"] = 2
    second_data["boards"][0]["margin_mm"] = 3
    second_data["panel"]["max_width_mm"] = 99
    paths = []
    roots = []
    try:
        for index, data in enumerate((first_data, second_data)):
            path = tmp_path / f"project-{index}.yaml"
            path.write_text(cast(str, yaml.safe_dump(data, sort_keys=False)))
            paths.append(path)
            root, plan, _ = prepare_run(load_project(path))
            roots.append(root)
            data["digest"] = plan["project_digest"]
        assert first_data["digest"] != second_data["digest"]
    finally:
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)


def test_cli_exit_codes_for_planning_and_preflight(tmp_path: Path):
    pytest.importorskip("pcbnew")
    impossible = project(tmp_path / "impossible.kicad_pcb", single=True)
    impossible["panel"]["max_width_mm"] = 1
    impossible_path = tmp_path / "impossible.yaml"
    impossible_path.write_text(cast(str, yaml.safe_dump(impossible, sort_keys=False)))
    planning = subprocess.run(
        [sys.executable, "-m", "kikit_packer", "pack", str(impossible_path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert planning.returncode == 4, planning.stderr

    missing = project(tmp_path / "missing-output.kicad_pcb", single=True)
    missing["boards"][0]["board"] = str(tmp_path / "missing.kicad_pcb")
    missing["panel"]["authority"]["reference_only"] = True
    missing_path = tmp_path / "missing.yaml"
    missing_path.write_text(cast(str, yaml.safe_dump(missing, sort_keys=False)))
    preflight = subprocess.run(
        [sys.executable, "-m", "kikit_packer", "pack", str(missing_path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert preflight.returncode == 3, preflight.stderr


def test_pre_cancelled_run_returns_130(tmp_path: Path):
    pytest.importorskip("pcbnew")
    from kikit_packer.config import load_project
    from kikit_packer.runner import RunError, execute_run

    path = tmp_path / "cancel.yaml"
    path.write_text(cast(str, yaml.safe_dump(project(tmp_path / "cancel.kicad_pcb"), sort_keys=False)))
    event = threading.Event()
    event.set()
    with pytest.raises(RunError) as caught:
        execute_run(load_project(path), Path(sys.executable), cancel_event=event)
    assert caught.value.exit_code == 130


def test_removed_internal_cutout_is_detected(tmp_path: Path):
    pcbnew = pytest.importorskip("pcbnew")
    from kikit.common import fakeKiCADGui

    from kikit_packer.refill import RefillAreaError, verify_refill_areas

    _app = fakeKiCADGui()
    board_path = tmp_path / "cutout.kicad_pcb"
    shutil.copy2(EXAMPLE / "main.kicad_pcb", board_path)
    remove_refilled_internal_cutout(pcbnew, board_path)

    with pytest.raises(
        RefillAreaError, match="zone fill area changed after temporary refill"
    ):
        verify_refill_areas(board_path, tmp_path)


def test_refill_area_change_blocks_promotion(tmp_path: Path):
    pcbnew = pytest.importorskip("pcbnew")
    from kikit.common import fakeKiCADGui

    _app = fakeKiCADGui()
    source = tmp_path / "source.kicad_pcb"
    shutil.copy2(EXAMPLE / "main.kicad_pcb", source)
    remove_refilled_internal_cutout(pcbnew, source)

    output = tmp_path / "panel.kicad_pcb"
    manifest_path = Path(str(output) + ".panel.json")
    prior_artifacts = {
        output: b"old-board",
        output.with_suffix(".kicad_pro"): b"old-project",
        output.with_suffix(".kicad_dru"): b"old-rules",
        manifest_path: b"old-manifest",
    }
    for path, content in prior_artifacts.items():
        path.write_bytes(content)
    config = project(output, single=True)
    config["panel"]["authority"]["board"] = str(source)
    config["boards"][0]["board"] = str(source)
    project_path = tmp_path / "project.yaml"
    project_path.write_text(cast(str, yaml.safe_dump(config, sort_keys=False)))

    process = subprocess.run(
        [sys.executable, "-m", "kikit_packer", "pack", str(project_path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode != 0
    assert "zone fill area changed after temporary refill" in process.stderr
    for path, content in prior_artifacts.items():
        assert path.read_bytes() == content
