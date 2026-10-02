"""No telemetry leaves the machine (PRD §3.2): LangGraph's LangSmith tracing is forced off."""

import os

TRACING_VARIABLES = ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2")


def disable_tracing() -> None:
    """Turn tracing off even if the environment asks for it. Call before LangGraph is imported."""
    for name in TRACING_VARIABLES:
        os.environ[name] = "false"
