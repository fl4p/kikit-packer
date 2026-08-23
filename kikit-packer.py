from pathlib import Path
import sys


_checkout = str(Path(__file__).resolve().parent)
_added = _checkout not in sys.path
if _added:
    sys.path.insert(0, _checkout)
try:
    from kikit_packer.plugin import FlatEdgeTabs, Plugin
finally:
    if _added and sys.path[0] == _checkout:
        sys.path.pop(0)


__all__ = ["Plugin", "FlatEdgeTabs"]
