"""Tests for the Claude Code hook scripts in .claude/hooks/."""

import importlib.util
import io
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

HOOKS = Path(__file__).resolve().parents[1] / ".claude" / "hooks"


def load_hook(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HOOKS / f"{name}.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event(**fields: object) -> io.StringIO:
    return io.StringIO(json.dumps(fields))


def test_format_on_edit_formats_python_file(tmp_path: Path) -> None:
    target = tmp_path / "ugly.py"
    target.write_text("x=[1,2 ,3]\n")
    assert load_hook("format_on_edit").main(event(tool_input={"file_path": str(target)})) == 0
    assert target.read_text() == "x = [1, 2, 3]\n"


def test_format_on_edit_ignores_other_files(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    assert load_hook("format_on_edit").main(event(tool_input={"file_path": "notes.md"})) == 0
    assert calls == []


def fake_run(changed: str, gate_code: int, calls: list[list[str]]):
    def run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, changed, "")
        return subprocess.CompletedProcess(cmd, gate_code, "ruff.....Passed\npytest: 1 failed", "")

    return run


@pytest.mark.parametrize(
    ("active", "changed", "gate_code", "expected_exit", "gate_ran"),
    [
        (True, " M src/app/core.py\n", 1, 0, False),  # second stop: never loop
        (False, "", 1, 0, False),  # no Python changes: skip gate
        (False, "?? src/app/new.py\n", 0, 0, True),  # gate passes
        (False, " M src/app/core.py\n", 1, 2, True),  # gate fails: block stop
    ],
)
def test_stop_gate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    active: bool,
    changed: str,
    gate_code: int,
    expected_exit: int,
    gate_ran: bool,
) -> None:
    calls: list[list[str]] = []
    hook = load_hook("stop_gate")
    monkeypatch.setattr(subprocess, "run", fake_run(changed, gate_code, calls))
    assert hook.main(event(stop_hook_active=active)) == expected_exit
    assert (hook.GATE in calls) == gate_ran
    err = capsys.readouterr().err
    assert ("1 failed" in err) == (expected_exit == 2)
    assert "Passed" not in err  # only failures are fed back to Claude
