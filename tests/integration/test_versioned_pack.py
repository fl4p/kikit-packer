import copy
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
FIXTURES = ROOT / "tests/fixtures"


def add_refilled_internal_cutout(pcbnew, board_path: Path):
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


def add_refilled_rectangular_cutout(pcbnew, board_path: Path):
    board = pcbnew.LoadBoard(str(board_path))
    center = board.GetBoardEdgesBoundingBox().GetCenter()
    radius = pcbnew.FromMM(1)
    points = [
        pcbnew.VECTOR2I(center.x - radius, center.y - radius),
        pcbnew.VECTOR2I(center.x + radius, center.y - radius),
        pcbnew.VECTOR2I(center.x + radius, center.y + radius),
        pcbnew.VECTOR2I(center.x - radius, center.y + radius),
    ]
    for start, end in zip(points, points[1:] + points[:1]):
        edge = pcbnew.PCB_SHAPE(board)
        edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
        edge.SetLayer(pcbnew.Edge_Cuts)
        edge.SetStart(start)
        edge.SetEnd(end)
        edge.SetWidth(pcbnew.FromMM(0.05))
        board.Add(edge)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.Save(str(board_path))


def remove_refilled_internal_cutout(pcbnew, board_path: Path):
    add_refilled_internal_cutout(pcbnew, board_path)
    board = pcbnew.LoadBoard(str(board_path))
    cutout = next(
        item
        for item in board.GetDrawings()
        if item.GetLayer() == pcbnew.Edge_Cuts and item.GetShape() == pcbnew.SHAPE_T_CIRCLE
    )
    board.Remove(cutout)
    board.Save(str(board_path))


def project(output: Path, single=False):
    boards = [{"board": str(EXAMPLE / "safe/main.kicad_pcb"), "qty": 1, "margin_mm": 2}]
    if not single:
        boards.append({"board": str(EXAMPLE / "safe/long.kicad_pcb"), "qty": 2, "margin_mm": 1})
    return {
        "version": 1,
        "panel": {
            "authority": {"board": str(EXAMPLE / "safe/main.kicad_pcb"), "reference_only": False},
            "output": str(output),
            "max_width_mm": 100,
            "max_height_mm": 1000,
            "tabs": {"mode": "flat-edge" if single else "fixed", "width_mm": 2},
            "cuts": {"mode": "none"},
            "post": {
                "mill_radius_mm": 0,
                "verify_refill_areas": True,
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
    assert refill_check["enabled"] is True
    assert refill_check["status"] == "passed"
    assert refill_check["canonical_refill"]["status"] == "refilled"
    assert len(refill_check["source_checks"]) == (1 if single else 2)
    if single:
        board_artifact = next(
            item for item in manifest["plugin_result"]["artifacts"] if item["kind"] == "board"
        )
        assert refill_check["board_sha256"] == board_artifact["sha256"]
        assert set(refill_check["input_sha256"]) == {"board", "kicad_pro", "kicad_dru"}
    assert output.is_file()


def test_refill_verification_can_be_skipped(tmp_path: Path):
    output = tmp_path / "skipped.kicad_pcb"
    data = project(output, single=True)
    data["panel"]["post"]["verify_refill_areas"] = False
    project_path = tmp_path / "skipped.yaml"
    project_path.write_text(cast(str, yaml.safe_dump(data, sort_keys=False)))
    process = subprocess.run(
        [sys.executable, "-m", "kikit_packer", "pack", str(project_path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    manifest = json.loads(Path(str(output) + ".panel.json").read_text())
    assert manifest["plugin_result"]["refill_area_check"] == {
        "enabled": False,
        "source_checks": [],
        "status": "skipped",
    }


def test_example_mousebites_are_classified_from_saved_output(tmp_path: Path):
    output = tmp_path / "mousebites.kicad_pcb"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "kikit_packer",
            "pack",
            str(EXAMPLE / "project.yaml"),
            "--output",
            str(output),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    manifest = json.loads(Path(str(output) + ".panel.json").read_text())
    assert manifest["verification"]["cut_inventory"]["mode"] == "mousebites"
    assert manifest["verification"]["cut_inventory"]["npth_pads"] > 0
    assert len(manifest["verification"]["tab_connectivity"]["components"]) == 1


def test_mousebite_position_mutation_is_rejected(tmp_path: Path):
    pcbnew = pytest.importorskip("pcbnew")
    from kikit_packer.config import load_project
    from kikit_packer.plugin_child import run
    from kikit_packer.protocol import atomic_write_json, file_sha256, load_json
    from kikit_packer.runner import prepare_run
    from kikit_packer.verify import VerificationError, verify_result

    root = None
    try:
        root, plan, contract = prepare_run(load_project(EXAMPLE / "project.yaml"))
        run(root / "run-contract.json")
        staged = root / contract["staged_output"]
        board = pcbnew.LoadBoard(str(staged))
        footprint = next(fp for fp in board.GetFootprints() if fp.GetReference().startswith("KiKit_MB_"))
        pad = list(footprint.Pads())[0]
        pad.SetPosition(pad.GetPosition() + pcbnew.VECTOR2I(1, 0))
        board.Save(str(staged))
        result_path = root / contract["plugin_result_path"]
        result = load_json(result_path)
        for artifact in result["artifacts"]:
            artifact_path = root / artifact["path"]
            artifact["sha256"] = file_sha256(artifact_path)
            result["refill_area_check"]["input_sha256"][artifact["kind"]] = artifact["sha256"]
        result["refill_area_check"]["board_sha256"] = file_sha256(staged)
        atomic_write_json(result_path, result)
        with pytest.raises(VerificationError, match="mousebite"):
            verify_result(root, plan, contract)
    finally:
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)


def test_rotated_semantic_rich_board_is_verified_from_saved_output(tmp_path: Path):
    pytest.importorskip("pcbnew")
    output = tmp_path / "rich-panel.kicad_pcb"
    data = project(output, single=True)
    source = str(FIXTURES / "semantic-rich.kicad_pcb")
    data["panel"]["authority"]["board"] = source
    data["panel"]["max_width_mm"] = 25
    data["panel"]["max_height_mm"] = 50
    data["boards"][0]["board"] = source
    project_path = tmp_path / "rich.yaml"
    project_path.write_text(cast(str, yaml.safe_dump(data, sort_keys=False)))
    process = subprocess.run(
        [sys.executable, "-m", "kikit_packer", "pack", str(project_path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    manifest = json.loads(Path(str(output) + ".panel.json").read_text())
    assert manifest["instances"][0]["packing_rotation_deg"] == 90
    assert manifest["verification"]["source_profiles"]["expected_items"] == 10
    assert manifest["plugin_result"]["inventories"]["saved_output"] == {
        "drawings": 5,
        "footprints": 1,
        "npth_pads": 0,
        "pads": 1,
        "tracks_and_vias": 2,
        "vias": 1,
        "zones": 1,
    }


def _assert_saved_mutation_rejected(tmp_path: Path, mutate, source_path: Path = FIXTURES / "semantic-rich.kicad_pcb"):
    pcbnew = pytest.importorskip("pcbnew")
    from kikit_packer.config import load_project
    from kikit_packer.plugin_child import run
    from kikit_packer.protocol import atomic_write_json, file_sha256, load_json
    from kikit_packer.runner import prepare_run
    from kikit_packer.verify import VerificationError, verify_result

    output = tmp_path / "mutated.kicad_pcb"
    data = project(output, single=True)
    source = str(source_path)
    data["panel"]["authority"]["board"] = source
    data["boards"][0]["board"] = source
    project_path = tmp_path / "mutated.yaml"
    project_path.write_text(cast(str, yaml.safe_dump(data, sort_keys=False)))
    root = None
    try:
        root, plan, contract = prepare_run(load_project(project_path))
        run(root / "run-contract.json")
        staged = root / contract["staged_output"]
        board = pcbnew.LoadBoard(str(staged))
        mutate(pcbnew, board)
        board.Save(str(staged))
        result_path = root / contract["plugin_result_path"]
        result = load_json(result_path)
        board_hash = file_sha256(staged)
        for artifact in result["artifacts"]:
            artifact_path = root / artifact["path"]
            artifact["sha256"] = file_sha256(artifact_path)
            result["refill_area_check"]["input_sha256"][artifact["kind"]] = artifact["sha256"]
        result["refill_area_check"]["board_sha256"] = board_hash
        atomic_write_json(result_path, result)
        with pytest.raises(VerificationError, match="geometry or fabrication"):
            verify_result(root, plan, contract)
    finally:
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)


def test_saved_geometry_mutation_is_rejected(tmp_path: Path):
    def mutate(pcbnew, board):
        track = next(item for item in board.GetTracks() if item.GetClass() == "PCB_VIA")
        track.SetPosition(track.GetPosition() + pcbnew.VECTOR2I(pcbnew.FromMM(1), 0))

    _assert_saved_mutation_rejected(tmp_path, mutate)


def test_saved_fabrication_text_mutation_is_rejected(tmp_path: Path):
    def mutate(_pcbnew, board):
        text = next(item for item in board.GetDrawings() if item.GetClass() == "PCB_TEXT")
        text.SetText("FAR")

    _assert_saved_mutation_rejected(tmp_path, mutate)


def test_saved_footprint_field_angle_mutation_is_rejected(tmp_path: Path):
    def mutate(pcbnew, board):
        field = next(
            item
            for footprint in board.GetFootprints()
            for item in footprint.GetFields()
            if item.GetText() == "TEST"
        )
        field.SetTextAngle(pcbnew.EDA_ANGLE(180, pcbnew.DEGREES_T))

    _assert_saved_mutation_rejected(tmp_path, mutate)


def test_saved_bezier_control_mutation_is_rejected(tmp_path: Path):
    pcbnew = pytest.importorskip("pcbnew")
    source = tmp_path / "bezier-source.kicad_pcb"
    board = pcbnew.LoadBoard(str(FIXTURES / "semantic-rich.kicad_pcb"))
    shape = pcbnew.PCB_SHAPE(board)
    shape.SetShape(pcbnew.SHAPE_T_BEZIER)
    shape.SetLayer(pcbnew.F_Fab)
    shape.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(3), pcbnew.FromMM(3)))
    shape.SetBezierC1(pcbnew.VECTOR2I(pcbnew.FromMM(4), pcbnew.FromMM(2)))
    shape.SetBezierC2(pcbnew.VECTOR2I(pcbnew.FromMM(6), pcbnew.FromMM(4)))
    shape.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(7), pcbnew.FromMM(3)))
    shape.SetWidth(pcbnew.FromMM(0.2))
    board.Add(shape)
    board.Save(str(source))

    def mutate(_pcbnew, saved):
        bezier = next(
            item
            for item in saved.GetDrawings()
            if item.GetClass() == "PCB_SHAPE" and int(item.GetShape()) == 5
        )
        point = bezier.GetBezierC1()
        bezier.SetBezierC1(_pcbnew.VECTOR2I(point.x + 1, point.y))

    _assert_saved_mutation_rejected(tmp_path, mutate, source)


def test_project_digest_binds_quantities_padding_and_limits(tmp_path: Path):
    pytest.importorskip("pcbnew")
    from kikit_packer.config import load_project
    from kikit_packer.runner import prepare_run

    first_data = project(tmp_path / "first.kicad_pcb")
    output_only_data = project(tmp_path / "other-output.kicad_pcb")
    second_data = project(tmp_path / "second.kicad_pcb")
    second_data["boards"][0]["qty"] = 2
    second_data["boards"][0]["margin_mm"] = 3
    second_data["panel"]["max_width_mm"] = 99
    paths = []
    roots = []
    try:
        for index, data in enumerate((first_data, output_only_data, second_data)):
            path = tmp_path / f"project-{index}.yaml"
            path.write_text(cast(str, yaml.safe_dump(data, sort_keys=False)))
            paths.append(path)
            root, plan, _ = prepare_run(load_project(path))
            roots.append(root)
            data["digest"] = plan["project_digest"]
        assert first_data["digest"] == output_only_data["digest"]
        assert first_data["digest"] != second_data["digest"]
        root, limited_plan, _ = prepare_run(load_project(paths[0]), candidate_limit=1_048_577)
        roots.append(root)
        assert limited_plan["project_digest"] != first_data["digest"]
    finally:
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)


def test_protocol_rejects_nested_plan_and_result_mutations(tmp_path: Path):
    pytest.importorskip("pcbnew")
    from kikit_packer.config import load_project
    from kikit_packer.plugin_child import run
    from kikit_packer.protocol import ProtocolError, load_json, validate_envelope
    from kikit_packer.runner import prepare_run

    path = tmp_path / "protocol.yaml"
    path.write_text(cast(str, yaml.safe_dump(project(tmp_path / "protocol.kicad_pcb", single=True), sort_keys=False)))
    root = None
    try:
        root, plan, contract = prepare_run(load_project(path))
        mutations = []
        mutated = copy.deepcopy(plan)
        mutated["sources"][0] = True
        mutations.append(mutated)
        mutated = copy.deepcopy(plan)
        mutated["instances"][0]["append"]["destination_iu"] = [True, 0]
        mutations.append(mutated)
        mutated = copy.deepcopy(plan)
        mutated["packing"]["unexpected"] = 1
        mutations.append(mutated)
        mutated = copy.deepcopy(plan)
        mutated["authority"]["stackup"]["unexpected"] = 1
        mutations.append(mutated)
        mutated = copy.deepcopy(plan)
        mutated["authority"]["stackup"] = {
            "present": True,
            "verified": True,
            "descriptor": {
                "layers": [{"name": "F.Cu", "fields": {"type": True}}],
                "globals": {"copper_finish": "none"},
            },
            "unknown_keys": [],
            "problems": [],
        }
        mutations.append(mutated)
        mutated = copy.deepcopy(plan)
        mutated["resolved_settings"]["project"]["cuts"]["unexpected"] = 1
        mutations.append(mutated)
        mutated = copy.deepcopy(plan)
        mutated["resolved_settings"]["project"]["boards"][0]["legacy_rotate"] = True
        mutations.append(mutated)
        mutated = copy.deepcopy(plan)
        mutated["instances"][0]["expected_inventory"]["profiles"]["0"]["items"][0]["unexpected"] = 1
        mutations.append(mutated)
        mutated = copy.deepcopy(plan)
        text_item = next(
            item
            for item in mutated["instances"][0]["expected_inventory"]["profiles"]["0"]["items"]
            if item["class"] in {"PCB_TEXT", "PCB_FIELD", "FP_TEXT"}
        )
        text_item["text"] = True
        mutations.append(mutated)
        mutated = copy.deepcopy(plan)
        text_item = next(
            item
            for item in mutated["instances"][0]["expected_inventory"]["profiles"]["0"]["items"]
            if item["class"] in {"PCB_TEXT", "PCB_FIELD", "FP_TEXT"}
        )
        text_item.pop("bold")
        mutations.append(mutated)
        mutated = copy.deepcopy(plan)
        text_item = next(
            item
            for item in mutated["instances"][0]["expected_inventory"]["profiles"]["0"]["items"]
            if item["class"] in {"PCB_TEXT", "PCB_FIELD", "FP_TEXT"}
        )
        text_item["linespacing"] = True
        mutations.append(mutated)
        mutated = copy.deepcopy(plan)
        text_item = next(
            item
            for item in mutated["instances"][0]["expected_inventory"]["profiles"]["0"]["items"]
            if item["class"] in {"PCB_TEXT", "PCB_FIELD", "FP_TEXT"}
        )
        text_item.pop("linespacing")
        mutations.append(mutated)
        for value in mutations:
            with pytest.raises(ProtocolError):
                validate_envelope(value, "kikit-packer.run-plan")

        run(root / "run-contract.json")
        result = load_json(root / contract["plugin_result_path"])
        result_mutations = []
        mutated = copy.deepcopy(result)
        mutated["lifecycle"]["save_complete"] = "yes"
        result_mutations.append(mutated)
        mutated = copy.deepcopy(result)
        mutated["artifacts"][0]["size"] = True
        result_mutations.append(mutated)
        mutated = copy.deepcopy(result)
        mutated["final_substrate_bounds_iu"][0] = [False, 0, 1, 1]
        result_mutations.append(mutated)
        mutated = copy.deepcopy(result)
        mutated["tabs"]["unexpected"] = 1
        result_mutations.append(mutated)
        mutated = copy.deepcopy(result)
        mutated["refill_area_check"]["unexpected"] = 1
        result_mutations.append(mutated)
        mutated = copy.deepcopy(result)
        mutated["refill_area_check"]["canonical_refill"]["after"]["total_area_iu2_x2"] = True
        result_mutations.append(mutated)
        mutated = copy.deepcopy(result)
        first = next(iter(mutated["semantic_copy_proof"]["saved_output"].values()))
        first["unexpected"] = 1
        result_mutations.append(mutated)
        for value in result_mutations:
            with pytest.raises(ProtocolError):
                validate_envelope(value, "kikit-packer.plugin-result")
    finally:
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)


def test_parent_refill_rejects_child_area_mutation(tmp_path: Path):
    pytest.importorskip("pcbnew")
    from kikit_packer.config import load_project
    from kikit_packer.plugin_child import run
    from kikit_packer.protocol import atomic_write_json, load_json
    from kikit_packer.runner import prepare_run
    from kikit_packer.verify import VerificationError, verify_result

    path = tmp_path / "refill-proof.yaml"
    path.write_text(cast(str, yaml.safe_dump(project(tmp_path / "refill-proof.kicad_pcb", single=True), sort_keys=False)))
    root = None
    try:
        root, plan, contract = prepare_run(load_project(path))
        run(root / "run-contract.json")
        result_path = root / contract["plugin_result_path"]
        result = load_json(result_path)
        result["refill_area_check"]["total_area_iu2_x2"] += 1
        atomic_write_json(result_path, result)
        with pytest.raises(VerificationError, match="independent parent"):
            verify_result(root, plan, contract)
    finally:
        if root is not None:
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


def test_canonical_refill_absorbs_final_edge_change(tmp_path: Path):
    pcbnew = pytest.importorskip("pcbnew")
    from kikit.common import fakeKiCADGui

    from kikit_packer.refill import refill_and_save, verify_refill_areas

    _app = fakeKiCADGui()
    board_path = tmp_path / "canonical.kicad_pcb"
    shutil.copy2(EXAMPLE / "main.kicad_pcb", board_path)
    remove_refilled_internal_cutout(pcbnew, board_path)

    canonical = refill_and_save(board_path)
    assert canonical["changed_zone_layer_count"] > 0
    assert verify_refill_areas(board_path, tmp_path)["status"] == "passed"


def test_single_panel_cutout_geometry_is_enforced_after_refill(tmp_path: Path):
    pcbnew = pytest.importorskip("pcbnew")
    from kikit.common import fakeKiCADGui

    from kikit_packer.config import load_project
    from kikit_packer.fingerprint import inventory_board
    from kikit_packer.plugin_child import run
    from kikit_packer.protocol import atomic_write_json, file_sha256, load_json
    from kikit_packer.runner import prepare_run
    from kikit_packer.verify import VerificationError, verify_result

    _app = fakeKiCADGui()
    source = tmp_path / "source.kicad_pcb"
    shutil.copy2(EXAMPLE / "main.kicad_pcb", source)
    add_refilled_rectangular_cutout(pcbnew, source)
    output = tmp_path / "panel.kicad_pcb"
    data = project(output, single=True)
    data["panel"]["authority"]["board"] = str(source)
    data["boards"][0]["board"] = str(source)
    project_path = tmp_path / "project.yaml"
    project_path.write_text(cast(str, yaml.safe_dump(data, sort_keys=False)))
    root = None
    try:
        root, plan, contract = prepare_run(load_project(project_path))
        run(root / "run-contract.json")
        staged = root / contract["staged_output"]
        board = pcbnew.LoadBoard(str(staged))
        center = board.GetBoardEdgesBoundingBox().GetCenter()
        cutout_edges = [
            item
            for item in board.GetDrawings()
            if item.GetLayer() == pcbnew.Edge_Cuts
            and abs(item.GetBoundingBox().GetCenter().x - center.x) < pcbnew.FromMM(2)
            and abs(item.GetBoundingBox().GetCenter().y - center.y) < pcbnew.FromMM(2)
        ]
        assert len(cutout_edges) == 4
        for edge in cutout_edges:
            board.Remove(edge)
        board.Save(str(staged))
        result_path = root / contract["plugin_result_path"]
        result = load_json(result_path)
        result["inventories"]["saved_output"] = inventory_board(board)
        for artifact in result["artifacts"]:
            artifact_path = root / artifact["path"]
            artifact["sha256"] = file_sha256(artifact_path)
        atomic_write_json(result_path, result)
        with pytest.raises(VerificationError, match="missing or unclassified geometry"):
            verify_result(root, plan, contract)
    finally:
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)


def test_removed_internal_cutout_is_detected(tmp_path: Path):
    pcbnew = pytest.importorskip("pcbnew")
    from kikit.common import fakeKiCADGui

    from kikit_packer.refill import RefillAreaError, verify_refill_areas

    _app = fakeKiCADGui()
    board_path = tmp_path / "cutout.kicad_pcb"
    shutil.copy2(EXAMPLE / "main.kicad_pcb", board_path)
    remove_refilled_internal_cutout(pcbnew, board_path)

    with pytest.raises(
        RefillAreaError, match="zone fill area changed after refill"
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
    assert "source refill audit failed" in process.stderr
    assert "zone fill area changed after refill" in process.stderr
    for path, content in prior_artifacts.items():
        assert path.read_bytes() == content
