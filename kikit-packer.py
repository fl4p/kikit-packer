import math
from itertools import chain, combinations, product
from pathlib import Path

import numpy as np
from kikit.annotations import AnnotationReader
from kikit.common import KiPoint, collectEdges, collectFootprints, collectItems, findBoundingBox
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
from kikit.substrate import Substrate
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


def strip_pack(sizes, axis, max_width=None, max_height=None):
    """Place all rectangles in one row (axis=0) or one column (axis=1), aligned at the top/left
    edge. A strip is the simplest layout whose V-cuts never cross another board."""
    positions, cursor = [], 0
    for w, h in sizes:
        positions.append((cursor, 0) if axis == 0 else (0, cursor))
        cursor += w if axis == 0 else h
    extent_w = cursor if axis == 0 else max(w for w, _ in sizes)
    extent_h = cursor if axis == 1 else max(h for _, h in sizes)
    if (max_width is not None and extent_w > max_width) or (max_height is not None and extent_h > max_height):
        raise PackingImpossibleError("strip exceeds max_width/max_height", [])
    return positions


# Board edges that face the frame, per KiKit framing type: (left/right, top/bottom).
FRAMED_SIDES = {
    "none": (False, False),
    "railstb": (False, True),
    "railslr": (True, False),
    "frame": (True, True),
    "tightframe": (True, True),
}
VCUT_TOLERANCE = 1000  # 1 um, in KiCad units (nm)
KIKIT_VCUT_TOLERANCE = 1 * mm  # KiKit only reports cuts deeper than this inside a board


def iter_vcut_conflicts(rects, framed_sides=(False, False), offset=0, strict=True, tol=VCUT_TOLERANCE):
    """
    Predict KiKit's V-cuts for boards with outlines `rects` [(x0, y0, x1, y1)]
    and yield the cut lines that cross a board, as
    (axis, coord, board index, depth inside that board).

    A V-cut runs across the whole panel. KiKit puts one on every board edge that
    gets a tab, moved `offset` into that board (the `offset` of --cuts). An edge
    gets a tab where it faces another board (spans overlapping at all) or a
    frame/rail; `framed_sides` says which edges face the frame for the framing
    type (left/right, top/bottom). With strict=False only the outermost edges
    count as facing the frame, which is how KiKit often but not always tabs.
    A cut crosses a board if it lies more than `tol` inside it, unless it is
    that board's own (offset) cut.
    """
    X0, Y0 = min(r[0] for r in rects), min(r[1] for r in rects)
    X1, Y1 = max(r[2] for r in rects), max(r[3] for r in rects)
    frame_lr, frame_tb = framed_sides
    cuts = {}  # (axis, coord) -> indices of the boards whose edge it is
    for i, (x0, y0, x1, y1) in enumerate(rects):
        left = right = top = bottom = False
        for j, (u0, v0, u1, v1) in enumerate(rects):
            if j == i:
                continue
            if min(y1, v1) > max(y0, v0):  # spans overlap: the boards face each other
                left = left or u1 <= x0 + tol
                right = right or u0 >= x1 - tol
            if min(x1, u1) > max(x0, u0):
                top = top or v1 <= y0 + tol
                bottom = bottom or v0 >= y1 - tol
        if frame_lr:
            left = left or strict or x0 <= X0 + tol
            right = right or strict or x1 >= X1 - tol
        if frame_tb:
            top = top or strict or y0 <= Y0 + tol
            bottom = bottom or strict or y1 >= Y1 - tol
        for edge, axis, coord in ((left, "x", x0 + offset), (right, "x", x1 - offset),
                                  (top, "y", y0 + offset), (bottom, "y", y1 - offset)):
            if edge:
                cuts.setdefault((axis, coord), set()).add(i)
    for (axis, c), owners in sorted(cuts.items()):
        for j, (x0, y0, x1, y1) in enumerate(rects):
            lo, hi = (x0, x1) if axis == "x" else (y0, y1)
            if lo + tol < c < hi - tol and j not in owners:
                yield axis, c, j, min(c - lo, hi - c)


def vcut_conflicts(*args, **kwargs):
    """All of iter_vcut_conflicts() as a list."""
    return list(iter_vcut_conflicts(*args, **kwargs))


def vcuts_clear(*args, **kwargs):
    """True if iter_vcut_conflicts() finds no crossing cut (stops at the first)."""
    return next(iter_vcut_conflicts(*args, **kwargs), None) is None


PACK_MODES = ("free", "row", "column", "vcut")


def optimal_pack(sizes, turned_sizes=None, groups=None, max_width=None, max_height=None,
                 mode="free", accept=(), stats=None):
    """
    sizes[i]: (w, h) of rectangle i in its configured orientation.
    turned_sizes[i]: (w, h) of rectangle i turned by another 90deg, or None if
    it must keep its orientation. Defaults to the swapped size for all.
    groups: lists of interchangeable indices (see rotation_subsets). Defaults to
    one group per rotatable rectangle, i.e. the full powerset.

    mode: "free" (rpack), "row" or "column" (one strip), "vcut" (all three).
    accept: checks in order of preference, each accept(turned flags, positions)
    -> bool. A layout that fits is ranked by the first check it passes and is
    dropped if it passes none; the best layout of the best rank wins. With no
    checks every layout that fits is accepted. stats (a dict), if given, gets
    "fits" (layouts that fit) and "rank" (rank of the result, or None).

    Returns (turned flags, positions); both are empty if nothing fits. Among
    equally dense packings the one that turns the least board area away from
    the configured orientation wins.
    """
    if turned_sizes is None:
        turned_sizes = [(h, w) for (w, h) in sizes]
    if groups is None:
        groups = [[i] for i in range(len(sizes)) if turned_sizes[i] is not None]
    assert all(turned_sizes[i] is not None for g in groups for i in g)

    free = lambda sz: pack(sz, max_width=max_width, max_height=max_height)
    row = lambda sz: strip_pack(sz, 0, max_width, max_height)
    column = lambda sz: strip_pack(sz, 1, max_width, max_height)
    packers = {"free": [free], "row": [row], "column": [column], "vcut": [free, row, column]}[mode]

    areas = np.array([w * h for w, h in sizes])
    checks = list(accept) or [None]
    best = [None] * len(checks)  # per rank: (density, rotated_area, rotate, positions)
    fits = 0
    for turned in rotation_subsets(groups):
        sizes_with_rotations = [
            (turned_sizes[i] if i in turned else sizes[i]) for i in range(len(sizes))
        ]

        rotate = [(i in turned) for i in range(len(sizes))]
        for packer in packers:
            try:
                positions = packer(sizes_with_rotations)
            except PackingImpossibleError:
                continue
            fits += 1
            density = packing_density(sizes_with_rotations, positions)
            assert density <= 1.0, "unexpected packing density > 1"
            rotated_area = sum(np.array(rotate) * areas)
            for rank, check in enumerate(checks):
                if any(b is not None for b in best[:rank]):
                    break  # a better rank already has a layout
                b = best[rank]
                # only check layouts that would beat the best one of this rank
                if b is not None and not (density > b[0] or (
                        density / b[0] > (1 - 1e-9) and rotated_area < b[1])):
                    continue
                if check is None or check(rotate, positions):
                    # prefer topologies with less rotational area
                    best[rank] = (density, rotated_area, rotate, positions)
                    break

    rank = next((r for r, b in enumerate(best) if b is not None), None)
    if stats is not None:
        stats.update(fits=fits, rank=rank)
    if rank is None:
        return (), ()
    return best[rank][2], best[rank][3]


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


def rotate_point(point, center, angle):
    """Rotate like KiCad's RotatePoint: positive angles turn counter-clockwise on screen (y down)."""
    a = angle.AsRadians()
    dx, dy = point.x - center.x, point.y - center.y
    return (
        center.x + round(dx * math.cos(a) + dy * math.sin(a)),
        center.y + round(-dx * math.sin(a) + dy * math.cos(a)),
    )


def rotated_outline(board, area, angle):
    """
    The board's Edge.Cuts items inside `area`, rotated by `angle` about the
    centre of `area` (the transform appendBoard applies with Origin.Center),
    and the widest edge line. Edges are matched against `area` grown by the
    widest edge line only, so outlines of neighbouring boards in a multi-board
    source are not picked up.
    """
    edges = collectEdges(board, Layer.Edge_Cuts)
    widest = max((e.GetWidth() for e in edges), default=0)
    center = getOriginCoord(Origin.Center, area)
    rotated_edges = []
    for edge in collectEdges(board, Layer.Edge_Cuts, expandRect(area, widest)):
        edge = edge.Duplicate()
        edge.Rotate(center, angle)
        rotated_edges.append(edge)
    return rotated_edges, widest


def rotated_bbox(board, area, angle, tolerance):
    """
    Bounding box of everything appendBoard extracts from `area` after rotating
    it by `angle` about the centre of `area` (the transform appendBoard applies
    with Origin.Center).

    Besides the outline this covers footprints, tracks and drawings that
    stick out of it, e.g. edge connectors, so the packer reserves room for
    them. Edges are matched against `area` grown by the widest edge line only,
    so outlines of neighbouring boards in a multi-board source are not picked
    up. Zones are left out: they are usually drawn larger than the board and
    clipped when filled. Footprint text and KiKit annotations are left out too.
    """
    rotated_edges, widest = rotated_outline(board, area, angle)
    center = getOriginCoord(Origin.Center, area)
    bbox = findBoundingBox(rotated_edges)

    # The same selection appendBoard makes.
    enlarged = expandRect(area, tolerance + widest)
    annotations = AnnotationReader.getDefault()
    boxes = [
        fp.GetBoundingBox(False, False)
        for fp in collectFootprints(board.GetFootprints(), enlarged)
        if not annotations.isAnnotation(fp)
    ]
    boxes += [t.GetBoundingBox() for t in collectItems(board.GetTracks(), enlarged)]
    boxes += [
        d.GetBoundingBox()
        for d in collectItems(board.GetDrawings(), enlarged)
        if d.GetLayer() != Layer.Edge_Cuts
    ]
    for box in boxes:
        # Rotating the corners of an item's box gives a box that covers the
        # rotated item (exact for multiples of 90deg).
        corners = [
            pcbnew.VECTOR2I(x, y)
            for x in (box.GetLeft(), box.GetRight())
            for y in (box.GetTop(), box.GetBottom())
        ]
        xs, ys = [], []
        for c in corners:
            x, y = rotate_point(c, center, angle)
            xs.append(x)
            ys.append(y)
        bbox.Merge(
            pcbnew.BOX2I(
                pcbnew.VECTOR2I(min(xs), min(ys)),
                pcbnew.VECTOR2L(max(xs) - min(xs), max(ys) - min(ys)),
            )
        )
    return bbox


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
            # The list format accepted (and ignored) `rotate` before it was a
            # setting; keep the optimizer free to turn these boards so old
            # configs keep packing as before.
            if "rotate" in cfg:
                cfg.setdefault("allow_rotation", True)
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


def framing_overlaps(outlines, extents, framing):
    """
    Find parts that stick out of a board into the frame or rails.

    KiKit places the framing from the board outlines and the framing settings,
    ignoring parts that stick out of an outline: rails and frames start
    `vspace`/`hspace` beyond the outlines' common bounding box, and a tight
    frame also fills everything farther than `slotwidth` from an outline.
    outlines[i] and extents[i] are (x0, y0, x1, y1) of board i's outline and
    of everything extracted with it, as placed. Yields (board index, side,
    [(setting, overlap)]) for every side whose parts reach into the framing,
    naming the setting that would have to grow by `overlap`.
    """
    kind = framing.get("type", "none")
    if kind not in ("railstb", "railslr", "frame", "tightframe") or not outlines:
        return
    X0 = min(o[0] for o in outlines)
    Y0 = min(o[1] for o in outlines)
    X1 = max(o[2] for o in outlines)
    Y1 = max(o[3] for o in outlines)
    hspace, vspace = framing.get("hspace", 0), framing.get("vspace", 0)
    lr = kind in ("railslr", "frame", "tightframe")
    tb = kind in ("railstb", "frame", "tightframe")
    slot = framing.get("slotwidth", 0)
    for i, ((ox0, oy0, ox1, oy1), (ex0, ey0, ex1, ey1)) in enumerate(zip(outlines, extents)):
        sides = (
            ("left", lr, "hspace", (X0 - hspace) - ex0, (ox0 - slot) - ex0),
            ("top", tb, "vspace", (Y0 - vspace) - ey0, (oy0 - slot) - ey0),
            ("right", lr, "hspace", ex1 - (X1 + hspace), ex1 - (ox1 + slot)),
            ("bottom", tb, "vspace", ey1 - (Y1 + vspace), ey1 - (oy1 + slot)),
        )
        for side, faces, space, frame_overlap, slot_overlap in sides:
            if not faces:
                continue
            found = []
            if frame_overlap > 1000:  # more than 1 um
                found.append((space, frame_overlap))
            if kind == "tightframe" and slot_overlap > 1000:
                found.append(("slotwidth", slot_overlap))
            if found:
                yield i, side, found


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

        # A V-cut is a straight line across the whole panel, so a free packing
        # usually scores through neighbouring boards (issue #1). With --cuts
        # vcuts, only accept layouts whose cut lines miss every board.
        cuts = self.preset.get("cuts", {})
        vcuts = cuts.get("type") == "vcuts"
        mode = config_data.get("pack", "vcut" if vcuts else "free")
        if mode not in PACK_MODES:
            raise RuntimeError(f"pack: '{mode}' unknown, use one of {', '.join(PACK_MODES)}")
        outlines = []  # per copy, per variant: board outline relative to the variant's bbox

        main_board = None
        annotated = []  # (name, area, tolerance) of boards taken from the main file
        # per copy: (filename, sourceArea, tolerance, variants); variants[0] is
        # the configured orientation, variants[1] (if allowed) is turned by
        # another 90deg. A variant is (angle, rotated board bbox, margin).
        placements = []
        names = []
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
                variants.append((angle, rotated_bbox(source_board, area, angle, tolerance), margin))
                if vcuts and round(angle.AsDegrees(), 6) % 90 != 0:
                    raise RuntimeError(
                        f"{what}: rotated by {angle.AsDegrees():g} deg; V-cuts need board edges"
                        " at multiples of 90 deg. Use --cuts mousebites or a rotate of 0/90/180/270.")
            if vcuts:
                # V-cuts lie on the outline, which may sit inside the bbox when
                # parts stick out of the board.
                outline_offsets = []
                for angle, bbox, _ in variants:
                    x0, y0, x1, y1 = Substrate(rotated_outline(source_board, area, angle)[0]).bounds()
                    outline_offsets.append((x0 - bbox.GetX(), y0 - bbox.GetY(),
                                            x1 - bbox.GetX(), y1 - bbox.GetY()))
                outlines.extend([outline_offsets] * count)

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
            names.extend([board_name] * count)
            if filename == mainInputFile:
                annotated.append((board_name, area, tolerance))

        if annotated:
            warn_overlapping_extraction(main_board, annotated)

        # Round the limits down (sizes are rounded up) so the panel never exceeds them.
        pack_max_w = int(max_width) // S if max_width is not None else None
        pack_max_h = int(max_height) // S if max_height is not None else None

        framing = self.preset.get("framing", {}).get("type", "none")
        framed_sides = FRAMED_SIDES.get(framing, (True, True))  # a framing plugin: assume all
        offset = int(cuts.get("offset", 0) or 0)
        if vcuts and offset > KIKIT_VCUT_TOLERANCE:
            print(f"kikit-packer: the V-cut offset ({offset / mm:g} mm) puts each cut more than 1 mm"
                  " into its own board; KiKit reports every such cut as an error.")

        def board_rects(rotate, positions):
            # appendBoard puts the rotated bbox `margin` inside its packed cell
            # (same as the placement below); the outline sits at its offset in it.
            rects = []
            for i, ((x, y), turned) in enumerate(zip(positions, rotate)):
                v = 1 if turned else 0
                margin = placements[i][3][v][2]
                ox0, oy0, ox1, oy1 = outlines[i][v]
                x0, y0 = x * S + margin, y * S + margin
                rects.append((x0 + ox0, y0 + oy0, x0 + ox1, y0 + oy1))
            return rects

        checks = []
        if vcuts:
            checks.append(lambda r, p: vcuts_clear(board_rects(r, p), framed_sides, offset))
            if any(framed_sides):
                # KiKit often leaves an edge facing the frame across open space
                # untabbed. If no layout passes the strict check, accept one
                # that passes when only the outermost edges are tabbed to the
                # frame, as long as every cut the strict check predicts is deep
                # enough for KiKit's own check to report it if KiKit does tab
                # that edge (KiKit ignores cuts up to 1 mm inside a board).
                def loose(r, p):
                    rects = board_rects(r, p)
                    return vcuts_clear(rects, framed_sides, offset, strict=False) and all(
                        c[3] > KIKIT_VCUT_TOLERANCE + VCUT_TOLERANCE
                        for c in iter_vcut_conflicts(rects, framed_sides, offset))
                checks.append(loose)
        stats = {}
        best_rotates, best_positions = optimal_pack(
            sizes, turned_sizes, groups, max_width=pack_max_w, max_height=pack_max_h,
            mode=mode, accept=checks, stats=stats,
        )
        if len(best_positions) != len(sizes):
            if vcuts and stats["fits"]:
                raise RuntimeError(
                    "No layout found whose V-cuts miss every board (a V-cut spans the whole"
                    " panel). Use --cuts mousebites, or relax max_width/max_height.")
            raise RuntimeError(
                "The boards do not fit into max_width x max_height "
                "(bare numbers are millimetres)"
            )
        if stats["rank"] == 1:
            print(f"kikit-packer: no layout passes the V-cut check with framing '{framing}';"
                  " using one that assumes only the outermost edges are tabbed to the frame."
                  " This relies on KiKit's own V-cut check: if a cut still crosses a board,"
                  " KiKit reports it, marks it in the output board and exits with code 1.")

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

        # Parts sticking out of a board on the outside of the panel can reach
        # into the frame or rails, which KiKit places from the outlines only.
        framing = self.preset.get("framing", {})
        outlines = [sub.bounds() for sub in panel.substrates]
        extents = []
        for i, (_, _, _, variants) in enumerate(placements):
            _, bbox, margin = variants[1 if best_rotates[i] else 0]
            x0 = int(best_positions[i][0] * S) + margin
            y0 = int(best_positions[i][1] * S) + margin
            extents.append((x0, y0, x0 + bbox.GetWidth(), y0 + bbox.GetHeight()))
        for i, side, found in framing_overlaps(outlines, extents, framing):
            copy = names[: i + 1].count(names[i])
            overlap = max(o for _, o in found)
            fixes = [
                f"{setting}: {math.ceil((framing.get(setting, 0) + o) / mm * 100) / 100:g}mm"
                for setting, o in found
            ]
            print(
                f"Warning: board '{names[i]}' (copy {copy}) has parts {overlap / mm:.3g} mm into the "
                f"{framing['type']} on its {side} side; KiKit places the framing from the board "
                f"outlines only. To clear it, set at least {' and '.join(fixes)} in --framing."
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
