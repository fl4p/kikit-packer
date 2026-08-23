from collections.abc import Iterable
from pathlib import Path

from .diagnostics import Diagnostic, warning
from .model import BoardInspection
from .protocol import file_sha256
from .stackup import parse_stackup


def _box_tuple(box) -> tuple[int, int, int, int]:
    return (box.GetLeft(), box.GetTop(), box.GetRight(), box.GetBottom())


def inspect_board(path: Path, source_id: str) -> BoardInspection:
    import pcbnew
    from kikit.panelize import findBoardBoundingBox

    board = pcbnew.LoadBoard(str(path))
    if board is None:
        raise RuntimeError(f"pcbnew could not load {path}")
    edge_bbox = findBoardBoundingBox(board)
    if edge_bbox.GetWidth() <= 0 or edge_bbox.GetHeight() <= 0:
        raise RuntimeError(f"board has an empty outline: {path}")
    full_bbox = pcbnew.BOX2I(edge_bbox.GetPosition(), edge_bbox.GetSize())
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            full_bbox.Merge(pad.GetBoundingBox())
        for graphic in footprint.GraphicalItems():
            if pcbnew.IsCopperLayer(graphic.GetLayer()):
                full_bbox.Merge(graphic.GetBoundingBox())
    for track in board.GetTracks():
        full_bbox.Merge(track.GetBoundingBox())
    for drawing in board.GetDrawings():
        if pcbnew.IsCopperLayer(drawing.GetLayer()):
            full_bbox.Merge(drawing.GetBoundingBox())
    diagnostics: list[Diagnostic] = []
    for zone in board.Zones():
        bounds = zone.GetBoundingBox()
        if (
            bounds.GetLeft() < full_bbox.GetLeft()
            or bounds.GetRight() > full_bbox.GetRight()
            or bounds.GetTop() < full_bbox.GetTop()
            or bounds.GetBottom() > full_bbox.GetBottom()
        ):
            diagnostics.append(warning(
                "ZONE_OUTSIDE_PACKED_EXTENT",
                f"/sources/{source_id}",
                "zone outline extends beyond the packed copper extent",
                zone=zone.GetZoneName() or zone.GetNetname(),
            ))
    enabled = board.GetEnabledLayers()
    copper_layers = tuple(
        board.GetLayerName(layer)
        for layer in enabled.CuStack()
        if pcbnew.IsCopperLayer(layer)
    )
    stackup = parse_stackup(path)
    has_stackup = bool(getattr(board.GetDesignSettings(), "m_HasStackup", False))
    if has_stackup != stackup["present"] or (stackup["present"] and not stackup["verified"]):
        diagnostics.append(warning(
            "STACKUP_UNVERIFIED",
            f"/sources/{source_id}",
            "explicit stackup could not be fully normalized",
            unknown_keys=stackup.get("unknown_keys", []),
            problems=stackup.get("problems", []),
        ))
    return BoardInspection(
        source_id=source_id,
        path=path,
        sha256=file_sha256(path),
        outline_bounds_iu=_box_tuple(edge_bbox),
        copper_bounds_iu=_box_tuple(full_bbox),
        copper_layers=copper_layers,
        copper_layer_count=board.GetCopperLayerCount(),
        thickness_iu=board.GetDesignSettings().GetBoardThickness(),
        stackup=stackup,
        diagnostics=tuple(diagnostics),
    )


def validate_authority(
    authority: BoardInspection,
    sources: Iterable[BoardInspection],
    allow_mixed_layers: bool,
    allow_mixed_thickness: bool,
) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    authority_layers = set(authority.copper_layers)
    for source in sources:
        source_layers = set(source.copper_layers)
        if not source_layers.issubset(authority_layers):
            raise RuntimeError(f"source {source.path} has copper layers absent from authority")
        if source_layers != authority_layers:
            if not allow_mixed_layers:
                raise RuntimeError(f"source {source.path} layer set differs from authority")
            diagnostics.append(warning(
                "MIXED_LAYER_COUNT",
                f"/sources/{source.source_id}",
                "source uses a strict subset of authority copper layers",
                source_layers=list(source.copper_layers),
                authority_layers=list(authority.copper_layers),
            ))
        if source.thickness_iu != authority.thickness_iu:
            if not allow_mixed_thickness:
                raise RuntimeError(f"source {source.path} thickness differs from authority")
            diagnostics.append(warning(
                "MIXED_THICKNESS",
                f"/sources/{source.source_id}",
                "source thickness will be coerced to authority thickness",
                source_thickness_iu=source.thickness_iu,
                authority_thickness_iu=authority.thickness_iu,
            ))
        a_stack = authority.stackup or {}
        s_stack = source.stackup or {}
        if source.copper_layer_count == authority.copper_layer_count and (a_stack.get("present") or s_stack.get("present")):
            if not a_stack.get("verified") or not s_stack.get("verified"):
                raise RuntimeError(f"explicit stackup comparison is unverified for {source.path}")
            if a_stack.get("descriptor") != s_stack.get("descriptor"):
                raise RuntimeError(f"source {source.path} explicit stackup differs from authority")
    return tuple(diagnostics)
