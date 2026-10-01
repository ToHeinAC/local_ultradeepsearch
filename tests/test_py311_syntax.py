"""CI also runs Python 3.11 (pyproject `requires-python`); newer-only syntax must not slip in."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CODE_DIRS = ("src", "tests", ".claude/hooks")
FLOOR = (3, 11)


def syntax_errors(source: str) -> list[str]:
    """Return a message if ``source`` does not parse under the oldest supported Python."""
    try:
        ast.parse(source, feature_version=FLOOR)
    except SyntaxError as exc:
        return [exc.msg]
    return []


@pytest.mark.parametrize(
    "source",
    [
        "def first[T](items: list[T]) -> T: return items[0]\n",
        "type Alias = list[int]\n",
        "class Box[T]: pass\n",
    ],
)
def test_detects_syntax_newer_than_the_floor(source: str) -> None:
    assert syntax_errors(source)


def test_accepts_floor_syntax() -> None:
    assert syntax_errors("from typing import TypeVar\nT = TypeVar('T')\n") == []


def test_repository_code_parses_on_the_floor_version() -> None:
    bad = {
        str(path.relative_to(ROOT)): errors
        for d in CODE_DIRS
        for path in (ROOT / d).rglob("*.py")
        if (errors := syntax_errors(path.read_text(encoding="utf-8")))
    }
    assert bad == {}
