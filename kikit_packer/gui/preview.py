import wx


class Preview(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent, size=(-1, 260))
        self.plan = None
        self.SetBackgroundColour(wx.Colour(248, 248, 248))
        self.Bind(wx.EVT_PAINT, self._paint)

    def set_plan(self, plan):
        self.plan = plan
        self.Refresh()

    def _paint(self, _event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.Clear()
        if not self.plan:
            dc.DrawText("Validate to preview placements", 12, 12)
            return
        bounds = self.plan["packing"]["bounds_iu"]
        width = max(1, bounds[2] - bounds[0])
        height = max(1, bounds[3] - bounds[1])
        client_w, client_h = self.GetClientSize()
        scale = min((client_w - 30) / width, (client_h - 30) / height)
        dc.SetPen(wx.Pen(wx.Colour(40, 80, 120), 1))
        dc.SetBrush(wx.Brush(wx.Colour(180, 215, 240)))
        for item in self.plan["instances"]:
            x, y = item["append"]["destination_iu"]
            w, h = item["packing_size_iu"]
            if item["packing_rotation_deg"] == 90:
                w, h = h, w
            rx = 15 + int(x * scale)
            ry = 15 + int(y * scale)
            rw = max(2, int(w * scale))
            rh = max(2, int(h * scale))
            dc.DrawRectangle(rx, ry, rw, rh)
            dc.DrawText(item["instance_id"], rx + 3, ry + 3)
