from __future__ import annotations

from collections.abc import Iterable, Sequence
from itertools import chain, combinations, product
from typing import Callable

from rpack import PackingImpossibleError, pack, packing_density

from .model import PackingResult, Placement


class PlanningError(RuntimeError):
    pass


class PlanningImpossible(PlanningError):
    pass


class PlanningLimitExceeded(PlanningError):
    pass


def powerset(iterable: Iterable[int]):
    values = list(iterable)
    return chain.from_iterable(combinations(values, r) for r in range(len(values) + 1))


def legacy_optimal_pack(
    sizes: Sequence[tuple[int, int]],
    max_width: int | None = None,
    max_height: int | None = None,
) -> tuple[list[bool], list[tuple[int, int]]]:
    if not sizes:
        raise PlanningImpossible("at least one board instance is required")
    best_rotate: Sequence[bool] = ()
    best_positions: Sequence[tuple[int, int]] = ()
    best_density = -1.0
    best_rotated_area = 0
    for rotated_indices in powerset(range(len(sizes))):
        rotated_set = set(rotated_indices)
        candidate_sizes = [
            (height, width) if i in rotated_set else (width, height)
            for i, (width, height) in enumerate(sizes)
        ]
        try:
            positions = pack(candidate_sizes, max_width=max_width, max_height=max_height)
        except PackingImpossibleError:
            continue
        density = packing_density(candidate_sizes, positions)
        if density > 1.0:
            raise AssertionError("unexpected packing density > 1")
        rotate = [i in rotated_set for i in range(len(candidate_sizes))]
        rotated_area = sum(
            width * height for (width, height), is_rotated in zip(sizes, rotate) if is_rotated
        )
        if density > best_density or (
            best_density != 0
            and density / best_density > (1 - 1e-9)
            and rotated_area < best_rotated_area
        ):
            best_rotate = rotate
            best_positions = positions
            best_density = density
            best_rotated_area = rotated_area
    if not best_positions:
        raise PlanningImpossible("boards do not fit within the requested bounds")
    return list(best_rotate), list(best_positions)


def _bounds(sizes: Sequence[tuple[int, int]], positions: Sequence[tuple[int, int]]) -> tuple[int, int]:
    return (
        max(x + width for (width, _), (x, _) in zip(sizes, positions)),
        max(y + height for (_, height), (_, y) in zip(sizes, positions)),
    )


def plan_v1(
    instance_ids: Sequence[str],
    source_ids: Sequence[str],
    sizes: Sequence[tuple[int, int]],
    max_width: int | None = None,
    max_height: int | None = None,
    candidate_limit: int = 1_048_576,
    cancelled: Callable[[], bool] | None = None,
) -> PackingResult:
    if not sizes:
        raise PlanningImpossible("at least one board instance is required")
    if len(instance_ids) != len(sizes) or len(source_ids) != len(sizes):
        raise ValueError("instance, source, and size counts must match")
    candidate_count = 1 << len(sizes)
    if candidate_count > candidate_limit:
        raise PlanningLimitExceeded(
            f"{candidate_count} rotation candidates exceed limit {candidate_limit}"
        )
    best = None
    evaluated = 0
    for bits in product((False, True), repeat=len(sizes)):
        if cancelled is not None and cancelled():
            raise PlanningError("planning cancelled")
        candidate_sizes = [
            (height, width) if rotated else (width, height)
            for (width, height), rotated in zip(sizes, bits)
        ]
        try:
            positions = pack(candidate_sizes, max_width=max_width, max_height=max_height)
        except PackingImpossibleError:
            continue
        evaluated += 1
        width, height = _bounds(candidate_sizes, positions)
        key = (
            width * height,
            sum(w * h for (w, h), rotated in zip(sizes, bits) if rotated),
            tuple(bits),
            tuple(value for pos in positions for value in pos),
        )
        if best is None or key < best[0]:
            best = (key, bits, candidate_sizes, positions, width, height)
    if best is None:
        raise PlanningImpossible("boards do not fit within the requested bounds")
    _, bits, candidate_sizes, positions, width, height = best
    placements = tuple(
        Placement(instance_id, source_id, x, y, rotated, size[0], size[1])
        for instance_id, source_id, (x, y), rotated, size in zip(
            instance_ids, source_ids, positions, bits, candidate_sizes
        )
    )
    return PackingResult(placements, (0, 0, width, height), candidate_count, evaluated)
