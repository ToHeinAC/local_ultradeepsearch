"""`udr gui` (PRD M7): Streamlit on loopback, no telemetry, and the key it needs."""

import subprocess
import tomllib
from pathlib import Path

import pytest
from support import make_settings
from typer.testing import CliRunner

from app import bootstrap, cli

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]


def launched(monkeypatch: pytest.MonkeyPatch, **settings: object) -> list[list[str]]:
    seen: list[list[str]] = []
    monkeypatch.setattr(bootstrap, "load_settings", lambda: make_settings(**settings))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda argv, **kw: seen.append(list(argv)) or subprocess.CompletedProcess(argv, 0),
    )
    return seen


def test_the_command_line_binds_loopback_and_switches_telemetry_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = launched(monkeypatch, gui_api_key="udr_x", gui_port=8599)
    result = runner.invoke(cli.app, ["gui"])
    assert result.exit_code == 0, result.output
    (argv,) = seen
    assert argv[1:4] == ["-m", "streamlit", "run"]
    assert argv[4].endswith("src/app/gui/app.py")
    for flag, value in [
        ("--server.address", "127.0.0.1"),
        ("--server.port", "8599"),
        ("--server.headless", "true"),
        ("--browser.gatherUsageStats", "false"),
    ]:
        assert argv[argv.index(flag) + 1] == value


def test_without_a_key_nothing_starts_and_the_message_names_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = launched(monkeypatch)
    result = runner.invoke(cli.app, ["gui"])
    assert result.exit_code == 1
    assert "UDR_GUI_API_KEY" in result.output
    assert seen == []


def test_the_repo_config_has_the_same_safe_defaults() -> None:
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["server"]["address"] == "127.0.0.1"
    assert config["browser"]["gatherUsageStats"] is False
