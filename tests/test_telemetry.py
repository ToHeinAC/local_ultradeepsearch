"""PRD §3.2: no telemetry leaves the machine. LangGraph's LangSmith tracing must be off."""

import os
import subprocess
import sys

import pytest

from app.telemetry import TRACING_VARIABLES, disable_tracing


def test_tracing_is_forced_off_whatever_the_environment_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in TRACING_VARIABLES:
        monkeypatch.setenv(name, "true")
    disable_tracing()
    assert {name: os.environ[name] for name in TRACING_VARIABLES} == dict.fromkeys(
        TRACING_VARIABLES, "false"
    )


def test_all_known_switches_are_covered() -> None:
    assert {"LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"} <= set(TRACING_VARIABLES)


def test_importing_the_graphs_package_turns_langsmith_off_before_langgraph_loads() -> None:
    env = {
        **os.environ,
        "LANGSMITH_TRACING": "true",
        "LANGCHAIN_TRACING_V2": "true",
        "LANGSMITH_API_KEY": "not-a-real-key",
    }
    code = (
        "import app.graphs\n"
        "from langsmith.utils import tracing_is_enabled\n"
        "print(tracing_is_enabled())\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (done.returncode, done.stdout.strip()) == (0, "False"), done.stderr
