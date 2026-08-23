import hashlib
import sys
from pathlib import Path
from typing import Any

from .connectivity import connected_components
from .fingerprint import fingerprints_by_uuid, inventory_board
from .protocol import (
    atomic_write_json,
    file_sha256,
    load_json,
    resolve_staging_path,
    validate_envelope,
)
from .refill import verify_refill_areas
from .snapshot import verify_snapshots_from_plan


def _mm(value: Any) -> str:
    return f"{value}mm"


def _raw_overrides(plan: dict[str, Any], contract_path: Path) -> dict[str, dict[str, Any]]:
    settings = plan["resolved_settings"]["project"]
    layout = settings["layout"]
    tabs = settings["tabs"]
    cuts = settings["cuts"]
    post = settings["post"]
    layout_section = {
        "type": "plugin",
        "code": "kikit_packer.plugin.SuppliedPlanPlugin",
        "arg": str(contract_path),
        "hspace": _mm(layout["horizontal_spacing_mm"]),
        "vspace": _mm(layout["vertical_spacing_mm"]),
        "rotation": "0deg",
        "renamenet": layout["rename_net"],
        "renameref": layout["rename_ref"],
        "baketext": layout["bake_text"],
        "bakeref": layout["bake_ref"],
    }
    if tabs["mode"] == "flat-edge":
        tabs_section = {
            "type": "plugin",
            "code": "kikit_packer.plugin.FlatEdgeTabs",
            "arg": str(tabs["width_mm"]),
            "hwidth": _mm(tabs["width_mm"]),
            "vwidth": _mm(tabs["width_mm"]),
        }
    else:
        tabs_section = {
            "type": "fixed",
            "hwidth": _mm(tabs["width_mm"]),
            "vwidth": _mm(tabs["width_mm"]),
            "hcount": tabs["horizontal_count"],
            "vcount": tabs["vertical_count"],
            "mindistance": _mm(tabs["min_distance_mm"]),
        }
    cuts_section = {
        "type": cuts["mode"],
        "drill": _mm(cuts["drill_mm"]),
        "spacing": _mm(cuts["spacing_mm"]),
        "offset": _mm(cuts["offset_mm"]),
        "prolong": _mm(cuts["prolong_mm"]),
    }
    return {
        "layout": layout_section,
        "tabs": tabs_section,
        "cuts": cuts_section,
        "framing": {"type": "none"},
        "tooling": {"type": "none"},
        "fiducials": {"type": "none"},
        "text": {"type": "none"},
        "text2": {"type": "none"},
        "text3": {"type": "none"},
        "text4": {"type": "none"},
        "copperfill": {"type": "none"},
        "page": {"type": "inherit"},
        "post": {
            "type": "auto",
            "millradius": "0mm",
            "millradiusouter": _mm(post["mill_radius_mm"]),
            "refillzones": False,
            "reconstructarcs": False,
            "dimensions": False,
            "origin": "tl",
        },
        "debug": {"drawtabfail": False, "trace": False, "deterministic": True},
    }


def run(contract_path: Path) -> dict[str, Any]:
    import pcbnew
    from kikit import panelize_ui_impl as ki
    from kikit.common import fakeKiCADGui
    from kikit.panelize_ui import doPanelization

    contract_path = contract_path.resolve()
    contract = load_json(contract_path)
    validate_envelope(contract, "kikit-packer.run-contract")
    root = Path(contract["staging_root"]).resolve()
    plan_path = resolve_staging_path(root, contract["run_plan_path"])
    plan = load_json(plan_path)
    validate_envelope(plan, "kikit-packer.run-plan")
    if file_sha256(plan_path) != contract["run_plan_sha256"]:
        raise RuntimeError("run plan hash mismatch")
    if plan["run_id"] != contract["run_id"] or plan["nonce"] != contract["nonce"]:
        raise RuntimeError("run identity mismatch")
    verify_snapshots_from_plan(root, plan)
    authority_source = next(
        source for source in plan["sources"] if source["source_id"] == plan["authority"]["source_id"]
    )
    authority = resolve_staging_path(root, authority_source["snapshot_path"])
    output = resolve_staging_path(root, contract["staged_output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    _app = fakeKiCADGui()
    overrides = _raw_overrides(plan, contract_path)
    resolved = plan["resolved_settings"]
    from .protocol import digest

    if overrides != resolved["kikit_raw_preset"] or digest(overrides) != resolved["kikit_raw_preset_digest"]:
        raise RuntimeError("resolved KiKit preset differs from run plan")
    preset = ki.obtainPreset(
        [],
        layout=overrides["layout"],
        tabs=overrides["tabs"],
        cuts=overrides["cuts"],
        framing=overrides["framing"],
        tooling=overrides["tooling"],
        fiducials=overrides["fiducials"],
        text=overrides["text"],
        text2=overrides["text2"],
        text3=overrides["text3"],
        text4=overrides["text4"],
        copperfill=overrides["copperfill"],
        page=overrides["page"],
        post=overrides["post"],
        debug=overrides["debug"],
    )
    processed_preset_sha256 = hashlib.sha256(ki.dumpPreset(preset).encode("utf-8")).hexdigest()
    doPanelization(
        str(authority),
        str(output),
        preset,
        [("kikit_packer.plugin", "RecorderHook", str(contract_path))],
    )
    verify_snapshots_from_plan(root, plan)
    state = load_json(root / "plugin-state.json")
    board = pcbnew.LoadBoard(str(output))
    if board is None:
        raise RuntimeError("saved output cannot be reloaded")
    if plan["resolved_settings"]["project"]["post"]["verify_refill_areas"]:
        refill_area_check = verify_refill_areas(output, root)
    else:
        refill_area_check = {"enabled": False, "status": "skipped"}
    artifacts = []
    for candidate in (output, output.with_suffix(".kicad_pro"), output.with_suffix(".kicad_dru")):
        if candidate.exists():
            artifacts.append({
                "kind": "board" if candidate.suffix == ".kicad_pcb" else candidate.suffix.lstrip("."),
                "path": str(candidate.relative_to(root)),
                "size": candidate.stat().st_size,
                "sha256": file_sha256(candidate),
            })
    instance_ids = [item["instance_id"] for item in plan["instances"]]
    copied_uuids = {
        item_uuid
        for instance in state.get("instances", [])
        for item_uuid in instance.get("output_item_uuids", [])
    }
    saved_fingerprints = {
        item_uuid: fingerprint
        for item_uuid, fingerprint in fingerprints_by_uuid(board).items()
        if item_uuid in copied_uuids
    }
    edges = state.get("tabs", {}).get("graph_edges", [])
    state.setdefault("tabs", {})["connected_components"] = connected_components(instance_ids, edges)
    result = {
        "kind": "kikit-packer.plugin-result",
        "schema_version": 1,
        "run_id": plan["run_id"],
        "nonce": plan["nonce"],
        "run_plan_sha256": contract["run_plan_sha256"],
        "raw_preset_sha256": resolved["kikit_raw_preset_digest"],
        "processed_preset_sha256": processed_preset_sha256,
        "lifecycle": {
            "layout_complete": bool(state.get("layout_complete")),
            "tabs_complete": bool(state.get("tabs_complete")),
            "cuts_complete": bool(state.get("cuts_complete")),
            "save_complete": True,
        },
        "instances": state.get("instances", []),
        "final_substrate_bounds_iu": state.get("final_substrate_bounds_iu", []),
        "tabs": state.get("tabs", {}),
        "cuts": {"mode": plan["resolved_settings"]["project"]["cuts"]["mode"]},
        "refill_area_check": refill_area_check,
        "semantic_copy_proof": {
            "before_save": state.get("uuid_fingerprints_before_save", {}),
            "saved_output": saved_fingerprints,
        },
        "inventories": {
            "after_layout": state.get("after_layout_inventory", {}),
            "after_cuts": state.get("after_cuts_inventory", {}),
            "before_save": state.get("before_save_inventory", {}),
            "saved_output": inventory_board(board),
        },
        "artifacts": artifacts,
        "diagnostics": [],
    }
    result_path = resolve_staging_path(root, contract["plugin_result_path"])
    atomic_write_json(result_path, result)
    return result


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m kikit_packer.plugin_child RUN_CONTRACT", file=sys.stderr)
        return 2
    try:
        run(Path(argv[0]))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"KiKit Packer child failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
