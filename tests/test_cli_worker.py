"""`udr worker` (PRD M6): wires the queue worker, ends cleanly on SIGTERM."""

import signal
import threading
from pathlib import Path

import pytest
from research_run_rig import make_rig
from typer.testing import CliRunner

from app import cli
from app.events import MemoryEventSink
from app.worker import Worker

runner = CliRunner()


def test_the_worker_command_runs_the_loop_and_logs_to_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path)
    run_id = rig.create().run_id
    worker = Worker(rig.service, poll_s=0.0, events=MemoryEventSink(), log=cli.typer.echo)
    monkeypatch.setattr(cli, "_worker", lambda: worker)
    stops: list[threading.Event] = []

    def one_round(self: Worker, stop: threading.Event) -> None:
        stops.append(stop)
        self.run_once()

    monkeypatch.setattr(Worker, "run_forever", one_round)
    installed: list[int] = []
    monkeypatch.setattr(cli.signal, "signal", lambda signum, _handler: installed.append(signum))
    result = runner.invoke(cli.app, ["worker"])
    assert result.exit_code == 0, result.output
    assert f"Lauf {run_id}: gestartet" in result.output
    assert installed == [signal.SIGTERM, signal.SIGINT]
    assert len(stops) == 1
    assert not stops[0].is_set()


def test_a_termination_signal_sets_the_stop_event_and_leaves_the_loop() -> None:
    stop = threading.Event()
    handler = cli._stop_on_signal(stop)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(SystemExit) as info:
        handler(signal.SIGTERM, None)
    assert info.value.code == 0
    assert stop.is_set()
