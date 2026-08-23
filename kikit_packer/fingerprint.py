import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

PROFILE = "kicad10-kikit181-v1"


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
    return str(value)


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
        yield from footprint.Pads()
        yield from footprint.GraphicalItems()
    yield from board.GetTracks()
    yield from board.Zones()
    yield from board.GetDrawings()


def item_uuid(item) -> str:
    return str(item.m_Uuid.AsString())


def item_fingerprint(item) -> dict[str, Any]:
    box = item.GetBoundingBox()
    value: dict[str, Any] = {
        "class": str(item.GetClass()),
        "layer": int(item.GetLayer()),
        "bbox": [int(box.GetLeft()), int(box.GetTop()), int(box.GetRight()), int(box.GetBottom())],
    }
    for name in ("GetPosition", "GetStart", "GetEnd", "GetCenter", "GetDrillSize", "GetSize"):
        result = _call(item, name)
        if result is not None:
            value[name[3:].lower()] = _point(result)
    for name in ("GetWidth", "GetDrill", "GetOrientationDegrees", "GetShape", "GetAttribute"):
        result = _call(item, name)
        if result is not None:
            try:
                value[name[3:].lower()] = float(result) if isinstance(result, float) else int(result)
            except (TypeError, ValueError):
                value[name[3:].lower()] = str(result)
    return value


def fingerprints_by_uuid(board) -> dict[str, dict[str, Any]]:
    return {item_uuid(item): item_fingerprint(item) for item in iter_board_items(board)}


def semantic_item(fingerprint: Mapping[str, Any]) -> dict[str, Any]:
    geometry = {"bbox", "position", "start", "end", "center", "orientationdegrees"}
    return {key: value for key, value in fingerprint.items() if key not in geometry}


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


def source_copy_profile(board, source_area_iu) -> dict[str, Any]:
    import pcbnew

    selected = []
    npth_count = 0
    for footprint in board.GetFootprints():
        if not _inside(footprint.GetBoundingBox(), source_area_iu):
            continue
        items = [footprint] + list(footprint.Pads()) + [
            item for item in footprint.GraphicalItems() if item.GetLayer() != pcbnew.Edge_Cuts
        ]
        selected.extend(items)
        npth_count += sum(1 for pad in footprint.Pads() if int(pad.GetAttribute()) == 2)
    selected.extend(track for track in board.GetTracks() if _inside(track.GetBoundingBox(), source_area_iu))
    selected.extend(zone for zone in board.Zones() if _intersects(zone.GetBoundingBox(), source_area_iu))
    selected.extend(
        drawing
        for drawing in board.GetDrawings()
        if drawing.GetLayer() != pcbnew.Edge_Cuts and _inside(drawing.GetBoundingBox(), source_area_iu)
    )
    values = [semantic_item(item_fingerprint(item)) for item in selected]
    return {
        "profile": PROFILE,
        "semantic_multiset": multiset(values),
        "selected_count": len(values),
        "npth_count": npth_count,
    }


def inventory_board(board) -> dict[str, int]:
    footprints = list(board.GetFootprints())
    pads = [pad for footprint in footprints for pad in footprint.Pads()]
    tracks = list(board.GetTracks())
    vias = [track for track in tracks if track.GetClass() == "PCB_VIA"]
    zones = list(board.Zones())
    drawings = list(board.GetDrawings())
    npth = [pad for pad in pads if str(pad.GetAttribute()).endswith("NPTH") or int(pad.GetAttribute()) == 2]
    return {
        "footprints": len(footprints),
        "pads": len(pads),
        "tracks_and_vias": len(tracks),
        "vias": len(vias),
        "zones": len(zones),
        "drawings": len(drawings),
        "npth_pads": len(npth),
    }
