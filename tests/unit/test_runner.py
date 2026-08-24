import os
import sys
import time
from pathlib import Path

import pytest

from kikit_packer.packing import PlanningError
from kikit_packer.runner import RunError, _plan_v1_with_cancellation, _run_child


def test_planning_cancellation_is_normalized_to_exit_130(monkeypatch):
    import threading

    cancelled = threading.Event()
    cancelled.set()

    def interrupted(*_args, **_kwargs):
        raise PlanningError("packing cancelled")

    monkeypatch.setattr("kikit_packer.runner.plan_v1", interrupted)
    with pytest.raises(RunError) as caught:
        _plan_v1_with_cancellation([], [], [], None, None, 1, cancelled)
    assert caught.value.exit_code == 130


def test_child_streams_are_drained_with_strict_tail_limits(tmp_path: Path):
    code = (
        "import os; "
        "os.write(1, b'a' * 200000); "
        "os.write(2, b'b' * 200000)"
    )
    returncode, stdout, stderr = _run_child(
        [sys.executable, "-c", code],
        tmp_path,
        dict(os.environ),
        1024,
        2048,
    )
    assert returncode == 0
    assert stdout == "a" * 1024
    assert stderr == "b" * 2048


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_leader_exit_does_not_leave_descendant_holding_pipes(tmp_path: Path):
    code = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "print('leader done')"
    )
    started = time.monotonic()
    returncode, stdout, _ = _run_child(
        [sys.executable, "-c", code],
        tmp_path,
        dict(os.environ),
        1024,
        1024,
    )
    assert returncode == 0
    assert "leader done" in stdout
    assert time.monotonic() - started < 6


def test_pre_cancelled_child_returns_130(tmp_path: Path):
    import threading

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(RunError) as caught:
        _run_child(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            tmp_path,
            dict(os.environ),
            1024,
            1024,
            cancelled,
        )
    assert caught.value.exit_code == 130
