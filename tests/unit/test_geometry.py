from kikit_packer.fingerprint import _transform_polyset, semantic_digest
from kikit_packer.geometry import canonical_ring_points


def test_ring_is_start_orientation_and_duplicate_invariant():
    ring = [(0, 0), (4, 0), (4, 3), (0, 3)]
    expected = canonical_ring_points(ring)
    assert canonical_ring_points(ring[2:] + ring[:2]) == expected
    assert canonical_ring_points(list(reversed(ring))) == expected
    assert canonical_ring_points([(0, 0), (0, 0), (4, 0), (4, 3), (0, 3), (0, 0)]) == expected


def test_transformed_multi_polygon_order_is_recanonicalized():
    polygons = [
        {"outline": [[0, 0], [4, 0], [4, 4], [0, 4]], "holes": []},
        {"outline": [[10, 0], [13, 0], [13, 3], [10, 3]], "holes": []},
    ]
    def transform(x, y):
        return y, -x

    transformed = _transform_polyset(polygons, transform)
    reversed_input = _transform_polyset(list(reversed(polygons)), transform)
    assert transformed == sorted(transformed, key=semantic_digest)
    assert transformed == reversed_input


def test_large_ring_does_not_require_quadratic_rotation_materialization():
    points = [(index, index * index % 1009) for index in range(20_000)]
    result = canonical_ring_points(points)
    assert len(result) == len(points)
