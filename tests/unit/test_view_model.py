from kikit_packer.gui.view_model import State, ViewModel


def test_stale_plan_is_rejected():
    model = ViewModel()
    first = model.begin(State.VALIDATING)
    model.transition(State.CANCELLED)
    second = model.begin(State.VALIDATING)
    assert second != first
    assert model.accept_plan(first, {}) is False
    assert model.accept_plan(second, {"packing": {}}) is True
    assert model.state == State.PLANNED


def test_busy_states():
    model = ViewModel()
    assert model.busy is False
    model.begin(State.GENERATING)
    assert model.busy is True
    model.finish(model.generation_token, True, "done")
    assert model.state == State.SUCCEEDED
