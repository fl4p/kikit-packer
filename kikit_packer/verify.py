import re
from pathlib import Path
from typing import Any

from .companions import CompanionError, verify_project_authority
from .connectivity import connected_components
from .fingerprint import (
    fingerprints_by_uuid,
    inventory_board,
    item_fingerprint,
    multiset,
    semantic_item,
    translated_profile_items,
)
from .geometry import planned_substrate_bounds
from .protocol import file_sha256, load_json, resolve_staging_path, validate_envelope
from .refill import RefillAreaError, verify_refill_areas
from .stackup import parse_setup_digest, parse_stackup


class VerificationError(RuntimeError):
    pass


def _polygon(value, x: int, y: int):
    from shapely.geometry import Polygon

    outline = [(point[0] + x, point[1] + y) for point in value["outline"]]
    holes = [
        [(point[0] + x, point[1] + y) for point in ring]
        for ring in value.get("holes", [])
    ]
    return Polygon(outline, holes)


def _verify_saved_material(board, plan, page_delta) -> tuple[list[list[str]], list[Any]]:
    import pcbnew
    from kikit.substrate import Substrate
    from shapely.ops import unary_union

    instance_material = []
    for planned in plan["instances"]:
        bounds = planned_substrate_bounds(planned)
        polygons = planned["expected_inventory"]["substrates"][str(planned["packing_rotation_deg"])]
        material = unary_union([
            _polygon(value, bounds[0] + page_delta[0], bounds[1] + page_delta[1])
            for value in polygons
        ])
        instance_material.append((planned["instance_id"], material))
    expected = unary_union([material for _, material in instance_material])
    edges = [item for item in board.GetDrawings() if item.GetLayer() == pcbnew.Edge_Cuts]
    saved = Substrate(edges).substrates
    if expected.difference(saved).area > 1:
        raise VerificationError("saved panel material removes planned source substrate")
    added = saved.difference(expected)
    components = [
        geometry
        for geometry in getattr(added, "geoms", [added])
        if not geometry.is_empty and geometry.area > 1
    ]
    if len(instance_material) == 1 and components:
        raise VerificationError("single-board panel contains unmodeled added substrate material")
    graph_edges = []
    tab_cuts = []
    for component in components:
        touched = [
            instance_id
            for instance_id, material in instance_material
            if component.buffer(1).intersects(material)
        ]
        if len(instance_material) > 1 and len(touched) != 2:
            raise VerificationError("saved tab material has invalid substrate incidence")
        if len(touched) == 2:
            graph_edges.append(sorted(touched))
            for _, material in instance_material:
                seam = component.boundary.intersection(material.boundary)
                for geometry in getattr(seam, "geoms", [seam]):
                    if geometry.geom_type == "LineString" and geometry.length > 1:
                        tab_cuts.append(geometry)
    connected = connected_components(
        [instance_id for instance_id, _ in instance_material],
        graph_edges,
    )
    if len(instance_material) > 1 and len(connected) != 1:
        raise VerificationError("saved tab material does not connect every board instance")
    return connected, tab_cuts


def _mousebite_candidates(cut, settings, saved_material) -> set[tuple[tuple[int, int], ...]]:
    from kikit.panelize import SHP_EPSILON, listGeometries, prolongCut, toKiCADPoint
    from shapely.geometry import LineString

    candidates = set()
    for coordinates in (list(cut.coords), list(reversed(cut.coords))):
        line = LineString(coordinates).simplify(SHP_EPSILON)
        line = prolongCut(line, float(settings["prolong_mm"]) * 1_000_000)
        offset = line.parallel_offset(float(settings["offset_mm"]) * 1_000_000, "left")
        positions = []
        for part in listGeometries(offset):
            count = int(part.length / (float(settings["spacing_mm"]) * 1_000_000)) + 1
            for index in range(count):
                fraction = 0.5 if count == 1 else index / (count - 1)
                point = part.interpolate(fraction, normalized=True)
                if saved_material.buffer(SHP_EPSILON).intersects(point):
                    position = toKiCADPoint((point.x, point.y))
                    positions.append((int(position.x), int(position.y)))
        candidates.add(tuple(sorted(positions)))
    return candidates


def _verify_saved_cuts(board, plan, tab_cuts) -> None:
    import pcbnew

    settings = plan["resolved_settings"]["project"]["cuts"]
    expected_source_npth = sum(
        item["expected_inventory"]["npth_count"] for item in plan["instances"]
    )
    all_npth = [
        pad
        for footprint in board.GetFootprints()
        for pad in footprint.Pads()
        if int(pad.GetAttribute()) == int(pcbnew.PAD_ATTRIB_NPTH)
    ]
    generated = []
    groups = {}
    for footprint in board.GetFootprints():
        reference = footprint.GetReference()
        match = re.fullmatch(r"KiKit_MB_(\d+)_(\d+)", reference)
        if match is None:
            continue
        pads = list(footprint.Pads())
        if len(pads) != 1:
            raise VerificationError("mousebite footprint must contain exactly one pad")
        pad = pads[0]
        generated.append(pad)
        groups.setdefault(int(match.group(1)), []).append((int(match.group(2)), pad))
    if settings["mode"] == "none":
        if generated or len(all_npth) != expected_source_npth:
            raise VerificationError("no-cuts output NPTH inventory differs from transformed sources")
        return
    if settings["mode"] != "mousebites" or not generated:
        raise VerificationError("mousebite output contains no classified generated holes")
    if len(groups) != len(tab_cuts):
        raise VerificationError("mousebite cut count differs from saved tab-boundary geometry")
    if len(all_npth) != expected_source_npth + len(generated):
        raise VerificationError("mousebite output contains unclassified NPTH changes")
    expected_drill = int(round(float(settings["drill_mm"]) * 1_000_000))
    from kikit.substrate import Substrate

    saved_material = Substrate([
        item for item in board.GetDrawings() if item.GetLayer() == pcbnew.Edge_Cuts
    ]).substrates
    remaining_candidates = [
        _mousebite_candidates(cut, settings, saved_material) for cut in tab_cuts
    ]
    for holes in groups.values():
        holes.sort(key=lambda item: item[0])
        if [index for index, _ in holes] != list(range(1, len(holes) + 1)) or len(holes) < 2:
            raise VerificationError("mousebite hole indices are not contiguous")
        positions = [pad.GetPosition() for _, pad in holes]
        for _, pad in holes:
            if (
                int(pad.GetAttribute()) != int(pcbnew.PAD_ATTRIB_NPTH)
                or int(pad.GetShape()) != int(pcbnew.PAD_SHAPE_CIRCLE)
                or pad.GetDrillSize().x != expected_drill
                or pad.GetDrillSize().y != expected_drill
            ):
                raise VerificationError("mousebite pad fabrication attributes differ from the run plan")
        first_dx = positions[1].x - positions[0].x
        first_dy = positions[1].y - positions[0].y
        distance2 = first_dx * first_dx + first_dy * first_dy
        if distance2 == 0:
            raise VerificationError("mousebite holes overlap")
        for left, right in zip(positions, positions[1:]):
            dx = right.x - left.x
            dy = right.y - left.y
            if dx * first_dy != dy * first_dx or dx * dx + dy * dy != distance2:
                raise VerificationError("mousebite holes are not a uniform collinear pattern")
        actual_positions = tuple(sorted((int(point.x), int(point.y)) for point in positions))
        match = next(
            (index for index, candidates in enumerate(remaining_candidates) if actual_positions in candidates),
            None,
        )
        if match is None:
            raise VerificationError("mousebite positions differ from saved tab geometry and cut settings")
        remaining_candidates.pop(match)
    if remaining_candidates:
        raise VerificationError("saved tab geometry has no matching mousebite pattern")


def _verify_tab_connectivity(tab_data: dict[str, Any], instance_ids: list[str]) -> list[list[str]]:
    known_instances = set(instance_ids)
    expected_edges = []
    connection_records = tab_data.get("connections", [])
    if not isinstance(connection_records, list):
        raise VerificationError("tab connections have an invalid representation")
    if connection_records:
        for connection in connection_records:
            if not isinstance(connection, dict):
                raise VerificationError("tab connection has an invalid representation")
            instances = connection.get("instances", [])
            endpoints = (connection.get("start_iu"), connection.get("end_iu"))
            if (
                not isinstance(instances, list)
                or len(instances) != 2
                or instances[0] == instances[1]
                or any(instance not in known_instances for instance in instances)
                or any(
                    not isinstance(point, list)
                    or len(point) != 2
                    or any(type(value) is not int for value in point)
                    for point in endpoints
                )
                or endpoints[0] == endpoints[1]
            ):
                raise VerificationError("tab connection has invalid substrate incidence")
            expected_edges.append(sorted(instances))
    else:
        material_components = tab_data.get("material_components", [])
        if not isinstance(material_components, list):
            raise VerificationError("tab material components have an invalid representation")
        for component in material_components:
            if not isinstance(component, dict):
                raise VerificationError("tab material component has an invalid representation")
            instances = component.get("instances", [])
            if len(instance_ids) > 1 and (
                not isinstance(instances, list)
                or len(instances) != 2
                or instances[0] == instances[1]
                or any(instance not in known_instances for instance in instances)
            ):
                raise VerificationError("tab material component has invalid substrate incidence")
            if isinstance(instances, list) and len(instances) == 2:
                expected_edges.append(sorted(instances))

    reported_edges = tab_data.get("graph_edges", [])
    if not isinstance(reported_edges, list):
        raise VerificationError("tab graph edges have an invalid representation")
    normalized_reported_edges = []
    for edge in reported_edges:
        if (
            not isinstance(edge, list)
            or len(edge) != 2
            or edge[0] == edge[1]
            or any(instance not in known_instances for instance in edge)
        ):
            raise VerificationError("tab graph edge has invalid substrate incidence")
        normalized_reported_edges.append(sorted(edge))
    if sorted(normalized_reported_edges) != sorted(expected_edges):
        raise VerificationError("tab graph edges do not match validated tab connections")

    computed = connected_components(instance_ids, expected_edges)
    if tab_data.get("connected_components") != computed:
        raise VerificationError("tab connectivity result does not match validated tab connections")
    return computed


def verify_result(root: Path, plan: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    result_path = resolve_staging_path(root, contract["plugin_result_path"])
    result = load_json(result_path)
    validate_envelope(result, "kikit-packer.plugin-result")
    if result["run_id"] != plan["run_id"] or result["nonce"] != plan["nonce"]:
        raise VerificationError("plugin result belongs to a different run")
    if result["run_plan_sha256"] != contract["run_plan_sha256"]:
        raise VerificationError("plugin result references a different plan")
    resolved = plan["resolved_settings"]
    if result["raw_preset_sha256"] != resolved["kikit_raw_preset_digest"]:
        raise VerificationError("plugin result references a different raw KiKit preset")
    if result["processed_preset_sha256"] != resolved["kikit_processed_preset_digest"]:
        raise VerificationError("plugin result references a different processed KiKit preset")
    lifecycle = result.get("lifecycle", {})
    required_lifecycle = {"layout_complete", "tabs_complete", "cuts_complete", "save_complete"}
    if set(lifecycle) != required_lifecycle or not all(lifecycle[key] is True for key in required_lifecycle):
        raise VerificationError("plugin lifecycle did not complete")
    artifacts = result.get("artifacts", [])
    output = resolve_staging_path(root, contract["staged_output"])
    board_artifacts = [artifact for artifact in artifacts if artifact.get("kind") == "board"]
    if len(board_artifacts) != 1:
        raise VerificationError("plugin result must contain exactly one board artifact")
    board_artifact = board_artifacts[0]
    if resolve_staging_path(root, board_artifact["path"]) != output:
        raise VerificationError("board artifact does not match the contracted staged output")
    for kind, suffix in (("kicad_pro", ".kicad_pro"), ("kicad_dru", ".kicad_dru")):
        companions = [artifact for artifact in artifacts if artifact.get("kind") == kind]
        if len(companions) > 1:
            raise VerificationError(f"plugin result contains duplicate {kind} artifacts")
        if companions and resolve_staging_path(root, companions[0]["path"]) != output.with_suffix(suffix):
            raise VerificationError(f"{kind} artifact does not match the staged output stem")
    for artifact in artifacts:
        path = resolve_staging_path(root, artifact["path"])
        if not path.is_file() or file_sha256(path) != artifact["sha256"]:
            raise VerificationError("staged artifact hash mismatch: {}".format(artifact["path"]))
    current = verify_output(output)
    if current != result["inventories"]["saved_output"]:
        raise VerificationError("saved output inventory changed after child verification")
    import pcbnew

    board = pcbnew.LoadBoard(str(output))
    authority = plan["authority"]
    if board.GetCopperLayerCount() != authority["copper_layer_count"]:
        raise VerificationError("output copper-layer count differs from authority")
    output_layers = [
        board.GetLayerName(layer)
        for layer in board.GetEnabledLayers().CuStack()
        if pcbnew.IsCopperLayer(layer)
    ]
    if output_layers != authority["copper_layers"]:
        raise VerificationError("output enabled copper-layer set differs from authority")
    if board.GetDesignSettings().GetBoardThickness() != authority["thickness_iu"]:
        raise VerificationError("output thickness differs from authority")
    artifacts_by_kind = {artifact.get("kind"): artifact for artifact in artifacts}
    for suffix, companion in authority.get("companions", {}).items():
        artifact = artifacts_by_kind.get(suffix)
        if companion.get("present"):
            if artifact is None:
                raise VerificationError(f"authority companion was not promoted into staged output: {suffix}")
            artifact_path = resolve_staging_path(root, artifact["path"])
            if suffix == "kicad_pro":
                try:
                    verify_project_authority(artifact_path, companion["authority_profile"])
                except CompanionError as exc:
                    raise VerificationError(str(exc)) from exc
            elif artifact["sha256"] != companion.get("sha256"):
                raise VerificationError(f"staged {suffix} content differs from authority")
    if parse_setup_digest(output) != authority.get("setup_sha256"):
        raise VerificationError("output board setup differs from authority")
    expected_stackup = authority.get("stackup") or {}
    output_stackup = parse_stackup(output)
    if expected_stackup.get("verified") and output_stackup.get("descriptor") != expected_stackup.get("descriptor"):
        raise VerificationError("output explicit stackup differs from authority")
    expected_count = len(plan["instances"])
    actual_instances = result.get("instances", [])
    if [item.get("instance_id") for item in actual_instances] != [item["instance_id"] for item in plan["instances"]]:
        raise VerificationError("plugin result does not represent every planned instance")
    from kikit.panelize import findBoardBoundingBox

    expected_bounds_by_instance = [planned_substrate_bounds(item) for item in plan["instances"]]
    expected_panel_bounds = [
        min(bounds[0] for bounds in expected_bounds_by_instance),
        min(bounds[1] for bounds in expected_bounds_by_instance),
        max(bounds[2] for bounds in expected_bounds_by_instance),
        max(bounds[3] for bounds in expected_bounds_by_instance),
    ]
    panel_bounds = findBoardBoundingBox(board)
    if (
        panel_bounds.GetWidth() != expected_panel_bounds[2] - expected_panel_bounds[0]
        or panel_bounds.GetHeight() != expected_panel_bounds[3] - expected_panel_bounds[1]
    ):
        raise VerificationError("saved panel outline dimensions differ from planned substrates")
    page_delta = (
        panel_bounds.GetLeft() - expected_panel_bounds[0],
        panel_bounds.GetTop() - expected_panel_bounds[1],
    )
    expected_items = []
    final_bounds = result.get("final_substrate_bounds_iu", [])
    if len(final_bounds) != len(plan["instances"]):
        raise VerificationError("final substrate bounds do not represent every instance")
    for planned, actual, expected_bounds, final_bound in zip(
        plan["instances"], actual_instances, expected_bounds_by_instance, final_bounds
    ):
        expected = planned["expected_inventory"]
        expected_profile = expected["profiles"][str(planned["packing_rotation_deg"])]
        origin = expected_profile["geometry_origin_iu"]
        expected_items.extend(translated_profile_items(
            expected_profile,
            expected_bounds[0] + origin[0] + page_delta[0],
            expected_bounds[1] + origin[1] + page_delta[1],
        ))
        if actual.get("substrate_bounds_pre_page_iu") != expected_bounds:
            raise VerificationError("actual placement differs from supplied plan for {}".format(planned["instance_id"]))
        translated_bounds = [
            expected_bounds[0] + page_delta[0],
            expected_bounds[1] + page_delta[1],
            expected_bounds[2] + page_delta[0],
            expected_bounds[3] + page_delta[1],
        ]
        if final_bound != translated_bounds:
            raise VerificationError("final substrate bounds differ from saved-board translation")
    actual_fingerprints = fingerprints_by_uuid(board)
    generated_items = [
        item_fingerprint(item)
        for item in board.GetDrawings()
        if item.GetLayer() == pcbnew.Edge_Cuts
    ]
    for footprint in board.GetFootprints():
        if re.fullmatch(r"KiKit_MB_\d+_\d+", footprint.GetReference()):
            generated_items.append(item_fingerprint(footprint))
            generated_items.extend(item_fingerprint(field) for field in footprint.GetFields())
            generated_items.extend(item_fingerprint(pad) for pad in footprint.Pads())
            generated_items.extend(item_fingerprint(item) for item in footprint.GraphicalItems())
    classified_multiset = multiset(
        semantic_item(item) for item in [*expected_items, *generated_items]
    )
    actual_multiset = multiset(
        semantic_item(item) for item in actual_fingerprints.values()
    )
    if actual_multiset != classified_multiset:
        raise VerificationError(
            "saved output has missing or unclassified geometry or fabrication fields"
        )
    saved_components, saved_tab_cuts = _verify_saved_material(board, plan, page_delta)
    packing = plan["packing"]
    if packing["max_width_iu"] is not None and panel_bounds.GetWidth() > packing["max_width_iu"]:
        raise VerificationError("saved panel exceeds maximum width")
    if packing["max_height_iu"] is not None and panel_bounds.GetHeight() > packing["max_height_iu"]:
        raise VerificationError("saved panel exceeds maximum height")
    tab_data = result.get("tabs", {})
    if not isinstance(tab_data, dict):
        raise VerificationError("tab result has an invalid representation")
    reported_components = _verify_tab_connectivity(
        tab_data,
        [instance["instance_id"] for instance in plan["instances"]],
    )
    if reported_components != saved_components:
        raise VerificationError("plugin tab telemetry differs from saved panel material")
    if expected_count == 1:
        if saved_components != [[plan["instances"][0]["instance_id"]]]:
            raise VerificationError("single-board connectivity result is invalid")
    elif len(saved_components) != 1 or len(saved_components[0]) != expected_count:
        raise VerificationError("tab material does not connect every board instance")
    _verify_saved_cuts(board, plan, saved_tab_cuts)
    refill_enabled = plan["resolved_settings"]["project"]["post"]["verify_refill_areas"]
    refill_check = result.get("refill_area_check", {})
    if refill_check.get("enabled") is not refill_enabled:
        raise VerificationError("refill-area verification does not match the run plan")
    expected_status = "passed" if refill_enabled else "skipped"
    if refill_check.get("status") != expected_status:
        raise VerificationError("refill-area verification did not complete")
    if refill_enabled:
        zone_layer_count = refill_check.get("zone_layer_count")
        total_area = refill_check.get("total_area_iu2_x2")
        if type(zone_layer_count) is not int or zone_layer_count < 0:
            raise VerificationError("refill-area verification has an invalid zone-layer count")
        if type(total_area) is not int or total_area < 0:
            raise VerificationError("refill-area verification has an invalid total area")
        artifact_hashes = {
            artifact["kind"]: artifact["sha256"]
            for artifact in artifacts
            if artifact.get("kind") in {"board", "kicad_pro", "kicad_dru"}
        }
        if refill_check.get("input_sha256") != artifact_hashes:
            raise VerificationError("refill-area verification is not bound to all refill inputs")
        if refill_check.get("board_sha256") != board_artifact["sha256"]:
            raise VerificationError("refill-area verification is not bound to the staged board")
        source_checks = refill_check.get("source_checks")
        if not isinstance(source_checks, list) or len(source_checks) != len(plan["sources"]):
            raise VerificationError("refill-area verification does not cover every source")
        for source, check in zip(plan["sources"], source_checks):
            expected_hashes = {"board": source["sha256"]}
            for kind, companion in source["companions"].items():
                if companion["present"]:
                    expected_hashes[kind] = companion["sha256"]
            if (
                check.get("enabled") is not True
                or check.get("status") != "passed"
                or check.get("source_id") != source["source_id"]
                or check.get("original_path") != source["original_path"]
                or check.get("input_sha256") != expected_hashes
                or check.get("board_sha256") != source["sha256"]
            ):
                raise VerificationError("source refill verification is not bound to the run plan")
        canonical = refill_check.get("canonical_refill")
        if not isinstance(canonical, dict) or canonical.get("status") != "refilled":
            raise VerificationError("canonical staged-panel refill did not complete")
        changes = canonical.get("changes")
        before = canonical.get("before")
        after = canonical.get("after")
        if not isinstance(changes, list) or not isinstance(before, dict) or not isinstance(after, dict):
            raise VerificationError("canonical refill telemetry has an invalid representation")
        for summary in (before, after):
            if (
                type(summary.get("zone_layer_count")) is not int
                or summary["zone_layer_count"] < 0
                or type(summary.get("total_area_iu2_x2")) is not int
                or summary["total_area_iu2_x2"] < 0
            ):
                raise VerificationError("canonical refill telemetry has an invalid summary")
        deltas = [change.get("delta_area_iu2_x2") for change in changes]
        if any(type(delta) is not int for delta in deltas):
            raise VerificationError("canonical refill telemetry has an invalid area delta")
        if (
            canonical.get("changed_zone_layer_count") != len(changes)
            or canonical.get("total_delta_area_iu2_x2") != sum(deltas)
            or after["total_area_iu2_x2"] - before["total_area_iu2_x2"] != sum(deltas)
        ):
            raise VerificationError("canonical refill telemetry is internally inconsistent")
        try:
            parent_refill_check = verify_refill_areas(output, root)
        except RefillAreaError as exc:
            raise VerificationError(f"parent canonical refill verification failed: {exc}") from exc
        for field in (
            "zone_layer_count",
            "total_area_iu2_x2",
            "input_sha256",
            "board_sha256",
        ):
            if parent_refill_check.get(field) != refill_check.get(field):
                raise VerificationError(
                    "child refill telemetry differs from independent parent verification"
                )
        expected_after = {
            field: parent_refill_check[field]
            for field in ("zone_layer_count", "total_area_iu2_x2")
        }
        if after != expected_after:
            raise VerificationError("canonical refill output differs from independent parent area")
    result["parent_verification"] = {
        "status": "passed",
        "source_profiles": {
            "expected_items": sum(item["expected_inventory"]["selected_count"] for item in plan["instances"]),
            "saved_items": len(actual_fingerprints),
        },
        "placements": {
            "page_delta_iu": list(page_delta),
            "panel_bounds_iu": [
                panel_bounds.GetLeft(),
                panel_bounds.GetTop(),
                panel_bounds.GetRight(),
                panel_bounds.GetBottom(),
            ],
        },
        "authority": {
            "setup_sha256": authority["setup_sha256"],
            "stackup_verified": bool((authority.get("stackup") or {}).get("verified")),
        },
        "tab_connectivity": {"components": saved_components},
        "cut_inventory": {
            "mode": plan["resolved_settings"]["project"]["cuts"]["mode"],
            "npth_pads": len([
                pad
                for footprint in board.GetFootprints()
                for pad in footprint.Pads()
                if int(pad.GetAttribute()) == int(pcbnew.PAD_ATTRIB_NPTH)
            ]),
        },
        "artifact_hashes": {
            artifact["kind"]: artifact["sha256"] for artifact in artifacts
        },
        "refill_geometry": refill_check,
    }
    return result


def verify_output(path: Path) -> dict[str, int]:
    import pcbnew

    board = pcbnew.LoadBoard(str(path))
    if board is None:
        raise VerificationError("output cannot be loaded by pcbnew")
    return inventory_board(board)
