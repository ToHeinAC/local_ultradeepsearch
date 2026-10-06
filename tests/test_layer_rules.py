"""AGENTS.md section 5.2: LangGraph is imported only in `src/app/graphs/`, and the web stack
(`fastapi`, `starlette`, `uvicorn`, `mcp`) only in `src/app/api/`."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
ALLOWED = "app/graphs/"
WEB_ALLOWED = "app/api/"
WEB_PACKAGES = frozenset({"fastapi", "starlette", "uvicorn", "mcp"})


def imports_of(source: str, packages: frozenset[str]) -> list[str]:
    """The modules of ``packages`` that ``source`` imports, each reported once."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.split(".")[0] in packages}
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.level == 0
            and node.module.split(".")[0] in packages
        ):
            found.add(node.module)
    return sorted(found)


def langgraph_imports(source: str) -> list[str]:
    return imports_of(source, frozenset({"langgraph"}))


def web_imports(source: str) -> list[str]:
    return imports_of(source, WEB_PACKAGES)


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


def web_violations(root: Path = SRC) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if not relative.startswith(WEB_ALLOWED) and (bad := web_imports(path.read_text("utf-8"))):
            found[relative] = bad
    return found


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import fastapi\n", ["fastapi"]),
        ("from starlette.requests import Request\n", ["starlette.requests"]),
        ("import uvicorn\n", ["uvicorn"]),
        ("from mcp.server.fastmcp import FastMCP\n", ["mcp.server.fastmcp"]),
        ("import mcpx\n", []),  # a different package
        ("from app.api import rest\n", []),
    ],
)
def test_web_detector(source: str, expected: list[str]) -> None:
    assert web_imports(source) == expected


def test_web_detector_flags_a_module_outside_the_api_package(tmp_path: Path) -> None:
    (tmp_path / "app" / "api").mkdir(parents=True)
    (tmp_path / "app" / "api" / "ok.py").write_text("import fastapi\n")
    (tmp_path / "app" / "cli.py").write_text("import uvicorn\n")
    assert web_violations(tmp_path) == {"app/cli.py": ["uvicorn"]}


def test_only_the_api_package_imports_the_web_stack() -> None:
    assert web_violations() == {}
