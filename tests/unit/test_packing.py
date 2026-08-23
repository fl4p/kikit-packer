import pytest

from kikit_packer.packing import (
    PlanningImpossible,
    PlanningLimitExceeded,
    legacy_optimal_pack,
    plan_v1,
)


def test_legacy_pack_is_stable():
    rotations, positions = legacy_optimal_pack([(10, 20), (30, 10)])
    assert rotations == [True, False]
    assert positions == [(30, 0), (0, 0)]


def test_version_one_tie_breaks_unrotated():
    result = plan_v1(["i1"], ["s1"], [(10, 10)])
    assert result.placements[0].rotated is False
    assert result.bounds_iu == (0, 0, 10, 10)


def test_empty_and_limit_are_structured():
    with pytest.raises(PlanningImpossible):
        plan_v1([], [], [])
    with pytest.raises(PlanningLimitExceeded):
        plan_v1(["i"] * 3, ["s"] * 3, [(1, 1)] * 3, candidate_limit=4)
