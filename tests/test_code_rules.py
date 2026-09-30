"""Enforces AGENTS.md §5.2: every function is at most MAX_FUNCTION_LINES lines long."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_DIRS = ("src", "tests", ".claude/hooks")
MAX_FUNCTION_LINES = 50


def long_functions(source: str, limit: int = MAX_FUNCTION_LINES) -> list[str]:
    """Return 'name (N lines)' for every function in ``source`` longer than ``limit``."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            if length > limit:
                found.append(f"{node.name} ({length} lines)")
    return found


def _function_of(length: int) -> str:
    body = "\n".join(f"    x{i} = {i}" for i in range(length - 1))
    return f"def f():\n{body}\n"


def test_detects_function_over_limit() -> None:
    assert long_functions(_function_of(MAX_FUNCTION_LINES + 1)) == ["f (51 lines)"]


def test_accepts_function_at_limit() -> None:
    assert long_functions(_function_of(MAX_FUNCTION_LINES)) == []


def test_repo_functions_within_limit() -> None:
    files = [p for d in CODE_DIRS for p in (ROOT / d).rglob("*.py")]
    offenders = {
        str(p.relative_to(ROOT)): found
        for p in files
        if (found := long_functions(p.read_text(encoding="utf-8")))
    }
    assert offenders == {}
