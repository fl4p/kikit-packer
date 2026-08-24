from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path
from typing import cast

import wx
import wx.dataview as dv
import yaml

from .board_table import BoardTable
from .events import EventCursor, read_events
from .preview import Preview
from .view_model import State, ViewModel


class MainFrame(wx.Frame):
    def __init__(self, parent, project=None):
        super().__init__(parent, title="KiKit Packer", size=(1400, 900))
        self.SetMinSize((1050, 700))
        self.model = ViewModel(project_path=project)
        self.cancel_event = None
        self.worker_thread = None
        self.close_pending = False
        self.last_output = None
        self.prepared: tuple | None = None
        self.saved_revision = 0
        self.loaded_layout = {
            "horizontal_spacing_mm": 0,
            "vertical_spacing_mm": 0,
            "rotation_deg": 0,
            "rename_net": "Board_{n}-{orig}",
            "rename_ref": "{orig}",
            "bake_text": True,
            "bake_ref": False,
        }
        self._build()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        if project is not None:
            self.load_project(project)

    def _build(self):
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.HORIZONTAL)
        preview_column = wx.BoxSizer(wx.VERTICAL)
        controls = wx.BoxSizer(wx.VERTICAL)
        root.Add(preview_column, 3, wx.EXPAND)
        root.Add(controls, 2, wx.EXPAND)
        toolbar = wx.BoxSizer(wx.HORIZONTAL)
        self.project_buttons = []
        for label, handler in (("Load", self._on_load), ("Save As", self._on_save)):
            button = wx.Button(panel, label=label)
            self.project_buttons.append(button)
            button.Bind(wx.EVT_BUTTON, handler)
            toolbar.Add(button, 0, wx.RIGHT, 5)
        controls.Add(toolbar, 0, wx.ALL, 8)

        self.boards = BoardTable(panel, self._mark_dirty)
        controls.Add(wx.StaticText(panel, label="Boards"), 0, wx.LEFT | wx.RIGHT, 8)
        controls.Add(self.boards, 1, wx.EXPAND | wx.ALL, 8)

        settings = wx.FlexGridSizer(cols=4, hgap=8, vgap=6)
        settings.AddGrowableCol(1, 1)
        settings.AddGrowableCol(3, 1)
        self.authority = wx.FilePickerCtrl(panel, wildcard="KiCad boards (*.kicad_pcb)|*.kicad_pcb")
        self.reference_only = wx.CheckBox(panel, label="Reference only")
        self.output = wx.FilePickerCtrl(panel, wildcard="KiCad boards (*.kicad_pcb)|*.kicad_pcb", style=wx.FLP_SAVE | wx.FLP_USE_TEXTCTRL)
        self.max_width = wx.TextCtrl(panel)
        self.max_height = wx.TextCtrl(panel)
        self.tab_mode = wx.Choice(panel, choices=["flat-edge", "fixed"])
        self.tab_mode.SetSelection(0)
        self.tab_width = wx.SpinCtrlDouble(panel, min=0.1, max=100, initial=2, inc=0.1)
        self.tab_hcount = wx.SpinCtrl(panel, min=1, max=100, initial=1)
        self.tab_vcount = wx.SpinCtrl(panel, min=1, max=100, initial=1)
        self.tab_min_distance = wx.SpinCtrlDouble(panel, min=0, max=1000, initial=0, inc=0.1)
        self.cut_mode = wx.Choice(panel, choices=["none", "mousebites"])
        self.cut_mode.SetSelection(0)
        self.cut_drill = wx.SpinCtrlDouble(panel, min=0.01, max=10, initial=0.5, inc=0.05)
        self.cut_spacing = wx.SpinCtrlDouble(panel, min=0.01, max=10, initial=0.8, inc=0.05)
        self.cut_offset = wx.SpinCtrlDouble(panel, min=0, max=10, initial=0, inc=0.05)
        self.cut_prolong = wx.SpinCtrlDouble(panel, min=0, max=10, initial=0, inc=0.05)
        self.mill_radius = wx.SpinCtrlDouble(panel, min=0, max=100, initial=1, inc=0.1)
        self.allow_mixed_layers = wx.CheckBox(panel, label="Allow mixed layer subsets")
        self.allow_mixed_thickness = wx.CheckBox(panel, label="Allow mixed thickness")
        pairs = [
            ("Authority", self.authority, "", self.reference_only),
            ("Output", self.output, "", wx.StaticText(panel, label="")),
            ("Max width mm", self.max_width, "Max height mm", self.max_height),
            ("Tab mode", self.tab_mode, "Tab width mm", self.tab_width),
            ("Horizontal tabs", self.tab_hcount, "Vertical tabs", self.tab_vcount),
            ("Tab minimum distance mm", self.tab_min_distance, "Cut mode", self.cut_mode),
            ("Mousebite drill mm", self.cut_drill, "Mousebite spacing mm", self.cut_spacing),
            ("Mousebite offset mm", self.cut_offset, "Mousebite prolong mm", self.cut_prolong),
            ("Mill radius mm", self.mill_radius, "", wx.StaticText(panel, label="")),
            ("", self.allow_mixed_layers, "", self.allow_mixed_thickness),
        ]
        for left_label, left, right_label, right in pairs:
            settings.Add(wx.StaticText(panel, label=left_label), 0, wx.ALIGN_CENTER_VERTICAL)
            settings.Add(left, 1, wx.EXPAND)
            settings.Add(wx.StaticText(panel, label=right_label), 0, wx.ALIGN_CENTER_VERTICAL)
            settings.Add(right, 1, wx.EXPAND)
        controls.Add(settings, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.preview = Preview(panel)
        preview_column.Add(wx.StaticText(panel, label="Validated panel preview"), 0, wx.ALL, 8)
        preview_column.Add(self.preview, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.validate_button = wx.Button(panel, label="Validate & Preview")
        self.generate_button = wx.Button(panel, label="Generate")
        self.cancel_button = wx.Button(panel, label="Cancel")
        self.open_button = wx.Button(panel, label="Open in KiCad")
        self.cancel_button.Disable()
        self.open_button.Disable()
        for button, handler in (
            (self.validate_button, self._on_validate),
            (self.generate_button, self._on_generate),
            (self.cancel_button, self._on_cancel),
            (self.open_button, self._on_open),
        ):
            button.Bind(wx.EVT_BUTTON, handler)
            actions.Add(button, 0, wx.RIGHT, 6)
        controls.Add(actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.status = wx.StaticText(panel, label="Ready")
        controls.Add(self.status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.logs = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY)
        controls.Add(self.logs, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        panel.SetSizer(root)
        self._editable_controls = [
            self.authority,
            self.reference_only,
            self.output,
            self.max_width,
            self.max_height,
            self.tab_mode,
            self.tab_width,
            self.tab_hcount,
            self.tab_vcount,
            self.tab_min_distance,
            self.cut_mode,
            self.cut_drill,
            self.cut_spacing,
            self.cut_offset,
            self.cut_prolong,
            self.mill_radius,
            self.allow_mixed_layers,
            self.allow_mixed_thickness,
        ]
        for control in (self.authority, self.output):
            control.Bind(wx.EVT_FILEPICKER_CHANGED, self._on_dirty)
        for control in (
            self.reference_only,
            self.allow_mixed_layers,
            self.allow_mixed_thickness,
        ):
            control.Bind(wx.EVT_CHECKBOX, self._on_dirty)
        for control in (self.tab_mode, self.cut_mode):
            control.Bind(wx.EVT_CHOICE, self._on_dirty)
        for control in (self.max_width, self.max_height):
            control.Bind(wx.EVT_TEXT, self._on_dirty)
        for control in (
            self.tab_width,
            self.tab_min_distance,
            self.cut_drill,
            self.cut_spacing,
            self.cut_offset,
            self.cut_prolong,
            self.mill_radius,
        ):
            control.Bind(wx.EVT_SPINCTRLDOUBLE, self._on_dirty)
        for control in (self.tab_hcount, self.tab_vcount):
            control.Bind(wx.EVT_SPINCTRL, self._on_dirty)
        self.boards.view.Bind(dv.EVT_DATAVIEW_ITEM_VALUE_CHANGED, self._on_board_value_changed)

    def _discard_prepared(self):
        if self.prepared is not None:
            root, _, _, _ = self.prepared
            shutil.rmtree(root, ignore_errors=True)
            self.prepared = None

    def _mark_dirty(self):
        if not self.model.busy:
            self._discard_prepared()
            self.model.dirty()
            self.preview.set_plan(None)
            self.status.SetLabel("Project changed; validate again")

    def _on_dirty(self, event):
        self._mark_dirty()
        event.Skip()

    def _on_board_value_changed(self, event):
        if event.GetColumn() >= 3:
            event.Skip()
            return
        self._on_dirty(event)

    def _on_close(self, event):
        if self.model.busy:
            event.Veto()
            if not self.close_pending:
                self.close_pending = True
                if self.cancel_event is not None:
                    self.cancel_event.set()
                self.status.SetLabel("Cancelling safely before close...")
            return
        if (
            self.model.revision != self.saved_revision
            and wx.MessageBox("Discard unsaved project changes?", "Unsaved changes", wx.YES_NO | wx.ICON_WARNING)
            != wx.YES
        ):
            event.Veto()
            return
        self._discard_prepared()
        event.Skip()

    def _finish_worker(self):
        self.worker_thread = None
        if not self.close_pending:
            return False
        self._discard_prepared()
        self.close_pending = False
        self.Destroy()
        return True

    def _append_log(self, message):
        self.logs.AppendText(str(message).rstrip() + "\n")

    def _run_event(self, token, event):
        if token != self.model.generation_token:
            return
        stage = event["stage"]
        action = event["event"]
        self.status.SetLabel(f"{stage.capitalize()}: {action}")
        self._append_log(f"[{event['sequence']}] {stage}: {action}")

    def _monitor_events(self, token, root, contract, stop_event):
        cursor = EventCursor()
        path = root / contract["events_path"]
        max_bytes = contract["log_limits"].get("events_bytes", 1_048_576)
        while True:
            try:
                cursor, events = read_events(
                    path,
                    cursor,
                    run_id=contract["run_id"],
                    nonce=contract["nonce"],
                    max_bytes=max_bytes,
                )
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._append_log, f"Event stream error: {exc}")
                return
            for event in events:
                wx.CallAfter(self._run_event, token, event)
            if stop_event.wait(0.05):
                return

    def _set_busy(self, busy):
        self.validate_button.Enable(not busy)
        self.generate_button.Enable(not busy)
        self.cancel_button.Enable(busy)
        self.boards.Enable(not busy)
        for control in self._editable_controls + self.project_buttons:
            control.Enable(not busy)

    def _number_or_none(self, control):
        text = control.GetValue().strip()
        return None if not text else float(text)

    def _project_data(self, destination_directory=None):
        def serialized(path):
            resolved = Path(path).expanduser().resolve()
            if destination_directory is None:
                return str(resolved)
            try:
                return os.path.relpath(str(resolved), str(destination_directory))
            except ValueError:
                return str(resolved)

        rows = []
        for path, qty, margin in self.boards.rows():
            rows.append({"board": serialized(path), "qty": int(qty), "margin_mm": float(margin)})
        tab_mode = self.tab_mode.GetStringSelection()
        return {
            "version": 1,
            "panel": {
                "authority": {
                    "board": serialized(self.authority.GetPath()),
                    "reference_only": self.reference_only.GetValue(),
                },
                "output": serialized(self.output.GetPath()),
                "max_width_mm": self._number_or_none(self.max_width),
                "max_height_mm": self._number_or_none(self.max_height),
                "layout": dict(self.loaded_layout),
                "tabs": {
                    "mode": tab_mode,
                    "width_mm": self.tab_width.GetValue(),
                    "horizontal_count": self.tab_hcount.GetValue(),
                    "vertical_count": self.tab_vcount.GetValue(),
                    "min_distance_mm": self.tab_min_distance.GetValue(),
                },
                "cuts": {
                    "mode": self.cut_mode.GetStringSelection(),
                    "drill_mm": self.cut_drill.GetValue(),
                    "spacing_mm": self.cut_spacing.GetValue(),
                    "offset_mm": self.cut_offset.GetValue(),
                    "prolong_mm": self.cut_prolong.GetValue(),
                },
                "post": {
                    "mill_radius_mm": self.mill_radius.GetValue(),
                    "origin": "top-left",
                    "refill_zones": False,
                    "verify_refill_areas": True,
                },
                "page": {"mode": "inherit"},
                "allow_mixed_layers": self.allow_mixed_layers.GetValue(),
                "allow_mixed_thickness": self.allow_mixed_thickness.GetValue(),
            },
            "boards": rows,
        }

    def _temporary_project(self):
        path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                suffix=".yaml",
                delete=False,
                encoding="utf-8",
            ) as handle:
                path = Path(handle.name)
                yaml.safe_dump(self._project_data(), handle, sort_keys=False)
            return path
        except BaseException:
            if path is not None:
                path.unlink(missing_ok=True)
            raise

    def load_project(self, path):
        from ..config import load_project

        project = load_project(Path(path))
        self._discard_prepared()
        self.preview.set_plan(None)
        self.last_output = None
        self.open_button.Disable()
        if self.model.state != State.DIRTY:
            self.model.dirty()
        self.model.project_path = Path(path).resolve()
        self.boards.set_rows([[str(board.board), board.qty, board.margin_mm] for board in project.boards])
        if project.panel.authority is not None:
            self.authority.SetPath(str(project.panel.authority.board))
            self.reference_only.SetValue(project.panel.authority.reference_only)
        if project.panel.output is not None:
            self.output.SetPath(str(project.panel.output))
        self.max_width.SetValue("" if project.panel.max_width_mm is None else str(project.panel.max_width_mm))
        self.max_height.SetValue("" if project.panel.max_height_mm is None else str(project.panel.max_height_mm))
        self.loaded_layout = asdict(project.panel.layout)
        self.tab_mode.SetStringSelection(project.panel.tabs.mode)
        self.tab_width.SetValue(project.panel.tabs.width_mm)
        self.tab_hcount.SetValue(project.panel.tabs.horizontal_count)
        self.tab_vcount.SetValue(project.panel.tabs.vertical_count)
        self.tab_min_distance.SetValue(project.panel.tabs.min_distance_mm)
        self.cut_mode.SetStringSelection(project.panel.cuts.mode)
        self.cut_drill.SetValue(project.panel.cuts.drill_mm)
        self.cut_spacing.SetValue(project.panel.cuts.spacing_mm)
        self.cut_offset.SetValue(project.panel.cuts.offset_mm)
        self.cut_prolong.SetValue(project.panel.cuts.prolong_mm)
        self.mill_radius.SetValue(project.panel.post.mill_radius_mm)
        self.allow_mixed_layers.SetValue(project.panel.allow_mixed_layers)
        self.allow_mixed_thickness.SetValue(project.panel.allow_mixed_thickness)
        self.saved_revision = self.model.revision
        self.status.SetLabel(f"Loaded {path}")

    def _on_load(self, _event):
        if (
            self.model.revision != self.saved_revision
            and wx.MessageBox("Discard unsaved project changes?", "Unsaved changes", wx.YES_NO | wx.ICON_WARNING)
            != wx.YES
        ):
            return
        with wx.FileDialog(self, "Load project", wildcard="YAML (*.yaml;*.yml)|*.yaml;*.yml", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                try:
                    self.load_project(dialog.GetPath())
                except Exception as exc:  # noqa: BLE001
                    wx.MessageBox(str(exc), "Load failed", wx.OK | wx.ICON_ERROR)

    def _on_save(self, _event):
        with wx.FileDialog(self, "Save project", wildcard="YAML (*.yaml)|*.yaml", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                path = Path(dialog.GetPath())
                path.write_text(
                    cast(str, yaml.safe_dump(self._project_data(path.parent.resolve()), sort_keys=False)),
                    encoding="utf-8",
                )
                self.model.project_path = path.resolve()
                self.saved_revision = self.model.revision
                self.status.SetLabel(f"Saved {path}")

    def _on_validate(self, _event):
        from ..config import load_project
        from ..runner import prepare_run

        self._discard_prepared()
        self.preview.set_plan(None)
        self.model.plan = None
        if self.model.state != State.DIRTY:
            self.model.transition(State.DIRTY)
        project_path = None
        try:
            project_path = self._temporary_project()
            project = load_project(project_path)
            token = self.model.begin(State.VALIDATING)
            self.cancel_event = threading.Event()
            self._set_busy(True)
            self.status.SetLabel("Validating...")
        except Exception as exc:  # noqa: BLE001
            if project_path is not None:
                project_path.unlink(missing_ok=True)
            wx.MessageBox(str(exc), "Validation failed", wx.OK | wx.ICON_ERROR)
            return

        def work():
            root = None
            prepared = None
            error = None
            try:
                root, plan, contract = prepare_run(project, cancel_event=self.cancel_event)
                prepared = (root, plan, contract, project)
                root = None
            except Exception as exc:  # noqa: BLE001
                error = exc
            finally:
                if root is not None:
                    shutil.rmtree(root, ignore_errors=True)
                project_path.unlink(missing_ok=True)
            wx.CallAfter(self._validated, token, prepared, error)

        self.worker_thread = threading.Thread(target=work, daemon=True)
        self.worker_thread.start()

    def _validated(self, token, prepared, error):
        if token != self.model.generation_token and prepared is not None:
            shutil.rmtree(prepared[0], ignore_errors=True)
        if token != self.model.generation_token:
            self._finish_worker()
            return
        self._set_busy(False)
        self.cancel_event = None
        if error is not None:
            cancelled = getattr(error, "exit_code", None) == 130
            self.model.transition(State.CANCELLED if cancelled else State.FAILED)
            self.status.SetLabel("Cancelled" if cancelled else "Validation failed")
            self._append_log(error)
        else:
            assert prepared is not None
            root, plan, contract, project = prepared
            if self.model.accept_plan(token, plan):
                self.prepared = (root, plan, contract, project)
                self.boards.set_metadata(project, plan)
                self.preview.set_plan(plan)
                self.status.SetLabel("Valid: {} instances".format(len(plan["instances"])))
        self._finish_worker()

    def _on_generate(self, _event):
        from ..runner import execute_prepared

        if self.prepared is None or self.model.state != State.PLANNED:
            wx.MessageBox(
                "Validate and preview the current project before generation",
                "Preview required",
                wx.OK | wx.ICON_INFORMATION,
            )
            return
        root, plan, contract, project = self.prepared
        if (
            project.panel.output is not None
            and project.panel.output.exists()
            and wx.MessageBox("Replace existing unlocked output?", "Confirm", wx.YES_NO | wx.ICON_QUESTION)
            != wx.YES
        ):
            return
        token = self.model.begin(State.GENERATING)
        self.cancel_event = threading.Event()
        self.prepared = None
        self._set_busy(True)
        self.status.SetLabel("Generating the validated plan...")

        def work():
            event_stop = threading.Event()
            event_thread = threading.Thread(
                target=self._monitor_events,
                args=(token, root, contract, event_stop),
                daemon=True,
            )
            event_thread.start()
            result = None
            error = None
            try:
                result = execute_prepared(
                    project,
                    Path(sys.executable),
                    root,
                    plan,
                    contract,
                    cancel_event=self.cancel_event,
                )
            except Exception as exc:  # noqa: BLE001
                error = exc
            finally:
                event_stop.set()
                event_thread.join()
            wx.CallAfter(self._generated, token, result, error, project.panel.output)

        self.worker_thread = threading.Thread(target=work, daemon=True)
        self.worker_thread.start()

    def _generated(self, token, result, error, output):
        if token != self.model.generation_token:
            self._finish_worker()
            return
        self._set_busy(False)
        self.cancel_event = None
        if error is not None:
            cancelled = getattr(error, "exit_code", None) == 130
            self.model.transition(State.CANCELLED if cancelled else State.FAILED)
            self.status.SetLabel("Cancelled" if cancelled else "Generation failed")
            self._append_log(error)
        elif self.model.finish(token, True, f"Generated {output}"):
            self.last_output = output
            self.open_button.Enable(True)
            self.preview.set_plan(result["plan"])
            self.status.SetLabel(f"Generated {output}")
            self._append_log("\n".join(result["artifacts"]))
        self._finish_worker()

    def _on_cancel(self, _event):
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.status.SetLabel("Cancelling...")

    def _on_open(self, _event):
        if self.last_output is None:
            return
        from ..command import open_board

        if open_board(self.last_output) != 0:
            wx.MessageBox("Could not open the generated board", "Open failed", wx.OK | wx.ICON_ERROR)
