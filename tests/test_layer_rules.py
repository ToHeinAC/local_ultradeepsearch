"""AGENTS.md section 5.2: LangGraph is imported only in `src/app/graphs/`."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
ALLOWED = "app/graphs/"


def langgraph_imports(source: str) -> list[str]:
    """The langgraph modules ``source`` imports, each reported once."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.split(".")[0] == "langgraph"}
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.level == 0
            and node.module.split(".")[0] == "langgraph"
        ):
            found.add(node.module)
    return sorted(found)


def violations(root: Path = SRC) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if not relative.startswith(ALLOWED) and (bad := langgraph_imports(path.read_text("utf-8"))):
            found[relative] = bad
    return found


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import langgraph\n", ["langgraph"]),
        ("from langgraph.graph import StateGraph\n", ["langgraph.graph"]),
        ("import langgraph.checkpoint.sqlite as s\n", ["langgraph.checkpoint.sqlite"]),
        ("from langgraph.types import interrupt, Command\n", ["langgraph.types"]),
        ("import langgraph_swarm\n", []),  # a different package
        ("from app.graphs import brief\n", []),
        ("from . import langgraph_helpers\n", []),
    ],
)
def test_detector(source: str, expected: list[str]) -> None:
    assert langgraph_imports(source) == expected


def test_detector_flags_a_module_outside_the_graphs_package(tmp_path: Path) -> None:
    (tmp_path / "app" / "graphs").mkdir(parents=True)
    (tmp_path / "app" / "brief").mkdir(parents=True)
    (tmp_path / "app" / "graphs" / "ok.py").write_text("from langgraph.graph import END\n")
    (tmp_path / "app" / "brief" / "leak.py").write_text("from langgraph.types import interrupt\n")
    (tmp_path / "app" / "bootstrap.py").write_text("import langgraph\n")
    assert violations(tmp_path) == {
        "app/bootstrap.py": ["langgraph"],
        "app/brief/leak.py": ["langgraph.types"],
    }


def test_only_the_graphs_package_imports_langgraph() -> None:
    assert violations() == {}
