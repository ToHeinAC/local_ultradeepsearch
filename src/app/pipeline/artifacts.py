"""Inspectable files of a run: one markdown file per note, and the statistics in `run.json`.

The vault (SQLite) is the source of truth; these files are a readable copy and can be regenerated
at any time with `export_note_files`. The note body is written exactly as stored, because it is
the text every quote is checked against.
"""

import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from app.artifacts import write_json, write_text
from app.store.models import ClaimRecord, Note, RunStats
from app.store.vault import Vault

_RUN_JSON_LOCK = threading.Lock()  # one read-modify-write of run.json at a time, in this process
NOT_EXTRACTED = "(not extracted yet)"
EM_DASH = chr(0x2014)


def note_path(run_dir: Path, note_id: str) -> Path:
    return run_dir / "notes" / f"{note_id}.md"


def _front_matter(note: Note) -> str:
    fields: dict[str, Any] = {
        "note_id": note.note_id,
        "kind": note.kind,
        "stage": note.stage,
        "url": note.url,
        "final_url": note.final_url,
        "title": note.title,
        "source_tier": note.source_tier,
        "doi": note.doi,
        "derivative_of": note.derivative_of,
        "analysis_of": note.analysis_of,
        "claims_kept": note.claims_kept,
        "claims_dropped": note.claims_dropped,
        "extract_failed": note.extract_failed,
    }
    # JSON scalars are valid YAML, and need no escaping rules of their own
    lines = [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items()]
    return "---\n" + "\n".join(lines) + "\n---\n"


def _claim_line(claim: ClaimRecord) -> str:
    text = " ".join(claim.claim.split())
    meta = f"{claim.stance}, {claim.evidence_type}, {claim.confidence}"
    return f'- [{claim.claim_id}] {text} — "{claim.quoted_support}" ({meta})'


def render_note_file(note: Note, claims: Sequence[ClaimRecord]) -> str:
    """The note as a markdown file: front matter, summary, claims and the verbatim source text.
    An analysis note is its own markdown and follows the front matter directly."""
    head = _front_matter(note)
    if note.kind == "source_analysis":
        return f"{head}\n{note.body.rstrip(chr(10))}\n"
    summary = NOT_EXTRACTED if note.stage == "fetched" else note.summary.strip() or "None stated."
    claim_lines = "\n".join(_claim_line(c) for c in claims) or "No claims."
    return (
        f"{head}\n## Summary\n\n{summary}\n\n## Claims\n\n{claim_lines}\n\n"
        f"## Source text\n\n{note.body}\n"
    )


def write_note_file(run_dir: Path, note: Note, claims: Sequence[ClaimRecord]) -> Path:
    path = note_path(run_dir, note.note_id)
    write_text(path, render_note_file(note, claims), scrub=False)
    return path


def export_note_files(vault: Vault, run_dir: Path) -> int:
    """Write every note whose file is missing or differs from the stored note; returns how many."""
    written = 0
    for note in vault.notes():
        rendered = render_note_file(note, vault.claims(note.note_id))
        path = note_path(run_dir, note.note_id)
        if path.exists() and path.read_text(encoding="utf-8") == rendered:
            continue
        write_text(path, rendered, scrub=False)
        written += 1
    return written


def _load_run_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} (run.json) is not valid JSON") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} (run.json) must hold a JSON object")
    return dict(cast("dict[str, object]", loaded))


def read_run_json(run_dir: Path) -> dict[str, object]:
    """The content of `run.json`, empty if it does not exist yet. A damaged file is an error."""
    with _RUN_JSON_LOCK:
        return _load_run_json(run_dir / "run.json")


def merge_run_json(run_dir: Path, updates: Mapping[str, object]) -> dict[str, object]:
    """Merge ``updates`` into `run.json` (created if missing) and return the result.

    Keys not in ``updates`` are kept. A damaged file is reported, never overwritten.
    """
    path = run_dir / "run.json"
    with _RUN_JSON_LOCK:
        data = _load_run_json(path)
        data.update(updates)
        write_json(path, data)
    return data


def write_run_stats(run_dir: Path, stats: RunStats) -> None:
    merge_run_json(run_dir, {"stats": asdict(stats)})
