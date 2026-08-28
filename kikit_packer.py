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
from kikit.panelize_ui_impl import encodePreset
from kikit.plugin import LayoutPlugin
from kikit.units import readLength
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

class Plugin(LayoutPlugin):
    def buildLayout(self, panel: Panel, mainInputFile: str, _sourceArea):
        if not getattr(self, 'userArg', None):
            raise RuntimeError("layout.arg is required but not provided or is empty - set it to the packer config file")
        board = pcbnew.LoadBoard(mainInputFile)

        layout = self.preset["layout"]
        config_file = self.userArg

        import yaml

        with open(config_file, "r") as file:
            config_data = yaml.safe_load(file)

        max_width = config_data.get("max_width")
        max_height = config_data.get("max_height")

        # upstream only supports when using annotations - not for single board extractions
        # omit it for now since - in this situation - we can't present a consistent api to users
        # see: https://github.com/yaqwsx/KiKit/blob/d1957c535781371db5a2f507f8154d460095f9f1/kikit/panelize_ui_impl.py#L187-L193
        # layer = config_data.get("layer", Layer.Edge_Cuts)
        layer = Layer.Edge_Cuts

        if max_width is not None:
            max_width = readLength(max_width)
            print(f"Panel width: {max_width}")
        if max_height is not None:
            max_height = readLength(max_height)
            print(f"Panel height: {max_height}")

        sourceAreas = []
        sizes = []
        filenames = []

        # TODO use math.gcd
        S = int(
            layout.get("eps", 1)
        )  # scale extents for better numerical stability, not sure if necessary
        assert S > 0, "eps must be a positive integer"

        for board_ref, board_config in config_data.get("boards", {}).items():
            margin = readLength(board_config.get("margin", "1mm"))
            count = board_config.get("qty", 1)

            if "filename" in board_config:
                filename = resolve_path(board_config["filename"], config_file)
                file_board = pcbnew.LoadBoard(filename)
                bbox = expandRect(findBoardBoundingBox(file_board), margin)
                filenames.extend([filename] * count)
                print(f"Extracted board {board_ref} from {filename.relative_to(Path.cwd())}; count: {count}")
            else:
                bbox = expandRect(
                    extractSourceAreaByAnnotation(board, board_ref, layer), margin
                )
                filenames.extend([mainInputFile] * count)
                print(f"Extracted board reference {board_ref}; count: {count}")

            sizes.extend(
                [
                    (
                        -(-int(bbox.GetWidth() + self.hspace) // S),
                        -(-int(bbox.GetHeight() + self.vspace) // S),
                    )
                ]
                * count
            )
            sourceAreas.extend([bbox] * count)

        pack_max_w = -(-int(max_width) // S) if max_width is not None else None
        pack_max_h = -(-int(max_height) // S) if max_height is not None else None
        best_rotates, best_positions = optimal_pack(
            sizes, max_width=pack_max_w, max_height=pack_max_h
        )

        panel.sourcePaths.add(mainInputFile)

        netRenamer = lambda n, orig: self.netPattern.format(n=n, orig=orig)
        refRenamer = lambda n, orig: self.refPattern.format(n=n, orig=orig)

        for i in range(len(sourceAreas)):
            panel.appendBoard(
                filename=filenames[i],
                destination=KiPoint(
                    int(best_positions[i][0] * S), int(best_positions[i][1] * S)
                ),
                origin=Origin.TopRight if best_rotates[i] else Origin.TopLeft,
                sourceArea=sourceAreas[i],
                netRenamer=netRenamer,
                refRenamer=refRenamer,
                rotationAngle=self.rotation
                + pcbnew.EDA_ANGLE((90 if best_rotates[i] else 0), pcbnew.DEGREES_T),
                inheritDrc=False,
                bakeText=True,
            )

        # print(best_rotates, best_positions)


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
