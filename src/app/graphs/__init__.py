"""LangGraph graphs. This package is the only place that imports LangGraph (AGENTS.md §5.2).

Tracing is switched off here, before any submodule loads LangGraph, so no telemetry can leave the
machine whichever module imports a graph first.
"""

from app.telemetry import disable_tracing

disable_tracing()
