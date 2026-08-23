import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .artifacts import ArtifactError, manifest_path, promote
from .command import child_argv
from .fingerprint import source_copy_profile
from .inspect import inspect_board, validate_authority
from .manifest import build_manifest
from .packing import PackingResult, PlanningError, legacy_optimal_pack, plan_v1
from .protocol import append_event, atomic_write_json, digest, file_sha256
from .snapshot import (
    SnapshotSource,
    snapshot_sources,
    verify_snapshots,
    verify_snapshots_from_plan,
)
from .verify import VerificationError, verify_result

IU_PER_MM = 1_000_000


def _emit(root: Path, contract: dict[str, Any], sequence: int, stage: str, event: str, payload=None) -> None:
    append_event(
        root / contract["events_path"],
        {
            "kind": "kikit-packer.event",
            "schema_version": 1,
            "run_id": contract["run_id"],
            "nonce": contract["nonce"],
            "sequence": sequence,
            "stage": stage,
            "event": event,
            "payload": payload or {},
        },
    )


class RunError(RuntimeError):
    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


def _jsonable(value: Any) -> Any:
    if isinstance(value, float):
        return format(value, ".15g")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "value"):
        return value.value
    return value


def _project_settings(project) -> dict[str, Any]:
    authority = project.panel.authority
    return _jsonable({
        "version": project.version,
        "authority": None if authority is None else {
            "board": authority.board,
            "reference_only": authority.reference_only,
        },
        "output": project.panel.output,
        "max_width_mm": project.panel.max_width_mm,
        "max_height_mm": project.panel.max_height_mm,
        "layout": asdict(project.panel.layout),
        "tabs": asdict(project.panel.tabs),
        "cuts": asdict(project.panel.cuts),
        "post": asdict(project.panel.post),
        "page": asdict(project.panel.page),
        "allow_mixed_layers": project.panel.allow_mixed_layers,
        "allow_mixed_thickness": project.panel.allow_mixed_thickness,
        "boards": [asdict(board) for board in project.boards],
    })


def _snapshot_record(root: Path, source: SnapshotSource, inspection) -> dict[str, Any]:
    return {
        "source_id": source.source_id,
        "original_path": str(source.board.original),
        "snapshot_path": str(source.board.relative),
        "sha256": source.board.sha256,
        "ignored_companions": [str(path) for path in source.ignored_companions],
        "companions": {
            "kicad_pro": {
                "present": source.kicad_pro is not None,
                "snapshot_path": None if source.kicad_pro is None else str(source.kicad_pro.relative),
                "sha256": None if source.kicad_pro is None else source.kicad_pro.sha256,
            },
            "kicad_dru": {
                "present": source.kicad_dru is not None,
                "snapshot_path": None if source.kicad_dru is None else str(source.kicad_dru.relative),
                "sha256": None if source.kicad_dru is None else source.kicad_dru.sha256,
            },
        },
        "inspection": _jsonable(asdict(inspection)),
    }


def _runtime_versions() -> dict[str, str]:
    import kikit
    import pcbnew
    import rpack
    import shapely

    return {
        "python": sys.version.split()[0],
        "kicad": str(pcbnew.GetBuildVersion()),
        "pcbnew": str(pcbnew.GetBuildVersion()),
        "kikit": str(getattr(kikit, "__version__", "unknown")),
        "rectangle_packer": str(getattr(rpack, "__version__", "unknown")),
        "shapely": str(getattr(shapely, "__version__", "unknown")),
    }


def prepare_run(project, candidate_limit: int = 1_048_576, cancel_event=None) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    from kikit.common import fakeKiCADGui

    _app = fakeKiCADGui()
    if project.panel.authority is None:
        raise RunError("legacy projects require --main", 2)
    if project.panel.output is None:
        raise RunError("an output path is required", 2)
    output = project.panel.output
    output.parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="." + output.stem + ".kikit-packer-", dir=str(output.parent))).resolve()
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise RunError("planning cancelled", 130)
        snapshots = snapshot_sources(
            [board.board for board in project.boards],
            project.panel.authority.board,
            root,
        )
        by_original = {source.board.original.resolve(): source for source in snapshots}
        inspections = {}
        for source in snapshots:
            if cancel_event is not None and cancel_event.is_set():
                raise RunError("planning cancelled", 130)
            inspections[source.source_id] = inspect_board(root / source.board.relative, source.source_id)
        import pcbnew

        copy_profiles = {}
        for source in snapshots:
            inspection = inspections[source.source_id]
            left, top, right, bottom = inspection.copper_bounds_iu
            source_board = pcbnew.LoadBoard(str(root / source.board.relative))
            copy_profiles[source.source_id] = source_copy_profile(
                source_board,
                [left - IU_PER_MM, top - IU_PER_MM, right + IU_PER_MM, bottom + IU_PER_MM],
            )
        authority_source = by_original[project.panel.authority.board.resolve()]
        authority_inspection = inspections[authority_source.source_id]
        source_inspections = [inspections[by_original[board.board.resolve()].source_id] for board in project.boards]
        compatibility_diagnostics = validate_authority(
            authority_inspection,
            source_inspections,
            project.panel.allow_mixed_layers,
            project.panel.allow_mixed_thickness,
        )
        instance_ids: list[str] = []
        source_ids: list[str] = []
        sizes: list[tuple[int, int]] = []
        instance_inputs = []
        horizontal = round(project.panel.layout.horizontal_spacing_mm * IU_PER_MM)
        vertical = round(project.panel.layout.vertical_spacing_mm * IU_PER_MM)
        for row_index, board in enumerate(project.boards, 1):
            source = by_original[board.board.resolve()]
            inspection = inspections[source.source_id]
            left, top, right, bottom = inspection.copper_bounds_iu
            margin = round(board.margin_mm * IU_PER_MM)
            for ordinal in range(1, board.qty + 1):
                instance_id = f"row-{row_index:04d}/instance-{ordinal:04d}"
                instance_ids.append(instance_id)
                source_ids.append(source.source_id)
                sizes.append((right - left + 2 * margin + horizontal, bottom - top + 2 * margin + vertical))
                instance_inputs.append((instance_id, source.source_id, inspection, margin, ordinal, row_index))
        max_width = None if project.panel.max_width_mm is None else round(project.panel.max_width_mm * IU_PER_MM)
        max_height = None if project.panel.max_height_mm is None else round(project.panel.max_height_mm * IU_PER_MM)
        if project.legacy:
            rotations, positions = legacy_optimal_pack(sizes, max_width, max_height)
            width = max(x + (sizes[i][1] if rotations[i] else sizes[i][0]) for i, (x, _) in enumerate(positions))
            height = max(y + (sizes[i][0] if rotations[i] else sizes[i][1]) for i, (_, y) in enumerate(positions))
            packing = PackingResult((), (0, 0, width, height), 1 << len(sizes), 1 << len(sizes))
        else:
            packing = plan_v1(
                instance_ids,
                source_ids,
                sizes,
                max_width,
                max_height,
                candidate_limit,
                cancelled=None if cancel_event is None else cancel_event.is_set,
            )
            rotations = [item.rotated for item in packing.placements]
            positions = [(item.x_iu, item.y_iu) for item in packing.placements]
        instances = []
        for index, (instance_id, source_id, inspection, margin, ordinal, row_index) in enumerate(instance_inputs):
            left, top, right, bottom = inspection.copper_bounds_iu
            rotated = bool(rotations[index])
            coercions = []
            if inspection.copper_layers != authority_inspection.copper_layers:
                coercions.append({"code": "MIXED_LAYER_COUNT", "authority_layers": list(authority_inspection.copper_layers)})
            if inspection.thickness_iu != authority_inspection.thickness_iu:
                coercions.append({"code": "MIXED_THICKNESS", "authority_thickness_iu": authority_inspection.thickness_iu})
            instances.append({
                "instance_id": instance_id,
                "row_id": f"row-{row_index:04d}",
                "source_id": source_id,
                "ordinal": ordinal,
                "outline_bounds_iu": list(inspection.outline_bounds_iu),
                "copper_bounds_iu": list(inspection.copper_bounds_iu),
                "source_area_iu": [left - IU_PER_MM, top - IU_PER_MM, right + IU_PER_MM, bottom + IU_PER_MM],
                "packing_size_iu": list(sizes[index]),
                "margin_iu": margin,
                "packing_rotation_deg": 90 if rotated else 0,
                "append": {
                    "destination_iu": list(positions[index]),
                    "origin": "top-right" if rotated else "top-left",
                    "rotation_deg": 90 if rotated else 0,
                },
                "expected_inventory": copy_profiles[source_id],
                "coercions": coercions,
            })
        settings = _project_settings(project)
        sources = [_snapshot_record(root, source, inspections[source.source_id]) for source in snapshots]
        authority = {
            "source_id": authority_source.source_id,
            "reference_only": project.panel.authority.reference_only,
            "board_sha256": authority_source.board.sha256,
            "copper_layer_count": authority_inspection.copper_layer_count,
            "copper_layers": list(authority_inspection.copper_layers),
            "thickness_iu": authority_inspection.thickness_iu,
            "stackup": authority_inspection.stackup,
            "companions": {
                "kicad_pro": {"present": authority_source.kicad_pro is not None, "sha256": None if authority_source.kicad_pro is None else authority_source.kicad_pro.sha256},
                "kicad_dru": {"present": authority_source.kicad_dru is not None, "sha256": None if authority_source.kicad_dru is None else authority_source.kicad_dru.sha256},
            },
        }
        row_identities = [
            {
                "source_id": by_original[board.board.resolve()].source_id,
                "qty": board.qty,
                "margin_mm": format(board.margin_mm, ".15g"),
            }
            for board in project.boards
        ]
        digest_input = {
            "version": project.version,
            "settings": settings,
            "rows": row_identities,
            "sources": [(item["source_id"], item["sha256"]) for item in sources],
            "authority": authority,
            "output": str(output),
            "max_width_iu": max_width,
            "max_height_iu": max_height,
        }
        plan = {
            "kind": "kikit-packer.run-plan",
            "schema_version": 1,
            "run_id": str(uuid.uuid4()),
            "nonce": secrets.token_hex(32),
            "project_digest": digest(digest_input),
            "runtime": _runtime_versions(),
            "authority": authority,
            "sources": sources,
            "instances": instances,
            "packing": {
                "max_width_iu": max_width,
                "max_height_iu": max_height,
                "candidate_limit": candidate_limit,
                "candidate_count": packing.candidate_count,
                "evaluated_count": packing.evaluated_count,
                "bounds_iu": list(packing.bounds_iu),
            },
            "resolved_settings": {
                "project": settings,
                "kikit_raw_preset": {},
                "kikit_raw_preset_digest": "0" * 64,
            },
            "diagnostics": (
                [diagnostic.to_dict() for inspection in inspections.values() for diagnostic in inspection.diagnostics]
                + [diagnostic.to_dict() for diagnostic in compatibility_diagnostics]
                + [diagnostic.to_dict() for diagnostic in project.diagnostics]
            ),
        }
        from .plugin_child import _raw_overrides

        raw_preset = _raw_overrides(plan, root / "run-contract.json")
        plan["resolved_settings"]["kikit_raw_preset"] = raw_preset
        plan["resolved_settings"]["kikit_raw_preset_digest"] = digest(raw_preset)
        plan_path = root / "run-plan.json"
        atomic_write_json(plan_path, plan)
        staged_output = Path("artifacts") / output.name
        contract = {
            "kind": "kikit-packer.run-contract",
            "schema_version": 1,
            "run_id": plan["run_id"],
            "nonce": plan["nonce"],
            "staging_root": str(root),
            "run_plan_path": "run-plan.json",
            "run_plan_sha256": file_sha256(plan_path),
            "staged_output": str(staged_output),
            "plugin_result_path": "plugin-result.json",
            "events_path": "events.jsonl",
            "log_limits": {
                "stdout_bytes": 1_048_576,
                "stderr_bytes": 1_048_576,
                "events_bytes": 1_048_576,
            },
        }
        atomic_write_json(root / "run-contract.json", contract)
        verify_snapshots(root, snapshots)
        _emit(root, contract, 1, "plan", "completed", {"instances": len(instances)})
        return root, plan, contract
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _run_child(argv, cwd: Path, environment: dict[str, str], cancel_event=None):
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name != "nt",
        creationflags=creationflags,
    )
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _terminate_process_tree(process)
                raise RunError("generation cancelled", 130)
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                return process.returncode, stdout[-1_048_576:], stderr[-1_048_576:]
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        _terminate_process_tree(process)
        raise


def execute_prepared(
    project,
    interpreter: Path,
    root: Path,
    plan: dict[str, Any],
    contract: dict[str, Any],
    cancel_event=None,
) -> dict[str, Any]:
    output = project.panel.output
    assert output is not None
    try:
        environment = dict(os.environ)
        package_root = str(Path(__file__).resolve().parent.parent)
        environment["PYTHONPATH"] = package_root + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
        _emit(root, contract, 2, "generate", "started")
        returncode, stdout, stderr = _run_child(
            child_argv(interpreter, root / "run-contract.json"), root, environment, cancel_event
        )
        if returncode != 0:
            message = stderr[-4000:] or stdout[-4000:] or "KiKit child failed"
            raise RunError(message.strip(), 5)
        _emit(root, contract, 3, "generate", "completed")
        if cancel_event is not None and cancel_event.is_set():
            raise RunError("generation cancelled", 130)
        _emit(root, contract, 4, "verify", "started")
        try:
            result = verify_result(root, plan, contract)
            verify_snapshots_from_plan(root, plan)
        except (VerificationError, OSError, RuntimeError) as exc:
            raise RunError(str(exc), 6) from exc
        _emit(root, contract, 5, "verify", "completed")
        staged_output = root / contract["staged_output"]
        staged_artifacts = [staged_output]
        for suffix in (".kicad_pro", ".kicad_dru"):
            candidate = staged_output.with_suffix(suffix)
            if candidate.exists():
                staged_artifacts.append(candidate)
        manifest = build_manifest(plan["project_digest"], plan, result, staged_artifacts)
        atomic_write_json(manifest_path(staged_output), manifest)
        if cancel_event is not None and cancel_event.is_set():
            raise RunError("generation cancelled", 130)
        _emit(root, contract, 6, "promote", "started")
        try:
            promoted = promote(staged_output, output)
        except (ArtifactError, OSError) as exc:
            raise RunError(str(exc), 6) from exc
        _emit(root, contract, 7, "promote", "completed")
        return {"plan": plan, "result": result, "manifest": manifest, "artifacts": [str(path) for path in promoted], "stdout": stdout, "stderr": stderr}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def execute_run(project, interpreter: Path, candidate_limit: int = 1_048_576, cancel_event=None) -> dict[str, Any]:
    try:
        root, plan, contract = prepare_run(project, candidate_limit, cancel_event)
    except RunError:
        raise
    except PlanningError as exc:
        if cancel_event is not None and cancel_event.is_set():
            raise RunError("planning cancelled", 130) from exc
        raise RunError(str(exc), 4) from exc
    except (OSError, RuntimeError) as exc:
        raise RunError(str(exc), 3) from exc
    return execute_prepared(project, interpreter, root, plan, contract, cancel_event)
