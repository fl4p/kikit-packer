from pathlib import Path
from typing import Any

from .connectivity import connected_components
from .fingerprint import inventory_board, multiset, semantic_item
from .protocol import file_sha256, load_json, resolve_staging_path, validate_envelope
from .stackup import parse_stackup


class VerificationError(RuntimeError):
    pass


def _expected_substrate_bounds(instance):
    source_left, source_top, _source_right, source_bottom = instance["source_area_iu"]
    outline_left, outline_top, outline_right, outline_bottom = instance["outline_bounds_iu"]
    destination_x, destination_y = instance["append"]["destination_iu"]
    if instance["packing_rotation_deg"] == 0:
        return [
            destination_x + outline_left - source_left,
            destination_y + outline_top - source_top,
            destination_x + outline_right - source_left,
            destination_y + outline_bottom - source_top,
        ]
    return [
        destination_x + source_bottom - outline_bottom,
        destination_y + outline_left - source_left,
        destination_x + source_bottom - outline_top,
        destination_y + outline_right - source_left,
    ]


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
    artifact_kinds = {artifact.get("kind") for artifact in artifacts}
    for suffix, companion in authority.get("companions", {}).items():
        if companion.get("present") and suffix not in artifact_kinds:
            raise VerificationError(f"authority companion was not promoted into staged output: {suffix}")
    expected_stackup = authority.get("stackup") or {}
    output_stackup = parse_stackup(output)
    if expected_stackup.get("verified") and output_stackup.get("descriptor") != expected_stackup.get("descriptor"):
        raise VerificationError("output explicit stackup differs from authority")
    expected_count = len(plan["instances"])
    actual_instances = result.get("instances", [])
    if [item.get("instance_id") for item in actual_instances] != [item["instance_id"] for item in plan["instances"]]:
        raise VerificationError("plugin result does not represent every planned instance")
    semantic = result.get("semantic_copy_proof", {})
    if semantic.get("before_save") != semantic.get("saved_output"):
        raise VerificationError("copied source entities changed or disappeared during save")
    saved_fingerprints = semantic.get("saved_output", {})
    for planned, actual in zip(plan["instances"], actual_instances):
        actual_values = [
            semantic_item(saved_fingerprints[item_uuid])
            for item_uuid in actual.get("output_item_uuids", [])
            if item_uuid in saved_fingerprints
        ]
        expected = planned["expected_inventory"]
        if len(actual_values) != expected["selected_count"]:
            raise VerificationError("copied entity count differs from source profile for {}".format(planned["instance_id"]))
        if multiset(actual_values) != expected["semantic_multiset"]:
            raise VerificationError("copied entities differ from source profile for {}".format(planned["instance_id"]))
        if actual.get("substrate_bounds_pre_page_iu") != _expected_substrate_bounds(planned):
            raise VerificationError("actual placement differs from supplied plan for {}".format(planned["instance_id"]))
    tab_data = result.get("tabs", {})
    if not isinstance(tab_data, dict):
        raise VerificationError("tab result has an invalid representation")
    components = _verify_tab_connectivity(
        tab_data,
        [instance["instance_id"] for instance in plan["instances"]],
    )
    if expected_count == 1:
        if components != [[plan["instances"][0]["instance_id"]]]:
            raise VerificationError("single-board connectivity result is invalid")
    elif len(components) != 1 or len(components[0]) != expected_count:
        raise VerificationError("tab material does not connect every board instance")
    if plan["resolved_settings"]["project"]["cuts"]["mode"] == "none":
        expected_npth = sum(item["expected_inventory"]["npth_count"] for item in plan["instances"])
        actual_npth = result["inventories"].get("saved_output", {}).get("npth_pads")
        if actual_npth != expected_npth:
            raise VerificationError("no-cuts output NPTH inventory differs from transformed sources")
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
    return result


def verify_output(path: Path) -> dict[str, int]:
    import pcbnew

    board = pcbnew.LoadBoard(str(path))
    if board is None:
        raise VerificationError("output cannot be loaded by pcbnew")
    return inventory_board(board)
