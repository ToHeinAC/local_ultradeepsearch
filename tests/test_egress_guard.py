"""PRD §3.2 / M2 AC6: only the outbound package (and the loopback-only Ollama transport) may
import network-capable modules. Everything else must go through the gateway."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
NETWORK_MODULES = (
    "httpx",
    "requests",
    "urllib.request",
    "urllib3",
    "http.client",
    "socket",
    "aiohttp",
    "tavily",
    "ddgs",
    "primp",
    "ollama",
)
ALLOWED = ("app/adapters/outbound/", "app/adapters/ollama_transport.py")


def _is_network(name: str) -> bool:
    return any(name == module or name.startswith(f"{module}.") for module in NETWORK_MODULES)


def network_imports(source: str) -> list[str]:
    """Network-capable modules imported by ``source``, each reported once."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names if _is_network(alias.name)}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _is_network(node.module):
                found.add(node.module)
            else:  # `from urllib import request`
                dotted = (f"{node.module}.{alias.name}" for alias in node.names)
                found |= {name for name in dotted if _is_network(name)}
    return sorted(found)


def violations(root: Path = SRC) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(ALLOWED):
            continue
        if bad := network_imports(path.read_text(encoding="utf-8")):
            found[relative] = bad
    return found


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import httpx\n", ["httpx"]),
        ("from urllib import request\n", ["urllib.request"]),
        ("import urllib.request as r\n", ["urllib.request"]),
        ("from ddgs.exceptions import DDGSException\n", ["ddgs.exceptions"]),
        ("from socket import create_connection\n", ["socket"]),
        ("import ollama, json\n", ["ollama"]),
        ("from urllib.parse import urlsplit\n", []),
        ("import json\nfrom http import HTTPStatus\n", []),
        ("from . import httpx_helpers\n", []),  # relative imports are our own modules
    ],
)
def test_detector(source: str, expected: list[str]) -> None:
    assert network_imports(source) == expected


def test_detector_flags_a_violating_module(tmp_path: Path) -> None:
    (tmp_path / "app" / "llm").mkdir(parents=True)
    (tmp_path / "app" / "adapters" / "outbound").mkdir(parents=True)
    (tmp_path / "app" / "llm" / "leak.py").write_text("import httpx\n")
    (tmp_path / "app" / "adapters" / "outbound" / "ok.py").write_text("import httpx\n")
    (tmp_path / "app" / "adapters" / "ollama_transport.py").write_text("import ollama\n")
    assert violations(tmp_path) == {"app/llm/leak.py": ["httpx"]}


def test_only_the_gateway_reaches_the_network() -> None:
    assert violations() == {}


# ---- Phase 1 never touches the gateway (PRD M4 AC3) --------------------------------------------

OUTBOUND = "app.adapters.outbound"
PHASE1_FILES = (
    "app/brief/",
    "app/graphs/brief.py",
    "app/store/sessions.py",
    "app/templates.py",
    "app/documents.py",
)


def _is_outbound(name: str) -> bool:
    return name == OUTBOUND or name.startswith(f"{OUTBOUND}.")


def outbound_imports(source: str) -> list[str]:
    """Imports of the outbound package (or anything inside it) in ``source``, each reported once."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if _is_outbound(a.name)}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _is_outbound(node.module):
                found.add(node.module)
            elif node.module == "app.adapters" and any(a.name == "outbound" for a in node.names):
                found.add(OUTBOUND)
    return sorted(found)


def phase1_violations(root: Path = SRC) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(PHASE1_FILES) and (bad := outbound_imports(path.read_text("utf-8"))):
            found[relative] = bad
    return found


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from app.adapters.outbound.gateway import OutboundGateway\n", [f"{OUTBOUND}.gateway"]),
        ("import app.adapters.outbound.tavily\n", [f"{OUTBOUND}.tavily"]),
        ("from app.adapters import outbound\n", [OUTBOUND]),
        ("from app.adapters.ollama_transport import OllamaTransport\n", []),
        (
            "from app.adapters.outbound_like import x\n",
            [],
        ),  # a different package, not a prefix match
        ("from . import outbound\n", []),  # relative imports are our own modules
    ],
)
def test_outbound_detector(source: str, expected: list[str]) -> None:
    assert outbound_imports(source) == expected


def test_detector_flags_phase1_code_that_imports_the_gateway(tmp_path: Path) -> None:
    for name in ("app/brief/leak.py", "app/graphs/brief.py", "app/templates.py"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("from app.adapters.outbound.gateway import OutboundGateway\n")
    research = tmp_path / "app" / "graphs" / "research.py"  # Phase 2 may use the gateway
    research.write_text("from app.adapters.outbound.gateway import OutboundGateway\n")
    assert sorted(phase1_violations(tmp_path)) == [
        "app/brief/leak.py",
        "app/graphs/brief.py",
        "app/templates.py",
    ]


def test_phase1_code_does_not_import_the_gateway() -> None:
    assert phase1_violations() == {}
