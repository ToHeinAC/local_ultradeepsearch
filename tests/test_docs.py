"""Enforces AGENTS.md §5.1: docs stay small and every local link or @import resolves."""

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", ".pytest_cache", ".ruff_cache", "data", "htmlcov", "node_modules"}
MAX_LINES = {"AGENTS.md": 200, "IMPLEMENTATION.md": 500, "README.md": 300}
MAX_LINES_DEFAULT = 800
LINK = re.compile(r"\]\(([^)#\s]+)")  # markdown link target, anchor stripped
IMPORT = re.compile(r"(?:^|\s)@([\w./-]+\.md)\b")  # Claude Code @import


def markdown_files() -> list[Path]:
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        files += [Path(dirpath, f) for f in filenames if f.endswith(".md")]
    return files


def broken_refs(doc: Path) -> list[str]:
    text = doc.read_text(encoding="utf-8")
    targets = LINK.findall(text) + IMPORT.findall(text)
    return [t for t in targets if ":" not in t and not (doc.parent / t).exists()]


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_broken_refs_detects_missing_targets(tmp_path: Path) -> None:
    (tmp_path / "ok.md").write_text("")
    doc = tmp_path / "doc.md"
    doc.write_text("[a](ok.md) [b](missing.md) @gone.md [c](https://x.org) [d](ok.md#part)")
    assert broken_refs(doc) == ["missing.md", "gone.md"]


def test_local_links_resolve() -> None:
    broken = {_rel(p): refs for p in markdown_files() if (refs := broken_refs(p))}
    assert broken == {}


def test_docs_within_size_limits() -> None:
    too_long = {
        _rel(p): n
        for p in markdown_files()
        if (n := len(p.read_text(encoding="utf-8").splitlines()))
        > MAX_LINES.get(_rel(p), MAX_LINES_DEFAULT)
    }
    assert too_long == {}
