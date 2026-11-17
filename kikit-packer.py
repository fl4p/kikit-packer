from itertools import chain, combinations, product
from pathlib import Path

import numpy as np
from kikit.common import KiPoint, collectEdges, findBoundingBox
from kikit.defs import Layer
from kikit.panelize import (
    Origin,
    Panel,
    expandRect,
    extractSourceAreaByAnnotation,
    findBoardBoundingBox,
    getOriginCoord,
    pcbnew,
)
from kikit.plugin import LayoutPlugin
from kikit.units import mm, readAngle, readLength
from rpack import PackingImpossibleError, pack, packing_density


def powerset(iterable):
    "powerset([1,2,3]) --> () (1,) (2,) (3,) (1,2) (1,3) (2,3) (1,2,3)"
    s = list(iterable)
    return chain.from_iterable(combinations(s, r) for r in range(len(s) + 1))


def rotation_subsets(groups):
    """
    Yield every set of rectangle indices the optimizer may turn by 90deg.

    `groups` lists the indices of interchangeable rectangles (the copies of one
    board entry); only boards that allow rotation take part. The copies in a
    group are identical, so only the number of turned copies matters: a group
    of qty q contributes q+1 choices instead of 2^q.
    """
    for counts in product(*(range(len(g) + 1) for g in groups)):
        yield frozenset(chain.from_iterable(g[:k] for g, k in zip(groups, counts)))


def optimal_pack(sizes, turned_sizes=None, groups=None, max_width=None, max_height=None):
    """
    sizes[i]: (w, h) of rectangle i in its configured orientation.
    turned_sizes[i]: (w, h) of rectangle i turned by another 90deg, or None if
    it must keep its orientation. Defaults to the swapped size for all.
    groups: lists of interchangeable indices (see rotation_subsets). Defaults to
    one group per rotatable rectangle, i.e. the full powerset.

    Returns (turned flags, positions); both are empty if nothing fits. Among
    equally dense packings the one that turns the least board area away from
    the configured orientation wins.
    """
    if turned_sizes is None:
        turned_sizes = [(h, w) for (w, h) in sizes]
    if groups is None:
        groups = [[i] for i in range(len(sizes)) if turned_sizes[i] is not None]
    assert all(turned_sizes[i] is not None for g in groups for i in g)

    areas = np.array([w * h for w, h in sizes])
    best_rotate, best_positions, best_density = (), (), -1
    best_rotated_area = 0
    for turned in rotation_subsets(groups):
        sizes_with_rotations = [
            (turned_sizes[i] if i in turned else sizes[i]) for i in range(len(sizes))
        ]

        try:
            positions = pack(
                sizes_with_rotations, max_width=max_width, max_height=max_height
            )
        except PackingImpossibleError:
            continue
        density = packing_density(sizes_with_rotations, positions)
        assert density <= 1.0, "unexpected packing density > 1"
        rotate = [(i in turned) for i in range(len(sizes))]
        rotated_area = sum(np.array(rotate) * areas)

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


def read_length(value, what, positive=False):
    """
    Read a length: a KiKit unit string ("2mm", "0.5in") or a bare number in mm.
    Negative lengths are refused; with positive=True zero is refused too.
    """
    if isinstance(value, bool):
        raise RuntimeError(f"{what}: expected a length, got '{value}'")
    if isinstance(value, (int, float)):
        length = int(round(value * mm))
    elif isinstance(value, str):
        length = int(readLength(value))
    else:
        raise RuntimeError(f"{what}: expected a length like '2mm' or a number in mm, got '{value}'")
    if length < 0 or (positive and length == 0):
        raise RuntimeError(f"{what}: must be {'greater than zero' if positive else 'zero or more'}, got '{value}'")
    return length


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


def read_rotation(board_config, what):
    """
    Return (rotate, allow_rotation) for a board.

    rotate: a KiKit angle string ("90deg", "0.5rad") or a bare number in
    degrees; default 0. Counter-clockwise, as in KiCad.
    allow_rotation: whether the optimizer may turn the board by another 90deg.
    Defaults to true, but to false when `rotate` is given, so `rotate` on its
    own fixes the angle.
    """
    value = board_config.get("rotate", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RuntimeError(f"{what} rotate: expected an angle like '90deg' or a number in degrees, got '{value}'")
    if isinstance(value, str):
        rotate = pcbnew.EDA_ANGLE(readAngle(value).AsDegrees(), pcbnew.DEGREES_T)
    else:
        rotate = pcbnew.EDA_ANGLE(float(value), pcbnew.DEGREES_T)
    allow_rotation = board_config.get("allow_rotation", "rotate" not in board_config)
    if not isinstance(allow_rotation, bool):
        raise RuntimeError(f"{what} allow_rotation: expected true or false, got '{allow_rotation}'")
    return rotate, allow_rotation


def rotated_bbox(board, area, angle):
    """
    Bounding box of the board outline inside `area` after rotating it by
    `angle` about the centre of `area`, the transform appendBoard applies with
    Origin.Center. Edges are matched against `area` grown by the widest edge
    line only, so outlines of neighbouring boards in a multi-board source are
    not picked up.
    """
    edges = collectEdges(board, Layer.Edge_Cuts)
    widest = max((e.GetWidth() for e in edges), default=0)
    center = getOriginCoord(Origin.Center, area)
    rotated = []
    for edge in collectEdges(board, Layer.Edge_Cuts, expandRect(area, widest)):
        edge = edge.Duplicate()
        edge.Rotate(center, angle)
        rotated.append(edge)
    return findBoundingBox(rotated)


def normalize_boards(boards):
    """
    Return the board list as (name, config) pairs.

    Accepts the current format, a mapping of board name to config, and the
    legacy format, a list of entries with a `board:` file path.
    """
    if isinstance(boards, dict):
        items = []
        for name, cfg in boards.items():
            if cfg is not None and not isinstance(cfg, dict):
                raise RuntimeError(
                    f"board '{name}': expected settings like 'qty: 2', got '{cfg}'"
                )
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


def is_board_annotation(footprint):
    fpid = footprint.GetFPID()
    return (
        str(fpid.GetLibNickname()).lower() in ("kikit", "pcm_kikit")
        and str(fpid.GetLibItemName()).lower() == "board"
    )


def warn_overlapping_extraction(board, annotated):
    """
    KiKit extracts everything within `tolerance` of a board's outline. In a
    multi-board file, a neighbour closer than that contributes its footprints
    and tracks to the board being extracted. Warn about such neighbours.
    """
    areas = {}
    for fp in board.GetFootprints():
        if is_board_annotation(fp):
            ref = fp.GetReference()
            try:
                areas[ref] = extractSourceAreaByAnnotation(board, ref, Layer.Edge_Cuts)
            except RuntimeError:
                pass  # an annotation not pointing at an outline is not our concern here
    for name, area, tolerance in annotated:
        band = expandRect(area, tolerance)
        for other, other_area in areas.items():
            if other != name and band.Intersects(other_area):
                print(
                    f"Warning: board '{other}' is closer than tolerance "
                    f"({tolerance / mm:g} mm) to board '{name}'; its footprints and "
                    f"tracks near the gap may be copied into '{name}'. Increase the "
                    f"gap or lower '{name}'s tolerance."
                )


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
            max_width = read_length(max_width, "max_width", positive=True)
            print(f"Panel max width: {max_width / mm:g} mm")
        if max_height is not None:
            max_height = read_length(max_height, "max_height", positive=True)
            print(f"Panel max height: {max_height / mm:g} mm")

        boards = normalize_boards(config_data.get("boards"))
        if not boards:
            raise RuntimeError(f"No boards listed in '{config_file}'")

        # TODO use math.gcd
        # scale extents for better numerical stability, not sure if necessary
        S = int(layout.get("eps", 1))
        assert S > 0, "eps must be a positive integer"

        main_board = None
        annotated = []  # (name, area, tolerance) of boards taken from the main file
        # per copy: (filename, sourceArea, tolerance, variants); variants[0] is
        # the configured orientation, variants[1] (if allowed) is turned by
        # another 90deg. A variant is (angle, rotated board bbox, margin).
        placements = []
        sizes, turned_sizes, groups = [], [], []

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
            rotate, allow_rotation = read_rotation(board_config, what)

            if "filename" in board_config:
                filename = resolve_path(board_config["filename"], config_file)
                source_board = pcbnew.LoadBoard(str(filename))
                area = findBoardBoundingBox(source_board)
                print(f"Extracted board {board_name} from {filename}; count: {count}")
            else:
                if main_board is None:
                    main_board = pcbnew.LoadBoard(mainInputFile)
                source_board = main_board
                area = extractSourceAreaByAnnotation(main_board, board_name, layer)
                filename = mainInputFile
                print(f"Extracted board reference {board_name}; count: {count}")

            # The margin only reserves space around the board in the packing; the
            # extracted area stays tight so that neighbouring boards in a
            # multi-board source file are not pulled in. The cell is the
            # rotated board's bounding box grown by the margin.
            variants = []
            for turn in (0, 90) if allow_rotation else (0,):
                angle = self.rotation + rotate + pcbnew.EDA_ANGLE(turn, pcbnew.DEGREES_T)
                variants.append((angle, rotated_bbox(source_board, area, angle), margin))

            def pack_size(variant):
                cell = expandRect(variant[1], variant[2])
                return (
                    -(-int(cell.GetWidth() + self.hspace) // S),
                    -(-int(cell.GetHeight() + self.vspace) // S),
                )

            first = len(placements)
            sizes.extend([pack_size(variants[0])] * count)
            turned_sizes.extend([pack_size(variants[1]) if allow_rotation else None] * count)
            if allow_rotation:
                groups.append(list(range(first, first + count)))
            placements.extend([(filename, area, tolerance, variants)] * count)
            if filename == mainInputFile:
                annotated.append((board_name, area, tolerance))

        if annotated:
            warn_overlapping_extraction(main_board, annotated)

        # Round the limits down (sizes are rounded up) so the panel never exceeds them.
        pack_max_w = int(max_width) // S if max_width is not None else None
        pack_max_h = int(max_height) // S if max_height is not None else None
        best_rotates, best_positions = optimal_pack(
            sizes, turned_sizes, groups, max_width=pack_max_w, max_height=pack_max_h
        )
        if len(best_positions) != len(sizes):
            raise RuntimeError(
                "The boards do not fit into max_width x max_height "
                "(bare numbers are millimetres)"
            )

        panel.sourcePaths.add(mainInputFile)

        netRenamer = lambda n, orig: self.netPattern.format(n=n, orig=orig)
        refRenamer = lambda n, orig: self.refPattern.format(n=n, orig=orig)

        for i, (filename, area, tolerance, variants) in enumerate(placements):
            angle, bbox, margin = variants[1 if best_rotates[i] else 0]
            # Rotate about the centre of the source area, then shift so that the
            # rotated board's bounding box sits `margin` inside its packed cell.
            center = getOriginCoord(Origin.Center, area)
            panel.appendBoard(
                filename=filename,
                destination=KiPoint(
                    int(best_positions[i][0] * S) + margin + center[0] - bbox.GetX(),
                    int(best_positions[i][1] * S) + margin + center[1] - bbox.GetY(),
                ),
                origin=Origin.Center,
                sourceArea=area,
                tolerance=tolerance,
                netRenamer=netRenamer,
                refRenamer=refRenamer,
                rotationAngle=angle,
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
