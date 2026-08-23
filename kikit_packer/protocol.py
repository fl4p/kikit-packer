from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 64
MAX_CONTAINER_ITEMS = 100_000
MAX_STRING_CHARS = 1_048_576
KINDS = {
    "kikit-packer.run-plan",
    "kikit-packer.run-contract",
    "kikit-packer.plugin-result",
    "kikit-packer.event",
}


class ProtocolError(ValueError):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_json_bytes(value))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_json(path: Path) -> dict[str, Any]:
    def reject_duplicate(pairs):
        output = {}
        for key, value in pairs:
            if key in output:
                raise ProtocolError(f"duplicate JSON key: {key}")
            output[key] = value
        return output

    with path.open("rb") as handle:
        encoded = handle.read(MAX_DOCUMENT_BYTES + 1)
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise ProtocolError("protocol document exceeds size limit")
    try:
        text = encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("protocol document is not UTF-8") from exc
    value = json.loads(
        text,
        object_pairs_hook=reject_duplicate,
        parse_constant=lambda item: (_ for _ in ()).throw(
            ProtocolError("invalid number: " + item)
        ),
    )
    if not isinstance(value, dict):
        raise ProtocolError("protocol document must be an object")
    return value


def _validate_tree(value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ProtocolError("protocol document exceeds nesting limit")
    if value is None or type(value) in (bool, int):
        return
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            raise ProtocolError("protocol string exceeds size limit")
        return
    if isinstance(value, list):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise ProtocolError("protocol array exceeds item limit")
        for item in value:
            _validate_tree(item, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise ProtocolError("protocol object exceeds item limit")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError("protocol object keys must be strings")
            _validate_tree(item, depth + 1)
        return
    raise ProtocolError(f"unsupported protocol value type: {type(value).__name__}")


def _require(value: dict[str, Any], fields: dict[str, type]) -> None:
    if set(value) != set(fields):
        missing = sorted(set(fields) - set(value))
        unknown = sorted(set(value) - set(fields))
        raise ProtocolError(f"protocol fields mismatch; missing={missing}, unknown={unknown}")
    for field, expected in fields.items():
        if type(value[field]) is not expected:
            raise ProtocolError(f"invalid type for protocol field: {field}")


def _shape(value: Any, template: Any, label: str) -> None:
    if isinstance(template, dict):
        if not isinstance(value, dict) or set(value) != set(template):
            raise ProtocolError(f"{label} fields mismatch")
        for key in template:
            _shape(value[key], template[key], f"{label}.{key}")
    elif isinstance(template, list):
        if not isinstance(value, list):
            raise ProtocolError(f"{label} must be an array")
        if template:
            for item in value:
                _shape(item, template[0], label + "[]")
    elif type(value) is not type(template):
        raise ProtocolError(f"invalid type for {label}")


def _validate_stackup(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise ProtocolError(f"{label} must be an object")
    _require(value, {
        "present": bool,
        "verified": bool,
        "descriptor": type(value.get("descriptor")),
        "unknown_keys": list,
        "problems": list,
    })
    if any(type(item) is not str for item in [*value["unknown_keys"], *value["problems"]]):
        raise ProtocolError(f"{label} problem lists must contain strings")
    descriptor = value["descriptor"]
    if descriptor is not None:
        if not isinstance(descriptor, dict):
            raise ProtocolError(f"{label}.descriptor must be an object")
        _require(descriptor, {"layers": list, "globals": dict})
        allowed_globals = {
            "copper_finish", "dielectric_constraints", "edge_connector", "castellated_pads", "edge_plating"
        }
        if not set(descriptor["globals"]).issubset(allowed_globals):
            raise ProtocolError(f"{label}.descriptor has unknown globals")
        if any(type(item) is not str for item in descriptor["globals"].values()):
            raise ProtocolError(f"{label}.descriptor globals must be strings")
        for layer in descriptor["layers"]:
            if not isinstance(layer, dict):
                raise ProtocolError(f"{label}.descriptor layer must be an object")
            _require(layer, {"name": str, "fields": dict})
            if not set(layer["fields"]).issubset({
                "type", "color", "thickness_iu", "material", "epsilon_r", "loss_tangent"
            }):
                raise ProtocolError(f"{label}.descriptor layer has unknown fields")
            for field, item in layer["fields"].items():
                expected = int if field == "thickness_iu" else str
                if type(item) is not expected:
                    raise ProtocolError(f"{label}.descriptor layer field has invalid type: {field}")


def _validate_companions(value: Any, label: str, authority: bool) -> None:
    if not isinstance(value, dict) or set(value) != {"kicad_pro", "kicad_dru"}:
        raise ProtocolError(f"{label} fields mismatch")
    for kind, record in value.items():
        if not isinstance(record, dict):
            raise ProtocolError(f"{label}.{kind} must be an object")
        expected = {"present", "sha256", "authority_profile"} if authority and kind == "kicad_pro" else (
            {"present", "sha256"} if authority else {"present", "snapshot_path", "sha256"}
        )
        if set(record) != expected or type(record["present"]) is not bool:
            raise ProtocolError(f"{label}.{kind} fields mismatch")
        for field in expected - {"present", "authority_profile"}:
            if record[field] is not None and type(record[field]) is not str:
                raise ProtocolError(f"{label}.{kind}.{field} has invalid type")
            if field == "sha256" and record[field] is not None:
                _hash(record[field], f"{label}.{kind}")
        if "authority_profile" in record and record["authority_profile"] is not None:
            profile = record["authority_profile"]
            if not isinstance(profile, dict):
                raise ProtocolError(f"{label}.kicad_pro.authority_profile must be an object")
            _require(profile, {"base_sha256": str, "net_classes": list, "netclass_assignments": dict})
            _hash(profile["base_sha256"], f"{label}.kicad_pro authority base")


def _validate_project(value: Any) -> None:
    if not isinstance(value, dict):
        raise ProtocolError("resolved project must be an object")
    _require(value, {
        "version": int,
        "authority": dict,
        "output": str,
        "max_width_mm": type(value.get("max_width_mm")),
        "max_height_mm": type(value.get("max_height_mm")),
        "layout": dict,
        "tabs": dict,
        "cuts": dict,
        "post": dict,
        "page": dict,
        "allow_mixed_layers": bool,
        "allow_mixed_thickness": bool,
        "boards": list,
    })
    for field in ("max_width_mm", "max_height_mm"):
        if value[field] is not None and type(value[field]) is not str:
            raise ProtocolError(f"resolved project {field} has invalid type")
    _require(value["authority"], {"board": str, "reference_only": bool})
    _require(value["layout"], {
        "horizontal_spacing_mm": str, "vertical_spacing_mm": str, "rotation_deg": str,
        "rename_net": str, "rename_ref": str, "bake_text": bool, "bake_ref": bool,
    })
    _require(value["tabs"], {
        "mode": str, "width_mm": str, "horizontal_count": int,
        "vertical_count": int, "min_distance_mm": str,
    })
    _require(value["cuts"], {
        "mode": str, "drill_mm": str, "spacing_mm": str, "offset_mm": str, "prolong_mm": str,
    })
    _require(value["post"], {
        "mill_radius_mm": str, "origin": str, "refill_zones": bool, "verify_refill_areas": bool,
    })
    _require(value["page"], {"mode": str})
    for board in value["boards"]:
        if not isinstance(board, dict):
            raise ProtocolError("resolved board must be an object")
        _require(board, {
            "board": str, "qty": int, "margin_mm": str,
            "legacy_rotate": type(board.get("legacy_rotate")),
        })
        if board["legacy_rotate"] is not None and type(board["legacy_rotate"]) is not str:
            raise ProtocolError("resolved board legacy_rotate must be null or a decimal string")


def _hash(value: str, label: str) -> None:
    if not HASH_RE.fullmatch(value):
        raise ProtocolError(f"invalid {label} hash")


def _integer(value: Any, label: str, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ProtocolError(f"invalid integer: {label}")
    return value


def _vector(value: Any, length: int, label: str) -> None:
    if not isinstance(value, list) or len(value) != length:
        raise ProtocolError(f"invalid vector: {label}")
    for item in value:
        _integer(item, label)


def _validate_diagnostic(value: Any) -> None:
    if not isinstance(value, dict):
        raise ProtocolError("diagnostic must be an object")
    required = {"code", "severity", "path", "message", "value_type", "context"}
    if set(value) != required:
        raise ProtocolError("diagnostic fields mismatch")
    for field in ("code", "severity", "path", "message"):
        if type(value[field]) is not str:
            raise ProtocolError(f"invalid diagnostic field: {field}")
    if value["value_type"] is not None and type(value["value_type"]) is not str:
        raise ProtocolError("invalid diagnostic value_type")
    if type(value["context"]) is not dict:
        raise ProtocolError("invalid diagnostic context")


def _validate_plan(value: dict[str, Any]) -> None:
    _require(value["runtime"], {
        "python": str,
        "kicad": str,
        "pcbnew": str,
        "kikit": str,
        "rectangle_packer": str,
        "shapely": str,
    })
    authority = value["authority"]
    _require(authority, {
        "source_id": str,
        "reference_only": bool,
        "board_sha256": str,
        "copper_layer_count": int,
        "copper_layers": list,
        "thickness_iu": int,
        "setup_sha256": str,
        "stackup": dict,
        "companions": dict,
    })
    _hash(authority["board_sha256"], "authority board")
    _hash(authority["setup_sha256"], "authority setup")
    _integer(authority["copper_layer_count"], "authority.copper_layer_count", 1)
    _integer(authority["thickness_iu"], "authority.thickness_iu", 1)
    if any(type(layer) is not str for layer in authority["copper_layers"]):
        raise ProtocolError("authority copper layers must be strings")
    _validate_stackup(authority["stackup"], "authority.stackup")
    _validate_companions(authority["companions"], "authority.companions", True)
    resolved = value["resolved_settings"]
    _require(resolved, {
        "project": dict,
        "kikit_raw_preset": dict,
        "kikit_raw_preset_digest": str,
        "kikit_processed_preset_digest": str,
    })
    _validate_project(resolved["project"])
    preset_template = json.loads(
        (Path(__file__).with_name("kikit_181_preset.json")).read_text(encoding="utf-8")
    )
    _shape(resolved["kikit_raw_preset"], preset_template, "resolved_settings.kikit_raw_preset")
    _hash(resolved["kikit_raw_preset_digest"], "raw preset")
    _hash(resolved["kikit_processed_preset_digest"], "processed preset")
    _require(value["packing"], {
        "max_width_iu": type(value["packing"].get("max_width_iu")),
        "max_height_iu": type(value["packing"].get("max_height_iu")),
        "candidate_limit": int,
        "candidate_count": int,
        "evaluated_count": int,
        "bounds_iu": list,
    })
    for field in ("max_width_iu", "max_height_iu"):
        if value["packing"][field] is not None:
            _integer(value["packing"][field], f"packing.{field}", 1)
    for field in ("candidate_limit", "candidate_count", "evaluated_count"):
        _integer(value["packing"][field], f"packing.{field}", 0)
    _vector(value["packing"]["bounds_iu"], 4, "packing.bounds_iu")
    for source in value["sources"]:
        if not isinstance(source, dict):
            raise ProtocolError("plan source must be an object")
        _require(source, {
            "source_id": str,
            "original_path": str,
            "snapshot_path": str,
            "sha256": str,
            "inspection": dict,
            "companions": dict,
            "ignored_companions": list,
        })
        _hash(source["sha256"], "source")
        _validate_companions(source["companions"], "source.companions", False)
        if any(type(item) is not str for item in source["ignored_companions"]):
            raise ProtocolError("source ignored companions must be strings")
        inspection = source["inspection"]
        _require(inspection, {
            "source_id": str,
            "path": str,
            "sha256": str,
            "outline_bounds_iu": list,
            "copper_bounds_iu": list,
            "copper_layers": list,
            "copper_layer_count": int,
            "thickness_iu": int,
            "setup_sha256": str,
            "stackup": dict,
            "diagnostics": list,
        })
        _hash(inspection["sha256"], "inspection")
        _hash(inspection["setup_sha256"], "setup")
        _validate_stackup(inspection["stackup"], "inspection.stackup")
        if any(type(layer) is not str for layer in inspection["copper_layers"]):
            raise ProtocolError("inspection copper layers must be strings")
        _vector(inspection["outline_bounds_iu"], 4, "inspection.outline_bounds_iu")
        _vector(inspection["copper_bounds_iu"], 4, "inspection.copper_bounds_iu")
        _integer(inspection["copper_layer_count"], "inspection.copper_layer_count", 1)
        _integer(inspection["thickness_iu"], "inspection.thickness_iu", 1)
        for diagnostic in inspection["diagnostics"]:
            _validate_diagnostic(diagnostic)
    for instance in value["instances"]:
        if not isinstance(instance, dict):
            raise ProtocolError("plan instance must be an object")
        _require(instance, {
            "instance_id": str,
            "source_id": str,
            "row_id": str,
            "ordinal": int,
            "outline_bounds_iu": list,
            "copper_bounds_iu": list,
            "source_area_iu": list,
            "packing_size_iu": list,
            "margin_iu": int,
            "packing_rotation_deg": int,
            "append": dict,
            "expected_inventory": dict,
            "coercions": list,
        })
        for field in ("outline_bounds_iu", "copper_bounds_iu", "source_area_iu"):
            _vector(instance[field], 4, f"instance.{field}")
        _vector(instance["packing_size_iu"], 2, "instance.packing_size_iu")
        _require(instance["append"], {
            "destination_iu": list,
            "origin": str,
            "rotation_deg": int,
        })
        _vector(instance["append"]["destination_iu"], 2, "instance.append.destination_iu")
        if instance["packing_rotation_deg"] not in (0, 90) or instance["append"]["rotation_deg"] not in (0, 90):
            raise ProtocolError("unsupported instance rotation")
        inventory = instance["expected_inventory"]
        _require(inventory, {
            "profile": str,
            "profiles": dict,
            "selected_count": int,
            "npth_count": int,
            "substrates": dict,
        })
        for count in ("selected_count", "npth_count"):
            _integer(inventory[count], f"expected_inventory.{count}", 0)
        if set(inventory["profiles"]) != {"0", "90"} or set(inventory["substrates"]) != {"0", "90"}:
            raise ProtocolError("expected inventory rotations are incomplete")
        for profile in inventory["profiles"].values():
            _require(profile, {
                "semantic_multiset": dict,
                "geometry_origin_iu": list,
                "items": list,
            })
            _vector(profile["geometry_origin_iu"], 2, "profile.geometry_origin_iu")
            for item_hash, count in profile["semantic_multiset"].items():
                _hash(item_hash, "profile semantic item")
                _integer(count, "profile semantic count", 1)
            for item in profile["items"]:
                _validate_fingerprint(item)
        for rotation, substrates in inventory["substrates"].items():
            if not isinstance(substrates, list):
                raise ProtocolError(f"substrates.{rotation} must be an array")
            for polygon in substrates:
                if not isinstance(polygon, dict):
                    raise ProtocolError("substrate polygon must be an object")
                _require(polygon, {"outline": list, "holes": list})
                for ring in [polygon["outline"], *polygon["holes"]]:
                    for point in ring:
                        _vector(point, 2, "substrate point")
    for diagnostic in value["diagnostics"]:
        _validate_diagnostic(diagnostic)


def _validate_polygon_geometry(value: Any, label: str) -> None:
    if not isinstance(value, list):
        raise ProtocolError(f"{label} must be an array")
    for polygon in value:
        if not isinstance(polygon, dict):
            raise ProtocolError(f"{label} polygon must be an object")
        _require(polygon, {"outline": list, "holes": list})
        for ring in [polygon["outline"], *polygon["holes"]]:
            if not isinstance(ring, list):
                raise ProtocolError(f"{label} ring must be an array")
            for point in ring:
                _vector(point, 2, label + " point")


def _validate_fingerprint(value: Any) -> None:
    if not isinstance(value, dict):
        raise ProtocolError("semantic fingerprint must be an object")
    from .fingerprint import SUPPORTED_CLASSES, _VALUE_METHODS, _value_key

    allowed = {
        "class", "layer", "bbox", "layers", "position", "start", "end", "center",
        "bezierc1", "bezierc2", "drillsize", "size", "offset", "outline_geometry", "filled_geometry",
        "custom_geometry", "polygon_geometry",
        *(_value_key(name) for name in _VALUE_METHODS),
    }
    if not set(value).issubset(allowed) or value.get("class") not in SUPPORTED_CLASSES:
        raise ProtocolError("semantic fingerprint has unknown fields or class")
    if type(value.get("class")) is not str or type(value.get("layer")) is not int:
        raise ProtocolError("semantic fingerprint class/layer has invalid type")
    _vector(value.get("bbox"), 4, "semantic fingerprint bbox")
    if "layers" in value:
        if not isinstance(value["layers"], list) or any(type(item) is not int for item in value["layers"]):
            raise ProtocolError("semantic fingerprint layers have invalid type")
    for field in ("position", "start", "end", "center", "drillsize", "size", "offset", "bezierc1", "bezierc2"):
        if field in value:
            _vector(value[field], 2, "semantic fingerprint " + field)
    for field in ("outline_geometry", "custom_geometry", "polygon_geometry"):
        if field in value:
            _validate_polygon_geometry(value[field], "semantic fingerprint " + field)
    if "filled_geometry" in value:
        if not isinstance(value["filled_geometry"], dict):
            raise ProtocolError("semantic fingerprint filled geometry must be an object")
        for layer, geometry in value["filled_geometry"].items():
            if type(layer) is not str:
                raise ProtocolError("semantic fingerprint filled-geometry layer must be a string")
            _validate_polygon_geometry(geometry, "semantic fingerprint filled geometry")
    required = {
        "FOOTPRINT": {"position", "orientationdegrees", "layers"},
        "PAD": {"position", "orientationdegrees", "size", "drillsize", "shape", "attribute", "layers"},
        "PCB_TRACK": {"start", "end", "width", "layer"},
        "PCB_VIA": {"position", "drill", "width", "viatype", "layers"},
        "PCB_ARC": {"start", "end", "center", "width", "layer"},
        "ZONE": {"outline_geometry", "layers", "minthickness"},
        "PCB_SHAPE": {"shape", "start", "end", "layer", "width"},
        "PCB_TEXT": {"text", "position", "layer", "textwidth", "textheight", "textthickness", "textangle", "linespacing", "mirrored", "bold", "italic", "knockout", "horizjustify", "vertjustify"},
        "PCB_TEXTBOX": {"text", "position", "layer", "textwidth", "textheight", "textthickness", "textangle", "linespacing", "mirrored", "bold", "italic", "knockout", "horizjustify", "vertjustify"},
        "PCB_FIELD": {"text", "position", "layer", "textwidth", "textheight", "textthickness", "textangle", "linespacing", "mirrored", "bold", "italic", "knockout", "horizjustify", "vertjustify"},
        "FP_TEXT": {"text", "position", "layer", "textwidth", "textheight", "textthickness", "textangle", "linespacing", "mirrored", "bold", "italic", "knockout", "horizjustify", "vertjustify"},
        "FP_SHAPE": {"shape", "start", "end", "layer", "width"},
    }[value["class"]]
    if not required.issubset(value):
        raise ProtocolError("semantic fingerprint is missing required class fields")
    string_fields = {
        "text", "orientationdegrees", "roundrectradiusratio", "chamferrectratio",
        "solderpastemarginratio", "thermalspokeangle", "arcangle", "textangle", "linespacing",
    }
    boolean_fields = {
        "locked", "filled", "rulearea", "donotallowvias", "donotallowtracks",
        "donotallowpads", "donotallowfootprints", "donotallowzonefills", "blindvia",
        "buriedvia", "microvia", "removeunconnected", "keeptopbottom", "solidfill",
        "mirrored", "bold", "italic", "knockout",
    }
    integer_fields = {
        "width", "drill", "shape", "attribute", "fabricationproperty", "drillshape",
        "roundrectradius", "chamferpositions", "clearance", "soldermaskmargin",
        "solderpastemargin", "zoneconnection", "thermalspokewidth", "thermalgap",
        "minthickness", "priority", "localclearance", "localsoldermaskmargin", "fillmode",
        "hatchstyle", "thermalreliefgap", "thermalreliefspokewidth", "cornerradius",
        "cornersmoothingtype", "viatype", "toplayer", "bottomlayer",
        "unconnectedlayermode", "radius", "horizjustify", "vertjustify", "textthickness",
        "textwidth", "textheight", "borderwidth",
    }
    typed_method_fields = string_fields | boolean_fields | integer_fields
    if {_value_key(name) for name in _VALUE_METHODS} != typed_method_fields:
        raise ProtocolError("fingerprint method type schema is incomplete")
    for fields, expected in (
        (string_fields, str),
        (boolean_fields, bool),
        (integer_fields, int),
    ):
        for field in fields & set(value):
            if type(value[field]) is not expected:
                raise ProtocolError(f"semantic fingerprint {field} has invalid type")
    if value["class"] in {"PCB_SHAPE", "FP_SHAPE"}:
        shape = value.get("shape")
        shape_requirements: dict[int, set[str]] = {
            2: {"center", "arcangle"},
            3: {"center", "radius"},
            4: {"polygon_geometry"},
            5: {"bezierc1", "bezierc2"},
        }
        shape_required = shape_requirements.get(shape, set()) if type(shape) is int else set()
        if not shape_required.issubset(value):
            raise ProtocolError("semantic fingerprint is missing shape-specific geometry")


def _validate_result(value: dict[str, Any]) -> None:
    _require(value["lifecycle"], {
        "layout_complete": bool,
        "tabs_complete": bool,
        "cuts_complete": bool,
        "save_complete": bool,
    })
    for bounds in value["final_substrate_bounds_iu"]:
        _vector(bounds, 4, "result.final_substrate_bounds_iu")
    for instance in value["instances"]:
        if not isinstance(instance, dict):
            raise ProtocolError("result instance must be an object")
        _require(instance, {
            "instance_id": str,
            "substrate_bounds_pre_page_iu": list,
            "output_item_uuids": list,
        })
        _vector(instance["substrate_bounds_pre_page_iu"], 4, "result substrate bounds")
    tabs = value["tabs"]
    _require(tabs, {
        "polygon_count": int,
        "material_components": list,
        "connections": list,
        "graph_edges": list,
        "connected_components": list,
    })
    _integer(tabs["polygon_count"], "tabs.polygon_count", 0)
    for component in tabs["material_components"]:
        if not isinstance(component, dict):
            raise ProtocolError("tab material component must be an object")
        _require(component, {"bounds_iu": list, "instances": list})
        _vector(component["bounds_iu"], 4, "tab material bounds")
        if any(type(item) is not str for item in component["instances"]):
            raise ProtocolError("tab material instances must be strings")
    for connection in tabs["connections"]:
        if not isinstance(connection, dict):
            raise ProtocolError("tab connection must be an object")
        _require(connection, {"instances": list, "start_iu": list, "end_iu": list})
        _vector(connection["start_iu"], 2, "tab connection start")
        _vector(connection["end_iu"], 2, "tab connection end")
        if len(connection["instances"]) != 2 or any(type(item) is not str for item in connection["instances"]):
            raise ProtocolError("tab connection instances are invalid")
    for field in ("graph_edges", "connected_components"):
        for group in tabs[field]:
            if not isinstance(group, list) or any(type(item) is not str for item in group):
                raise ProtocolError(f"tabs.{field} entries must be string arrays")
    _require(value["cuts"], {"mode": str})
    proof = value["semantic_copy_proof"]
    _require(proof, {"before_save": dict, "saved_output": dict})
    for field in proof:
        for item_uuid, fingerprint in proof[field].items():
            if type(item_uuid) is not str:
                raise ProtocolError("semantic proof UUID is invalid")
            _validate_fingerprint(fingerprint)
    expected_inventories = {"after_layout", "after_cuts", "before_save", "saved_output"}
    if set(value["inventories"]) != expected_inventories:
        raise ProtocolError("inventory stages mismatch")
    inventory_fields = {
        "footprints", "pads", "tracks_and_vias", "vias", "zones", "drawings", "npth_pads"
    }
    for artifact in value["artifacts"]:
        if not isinstance(artifact, dict):
            raise ProtocolError("artifact must be an object")
        _require(artifact, {"kind": str, "path": str, "size": int, "sha256": str})
        _integer(artifact["size"], "artifact.size", 0)
        _hash(artifact["sha256"], "artifact")
    for inventory in value["inventories"].values():
        if not isinstance(inventory, dict) or set(inventory) != inventory_fields:
            raise ProtocolError("inventory fields mismatch")
        for field, count in inventory.items():
            _integer(count, f"inventory.{field}", 0)
    refill = value["refill_area_check"]
    if not isinstance(refill, dict):
        raise ProtocolError("refill-area result must be an object")
    if refill.get("enabled") is False:
        _require(refill, {"enabled": bool, "status": str, "source_checks": list})
        if refill["status"] != "skipped" or refill["source_checks"]:
            raise ProtocolError("disabled refill-area verification has invalid telemetry")
    else:
        _require(refill, {
            "enabled": bool,
            "status": str,
            "zone_layer_count": int,
            "total_area_iu2_x2": int,
            "input_sha256": dict,
            "board_sha256": str,
            "source_checks": list,
            "canonical_refill": dict,
        })
        if refill["enabled"] is not True or refill["status"] != "passed":
            raise ProtocolError("enabled refill-area verification did not pass")
        for field in ("zone_layer_count", "total_area_iu2_x2"):
            _integer(refill[field], f"refill.{field}", 0)
        _hash(refill["board_sha256"], "refill board")
        for item in refill["input_sha256"].values():
            if type(item) is not str:
                raise ProtocolError("refill input hash must be a string")
            _hash(item, "refill input")
        source_fields = {
            "enabled", "status", "zone_layer_count", "total_area_iu2_x2",
            "input_sha256", "board_sha256", "source_id", "original_path",
        }
        for check in refill["source_checks"]:
            if not isinstance(check, dict) or set(check) != source_fields:
                raise ProtocolError("source refill fields mismatch")
            if check["enabled"] is not True or check["status"] != "passed":
                raise ProtocolError("source refill verification did not pass")
            for field in ("zone_layer_count", "total_area_iu2_x2"):
                _integer(check[field], f"source refill.{field}", 0)
            _hash(check["board_sha256"], "source refill board")
            if type(check["source_id"]) is not str or type(check["original_path"]) is not str:
                raise ProtocolError("source refill identity is invalid")
            if not isinstance(check["input_sha256"], dict):
                raise ProtocolError("source refill input hashes must be an object")
            for item in check["input_sha256"].values():
                if type(item) is not str:
                    raise ProtocolError("source refill input hash must be a string")
                _hash(item, "source refill input")
        canonical = refill["canonical_refill"]
        _require(canonical, {
            "status": str,
            "before": dict,
            "after": dict,
            "changed_zone_layer_count": int,
            "total_delta_area_iu2_x2": int,
            "changes": list,
        })
        if canonical["status"] != "refilled":
            raise ProtocolError("canonical refill did not complete")
        _integer(canonical["changed_zone_layer_count"], "canonical changed zones", 0)
        for summary in (canonical["before"], canonical["after"]):
            _require(summary, {"zone_layer_count": int, "total_area_iu2_x2": int})
            _integer(summary["zone_layer_count"], "canonical zone layers", 0)
            _integer(summary["total_area_iu2_x2"], "canonical total area", 0)
        change_fields = {
            "zone_uuid", "zone", "layer", "layer_name", "area_iu2_x2",
            "before_area_iu2_x2", "after_area_iu2_x2", "delta_area_iu2_x2",
        }
        for change in canonical["changes"]:
            if not isinstance(change, dict) or set(change) != change_fields:
                raise ProtocolError("canonical refill change fields mismatch")
            for field in ("zone_uuid", "zone", "layer_name"):
                if type(change[field]) is not str:
                    raise ProtocolError("canonical refill change identity is invalid")
            for field in (
                "layer", "area_iu2_x2", "before_area_iu2_x2",
                "after_area_iu2_x2", "delta_area_iu2_x2",
            ):
                _integer(change[field], f"canonical change.{field}")
    for diagnostic in value["diagnostics"]:
        _validate_diagnostic(diagnostic)


def validate_envelope(value: dict[str, Any], expected_kind: str) -> None:
    _validate_tree(value)
    if expected_kind not in KINDS:
        raise ValueError("unknown expected protocol kind")
    if value.get("kind") != expected_kind:
        raise ProtocolError(f"expected {expected_kind}, got {value.get('kind')}")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ProtocolError("unsupported schema version")
    run_id = value.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ProtocolError("invalid run ID")
    nonce = value.get("nonce")
    if nonce is not None and not HASH_RE.fullmatch(str(nonce)):
        raise ProtocolError("nonce must be 64 lowercase hexadecimal characters")

    common = {"kind": str, "schema_version": int, "run_id": str, "nonce": str}
    if expected_kind == "kikit-packer.run-plan":
        _require(value, common | {
            "project_digest": str,
            "runtime": dict,
            "authority": dict,
            "sources": list,
            "instances": list,
            "packing": dict,
            "resolved_settings": dict,
            "diagnostics": list,
        })
        _hash(value["project_digest"], "project digest")
        if not value["sources"] or not value["instances"]:
            raise ProtocolError("run plan must contain sources and instances")
        _validate_plan(value)
    elif expected_kind == "kikit-packer.run-contract":
        _require(value, common | {
            "staging_root": str,
            "run_plan_path": str,
            "run_plan_sha256": str,
            "staged_output": str,
            "plugin_result_path": str,
            "events_path": str,
            "log_limits": dict,
        })
        if not Path(value["staging_root"]).is_absolute():
            raise ProtocolError("staging root must be absolute")
        _hash(value["run_plan_sha256"], "run plan")
        _require(value["log_limits"], {
            "stdout_bytes": int,
            "stderr_bytes": int,
            "events_bytes": int,
        })
        if any(limit <= 0 for limit in value["log_limits"].values()):
            raise ProtocolError("log limits must be positive")
        for field in ("run_plan_path", "staged_output", "plugin_result_path", "events_path"):
            if Path(value[field]).is_absolute():
                raise ProtocolError("contract child paths must be relative")
    elif expected_kind == "kikit-packer.plugin-result":
        _require(value, common | {
            "run_plan_sha256": str,
            "raw_preset_sha256": str,
            "processed_preset_sha256": str,
            "lifecycle": dict,
            "instances": list,
            "final_substrate_bounds_iu": list,
            "tabs": dict,
            "cuts": dict,
            "refill_area_check": dict,
            "semantic_copy_proof": dict,
            "inventories": dict,
            "artifacts": list,
            "diagnostics": list,
        })
        for field in ("run_plan_sha256", "raw_preset_sha256", "processed_preset_sha256"):
            _hash(value[field], field)
        _validate_result(value)
    else:
        _require(value, common | {
            "sequence": int,
            "stage": str,
            "event": str,
            "payload": dict,
        })
        if value["sequence"] <= 0:
            raise ProtocolError("event sequence must be positive")


def append_event(path: Path, value: dict[str, Any]) -> None:
    validate_envelope(value, "kikit-packer.event")
    encoded = canonical_json_bytes(value) + b"\n"
    if len(encoded) > 65536:
        raise ProtocolError("event exceeds 64 KiB")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def resolve_staging_path(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute():
        raise ProtocolError("protocol child path must be relative")
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        raise ProtocolError(f"protocol path escapes staging root: {relative}")
    return candidate
