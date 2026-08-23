from collections.abc import Iterable


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
