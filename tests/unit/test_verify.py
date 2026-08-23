import copy

import pytest

from kikit_packer.verify import VerificationError, _verify_tab_connectivity


INSTANCE_IDS = ["a", "b", "c"]
VALID_TABS = {
    "connections": [
        {"instances": ["a", "b"], "start_iu": [0, 0], "end_iu": [1, 0]},
        {"instances": ["b", "c"], "start_iu": [2, 0], "end_iu": [3, 0]},
    ],
    "graph_edges": [["a", "b"], ["b", "c"]],
    "connected_components": [["a", "b", "c"]],
}


def test_parent_recomputes_tab_connectivity_from_connection_records():
    assert _verify_tab_connectivity(VALID_TABS, INSTANCE_IDS) == [["a", "b", "c"]]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda tabs: tabs["graph_edges"].pop(),
        lambda tabs: (tabs["connections"].pop(), tabs["graph_edges"].pop()),
        lambda tabs: tabs["connections"][0].update(instances=["a", "a"]),
        lambda tabs: tabs["connections"][0].update(end_iu=[0, 0]),
        lambda tabs: tabs["connections"][0].update(instances=["a", "unknown"]),
    ],
)
def test_parent_rejects_self_reported_or_invalid_tab_connectivity(mutation):
    tabs = copy.deepcopy(VALID_TABS)
    mutation(tabs)
    with pytest.raises(VerificationError):
        _verify_tab_connectivity(tabs, INSTANCE_IDS)
