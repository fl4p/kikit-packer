from __future__ import annotations

from typing import Any

import wx

from ..geometry import planned_substrate_bounds

_BACKGROUND = wx.Colour(18, 21, 24)
_BOARD_FILL = wx.Colour(24, 74, 49)
_BOARD_EDGE = wx.Colour(104, 205, 151)
_HOLE_FILL = _BACKGROUND
_PACKING_EDGE = wx.Colour(69, 112, 143)
_TEXT = wx.Colour(230, 236, 240)
_MUTED_TEXT = wx.Colour(145, 158, 166)
_PADDING = 34
_IU_PER_MM = 1_000_000


def preview_instances(plan: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for instance in plan["instances"]:
        rotation = int(instance["packing_rotation_deg"])
        bounds = planned_substrate_bounds(instance)
        polygons = []
        for polygon in instance["expected_inventory"]["substrates"][str(rotation)]:
            polygons.append({
                "outline": [
                    [int(point[0]) + bounds[0], int(point[1]) + bounds[1]]
                    for point in polygon["outline"]
                ],
                "holes": [
                    [
                        [int(point[0]) + bounds[0], int(point[1]) + bounds[1]]
                        for point in ring
                    ]
                    for ring in polygon.get("holes", [])
                ],
            })
        destination_x, destination_y = instance["append"]["destination_iu"]
        packing_width, packing_height = instance["packing_size_iu"]
        if rotation == 90:
            packing_width, packing_height = packing_height, packing_width
        outline_left, outline_top, outline_right, outline_bottom = bounds
        output.append({
            "instance_id": instance["instance_id"],
            "rotation_deg": rotation,
            "polygons": polygons,
            "packing_bounds": [
                destination_x,
                destination_y,
                destination_x + packing_width,
                destination_y + packing_height,
            ],
            "substrate_bounds": bounds,
            "size_mm": [
                (outline_right - outline_left) / _IU_PER_MM,
                (outline_bottom - outline_top) / _IU_PER_MM,
            ],
        })
    return output


def preview_world_bounds(plan: dict[str, Any], instances: list[dict[str, Any]]) -> list[int]:
    points = []
    packing = plan["packing"]["bounds_iu"]
    points.extend(((int(packing[0]), int(packing[1])), (int(packing[2]), int(packing[3]))))
    for instance in instances:
        left, top, right, bottom = instance["packing_bounds"]
        points.extend(((left, top), (right, bottom)))
        for polygon in instance["polygons"]:
            points.extend((int(point[0]), int(point[1])) for point in polygon["outline"])
    return [
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    ]


def fit_transform(bounds: list[int], width: int, height: int, padding: int = _PADDING) -> tuple[float, float, float]:
    world_width = max(1, bounds[2] - bounds[0])
    world_height = max(1, bounds[3] - bounds[1])
    available_width = max(1, width - 2 * padding)
    available_height = max(1, height - 2 * padding)
    scale = min(available_width / world_width, available_height / world_height)
    offset_x = (width - world_width * scale) / 2 - bounds[0] * scale
    offset_y = (height - world_height * scale) / 2 - bounds[1] * scale
    return scale, offset_x, offset_y


class Preview(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent, size=(620, 620))
        self.SetMinSize((420, 360))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetBackgroundColour(_BACKGROUND)
        self.plan = None
        self.instances = []
        self.world_bounds = None
        self.scale = 1.0
        self.fit_scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.drag_origin = None
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SIZE, self._on_size)
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_LEFT_DCLICK, self._on_double_click)

    def set_plan(self, plan):
        self.plan = plan
        self.instances = [] if plan is None else preview_instances(plan)
        self.world_bounds = None if plan is None else preview_world_bounds(plan, self.instances)
        self.fit()
        self.Refresh()

    def fit(self):
        if self.world_bounds is None:
            return
        width, height = self.GetClientSize()
        if width <= 0 or height <= 0:
            return
        self.scale, self.offset_x, self.offset_y = fit_transform(self.world_bounds, width, height)
        self.fit_scale = self.scale

    def _screen_point(self, point):
        return wx.Point(
            int(round(point[0] * self.scale + self.offset_x)),
            int(round(point[1] * self.scale + self.offset_y)),
        )

    def _screen_rect(self, bounds):
        top_left = self._screen_point(bounds[:2])
        bottom_right = self._screen_point(bounds[2:])
        return (
            top_left.x,
            top_left.y,
            max(1, bottom_right.x - top_left.x),
            max(1, bottom_right.y - top_left.y),
        )

    def _paint(self, _event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(_BACKGROUND))
        dc.Clear()
        if not self.plan:
            dc.SetTextForeground(_MUTED_TEXT)
            dc.DrawText("Validate to preview board geometry", 18, 18)
            return
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.SetPen(wx.Pen(_PACKING_EDGE, 1, wx.PENSTYLE_SHORT_DASH))
        for instance in self.instances:
            dc.DrawRectangle(*self._screen_rect(instance["packing_bounds"]))
        for instance in self.instances:
            dc.SetPen(wx.Pen(_BOARD_EDGE, 1))
            dc.SetBrush(wx.Brush(_BOARD_FILL))
            for polygon in instance["polygons"]:
                outline = [self._screen_point(point) for point in polygon["outline"]]
                if len(outline) >= 3:
                    dc.DrawPolygon(outline)
                dc.SetPen(wx.Pen(_BOARD_EDGE, 1))
                dc.SetBrush(wx.Brush(_HOLE_FILL))
                for ring in polygon["holes"]:
                    hole = [self._screen_point(point) for point in ring]
                    if len(hole) >= 3:
                        dc.DrawPolygon(hole)
                dc.SetBrush(wx.Brush(_BOARD_FILL))
            left, top, _right, _bottom = instance["substrate_bounds"]
            label_x, label_y = self._screen_point((left, top))
            width_mm, height_mm = instance["size_mm"]
            dc.SetTextForeground(_TEXT)
            dc.DrawText(instance["instance_id"], label_x + 6, label_y + 5)
            dc.SetTextForeground(_MUTED_TEXT)
            dc.DrawText(
                f"{width_mm:.2f} × {height_mm:.2f} mm · {instance['rotation_deg']}°",
                label_x + 6,
                label_y + 22,
            )
        width, height = self.GetClientSize()
        dc.SetTextForeground(_MUTED_TEXT)
        hint = "Wheel: zoom  ·  Drag: pan  ·  Double-click: fit"
        text_width, text_height = dc.GetTextExtent(hint)
        dc.DrawText(hint, max(10, width - text_width - 12), max(10, height - text_height - 10))

    def _on_size(self, event):
        if self.world_bounds is not None and self.drag_origin is None:
            self.fit()
        self.Refresh()
        event.Skip()

    def _on_wheel(self, event):
        if self.world_bounds is None:
            return
        rotation = event.GetWheelRotation()
        if rotation == 0:
            return
        cursor = event.GetPosition()
        world_x = (cursor.x - self.offset_x) / self.scale
        world_y = (cursor.y - self.offset_y) / self.scale
        factor = 1.2 ** (rotation / event.GetWheelDelta())
        new_scale = min(self.fit_scale * 64, max(self.fit_scale / 8, self.scale * factor))
        self.offset_x = cursor.x - world_x * new_scale
        self.offset_y = cursor.y - world_y * new_scale
        self.scale = new_scale
        self.Refresh()

    def _on_left_down(self, event):
        position = event.GetPosition()
        self.drag_origin = (position.x, position.y, self.offset_x, self.offset_y)
        if not self.HasCapture():
            self.CaptureMouse()

    def _on_left_up(self, _event):
        self.drag_origin = None
        if self.HasCapture():
            self.ReleaseMouse()

    def _on_motion(self, event):
        if self.drag_origin is None or not event.Dragging() or not event.LeftIsDown():
            return
        position = event.GetPosition()
        start_x, start_y, offset_x, offset_y = self.drag_origin
        self.offset_x = offset_x + position.x - start_x
        self.offset_y = offset_y + position.y - start_y
        self.Refresh()

    def _on_double_click(self, _event):
        self.drag_origin = None
        self.fit()
        self.Refresh()
