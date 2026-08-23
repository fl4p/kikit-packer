import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any, Callable

from .geometry import canonical_ring_points

PROFILE = "kicad10-kikit181-v2"
SUPPORTED_CLASSES = {
    "FOOTPRINT",
    "PAD",
    "PCB_TRACK",
    "PCB_VIA",
    "PCB_ARC",
    "ZONE",
    "PCB_SHAPE",
    "PCB_TEXT",
    "PCB_TEXTBOX",
    "PCB_FIELD",
    "FP_TEXT",
    "FP_SHAPE",
}


class FingerprintError(RuntimeError):
    pass
_POINT_FIELDS = {"bbox", "position", "start", "end", "center", "bezierc1", "bezierc2"}
_VALUE_METHODS = (
    "GetText",
    "GetWidth",
    "GetDrill",
    "GetOrientationDegrees",
    "GetShape",
    "GetAttribute",
    "GetFabricationProperty",
    "GetDrillShape",
    "GetRoundRectRadius",
    "GetRoundRectRadiusRatio",
    "GetChamferPositions",
    "GetChamferRectRatio",
    "GetClearance",
    "GetSolderMaskMargin",
    "GetSolderPasteMargin",
    "GetSolderPasteMarginRatio",
    "GetZoneConnection",
    "GetThermalSpokeWidth",
    "GetThermalSpokeAngle",
    "GetThermalGap",
    "GetMinThickness",
    "GetPriority",
    "GetLocalClearance",
    "GetLocalSolderMaskMargin",
    "GetFillMode",
    "GetHatchStyle",
    "IsLocked",
    "IsFilled",
    "IsRuleArea",
    "GetDoNotAllowVias",
    "GetDoNotAllowTracks",
    "GetDoNotAllowPads",
    "GetDoNotAllowFootprints",
    "GetDoNotAllowZoneFills",
    "GetThermalReliefGap",
    "GetThermalReliefSpokeWidth",
    "GetCornerRadius",
    "GetCornerSmoothingType",
    "GetViaType",
    "IsBlindVia",
    "IsBuriedVia",
    "IsMicroVia",
    "GetTopLayer",
    "GetBottomLayer",
    "GetUnconnectedLayerMode",
    "GetRemoveUnconnected",
    "GetKeepTopBottom",
    "GetArcAngle",
    "GetRadius",
    "IsSolidFill",
    "IsMirrored",
    "IsBold",
    "IsItalic",
    "GetHorizJustify",
    "GetVertJustify",
    "GetTextThickness",
    "GetTextWidth",
    "GetTextHeight",
    "GetTextAngle",
    "GetLineSpacing",
    "GetBorderWidth",
    "IsKnockout",
)


def semantic_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def multiset(values: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    return dict(Counter(semantic_digest(value) for value in values))


def _point(value):
    if value is None:
        return None
    if hasattr(value, "x") and hasattr(value, "y"):
        return [int(value.x), int(value.y)]
    return None


def _primitive(value: Any) -> Any:
    if value is None or type(value) in (bool, int, str):
        return value
    if isinstance(value, float):
        return format(value, ".15g")
    if hasattr(value, "AsDegrees"):
        return format(float(value.AsDegrees()), ".15g")
    point = _point(value)
    if point is not None:
        return point
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FingerprintError(f"unsupported fingerprint value type: {type(value).__name__}") from exc


def _call(item, name):
    method = getattr(item, name, None)
    if method is None:
        return None
    try:
        return method()
    except (TypeError, RuntimeError):
        return None


def iter_board_items(board):
    for footprint in board.GetFootprints():
        yield footprint
        yield from footprint.GetFields()
        yield from footprint.Pads()
        yield from footprint.GraphicalItems()
    yield from board.GetTracks()
    yield from board.Zones()
    yield from board.GetDrawings()


def item_uuid(item) -> str:
    return str(item.m_Uuid.AsString())


def _canonical_points(points: list[tuple[int, int]]) -> list[list[int]]:
    return canonical_ring_points(points)


def _canonical_ring(chain) -> list[list[int]]:
    return canonical_ring_points([
        (chain.CPoint(index).x, chain.CPoint(index).y)
        for index in range(chain.PointCount())
    ])


def _polyset_geometry(polyset) -> list[dict[str, Any]]:
    polygons = []
    for outline_index in range(polyset.OutlineCount()):
        polygons.append({
            "outline": _canonical_ring(polyset.Outline(outline_index)),
            "holes": sorted(
                _canonical_ring(polyset.Hole(outline_index, hole_index))
                for hole_index in range(polyset.HoleCount(outline_index))
            ),
        })
    return sorted(polygons, key=semantic_digest)


def _value_key(method: str) -> str:
    return method[2:].lower() if method.startswith("Is") else method[3:].lower()


def item_fingerprint(item) -> dict[str, Any]:
    item_class = str(item.GetClass())
    if item_class not in SUPPORTED_CLASSES:
        raise FingerprintError(f"unsupported KiCad item class: {item_class}")
    box = item.GetBoundingBox()
    value: dict[str, Any] = {
        "class": item_class,
        "layer": int(item.GetLayer()),
        "bbox": [int(box.GetLeft()), int(box.GetTop()), int(box.GetRight()), int(box.GetBottom())],
    }
    layer_set = _call(item, "GetLayerSet")
    if layer_set is not None and hasattr(layer_set, "Seq"):
        value["layers"] = sorted(int(layer) for layer in layer_set.Seq())
    for name in (
        "GetPosition",
        "GetStart",
        "GetEnd",
        "GetCenter",
        "GetDrillSize",
        "GetSize",
        "GetOffset",
        "GetBezierC1",
        "GetBezierC2",
    ):
        result = _call(item, name)
        point = _point(result)
        if point is not None:
            value[name[3:].lower()] = point
    for name in _VALUE_METHODS:
        result = _call(item, name)
        if result is not None:
            value[_value_key(name)] = _primitive(result)
    if value["class"] == "ZONE":
        value.pop("position", None)
        value.pop("center", None)
        outline = _call(item, "Outline")
        if outline is not None:
            value["outline_geometry"] = _polyset_geometry(outline)
        filled = {}
        for layer in value.get("layers", []):
            if item.HasFilledPolysForLayer(layer):
                filled[str(layer)] = _polyset_geometry(item.GetFilledPolysList(layer))
        value["filled_geometry"] = filled
    elif value["class"] == "PAD" and str(value.get("shape")) == "9":
        polygon = _call(item, "GetCustomShapeAsPolygon")
        if polygon is not None:
            value["custom_geometry"] = _polyset_geometry(polygon)
    elif value["class"] in {"PCB_SHAPE", "FP_SHAPE"}:
        polygon = _call(item, "GetPolyShape")
        if polygon is not None and hasattr(polygon, "OutlineCount"):
            value["polygon_geometry"] = _polyset_geometry(polygon)
        shape = value.get("shape")
        if shape == 2 and not {"center", "arcangle"}.issubset(value):
            raise FingerprintError(f"unsupported {value['class']} arc geometry")
        if shape == 3 and not {"center", "radius"}.issubset(value):
            raise FingerprintError(f"unsupported {value['class']} circle geometry")
        if shape == 4 and "polygon_geometry" not in value:
            raise FingerprintError(f"unsupported {value['class']} polygon geometry")
        if shape == 5 and not {"bezierc1", "bezierc2"}.issubset(value):
            raise FingerprintError(f"unsupported {value['class']} Bézier geometry")
    required = {
        "FOOTPRINT": {"position", "orientationdegrees", "layers"},
        "PAD": {"position", "orientationdegrees", "size", "drillsize", "shape", "attribute", "layers"},
        "PCB_TRACK": {"start", "end", "width", "layer"},
        "PCB_VIA": {"position", "drill", "width", "viatype", "layers"},
        "PCB_ARC": {"start", "end", "center", "width", "layer"},
        "ZONE": {"outline_geometry", "layers", "minthickness"},
        "PCB_SHAPE": {"shape", "start", "end", "layer", "width"},
        "PCB_TEXT": {
            "text", "position", "layer", "textwidth", "textheight", "textthickness",
            "textangle", "linespacing", "mirrored", "bold", "italic", "knockout",
            "horizjustify", "vertjustify",
        },
        "PCB_TEXTBOX": {
            "text", "position", "layer", "textwidth", "textheight", "textthickness",
            "textangle", "linespacing", "mirrored", "bold", "italic", "knockout",
            "horizjustify", "vertjustify",
        },
        "PCB_FIELD": {
            "text", "position", "layer", "textwidth", "textheight", "textthickness",
            "textangle", "linespacing", "mirrored", "bold", "italic", "knockout",
            "horizjustify", "vertjustify",
        },
        "FP_TEXT": {
            "text", "position", "layer", "textwidth", "textheight", "textthickness",
            "textangle", "linespacing", "mirrored", "bold", "italic", "knockout",
            "horizjustify", "vertjustify",
        },
        "FP_SHAPE": {"shape", "start", "end", "layer", "width"},
    }[item_class]
    missing = required - set(value)
    if missing:
        raise FingerprintError(f"unsupported {item_class} fingerprint fields: {sorted(missing)}")
    return _map_geometry(value, lambda x, y: (x, y))


def fingerprints_by_uuid(board) -> dict[str, dict[str, Any]]:
    return {item_uuid(item): item_fingerprint(item) for item in iter_board_items(board)}


def semantic_item(fingerprint: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(fingerprint)
    value.pop("filled_geometry", None)
    return value


def _inside(box, bounds) -> bool:
    left, top, right, bottom = bounds
    return (
        box.GetLeft() >= left
        and box.GetTop() >= top
        and box.GetRight() <= right
        and box.GetBottom() <= bottom
    )


def _intersects(box, bounds) -> bool:
    left, top, right, bottom = bounds
    return not (
        box.GetRight() < left
        or box.GetLeft() > right
        or box.GetBottom() < top
        or box.GetTop() > bottom
    )


def _transform_polyset(value: Any, transform: Callable[[int, int], tuple[int, int]]) -> Any:
    if not isinstance(value, list):
        return value
    output = []
    for polygon in value:
        if not isinstance(polygon, dict):
            continue
        output.append({
            "outline": _canonical_points([
                transform(point[0], point[1]) for point in polygon.get("outline", [])
            ]),
            "holes": sorted(
                _canonical_points([transform(point[0], point[1]) for point in ring])
                for ring in polygon.get("holes", [])
            ),
        })
    return sorted(output, key=semantic_digest)


def _quantized(point: tuple[int, int]) -> tuple[int, int]:
    return (
        int(round(point[0] / 10)) * 10,
        int(round(point[1] / 10)) * 10,
    )


def _map_geometry(value: dict[str, Any], transform: Callable[[int, int], tuple[int, int]]) -> dict[str, Any]:
    def transformed(x, y):
        return _quantized(transform(x, y))

    output = dict(value)
    for field in _POINT_FIELDS:
        item = output.get(field)
        if not isinstance(item, list):
            continue
        if field == "bbox" and len(item) == 4:
            corners = [
                transformed(item[0], item[1]),
                transformed(item[0], item[3]),
                transformed(item[2], item[1]),
                transformed(item[2], item[3]),
            ]
            output[field] = [
                min(point[0] for point in corners),
                min(point[1] for point in corners),
                max(point[0] for point in corners),
                max(point[1] for point in corners),
            ]
        elif len(item) == 2:
            output[field] = list(transformed(item[0], item[1]))
    for field in ("outline_geometry", "custom_geometry", "polygon_geometry"):
        if field in output:
            output[field] = _transform_polyset(output[field], transformed)
    if isinstance(output.get("filled_geometry"), dict):
        output["filled_geometry"] = {
            layer: _transform_polyset(polygons, transformed)
            for layer, polygons in output["filled_geometry"].items()
        }
    return output


def _geometry_min(values: list[dict[str, Any]]) -> tuple[int, int]:
    points = []
    for value in values:
        for field in _POINT_FIELDS:
            item = value.get(field)
            if isinstance(item, list) and len(item) == 2:
                points.append((int(item[0]), int(item[1])))
            elif isinstance(item, list) and len(item) == 4:
                points.extend(((int(item[0]), int(item[1])), (int(item[2]), int(item[3]))))
    return (
        min((point[0] for point in points), default=0),
        min((point[1] for point in points), default=0),
    )


def _rotated_semantics(value: dict[str, Any], rotation: int) -> dict[str, Any]:
    output = dict(value)
    for field in ("orientationdegrees", "textangle"):
        angle = output.get(field)
        if angle is not None:
            output[field] = format((float(angle) + rotation) % 360, ".15g")
    return output


def _profile(values: list[dict[str, Any]]) -> dict[str, Any]:
    origin_x, origin_y = _geometry_min(values)
    normalized = [
        _map_geometry(value, lambda x, y: (x - origin_x, y - origin_y))
        for value in values
    ]
    return {
        "semantic_multiset": multiset(semantic_item(value) for value in normalized),
        "geometry_origin_iu": [origin_x, origin_y],
        "items": normalized,
    }


def _substrate_profiles(board, outline_bounds_iu) -> dict[str, list[dict[str, Any]]]:
    import pcbnew
    from kikit.substrate import Substrate

    edges = [item for item in board.GetDrawings() if item.GetLayer() == pcbnew.Edge_Cuts]
    material = Substrate(edges).substrates
    geometries = list(getattr(material, "geoms", [material]))
    left, top, right, _bottom = outline_bounds_iu
    profiles = {}
    for rotation in (0, 90):
        polygons = []
        transform = (
            (lambda x, y: (int(round(x)) - left, int(round(y)) - top))
            if rotation == 0
            else (lambda x, y: (int(round(y)) - top, right - int(round(x))))
        )
        for geometry in geometries:
            polygons.append({
                "outline": _canonical_points([transform(x, y) for x, y in list(geometry.exterior.coords)[:-1]]),
                "holes": sorted(
                    _canonical_points([transform(x, y) for x, y in list(ring.coords)[:-1]])
                    for ring in geometry.interiors
                ),
            })
        profiles[str(rotation)] = sorted(polygons, key=semantic_digest)
    return profiles


def _is_npth(pad) -> bool:
    import pcbnew

    return int(pad.GetAttribute()) == int(pcbnew.PAD_ATTRIB_NPTH)


def source_copy_profile(board, source_area_iu, outline_bounds_iu) -> dict[str, Any]:
    import pcbnew

    selected = []
    npth_count = 0
    for footprint in board.GetFootprints():
        if not _inside(footprint.GetBoundingBox(), source_area_iu):
            continue
        items = [footprint] + list(footprint.GetFields()) + list(footprint.Pads()) + [
            item for item in footprint.GraphicalItems() if item.GetLayer() != pcbnew.Edge_Cuts
        ]
        selected.extend(items)
        npth_count += sum(1 for pad in footprint.Pads() if _is_npth(pad))
    selected.extend(track for track in board.GetTracks() if _inside(track.GetBoundingBox(), source_area_iu))
    selected.extend(zone for zone in board.Zones() if _intersects(zone.GetBoundingBox(), source_area_iu))
    selected.extend(
        drawing
        for drawing in board.GetDrawings()
        if drawing.GetLayer() != pcbnew.Edge_Cuts and _inside(drawing.GetBoundingBox(), source_area_iu)
    )
    fingerprints = [item_fingerprint(item) for item in selected]
    left, top, _right, bottom = outline_bounds_iu
    profiles = {}
    for rotation in (0, 90):
        if rotation == 0:
            transformed = [
                _rotated_semantics(_map_geometry(value, lambda x, y: (x - left, y - top)), rotation)
                for value in fingerprints
            ]
        else:
            transformed = [
                _rotated_semantics(_map_geometry(value, lambda x, y: (y - top, _right - x)), rotation)
                for value in fingerprints
            ]
        profiles[str(rotation)] = _profile(transformed)
    return {
        "profile": PROFILE,
        "profiles": profiles,
        "selected_count": len(fingerprints),
        "npth_count": npth_count,
        "substrates": _substrate_profiles(board, outline_bounds_iu),
    }


def saved_copy_profile(fingerprints: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    return _profile([dict(value) for value in fingerprints])


def translated_profile_items(profile: Mapping[str, Any], x: int, y: int) -> list[dict[str, Any]]:
    return [
        _map_geometry(dict(value), lambda px, py: (px + x, py + y))
        for value in profile["items"]
    ]


def inventory_board(board) -> dict[str, int]:
    footprints = list(board.GetFootprints())
    pads = [pad for footprint in footprints for pad in footprint.Pads()]
    tracks = list(board.GetTracks())
    vias = [track for track in tracks if track.GetClass() == "PCB_VIA"]
    zones = list(board.Zones())
    drawings = list(board.GetDrawings())
    npth = [pad for pad in pads if _is_npth(pad)]
    return {
        "footprints": len(footprints),
        "pads": len(pads),
        "tracks_and_vias": len(tracks),
        "vias": len(vias),
        "zones": len(zones),
        "drawings": len(drawings),
        "npth_pads": len(npth),
    }
