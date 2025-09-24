import os.path
from itertools import chain, combinations

import numpy as np
from kikit.common import KiPoint
from kikit.panelize import Panel, expandRect, findBoardBoundingBox, pcbnew, Origin
from kikit.plugin import LayoutPlugin
from kikit.units import mm
from rpack import pack, packing_density, PackingImpossibleError


def powerset(iterable):
    "powerset([1,2,3]) --> () (1,) (2,) (3,) (1,2) (1,3) (2,3) (1,2,3)"
    s = list(iterable)
    return chain.from_iterable(combinations(s, r) for r in range(len(s) + 1))


def optimal_pack(sizes, max_width=None, max_height=None):
    best_rotate, best_positions, best_density = (), (), -1
    best_rotated_area = 0
    for rotated_sizes_indices in powerset(range(len(sizes))):
        sizes_with_rotations = [((height, width) if i in rotated_sizes_indices else (width, height))
                                for (i, (width, height)) in enumerate(sizes)]

        try:
            positions = pack(sizes_with_rotations, max_width=max_width, max_height=max_height)
        except PackingImpossibleError:
            continue
        density = packing_density(sizes_with_rotations, positions)
        assert density <= 1.0, "unexpected packing density > 1"
        rotate = [(i in rotated_sizes_indices) for (i, _) in enumerate(sizes_with_rotations)]
        rotated_area = sum(np.array(rotate) * np.array(list(w * h for w, h in sizes)))

        # prefer topologies with less rotational area
        if density > best_density or (density / best_density > (1 - 1e-9) and rotated_area < best_rotated_area):
            best_rotate, best_positions, best_density = rotate, positions, density
            best_rotated_area = rotated_area
            # print('best', best_rotate, best_positions, best_density)

    return best_rotate, best_positions


class Plugin(LayoutPlugin):
    def buildLayout(self, panel: Panel, mainInputFile: str, _sourceArea):
        layout = self.preset["layout"]

        input_yaml: str = layout.get("input", "")
        if not input_yaml:
            raise RuntimeError("Specify the yaml input file like this: --layout '...; input: boards.yaml'")

        import yaml
        with open(input_yaml, 'r') as file:
            input_boards = yaml.safe_load(file)
        print(input_boards)

        panel.sourcePaths.add(mainInputFile)

        netRenamer = lambda n, orig: self.netPattern.format(n=n, orig=orig)
        refRenamer = lambda n, orig: self.refPattern.format(n=n, orig=orig)

        S = int(layout.get("eps", 1))  # scale extents for better numerical stability, not sure if necessary
        assert S > 0, "eps must be a positive integer"

        sizes = []
        boards = []
        filenames = []
        for d in input_boards:
            filename = d['board']
            rotate_deg = float(d.get('rotate', 0))  # pre-rotate TODO
            count = int(d.get('qty', 1))
            assert count > 0, "Count must be > 0"

            margin = float(d.get('margin_mm', 1)) * mm

            if not os.path.isabs(filename):
                filename = os.path.join(os.path.dirname(input_yaml), filename)

            filename = os.path.realpath(filename)

            if not os.path.isfile(filename):
                raise RuntimeError("File '{}' does not exist".format(filename))

            board = pcbnew.LoadBoard(filename)

            bbox = expandRect(findBoardBoundingBox(board), margin)

            assert (bbox.GetWidth() + self.hspace) % S == 0, (
                f"Board width+hspace ({bbox.GetWidth()}+{self.hspace}) is not multiple of {S}")
            assert (bbox.GetHeight() + self.vspace) % S == 0, (
                f"Board height+vspace ({bbox.GetHeight()}+{self.vspace}) is not multiple of {S}")

            sizes.extend([(
                int((bbox.GetWidth() + self.hspace) / S),
                int((bbox.GetHeight() + self.vspace) / S)
            )] * count)

            boards.extend([board] * count)
            filenames.extend([filename] * count)

        best_rotates, best_positions = optimal_pack(sizes, max_width=None, max_height=None)

        print(best_rotates, best_positions)

        for i in range(len(boards)):
            panel.appendBoard(
                filename=filenames[i],
                destination=KiPoint(int(best_positions[i][0] * S), int(best_positions[i][1] * S)),
                origin=Origin.TopRight if best_rotates[i] else Origin.TopLeft,
                sourceArea=expandRect(findBoardBoundingBox(boards[i]), 1 * mm),
                netRenamer=netRenamer,
                refRenamer=refRenamer,
                rotationAngle=self.rotation + pcbnew.EDA_ANGLE((90 if best_rotates[i] else 0), pcbnew.DEGREES_T),
                inheritDrc=False,
            )

        print('Done.')

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
