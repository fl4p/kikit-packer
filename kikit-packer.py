from itertools import chain, combinations
from pathlib import Path

import numpy as np
from kikit.common import KiPoint
from kikit.defs import Layer
from kikit.panelize import (
    Origin,
    Panel,
    expandRect,
    extractSourceAreaByAnnotation,
    findBoardBoundingBox,
    pcbnew,
)
from kikit.plugin import LayoutPlugin
from kikit.units import mm, readLength
from rpack import PackingImpossibleError, pack, packing_density


def powerset(iterable):
    "powerset([1,2,3]) --> () (1,) (2,) (3,) (1,2) (1,3) (2,3) (1,2,3)"
    s = list(iterable)
    return chain.from_iterable(combinations(s, r) for r in range(len(s) + 1))


def optimal_pack(sizes, max_width=None, max_height=None):
    best_rotate, best_positions, best_density = (), (), -1
    best_rotated_area = 0
    for rotated_sizes_indices in powerset(range(len(sizes))):
        sizes_with_rotations = [
            ((height, width) if i in rotated_sizes_indices else (width, height))
            for (i, (width, height)) in enumerate(sizes)
        ]

        try:
            positions = pack(
                sizes_with_rotations, max_width=max_width, max_height=max_height
            )
        except PackingImpossibleError:
            continue
        density = packing_density(sizes_with_rotations, positions)
        assert density <= 1.0, "unexpected packing density > 1"
        rotate = [
            (i in rotated_sizes_indices) for (i, _) in enumerate(sizes_with_rotations)
        ]
        rotated_area = sum(np.array(rotate) * np.array(list(w * h for w, h in sizes)))

        # prefer topologies with less rotational area
        if density > best_density or (
            density / best_density > (1 - 1e-9) and rotated_area < best_rotated_area
        ):
            best_rotate, best_positions, best_density = rotate, positions, density
            best_rotated_area = rotated_area
            # print('best', best_rotate, best_positions, best_density)

    return best_rotate, best_positions


def resolve_path(filename, reference_file):
    path = Path(filename)
    if not path.is_absolute():
        path = Path(reference_file).parent / path
    path = path.resolve()
    if not path.is_file():
        raise RuntimeError("File '{}' does not exist".format(path))
    return path


def read_length(value, what):
    """Read a length: a KiKit unit string ("2mm", "0.5in") or a bare number in mm."""
    if isinstance(value, bool):
        raise RuntimeError(f"{what}: expected a length, got '{value}'")
    if isinstance(value, (int, float)):
        return int(round(value * mm))
    if isinstance(value, str):
        return int(readLength(value))
    raise RuntimeError(f"{what}: expected a length like '2mm' or a number in mm, got '{value}'")


def read_qty(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RuntimeError(f"{what}: qty must be a positive integer, got '{value}'")
    try:
        qty = int(value)
    except ValueError:
        raise RuntimeError(f"{what}: qty must be a positive integer, got '{value}'")
    if qty <= 0:
        raise RuntimeError(f"{what}: qty must be a positive integer, got '{value}'")
    return qty


def normalize_boards(boards):
    """
    Return the board list as (name, config) pairs.

    Accepts the current format, a mapping of board name to config, and the
    legacy format, a list of entries with a `board:` file path.
    """
    if isinstance(boards, dict):
        items = []
        for name, cfg in boards.items():
            cfg = dict(cfg or {})
            if "board" in cfg and "filename" not in cfg:
                cfg["filename"] = cfg.pop("board")
            items.append((str(name), cfg))
        return items
    if isinstance(boards, list):
        items = []
        for i, cfg in enumerate(boards):
            if not isinstance(cfg, dict) or "board" not in cfg:
                raise RuntimeError(f"boards[{i}]: legacy list entries need a 'board:' file path")
            cfg = dict(cfg)
            cfg["filename"] = cfg.pop("board")
            items.append((Path(cfg["filename"]).stem, cfg))
        return items
    raise RuntimeError("'boards' must be a mapping of board names to settings")


class Plugin(LayoutPlugin):
    def buildLayout(self, panel: Panel, mainInputFile: str, _sourceArea):
        layout = self.preset["layout"]

        # `arg:` is KiKit's standard plugin argument; `input:` is the legacy name.
        config_file = getattr(self, "userArg", None) or layout.get("input", "")
        if not config_file:
            raise RuntimeError(
                "Specify the packer config file like this: --layout 'plugin; code: ...; arg: boards.yaml'"
            )

        import yaml

        with open(config_file, "r") as file:
            config_data = yaml.safe_load(file) or {}

        # The layer for annotation-based extraction is fixed to Edge.Cuts: upstream
        # KiKit only honours a custom layer for annotations, not for whole-board
        # extraction, so a config option could not behave consistently.
        # see: https://github.com/yaqwsx/KiKit/blob/d1957c535781371db5a2f507f8154d460095f9f1/kikit/panelize_ui_impl.py#L187-L193
        layer = Layer.Edge_Cuts

        max_width = config_data.get("max_width")
        max_height = config_data.get("max_height")
        if max_width is not None:
            max_width = read_length(max_width, "max_width")
            print(f"Panel max width: {max_width / mm:g} mm")
        if max_height is not None:
            max_height = read_length(max_height, "max_height")
            print(f"Panel max height: {max_height / mm:g} mm")

        boards = normalize_boards(config_data.get("boards"))
        if not boards:
            raise RuntimeError(f"No boards listed in '{config_file}'")

        # TODO use math.gcd
        # scale extents for better numerical stability, not sure if necessary
        S = int(layout.get("eps", 1))
        assert S > 0, "eps must be a positive integer"

        main_board = None
        placements = []  # (filename, sourceArea, margin, tolerance) per copy
        sizes = []

        for board_name, board_config in boards:
            what = f"board '{board_name}'"
            count = read_qty(board_config.get("qty", 1), what)
            if "margin" in board_config:
                margin = read_length(board_config["margin"], f"{what} margin")
            elif "margin_mm" in board_config:  # legacy key
                margin = read_length(float(board_config["margin_mm"]), f"{what} margin_mm")
            else:
                margin = 1 * mm
            tolerance = read_length(board_config.get("tolerance", "1mm"), f"{what} tolerance")

            if "filename" in board_config:
                filename = resolve_path(board_config["filename"], config_file)
                area = findBoardBoundingBox(pcbnew.LoadBoard(str(filename)))
                print(f"Extracted board {board_name} from {filename}; count: {count}")
            else:
                if main_board is None:
                    main_board = pcbnew.LoadBoard(mainInputFile)
                area = extractSourceAreaByAnnotation(main_board, board_name, layer)
                filename = mainInputFile
                print(f"Extracted board reference {board_name}; count: {count}")

            # The margin only reserves space around the board in the packing; the
            # extracted area stays tight so that neighbouring boards in a
            # multi-board source file are not pulled in.
            cell = expandRect(area, margin)
            sizes.extend(
                [
                    (
                        -(-int(cell.GetWidth() + self.hspace) // S),
                        -(-int(cell.GetHeight() + self.vspace) // S),
                    )
                ]
                * count
            )
            placements.extend([(filename, area, margin, tolerance)] * count)

        pack_max_w = -(-int(max_width) // S) if max_width is not None else None
        pack_max_h = -(-int(max_height) // S) if max_height is not None else None
        best_rotates, best_positions = optimal_pack(
            sizes, max_width=pack_max_w, max_height=pack_max_h
        )
        if len(best_positions) != len(sizes):
            raise RuntimeError(
                "The boards do not fit into max_width x max_height "
                "(bare numbers are millimetres)"
            )

        panel.sourcePaths.add(mainInputFile)

        netRenamer = lambda n, orig: self.netPattern.format(n=n, orig=orig)
        refRenamer = lambda n, orig: self.refPattern.format(n=n, orig=orig)

        for i, (filename, area, margin, tolerance) in enumerate(placements):
            # The margin is the same on all sides, so the offset of the board
            # inside its cell does not depend on the rotation.
            panel.appendBoard(
                filename=filename,
                destination=KiPoint(
                    int(best_positions[i][0] * S) + margin,
                    int(best_positions[i][1] * S) + margin,
                ),
                origin=Origin.TopRight if best_rotates[i] else Origin.TopLeft,
                sourceArea=area,
                tolerance=tolerance,
                netRenamer=netRenamer,
                refRenamer=refRenamer,
                rotationAngle=self.rotation
                + pcbnew.EDA_ANGLE((90 if best_rotates[i] else 0), pcbnew.DEGREES_T),
                inheritDrc=False,
                bakeText=True,
            )

        return panel.substrates


"""

names:
pcb-pack
boardpack
pcbpack

2d rectangle packing problem
https://github.com/Penlect/rectangle-packer
with rotation: https://github.com/Penlect/rectangle-packer/issues/17

TODO optimal packaing
https://stackoverflow.com/questions/1213394/what-algorithm-can-be-used-for-packing-rectangles-of-different-sizes-into-the-sm
https://www.csc.liv.ac.uk/~epa/surveyhtml.html


"""
