from __future__ import annotations

from pathlib import Path


def run(project: Path | None = None) -> int:
    import wx

    from .frame import MainFrame

    app = wx.App(False)
    frame = MainFrame(None, project)
    frame.Show()
    app.MainLoop()
    return 0
