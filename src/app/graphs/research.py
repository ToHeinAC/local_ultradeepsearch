"""The `research` graph (PRD M5, AD1, AD10): an approved brief to a shipped report.

    bootstrap -> decompose -> plan -> approve_plan (interrupt) -> sweep -> finish -> END

Part B inserts the drafting, polish, readability, gate and export steps before `finish`. The thread
id is the run id and the checkpointer is the same SQLite file as the brief graph's. The node with
the interrupt does no work before it, because LangGraph runs an interrupted node again from its
start when it is answered; every other node is idempotent."""

from itertools import pairwise
from typing import Any, Protocol, TypedDict, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.types import Command, interrupt

from app.graphs.brief import Snapshot


class ResearchState(TypedDict):
    run_id: str
    plan_sha256: str
    gate_round: int
    gate_passed: bool


Update = dict[str, Any]


class Steps(Protocol):
    """`LightSteps`; named here so this module does not import the research package."""

    def bootstrap(self, run_id: str) -> None: ...
    def decompose(self, run_id: str) -> None: ...
    def plan(self, run_id: str) -> str: ...
    def approve(self, run_id: str, sha: str) -> None: ...
    def sweep(self, run_id: str) -> None: ...
    def finish(self, run_id: str) -> None: ...


class _Nodes:
    def __init__(self, steps: Steps) -> None:
        self._s = steps

    def bootstrap(self, state: ResearchState) -> Update:
        self._s.bootstrap(state["run_id"])
        return {}

    def decompose(self, state: ResearchState) -> Update:
        self._s.decompose(state["run_id"])
        return {}

    def plan(self, state: ResearchState) -> Update:
        return {"plan_sha256": self._s.plan(state["run_id"])}

    def approve_plan(self, state: ResearchState) -> Update:
        value = interrupt({"kind": "plan_approval", "plan_sha256": state["plan_sha256"]})
        sha = str(value["plan_sha256"])
        self._s.approve(state["run_id"], sha)
        return {"plan_sha256": sha}

    def sweep(self, state: ResearchState) -> Update:
        self._s.sweep(state["run_id"])
        return {}

    def finish(self, state: ResearchState) -> Update:
        self._s.finish(state["run_id"])
        return {}


def build_research_graph(steps: Steps, checkpointer: BaseCheckpointSaver[Any]) -> Any:
    """The compiled research graph, checkpointed by ``checkpointer``."""
    nodes = _Nodes(steps)
    builder = cast("Any", StateGraph(ResearchState))
    order = ("bootstrap", "decompose", "plan", "approve_plan", "sweep", "finish")
    for name in order:
        builder.add_node(name, getattr(nodes, name))
    builder.add_edge(START, order[0])
    for before, after in pairwise(order):
        builder.add_edge(before, after)
    builder.add_edge(order[-1], END)
    return builder.compile(checkpointer=checkpointer)


class ResearchRunner:
    """The only door to a compiled research graph for code outside this package."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    @staticmethod
    def _config(run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}}

    def _invoke(self, value: Any, run_id: str) -> None:
        # "sync" persists each step before the next one starts, so a SIGKILL loses nothing (AD10).
        self._graph.invoke(value, self._config(run_id), durability="sync")

    def start(self, run_id: str) -> None:
        state: ResearchState = {
            "run_id": run_id,
            "plan_sha256": "",
            "gate_round": 0,
            "gate_passed": False,
        }
        self._invoke(state, run_id)

    def resume(self, run_id: str, value: dict[str, Any]) -> None:
        """Answer the pending interrupt with ``value``."""
        self._invoke(Command(resume=value), run_id)

    def proceed(self, run_id: str) -> None:
        """Continue from the last checkpoint (after a crash or a model error)."""
        self._invoke(None, run_id)

    def snapshot(self, run_id: str) -> Snapshot:
        state = self._graph.get_state(self._config(run_id))
        waiting = [i.value for task in state.tasks for i in task.interrupts]
        return Snapshot(dict(state.values), waiting[0] if waiting else None, tuple(state.next))
