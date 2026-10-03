"""One active run at a time (PRD A3, M5 AC2): a lock file held only while a graph executes."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.research.errors import WorkerBusy
from app.research.worker import WorkerLock


def test_the_lock_is_exclusive_and_released_on_exit(tmp_path: Path) -> None:
    path = tmp_path / "worker.lock"
    with WorkerLock(path), pytest.raises(WorkerBusy, match="another run"), WorkerLock(path):
        pass
    holder = WorkerLock(path)
    with holder:
        pass
    with WorkerLock(path):  # free again, although `holder` is still around
        pass
    assert holder is not None


def test_the_lock_is_released_when_the_work_fails(tmp_path: Path) -> None:
    path = tmp_path / "worker.lock"
    with pytest.raises(RuntimeError), WorkerLock(path):
        raise RuntimeError("boom")
    with WorkerLock(path):
        pass


def test_the_lock_creates_its_directory(tmp_path: Path) -> None:
    with WorkerLock(tmp_path / "deep" / "dir" / "worker.lock"):
        assert (tmp_path / "deep" / "dir" / "worker.lock").exists()


def test_another_process_holding_the_lock_blocks_this_one_until_it_dies(tmp_path: Path) -> None:
    path = tmp_path / "worker.lock"
    code = (
        "import sys, time\n"
        "from app.research.worker import WorkerLock\n"
        "lock = WorkerLock(sys.argv[1])\n"
        "lock.__enter__()\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(WorkerBusy), WorkerLock(path):
            pass
    finally:
        child.kill()
        child.wait()
    deadline = time.monotonic() + 5
    while True:  # the operating system frees the lock of a dead process
        try:
            with WorkerLock(path):
                break
        except WorkerBusy:
            assert time.monotonic() < deadline
            time.sleep(0.05)
