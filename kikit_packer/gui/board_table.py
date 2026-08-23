import wx
import wx.dataview as dv


class BoardDropTarget(wx.FileDropTarget):
    def __init__(self, table):
        super().__init__()
        self.table = table

    def OnDropFiles(self, _x, _y, filenames):
        added = False
        for filename in filenames:
            if filename.lower().endswith(".kicad_pcb"):
                self.table.view.AppendItem([filename, "1", "1", "", "", "", "", ""])
                added = True
        if added:
            self.table.notify_change()
        return added


class BoardTable(wx.Panel):
    def __init__(self, parent, on_change=None):
        super().__init__(parent)
        self.on_change = on_change
        self.view = dv.DataViewListCtrl(self, style=dv.DV_ROW_LINES | dv.DV_VERT_RULES)
        self.view.AppendTextColumn("Board", width=420, mode=dv.DATAVIEW_CELL_EDITABLE)
        self.view.AppendTextColumn("Quantity", width=80, mode=dv.DATAVIEW_CELL_EDITABLE)
        self.view.AppendTextColumn("Padding mm", width=100, mode=dv.DATAVIEW_CELL_EDITABLE)
        self.view.AppendTextColumn("Width mm", width=85)
        self.view.AppendTextColumn("Height mm", width=85)
        self.view.AppendTextColumn("Layers", width=60)
        self.view.AppendTextColumn("Thickness mm", width=100)
        self.view.AppendTextColumn("Rotations", width=90)
        self.view.SetDropTarget(BoardDropTarget(self))
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in (("Add", self._add), ("Remove", self._remove), ("Up", lambda event: self._move(-1)), ("Down", lambda event: self._move(1))):
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(button, 0, wx.RIGHT, 5)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(self.view, 1, wx.EXPAND | wx.BOTTOM, 5)
        layout.Add(buttons, 0)
        self.SetSizer(layout)

    def _add(self, _event):
        with wx.FileDialog(self, "Add board", wildcard="KiCad boards (*.kicad_pcb)|*.kicad_pcb", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                for path in dialog.GetPaths():
                    self.view.AppendItem([path, "1", "1", "", "", "", "", ""])
                self.notify_change()

    def _remove(self, _event):
        row = self.view.GetSelectedRow()
        if row >= 0:
            self.view.DeleteItem(row)
            self.notify_change()

    def _move(self, delta):
        row = self.view.GetSelectedRow()
        target = row + delta
        rows = self.rows()
        if row < 0 or target < 0 or target >= len(rows):
            return
        rows[row], rows[target] = rows[target], rows[row]
        self.set_rows(rows)
        self.view.SelectRow(target)
        self.notify_change()

    def rows(self):
        return [
            [str(self.view.GetValue(row, column)) for column in range(3)]
            for row in range(self.view.GetItemCount())
        ]

    def notify_change(self):
        if self.on_change is not None:
            self.on_change()

    def set_rows(self, rows):
        self.view.DeleteAllItems()
        for row in rows:
            values = [str(value) for value in row]
            self.view.AppendItem(values + [""] * (8 - len(values)))

    def set_metadata(self, project, plan):
        sources = {source["original_path"]: source for source in plan["sources"]}
        for row, board in enumerate(project.boards):
            source = sources.get(str(board.board))
            if source is None:
                continue
            inspection = source["inspection"]
            left, top, right, bottom = inspection["outline_bounds_iu"]
            rotations = [
                str(instance["packing_rotation_deg"])
                for instance in plan["instances"]
                if instance["row_id"] == f"row-{row + 1:04d}"
            ]
            values = [
                (right - left) / 1_000_000,
                (bottom - top) / 1_000_000,
                inspection["copper_layer_count"],
                inspection["thickness_iu"] / 1_000_000,
                ",".join(rotations),
            ]
            for column, value in enumerate(values, 3):
                self.view.SetValue(str(value), row, column)
