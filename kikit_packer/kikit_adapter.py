from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


def box_from_bounds(bounds):
    import pcbnew

    left, top, right, bottom = (int(value) for value in bounds)
    return pcbnew.BOX2I(
        pcbnew.VECTOR2I(left, top),
        pcbnew.VECTOR2I(right - left, bottom - top),
    )


@contextmanager
def append_compatibility(panel, source_board) -> Iterator[None]:
    authority_thickness = panel.board.GetDesignSettings().GetBoardThickness()
    authority_layers = panel.board.GetCopperLayerCount()
    source_thickness = source_board.GetDesignSettings().GetBoardThickness()
    source_layers = source_board.GetCopperLayerCount()
    panel.board.GetDesignSettings().SetBoardThickness(source_thickness)
    panel.copperLayerCount = source_layers
    try:
        yield
    finally:
        panel.board.GetDesignSettings().SetBoardThickness(authority_thickness)
        panel.copperLayerCount = authority_layers
        panel.setCopperLayers(authority_layers)


def inherit_reference_authority_rules(panel, authority_board) -> None:
    def identity(value):
        return value

    panel._inheritNetClasses(authority_board, identity)
    panel._inheriCustomDrcRules(authority_board, identity)


def assert_authority(panel, expected: dict[str, Any]) -> None:
    actual_layers = panel.board.GetCopperLayerCount()
    actual_thickness = panel.board.GetDesignSettings().GetBoardThickness()
    if actual_layers != int(expected["copper_layer_count"]):
        raise RuntimeError("panel copper-layer count changed during append")
    if actual_thickness != int(expected["thickness_iu"]):
        raise RuntimeError("panel thickness changed during append")
