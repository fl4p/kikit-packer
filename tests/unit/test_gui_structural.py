import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

pytest.importorskip("wx")

from kikit_packer.gui.board_table import BoardDropTarget, BoardTable
from kikit_packer.gui.frame import MainFrame
from kikit_packer.gui.preview import Preview, fit_transform, preview_instances, preview_world_bounds
from kikit_packer.gui.view_model import State, ViewModel


class View:
    def __init__(self):
        self.rows = [["a.kicad_pcb", "1", "1"], ["b.kicad_pcb", "1", "1"]]
        self.selected = 0

    def GetSelectedRow(self):
        return self.selected

    def DeleteItem(self, row):
        self.rows.pop(row)

    def AppendItem(self, row):
        self.rows.append(row[:3])

    def SelectRow(self, row):
        self.selected = row


class Table:
    def __init__(self):
        self.view = View()
        self.changes = 0

    def notify_change(self):
        self.changes += 1

    def rows(self):
        return [list(row) for row in self.view.rows]

    def set_rows(self, rows):
        self.view.rows = rows


def test_remove_move_and_drop_each_notify_once():
    table = Table()
    BoardTable._remove(cast(Any, table), None)
    assert table.changes == 1
    table.view.rows = [["a", "1", "1"], ["b", "1", "1"]]
    table.view.selected = 0
    BoardTable._move(cast(Any, table), 1)
    assert table.changes == 2
    assert table.view.rows[0][0] == "b"
    target = SimpleNamespace(table=table)
    assert BoardDropTarget.OnDropFiles(cast(Any, target), 0, 0, ["ignored.txt", "c.kicad_pcb"])
    assert table.changes == 3


def test_default_gui_project_data_requires_refill_verification(tmp_path: Path):
    board = tmp_path / "board.kicad_pcb"
    board.write_text("board")

    class Control:
        def __init__(self, value):
            self.value = value

        def GetValue(self):
            return self.value

        def GetPath(self):
            return self.value

        def GetStringSelection(self):
            return self.value

    frame = SimpleNamespace(
        boards=SimpleNamespace(rows=lambda: [[str(board), "1", "1"]]),
        authority=Control(str(board)),
        reference_only=Control(False),
        output=Control(str(tmp_path / "panel.kicad_pcb")),
        max_width=Control(""),
        max_height=Control(""),
        loaded_layout={},
        tab_mode=Control("flat-edge"),
        tab_width=Control(2.0),
        tab_hcount=Control(1),
        tab_vcount=Control(1),
        tab_min_distance=Control(0.0),
        cut_mode=Control("none"),
        cut_drill=Control(0.5),
        cut_spacing=Control(0.8),
        cut_offset=Control(0.0),
        cut_prolong=Control(0.0),
        mill_radius=Control(0.0),
        allow_mixed_layers=Control(False),
        allow_mixed_thickness=Control(False),
    )
    frame._number_or_none = lambda control: MainFrame._number_or_none(cast(Any, frame), control)
    data = MainFrame._project_data(cast(Any, frame))
    assert data["panel"]["post"]["verify_refill_areas"] is True


def test_buffered_preview_enables_paint_background_style():
    source = inspect.getsource(Preview.__init__)
    assert "SetBackgroundStyle(wx.BG_STYLE_PAINT)" in source


def test_preview_uses_transformed_substrate_geometry():
    plan = {
        "packing": {"bounds_iu": [0, 0, 1100, 2120]},
        "instances": [{
            "instance_id": "board-1",
            "source_area_iu": [0, 0, 100, 80],
            "outline_bounds_iu": [10, 20, 90, 70],
            "append": {"destination_iu": [1000, 2000]},
            "packing_rotation_deg": 90,
            "packing_size_iu": [120, 100],
            "expected_inventory": {"substrates": {"90": [{
                "outline": [[0, 0], [50, 0], [50, 80], [0, 80]],
                "holes": [[[10, 10], [20, 10], [20, 20], [10, 20]]],
            }]}},
        }],
    }
    instances = preview_instances(plan)
    assert instances[0]["substrate_bounds"] == [1010, 2010, 1060, 2090]
    assert instances[0]["packing_bounds"] == [1000, 2000, 1100, 2120]
    assert instances[0]["polygons"][0]["outline"][0] == [1010, 2010]
    assert instances[0]["polygons"][0]["holes"][0][0] == [1020, 2020]
    assert preview_world_bounds(plan, instances) == [0, 0, 1100, 2120]


def test_preview_fit_centers_world_geometry():
    assert fit_transform([0, 0, 100, 50], 240, 140, padding=20) == (2.0, 20.0, 20.0)


def test_metadata_columns_do_not_invalidate_prepared_plan():
    class Event:
        def __init__(self, column):
            self.column = column
            self.skipped = False

        def GetColumn(self):
            return self.column

        def Skip(self):
            self.skipped = True

    frame = SimpleNamespace(dirty_count=0)
    frame._mark_dirty = lambda: setattr(frame, "dirty_count", frame.dirty_count + 1)
    frame._on_dirty = lambda event: MainFrame._on_dirty(cast(Any, frame), event)
    metadata_event = Event(3)
    MainFrame._on_board_value_changed(cast(Any, frame), metadata_event)
    assert frame.dirty_count == 0
    assert metadata_event.skipped is True
    editable_event = Event(1)
    MainFrame._on_board_value_changed(cast(Any, frame), editable_event)
    assert frame.dirty_count == 1
    assert editable_event.skipped is True


def test_structural_change_discards_prepared_plan(tmp_path: Path):
    prepared_root = tmp_path / "prepared"
    prepared_root.mkdir()
    model = ViewModel()
    model.state = State.PLANNED
    model.plan = {"instances": []}
    frame = SimpleNamespace(
        model=model,
        prepared=(prepared_root, {}, {}, {}),
        preview=SimpleNamespace(set_plan=lambda value: setattr(frame, "preview_value", value)),
        status=SimpleNamespace(SetLabel=lambda value: setattr(frame, "status_value", value)),
    )
    frame._discard_prepared = lambda: MainFrame._discard_prepared(cast(Any, frame))
    MainFrame._mark_dirty(cast(Any, frame))
    assert model.state == State.DIRTY
    assert model.plan is None
    assert frame.prepared is None
    assert not prepared_root.exists()
    assert frame.preview_value is None
