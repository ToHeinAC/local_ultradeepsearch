"""AGENTS.md section 5.2: LangGraph is imported only in `src/app/graphs/`, the web stack
(`fastapi`, `starlette`, `uvicorn`, `mcp`) only in `src/app/api/`, `streamlit` only in
`src/app/gui/`, and `src/app/gui/` imports from `app` only the API client (PRD M7 AC1)."""

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


GUI = "app/gui/"
GUI_ALLOWED_APP = ("app.client", "app.gui")


def streamlit_imports(source: str) -> list[str]:
    return imports_of(source, frozenset({"streamlit"}))


def gui_app_imports(source: str) -> list[str]:
    """The `app` modules ``source`` imports that a GUI module may not (anything but the client
    and the GUI's own modules). A relative import above the package counts as one."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if _forbidden_for_gui(a.name)}
        elif isinstance(node, ast.ImportFrom):
            found |= _forbidden_from(node)
    return sorted(found)


def _forbidden_for_gui(name: str) -> bool:
    if name != "app" and not name.startswith("app."):
        return False
    return not any(name == ok or name.startswith(f"{ok}.") for ok in GUI_ALLOWED_APP)


def _forbidden_from(node: ast.ImportFrom) -> set[str]:
    if node.level > 1:
        return {"." * node.level + (node.module or "")}
    if node.level == 1 or node.module is None:
        return set()
    if node.module == "app":  # `from app import client` names the module in the alias
        return {f"app.{a.name}" for a in node.names if _forbidden_for_gui(f"app.{a.name}")}
    return {node.module} if _forbidden_for_gui(node.module) else set()


def gui_violations(root: Path = SRC) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        source = path.read_text("utf-8")
        if not relative.startswith(GUI) and (bad := streamlit_imports(source)):
            found[relative] = bad
        if relative.startswith(GUI) and (bad := gui_app_imports(source)):
            found[relative] = bad
    return found


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from app.client import ApiClient\n", []),
        ("from app import client\n", []),
        ("from app.gui import texts\n", []),
        ("from . import texts\n", []),
        ("import streamlit as st\n", []),
        ("import json\nfrom pathlib import Path\n", []),
        ("from app.store.vault import Vault\n", ["app.store.vault"]),
        ("import app.graphs.brief\n", ["app.graphs.brief"]),
        ("from app.adapters.outbound import gateway\n", ["app.adapters.outbound"]),
        ("from app import bootstrap\n", ["app.bootstrap"]),
        ("from app.pipeline.fetch import FetchPipeline\n", ["app.pipeline.fetch"]),
        ("from .. import config\n", [".."]),
        ("from app.clientele import x\n", ["app.clientele"]),  # not the client
    ],
)
def test_gui_detector(source: str, expected: list[str]) -> None:
    assert gui_app_imports(source) == expected


def test_gui_rules_flag_a_leak_in_both_directions(tmp_path: Path) -> None:
    (tmp_path / "app" / "gui").mkdir(parents=True)
    (tmp_path / "app" / "gui" / "ok.py").write_text("import streamlit\nfrom app.client import C\n")
    (tmp_path / "app" / "gui" / "leak.py").write_text("from app.store.runs import RunStore\n")
    (tmp_path / "app" / "cli.py").write_text("import streamlit\n")
    assert gui_violations(tmp_path) == {
        "app/cli.py": ["streamlit"],
        "app/gui/leak.py": ["app.store.runs"],
    }


def test_only_the_gui_imports_streamlit_and_it_imports_only_the_client() -> None:
    assert gui_violations() == {}
