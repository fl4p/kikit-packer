import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .protocol import file_sha256

IU2_X2_PER_MM2 = 2_000_000_000_000
_REFILL_SUFFIXES = (".kicad_pcb", ".kicad_pro", ".kicad_dru")


class RefillAreaError(RuntimeError):
    pass


def _zone_label(zone) -> str:
    return str(zone.GetZoneName() or zone.GetNetname() or zone.m_Uuid.AsString())


def _line_chain_area_x2(chain) -> int:
    points = [chain.CPoint(index) for index in range(chain.PointCount())]
    if len(points) < 3:
        return 0
    return abs(sum(
        point.x * points[(index + 1) % len(points)].y
        - points[(index + 1) % len(points)].x * point.y
        for index, point in enumerate(points)
    ))


def _polyset_area_x2(polyset) -> int:
    area = 0
    for outline_index in range(polyset.OutlineCount()):
        area += _line_chain_area_x2(polyset.Outline(outline_index))
        for hole_index in range(polyset.HoleCount(outline_index)):
            area -= _line_chain_area_x2(polyset.Hole(outline_index, hole_index))
    if area < 0:
        raise RefillAreaError("filled polygon holes exceed their outlines")
    return area


def fill_area_snapshot(board) -> dict[str, dict[str, Any]]:
    snapshot = {}
    for zone in board.Zones():
        zone_uuid = str(zone.m_Uuid.AsString())
        for layer in sorted(int(value) for value in zone.GetLayerSet().Seq()):
            area = 0
            if zone.HasFilledPolysForLayer(layer):
                area = _polyset_area_x2(zone.GetFilledPolysList(layer))
            key = f"{zone_uuid}:{layer}"
            snapshot[key] = {
                "zone_uuid": zone_uuid,
                "zone": _zone_label(zone),
                "layer": layer,
                "layer_name": str(board.GetLayerName(layer)),
                "area_iu2_x2": area,
            }
    return snapshot


def compare_fill_area_snapshots(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    changes = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        old_area = 0 if old is None else int(old["area_iu2_x2"])
        new_area = 0 if new is None else int(new["area_iu2_x2"])
        if old is not None and new is not None and old_area == new_area:
            continue
        record = dict(new or old or {})
        record.update({
            "before_area_iu2_x2": old_area,
            "after_area_iu2_x2": new_area,
            "delta_area_iu2_x2": new_area - old_area,
        })
        changes.append(record)
    if changes:
        total_delta = sum(item["delta_area_iu2_x2"] for item in changes)
        details = "; ".join(
            "{} on {}: {:+.9f} mm^2".format(
                item.get("zone") or item.get("zone_uuid"),
                item.get("layer_name") or item.get("layer"),
                item["delta_area_iu2_x2"] / IU2_X2_PER_MM2,
            )
            for item in sorted(
                changes,
                key=lambda item: abs(item["delta_area_iu2_x2"]),
                reverse=True,
            )[:8]
        )
        raise RefillAreaError(
            f"zone fill area changed after temporary refill: {len(changes)} zone-layer(s), "
            f"total {total_delta / IU2_X2_PER_MM2:+.9f} mm^2; {details}"
        )
    return {
        "enabled": True,
        "status": "passed",
        "zone_layer_count": len(before),
        "total_area_iu2_x2": sum(int(item["area_iu2_x2"]) for item in before.values()),
    }


def _input_hashes(path: Path) -> dict[str, str]:
    return {
        "board" if suffix == ".kicad_pcb" else suffix.lstrip("."): file_sha256(
            path.with_suffix(suffix)
        )
        for suffix in _REFILL_SUFFIXES
        if path.with_suffix(suffix).exists()
    }


def verify_refill_areas(path: Path, temporary_parent: Path) -> dict[str, Any]:
    import pcbnew

    input_hashes = _input_hashes(path)
    with tempfile.TemporaryDirectory(
        prefix="refill-area-", dir=str(temporary_parent)
    ) as directory:
        copy_path = Path(directory) / path.name
        for suffix in _REFILL_SUFFIXES:
            source = path.with_suffix(suffix)
            if source.exists():
                shutil.copy2(source, copy_path.with_suffix(suffix))
        if _input_hashes(copy_path) != input_hashes:
            raise RefillAreaError("refill-area inputs changed while creating the copy")
        refill_board = pcbnew.LoadBoard(str(copy_path))
        if refill_board is None:
            raise RefillAreaError(
                "cannot load temporary board copy for refill-area verification"
            )
        before = fill_area_snapshot(refill_board)
        if before:
            filled = pcbnew.ZONE_FILLER(refill_board).Fill(refill_board.Zones())
            if filled is False:
                raise RefillAreaError(
                    "KiCad zone refill failed on temporary board copy"
                )
            after = fill_area_snapshot(refill_board)
        else:
            after = before
    if _input_hashes(path) != input_hashes:
        raise RefillAreaError("refill-area inputs changed during verification")
    result = compare_fill_area_snapshots(before, after)
    result["input_sha256"] = input_hashes
    result["board_sha256"] = input_hashes["board"]
    return result
