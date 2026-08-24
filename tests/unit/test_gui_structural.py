import inspect
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

pytest.importorskip("wx")

from kikit_packer.gui.board_table import BoardDropTarget, BoardTable
from kikit_packer.gui.frame import MainFrame
from kikit_packer.gui.preview import (
    Preview,
    fit_transform,
    preview_instances,
    preview_world_bounds,
    substrate_rings,
)
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


def test_nested_multipolygon_is_a_single_even_odd_compound():
    instance = {
        "polygons": [
            {
                "outline": [[0, 0], [100, 0], [100, 100], [0, 100]],
                "holes": [[[20, 20], [80, 20], [80, 80], [20, 80]]],
            },
            {
                "outline": [[40, 40], [60, 40], [60, 60], [40, 60]],
                "holes": [],
            },
        ],
    }
    rings = substrate_rings(instance)
    assert len(rings) == 3

    def contains(ring, x, y):
        inside = False
        previous = ring[-1]
        for current in ring:
            if (current[1] > y) != (previous[1] > y):
                crossing = (previous[0] - current[0]) * (y - current[1]) / (previous[1] - current[1]) + current[0]
                if x < crossing:
                    inside = not inside
            previous = current
        return inside

    assert sum(contains(ring, 50, 50) for ring in rings) % 2 == 1
    assert sum(contains(ring, 30, 30) for ring in rings) % 2 == 0


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


@pytest.mark.parametrize(
    "state",
    [State.VALIDATING, State.GENERATING, State.VERIFYING, State.PROMOTING],
)
def test_close_waits_for_busy_worker_cleanup(state):
    class Event:
        def __init__(self):
            self.vetoed = False

        def Veto(self):
            self.vetoed = True

    cancel = threading.Event()
    frame = SimpleNamespace(
        model=SimpleNamespace(busy=True, state=state),
        close_pending=False,
        cancel_event=cancel,
        status=SimpleNamespace(SetLabel=lambda value: setattr(frame, "status_value", value)),
    )
    event = Event()
    MainFrame._on_close(cast(Any, frame), event)
    assert event.vetoed is True
    assert frame.close_pending is True
    assert cancel.is_set()
    assert frame.status_value == "Cancelling safely before close..."


def test_pending_close_destroys_only_after_prepared_cleanup(tmp_path: Path):
    prepared_root = tmp_path / "prepared"
    prepared_root.mkdir()
    frame = SimpleNamespace(
        worker_thread=object(),
        close_pending=True,
        prepared=(prepared_root, {}, {}, {}),
        destroyed=False,
        Destroy=lambda: setattr(frame, "destroyed", True),
    )
    frame._discard_prepared = lambda: MainFrame._discard_prepared(cast(Any, frame))
    assert MainFrame._finish_worker(cast(Any, frame)) is True
    assert frame.worker_thread is None
    assert frame.destroyed is True
    assert not prepared_root.exists()


def test_temporary_project_is_removed_when_serialization_fails(tmp_path: Path, monkeypatch):
    temporary = tmp_path / "project.yaml"

    def named_temporary(*_args, **_kwargs):
        return temporary.open("w", encoding="utf-8")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", named_temporary)
    monkeypatch.setattr("kikit_packer.gui.frame.yaml.safe_dump", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("dump failed")))
    frame = SimpleNamespace(_project_data=lambda: {})
    with pytest.raises(RuntimeError, match="dump failed"):
        MainFrame._temporary_project(cast(Any, frame))
    assert not temporary.exists()


def test_failed_revalidation_discards_previous_plan_and_temp_file(tmp_path: Path, monkeypatch):
    prepared_root = tmp_path / "prepared"
    prepared_root.mkdir()
    project_path = tmp_path / "temporary.yaml"
    project_path.write_text("invalid")
    model = ViewModel()
    model.state = State.PLANNED
    model.plan = {"instances": []}
    frame = SimpleNamespace(
        model=model,
        prepared=(prepared_root, {}, {}, {}),
        preview=SimpleNamespace(set_plan=lambda value: setattr(frame, "preview_value", value)),
        _temporary_project=lambda: project_path,
    )
    frame._discard_prepared = lambda: MainFrame._discard_prepared(cast(Any, frame))
    monkeypatch.setattr("kikit_packer.config.load_project", lambda _path: (_ for _ in ()).throw(RuntimeError("invalid project")))
    monkeypatch.setattr("kikit_packer.gui.frame.wx.MessageBox", lambda *_args, **_kwargs: None)
    MainFrame._on_validate(cast(Any, frame), None)
    assert model.state == State.DIRTY
    assert model.plan is None
    assert frame.prepared is None
    assert frame.preview_value is None
    assert not prepared_root.exists()
    assert not project_path.exists()


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
