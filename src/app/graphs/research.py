"""The `research` graph (PRD M5, AD1, AD8, AD10): Phase 2 for the Lite tier. The step list is the
graph's edges, so no model decides what happens next. The thread id is the run id and the
checkpointer is SQLite, so a run survives a restart.

    bootstrap -> decompose -> plan -> approve_plan (interrupt) -> sweep -> draft -> polish
              -> readability -> gate -> export -> END

Rules that keep it resumable:
- `approve_plan` does no model work and writes nothing before its interrupt; LangGraph runs an
  interrupted node again from its start when the interrupt is answered.
- Every other node is idempotent: a step that finished does nothing when reached again, and what
  happens inside a step is saved item by item (see `app.research.steps`).
- The graph state holds only the run id; everything else is read from the run's files and rows.
"""

from itertools import pairwise
from typing import Any, TypedDict, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.types import Command, interrupt

from app.graphs.brief import Snapshot
from app.research.steps import ResearchSteps

NODES = (
    "bootstrap",
    "decompose",
    "plan",
    "approve_plan",
    "sweep",
    "draft",
    "polish",
    "readability",
    "gate",
    "export",
)


class ResearchState(TypedDict):
    run_id: str
    plan_sha256: str  # the hash the owner approved; empty before


def build_research_graph(steps: ResearchSteps, checkpointer: BaseCheckpointSaver[Any]) -> Any:
    """The compiled research graph, checkpointed by ``checkpointer``."""

    def approve_plan(state: ResearchState) -> dict[str, str]:
        steps.mark_awaiting(state["run_id"])
        value = interrupt({"waiting_for": "plan_approval", "run_id": state["run_id"]})
        steps.confirm_plan(state["run_id"], value["plan_sha256"])
        return {"plan_sha256": value["plan_sha256"]}

    def node(name: str) -> Any:
        action = getattr(steps, name)

        def run(state: ResearchState) -> dict[str, str]:
            action(state["run_id"])
            return {}

        return run

    # LangGraph's builder signatures are partly untyped; the nodes around it are typed.
    builder = cast("Any", StateGraph(ResearchState))
    for name in NODES:
        builder.add_node(name, approve_plan if name == "approve_plan" else node(name))
    builder.add_edge(START, NODES[0])
    for before, after in pairwise(NODES):
        builder.add_edge(before, after)
    builder.add_edge(NODES[-1], END)
    return builder.compile(checkpointer=checkpointer)


class ResearchRunner:
    """The only door to a compiled research graph for code outside this package."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    @staticmethod
    def _config(run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}}

    def _invoke(self, value: Any, run_id: str) -> None:
        # "sync" writes each checkpoint before the next step starts; the default writes them in
        # the background and a SIGKILL could lose the latest ones (AD10).
        self._graph.invoke(value, self._config(run_id), durability="sync")

    def start(self, run_id: str) -> None:
        self._invoke({"run_id": run_id, "plan_sha256": ""}, run_id)

    def resume(self, run_id: str, value: dict[str, Any]) -> None:
        """Answer the pending interrupt (the plan approval) with ``value``."""
        self._invoke(Command(resume=value), run_id)

    def proceed(self, run_id: str) -> None:
        """Continue from the last checkpoint (after a crash or an error)."""
        self._invoke(None, run_id)

    def delete_thread(self, run_id: str) -> None:
        """Forget every checkpoint of the run."""
        self._graph.checkpointer.delete_thread(run_id)

    def snapshot(self, run_id: str) -> Snapshot:
        state = self._graph.get_state(self._config(run_id))
        waiting = [i.value for task in state.tasks for i in task.interrupts]
        return Snapshot(dict(state.values), waiting[0] if waiting else None, tuple(state.next))
