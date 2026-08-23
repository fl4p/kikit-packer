import os.path
from pathlib import Path
from typing import Any, cast

from kikit.annotations import TabAnnotation
from kikit.common import KiPoint, shpBBoxBottom, shpBBoxLeft, shpBBoxRight, shpBBoxTop
from kikit.panelize import Origin, Panel, expandRect, findBoardBoundingBox, pcbnew
from kikit.plugin import HookPlugin, LayoutPlugin, TabsPlugin
from kikit.substrate import SubstrateNeighbors
from kikit.units import mm
from shapely.geometry import LineString

from .fingerprint import fingerprints_by_uuid, inventory_board
from .kikit_adapter import (
    append_compatibility,
    assert_authority,
    box_from_bounds,
    inherit_reference_authority_rules,
)
from .packing import legacy_optimal_pack as optimal_pack
from .protocol import (
    atomic_write_json,
    file_sha256,
    load_json,
    resolve_staging_path,
    validate_envelope,
)
from .snapshot import verify_snapshots_from_plan


class FlatEdgeTabs(TabsPlugin):
    """
    Tab generator that only attaches tabs where a board's outline actually
    runs along its bounding-box edge. kikit's 'fixed' generator aims at the
    bbox side midpoint, which for non-rectangular boards (necked probes,
    notched outlines) lands tabs on recessed edges — putting mousebite
    drills deep inside the board. Usage:
        --tabs 'plugin; code: .../kikit-packer.py.FlatEdgeTabs; arg: 2'
    where arg is the tab width in mm (default 2).
    """

    def __init__(self, preset, arg):
        self.preset = preset
        self.width = float(arg) * mm if arg else 2 * mm

    def _flatIntervals(self, s, edge, vertical):
        """Intervals along the bbox edge where the outline actually lies on it."""
        if vertical:
            line = LineString([(edge.x, edge.min), (edge.x, edge.max)])
        else:
            line = LineString([(edge.min, edge.x), (edge.max, edge.x)])
        flat = s.substrates.boundary.intersection(line.buffer(1000))  # 1 um tol
        out = []
        for piece in getattr(flat, 'geoms', [flat]):
            if piece.is_empty or piece.length < self.width:
                continue
            b = piece.bounds
            out.append((b[1], b[3]) if vertical else (b[0], b[2]))
        return out

    def buildTabs(self, panel):
        panel.clearTabsAnnotations()
        subs = panel.substrates
        index = {id(s): k for k, s in enumerate(subs)}
        neighbors = SubstrateNeighbors(subs)
        S = SubstrateNeighbors
        sides = [
            (S.leftC, shpBBoxLeft, [1, 0], shpBBoxRight),
            (S.rightC, shpBBoxRight, [-1, 0], shpBBoxLeft),
            (S.topC, shpBBoxTop, [0, 1], shpBBoxBottom),
            (S.bottomC, shpBBoxBottom, [0, -1], shpBBoxTop),
        ]
        counts = [0] * len(subs)
        connections = []
        for i, s in enumerate(subs):
            for query, side, direction, oppSide in sides:
                for n, shadow in query(neighbors, s):
                    if index[id(n)] < i:
                        continue  # handle each pair once (from the lower index)
                    j = index[id(n)]
                    vertical = direction[0] != 0
                    edge_s = side(s.bounds())
                    edge_n = oppSide(n.bounds())
                    if abs(edge_s.x - edge_n.x) > 4 * self.width:
                        continue
                    # a tab is only placed where BOTH facing edges are flat and
                    # outermost, so both ends get a mousebite cut and no stub
                    # survives depaneling
                    for sec in shadow.intervals:
                        for alo, ahi in self._flatIntervals(s, edge_s, vertical):
                            for blo, bhi in self._flatIntervals(n, edge_n, vertical):
                                lo = max(alo, blo, sec.min)
                                hi = min(ahi, bhi, sec.max)
                                if hi - lo < self.width:
                                    continue
                                mid = (lo + hi) / 2
                                o_s = (edge_s.x, mid) if vertical else (mid, edge_s.x)
                                o_n = (edge_n.x, mid) if vertical else (mid, edge_n.x)
                                s.annotations.append(
                                    TabAnnotation(None, o_s, direction, self.width))
                                n.annotations.append(TabAnnotation(
                                    None, o_n, [-direction[0], -direction[1]], self.width))
                                connections.append({
                                    "left": i,
                                    "right": j,
                                    "start": o_s,
                                    "end": o_n,
                                })
                                counts[i] += 1
                                counts[j] += 1
        print('flat-edge tabs per board:', counts)
        if len(counts) == 1:
            return []
        if any(c == 0 for c in counts):
            raise RuntimeError(
                f"flat-edge tab generator: board(s) {[i for i, c in enumerate(counts) if c == 0]} got no tab (no straight "
                "outermost edge segment faces a neighbor) — the panel would "
                "fall apart. Rearrange the layout or add tabs manually.")
        cuts = panel.buildTabsFromAnnotations(0)
        from shapely.ops import unary_union

        material = unary_union(list(panel.forwardTabs))
        graph = {index: set() for index in range(len(subs))}
        for connection in connections:
            centerline = LineString([connection["start"], connection["end"]])
            if not material.buffer(5000).covers(centerline):
                raise RuntimeError(
                    "flat-edge tab material does not span its intended substrates: "
                    f"start={connection['start']} end={connection['end']}"
                )
            corridor = centerline.buffer(self.width / 2, cap_style="flat").buffer(1000)
            touched = {
                index
                for index, substrate in enumerate(subs)
                if corridor.intersects(substrate.substrates)
            }
            expected = {connection["left"], connection["right"]}
            if touched != expected:
                raise RuntimeError(
                    "flat-edge tab corridor has invalid substrate incidence: "
                    f"expected={sorted(expected)} touched={sorted(touched)} "
                    f"start={connection['start']} end={connection['end']}"
                )
            graph[connection["left"]].add(connection["right"])
            graph[connection["right"]].add(connection["left"])
        setattr(panel, "_kikit_packer_tab_connections", connections)
        visited = set()
        pending = [0]
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            pending.extend(graph[current] - visited)
        if len(visited) != len(subs):
            raise RuntimeError("flat-edge tabs do not connect the panel into one component")
        return cuts


class SuppliedPlanPlugin(LayoutPlugin):
    def _load(self):
        contract_path = Path(self.userArg).resolve()
        contract = load_json(contract_path)
        validate_envelope(contract, "kikit-packer.run-contract")
        root = Path(contract["staging_root"]).resolve()
        plan_path = resolve_staging_path(root, contract["run_plan_path"])
        if file_sha256(plan_path) != contract["run_plan_sha256"]:
            raise RuntimeError("run plan hash mismatch before layout")
        plan = load_json(plan_path)
        validate_envelope(plan, "kikit-packer.run-plan")
        if plan["run_id"] != contract["run_id"] or plan["nonce"] != contract["nonce"]:
            raise RuntimeError("run contract does not match run plan")
        verify_snapshots_from_plan(root, plan)
        return contract, plan, root

    def buildLayout(self, panel: Panel, mainInputFile: str, _sourceArea):
        _, plan, root = self._load()
        panel.sourcePaths.add(mainInputFile)
        source_paths = {
            source["source_id"]: resolve_staging_path(root, source["snapshot_path"])
            for source in plan["sources"]
        }
        def net_renamer(n, orig):
            return self.netPattern.format(n=n, orig=orig)

        def ref_renamer(n, orig):
            return self.refPattern.format(n=n, orig=orig)

        if plan["authority"]["reference_only"]:
            authority_board = pcbnew.LoadBoard(mainInputFile)
            inherit_reference_authority_rules(panel, authority_board)
        instance_records = []
        for instance in plan["instances"]:
            filename = source_paths[instance["source_id"]]
            board = pcbnew.LoadBoard(str(filename))
            if board is None:
                raise RuntimeError(f"pcbnew could not load snapshot {filename}")
            append = instance["append"]
            rotated = int(append["rotation_deg"]) == 90
            before_uuids = set(fingerprints_by_uuid(panel.board))
            with append_compatibility(panel, board):
                panel.appendBoard(
                    filename=str(filename),
                    destination=KiPoint(int(append["destination_iu"][0]), int(append["destination_iu"][1])),
                    origin=Origin.TopRight if rotated else Origin.TopLeft,
                    sourceArea=box_from_bounds(instance["source_area_iu"]),
                    netRenamer=net_renamer,
                    refRenamer=ref_renamer,
                    rotationAngle=cast(Any, self.rotation) + pcbnew.EDA_ANGLE((90 if rotated else 0), pcbnew.DEGREES_T),
                    inheritDrc=False,
                    bakeText=bool(plan["resolved_settings"]["project"]["layout"]["bake_text"]),
                    bakeRef=bool(plan["resolved_settings"]["project"]["layout"]["bake_ref"]),
                )
            after_uuids = set(fingerprints_by_uuid(panel.board))
            instance_records.append({
                "instance_id": instance["instance_id"],
                "output_item_uuids": sorted(after_uuids - before_uuids),
            })
        setattr(panel, "_kikit_packer_instance_records", instance_records)
        assert_authority(panel, plan["authority"])
        return panel.substrates


class RecorderHook(HookPlugin):
    def __init__(self, userArg, board, preset):
        super().__init__(userArg, board, preset)
        self.contract_path = Path(userArg).resolve()
        self.contract = load_json(self.contract_path)
        validate_envelope(self.contract, "kikit-packer.run-contract")
        self.root = Path(self.contract["staging_root"]).resolve()
        self.plan = load_json(resolve_staging_path(self.root, self.contract["run_plan_path"]))
        self.state: dict[str, Any] = {"layout_complete": False, "tabs_complete": False, "cuts_complete": False}
        self.substrates = []

    def afterLayout(self, panel, substrates):
        self.substrates = list(substrates)
        self.state["layout_complete"] = True
        records = {
            record["instance_id"]: record
            for record in getattr(panel, "_kikit_packer_instance_records", [])
        }
        self.state["instances"] = [
            {
                "instance_id": instance["instance_id"],
                "substrate_bounds_pre_page_iu": [int(value) for value in substrate.substrates.bounds],
                "output_item_uuids": records.get(instance["instance_id"], {}).get("output_item_uuids", []),
            }
            for instance, substrate in zip(self.plan["instances"], self.substrates)
        ]
        self.state["after_layout_inventory"] = inventory_board(panel.board)

    def afterTabs(self, panel, tabCuts, backboneCuts):
        from shapely.ops import unary_union

        material = unary_union(list(panel.forwardTabs)) if panel.forwardTabs else None
        components = [] if material is None or material.is_empty else list(getattr(material, "geoms", [material]))
        graph_edges = []
        component_records = []
        for component in components:
            touched = [
                instance["instance_id"]
                for instance, substrate in zip(self.plan["instances"], self.substrates)
                if component.buffer(1).intersects(substrate.substrates)
            ]
            component_records.append({"bounds_iu": [int(value) for value in component.bounds], "instances": touched})
            if len(touched) == 2:
                graph_edges.append(sorted(touched))
        connection_records = []
        connections = getattr(panel, "_kikit_packer_tab_connections", None)
        if connections is not None:
            graph_edges = []
            for connection in connections:
                instances = sorted([
                    self.plan["instances"][connection["left"]]["instance_id"],
                    self.plan["instances"][connection["right"]]["instance_id"],
                ])
                graph_edges.append(instances)
                connection_records.append({
                    "instances": instances,
                    "start_iu": [int(value) for value in connection["start"]],
                    "end_iu": [int(value) for value in connection["end"]],
                })
        self.state["tabs_complete"] = True
        self.state["tabs"] = {
            "polygon_count": len(panel.forwardTabs),
            "material_components": component_records,
            "connections": connection_records,
            "graph_edges": graph_edges,
        }

    def afterCuts(self, panel):
        self.state["cuts_complete"] = True
        self.state["after_cuts_inventory"] = inventory_board(panel.board)

    def finish(self, panel):
        self.state["before_save_inventory"] = inventory_board(panel.board)
        all_uuids = {
            item_uuid
            for instance in self.state.get("instances", [])
            for item_uuid in instance.get("output_item_uuids", [])
        }
        self.state["uuid_fingerprints_before_save"] = {
            item_uuid: fingerprint
            for item_uuid, fingerprint in fingerprints_by_uuid(panel.board).items()
            if item_uuid in all_uuids
        }
        self.state["final_substrate_bounds_iu"] = [
            [int(value) for value in substrate.substrates.bounds] for substrate in self.substrates
        ]
        atomic_write_json(self.root / "plugin-state.json", self.state)


class Plugin(LayoutPlugin):
    def buildLayout(self, panel: Panel, mainInputFile: str, _sourceArea):
        layout = self.preset["layout"]

        input_yaml: str = layout.get("input", "")
        if not input_yaml:
            raise RuntimeError("Specify the yaml input file like this: --layout '...; input: boards.yaml'")

        import yaml
        with open(input_yaml, 'r') as file:
            loaded_yaml = yaml.safe_load(file)
        if not isinstance(loaded_yaml, dict):
            raise TypeError("YAML input must be a mapping")
        yaml_data: dict[str, Any] = loaded_yaml
        if "version" in yaml_data:
            raise RuntimeError(
                "versioned projects must be generated with 'kikit-packer pack'; "
                "raw KiKit invocation cannot enforce project-owned tabs, cuts, and post settings"
            )

        input_boards = yaml_data['boards']
        print(input_boards)

        max_width = yaml_data['max_width'] * mm if 'max_width' in yaml_data else None
        max_height = yaml_data['max_height'] * mm if 'max_height' in yaml_data else None
        print(f"max_height: {max_height}, max_width: {max_width}")

        ignore_thickness = bool(yaml_data.get('ignore_thickness', False))
        ignore_layer_count = bool(yaml_data.get('ignore_layer_count', False))

        panel.sourcePaths.add(mainInputFile)

        def netRenamer(n, orig):
            return self.netPattern.format(n=n, orig=orig)

        def refRenamer(n, orig):
            return self.refPattern.format(n=n, orig=orig)

        # TODO use math.gcd
        S = int(layout.get("eps", 1))  # scale extents for better numerical stability, not sure if necessary
        assert S > 0, "eps must be a positive integer"

        sizes = []
        boards = []
        filenames = []
        source_rects = []
        for d in input_boards:
            filename = d['board']
            float(d.get('rotate', 0))  # accepted legacy field; intentionally has no effect
            count = int(d.get('qty', 1))
            assert count > 0, "Count must be > 0"

            margin = float(d.get('margin_mm', 1)) * mm

            if not os.path.isabs(filename):
                filename = os.path.join(os.path.dirname(input_yaml), filename)

            filename = os.path.realpath(filename)

            if not os.path.isfile(filename):
                raise RuntimeError(f"File '{filename}' does not exist")

            board = pcbnew.LoadBoard(filename)
            if board is None:
                raise RuntimeError(
                    f"pcbnew could not load '{filename}' as a board. "
                    "Make sure 'board:' entries reference .kicad_pcb files "
                    "(not .kicad_pro project files).")

            edge_bbox = findBoardBoundingBox(board)
            # Copper may overhang the board outline (e.g. edge-routed ring-lug
            # pads). Overhang into the routed-away gap is fine — the fab crops
            # it — but it must never reach a neighbor board's substrate, so
            # pack by the full copper extent, not just the outline.
            # Zones are excluded: their fills are clipped at the source board
            # outline, so a sloppy zone outline cannot deposit copper outside.
            full_bbox = pcbnew.BOX2I(edge_bbox.GetPosition(), edge_bbox.GetSize())
            for fp in board.GetFootprints():
                for pad in fp.Pads():
                    full_bbox.Merge(pad.GetBoundingBox())
                for g in fp.GraphicalItems():
                    if pcbnew.IsCopperLayer(g.GetLayer()):
                        full_bbox.Merge(g.GetBoundingBox())
            for t in board.GetTracks():
                full_bbox.Merge(t.GetBoundingBox())
            for d in board.GetDrawings():
                if pcbnew.IsCopperLayer(d.GetLayer()):
                    full_bbox.Merge(d.GetBoundingBox())
            for z in board.Zones():
                zb = z.GetBoundingBox()
                if (zb.GetLeft() < full_bbox.GetLeft() or zb.GetRight() > full_bbox.GetRight()
                        or zb.GetTop() < full_bbox.GetTop() or zb.GetBottom() > full_bbox.GetBottom()):
                    print(f"warning: {os.path.basename(filename)} zone '{z.GetZoneName() or z.GetNetname()}' outline extends beyond the packed extent; "
                          "its stored fill is clipped at the board outline, but do not "
                          "REFILL zones in the panel or it may pour onto neighbors")
            if full_bbox.GetWidth() > edge_bbox.GetWidth() or \
                    full_bbox.GetHeight() > edge_bbox.GetHeight():
                print(f"note: {os.path.basename(filename)} has pads overhanging its outline; packing with "
                      f"the padded extent {full_bbox.GetWidth() / mm:.2f} x {full_bbox.GetHeight() / mm:.2f} mm (outline {edge_bbox.GetWidth() / mm:.2f} x {edge_bbox.GetHeight() / mm:.2f} mm)")

            bbox = expandRect(full_bbox, cast(Any, margin))

            sizes.extend([(
                -(-int(bbox.GetWidth() + self.hspace) // S),
                -(-int(bbox.GetHeight() + self.vspace) // S)
            )] * count)

            boards.extend([board] * count)
            filenames.extend([filename] * count)
            source_rects.extend([full_bbox] * count)

        pack_max_w = -(-int(max_width) // S) if max_width is not None else None
        pack_max_h = -(-int(max_height) // S) if max_height is not None else None
        best_rotates, best_positions = optimal_pack(sizes, max_width=pack_max_w, max_height=pack_max_h)

        print(best_rotates, best_positions)

        thicknesses = [b.GetDesignSettings().GetBoardThickness() for b in boards]
        if len(set(thicknesses)) > 1 and not ignore_thickness:
            details = ", ".join(f"{os.path.basename(f)}: {t / mm} mm"
                                for f, t in zip(filenames, thicknesses))
            raise RuntimeError(
                f"Boards have different thicknesses ({details}). The fab makes the whole "
                "panel from ONE laminate, so they cannot be panelized as-is. "
                "Either align the board thicknesses, or set 'ignore_thickness: true' "
                "in the yaml to merge anyway (all boards will be fabbed at the "
                "panel thickness).")

        layer_counts = [b.GetCopperLayerCount() for b in boards]
        max_layers = max(layer_counts)
        main_layer_count = pcbnew.LoadBoard(mainInputFile).GetCopperLayerCount()
        if ignore_layer_count and main_layer_count != max_layers:
            raise RuntimeError(
                f"the raw KiKit main input must have the maximum placed copper-layer count ({max_layers})"
            )
        if len(set(layer_counts)) > 1 and not ignore_layer_count:
            details = ", ".join(f"{os.path.basename(f)}: {c}"
                                for f, c in zip(filenames, layer_counts))
            raise RuntimeError(
                f"Boards have different copper layer counts ({details}). Set "
                "'ignore_layer_count: true' in the yaml to merge anyway; the "
                f"panel will be a {max_layers}-layer board and the boards with fewer "
                "layers get unused inner layers.")

        if ignore_layer_count:
            # the panel must carry the max count from the start so inner-layer
            # copper of the deeper boards is never dropped
            panel.setCopperLayers(max_layers)

        for i in range(len(boards)):
            if ignore_thickness:
                # defeat kikit's thickness check for this append only
                panel.board.GetDesignSettings().SetBoardThickness(thicknesses[i])
            if ignore_layer_count:
                # defeat the equality check in inheritCopperLayers without
                # touching the panel board's actual (max) layer count
                panel.copperLayerCount = layer_counts[i]
            panel.appendBoard(
                filename=filenames[i],
                destination=KiPoint(int(best_positions[i][0] * S), int(best_positions[i][1] * S)),
                origin=Origin.TopRight if best_rotates[i] else Origin.TopLeft,
                sourceArea=expandRect(source_rects[i], 1 * mm),
                netRenamer=netRenamer,
                refRenamer=refRenamer,
                rotationAngle=cast(Any, self.rotation) + pcbnew.EDA_ANGLE((90 if best_rotates[i] else 0), pcbnew.DEGREES_T),
                inheritDrc=False,
                bakeText=True,
                bakeRef=bool(layout.get("bakeref", False)),
            )

        if ignore_layer_count:
            panel.setCopperLayers(max_layers)
            if len(set(layer_counts)) > 1:
                print("WARNING: merging boards of different copper layer counts — "
                      f"panel is a {max_layers}-layer board:")
                for f, c in zip(filenames, layer_counts):
                    print(f"  {os.path.basename(f)}: {c} layers")

        if ignore_thickness and len(set(thicknesses)) > 1:
            panel_thickness = pcbnew.LoadBoard(mainInputFile).GetDesignSettings().GetBoardThickness()
            panel.board.GetDesignSettings().SetBoardThickness(panel_thickness)
            print("WARNING: merging boards of different thickness — the fab makes "
                  f"the whole panel at ONE thickness. Panel is set to {panel_thickness / mm} mm (from {os.path.basename(mainInputFile)}):")
            for f, t in zip(filenames, thicknesses):
                marker = "" if t == panel_thickness else f"  <-- will be fabbed at {panel_thickness / mm} mm instead"
                print(f"  {os.path.basename(f)}: {t / mm} mm{marker}")

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
