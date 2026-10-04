"""Step 0: the files a run starts with (PRD M5, AD9). `query.md` is the approved brief, byte for
byte; `scaffold.md` is the run's private notebook, one `## ` section per topic."""

from pathlib import Path

from app.artifacts import write_text
from app.research.models import RunSpec

SCAFFOLD_TITLE = "# Scaffold"


def _sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """The preamble and the (heading, body) pairs of a scaffold."""
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for line in text.split("\n"):
        if line.startswith("## "):
            current = (line[3:].strip(), [])
            sections.append(current)
        elif current is None:
            preamble.append(line)
        else:
            current[1].append(line)
    pairs = [(heading, "\n".join(body).strip("\n")) for heading, body in sections]
    return "\n".join(preamble).strip("\n"), pairs


def set_scaffold_section(run_dir: Path, heading: str, text: str) -> None:
    """Replace the section ``heading`` of `scaffold.md`, or append it."""
    path = run_dir / "scaffold.md"
    old = path.read_text(encoding="utf-8") if path.exists() else SCAFFOLD_TITLE
    preamble, pairs = _sections(old)
    body = text.strip("\n")
    if any(h == heading for h, _ in pairs):
        pairs = [(h, body if h == heading else b) for h, b in pairs]
    else:
        pairs.append((heading, body))
    blocks = [preamble or SCAFFOLD_TITLE] + [f"## {h}\n\n{b}" for h, b in pairs]
    write_text(path, "\n\n".join(blocks) + "\n")


def bootstrap_workspace(run_dir: Path, spec: RunSpec, brief: str) -> None:
    """Write `query.md` (exact bytes, never scrubbed) and the run config. Safe to repeat."""
    run_dir.mkdir(parents=True, exist_ok=True)
    write_text(run_dir / "query.md", brief, scrub=False)
    config = [
        f"- Run: {spec.run_id}",
        f"- Tier: {spec.tier}",
        f"- Template: {spec.template_id}",
        f"- Format: {spec.response_format}",
        f"- Language: {spec.report_language}",
    ]
    set_scaffold_section(run_dir, "Run config", "\n".join(config))
