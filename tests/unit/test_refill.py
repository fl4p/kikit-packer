import pytest

from kikit_packer.refill import (
    RefillAreaError,
    _line_chain_area_x2,
    compare_fill_area_snapshots,
)


class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class Chain:
    def __init__(self, points):
        self.points = [Point(x, y) for x, y in points]

    def PointCount(self):
        return len(self.points)

    def CPoint(self, index):
        return self.points[index]


def record(area, geometry="a" * 64):
    value = {
        "zone_uuid": "zone-1",
        "zone": "GND",
        "layer": 0,
        "layer_name": "F.Cu",
        "area_iu2_x2": area,
    }
    value["geometry_sha256"] = geometry
    return value


def test_exact_area_preserves_one_iu2_at_large_coordinates():
    size = 100_000_000
    square = Chain([(0, 0), (size, 0), (size, size), (0, size)])
    notched = Chain([(0, 0), (size, 0), (size, size), (1, size), (0, size - 1)])
    assert _line_chain_area_x2(square) - _line_chain_area_x2(notched) == 1


def test_identical_refill_areas_pass():
    snapshot = {"zone-1:0": record(12345)}
    result = compare_fill_area_snapshots(snapshot, snapshot)
    assert result["enabled"] is True
    assert result["status"] == "passed"
    assert result["zone_layer_count"] == 1
    assert result["total_area_iu2_x2"] == 12345


def test_changed_refill_area_fails_with_zone_and_delta():
    before = {"zone-1:0": record(2_000_000_000_000)}
    after = {"zone-1:0": record(4_000_000_000_000)}
    with pytest.raises(RefillAreaError, match=r"GND on F.Cu: \+1\.000000000 mm\^2"):
        compare_fill_area_snapshots(before, after)


def test_equal_area_changed_geometry_passes():
    before = {"zone-1:0": record(12345, "a" * 64)}
    after = {"zone-1:0": record(12345, "b" * 64)}
    assert compare_fill_area_snapshots(before, after)["status"] == "passed"


def test_added_zone_layer_fill_fails():
    with pytest.raises(RefillAreaError, match="1 zone-layer"):
        compare_fill_area_snapshots({}, {"zone-1:0": record(1)})
