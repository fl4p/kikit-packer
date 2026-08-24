from collections.abc import Iterable, Mapping
from typing import Any


def _least_rotation(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not points:
        return []
    doubled = points + points
    length = len(points)
    left = 0
    right = 1
    offset = 0
    while left < length and right < length and offset < length:
        first = doubled[left + offset]
        second = doubled[right + offset]
        if first == second:
            offset += 1
            continue
        if first > second:
            left += offset + 1
            if left <= right:
                left = right + 1
        else:
            right += offset + 1
            if right <= left:
                right = left + 1
        offset = 0
    start = min(left, right)
    return doubled[start : start + length]


def planned_substrate_bounds(instance: Mapping[str, Any]) -> list[int]:
    source_left, source_top, _source_right, source_bottom = instance["source_area_iu"]
    outline_left, outline_top, outline_right, outline_bottom = instance["outline_bounds_iu"]
    destination_x, destination_y = instance["append"]["destination_iu"]
    if instance["packing_rotation_deg"] == 0:
        return [
            destination_x + outline_left - source_left,
            destination_y + outline_top - source_top,
            destination_x + outline_right - source_left,
            destination_y + outline_bottom - source_top,
        ]
    return [
        destination_x + source_bottom - outline_bottom,
        destination_y + outline_left - source_left,
        destination_x + source_bottom - outline_top,
        destination_y + outline_right - source_left,
    ]


def canonical_ring_points(points: Iterable[tuple[int, int]]) -> list[list[int]]:
    cleaned = []
    for point in points:
        normalized = (int(point[0]), int(point[1]))
        if not cleaned or cleaned[-1] != normalized:
            cleaned.append(normalized)
    if len(cleaned) > 1 and cleaned[0] == cleaned[-1]:
        cleaned.pop()
    forward = _least_rotation(cleaned)
    reverse = _least_rotation(list(reversed(cleaned)))
    return [list(point) for point in min(forward, reverse)]
