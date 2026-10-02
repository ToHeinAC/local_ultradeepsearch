"""Files of an approved or saved brief (PRD M4): the immutable archive and per-session drafts."""

import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from app.artifacts import write_text
from app.brief.render import canonical_text

_SESSION_ID = re.compile(r"s[0-9a-f]{12}")


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_temp(directory: Path, stem: str, data: bytes) -> Path:
    """Complete, fsynced and read-only before it gets its final name."""
    tmp = directory / f".{stem}.{os.getpid()}.{threading.get_ident()}.tmp"
    with tmp.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.chmod(0o444)
    return tmp


def archive_brief(directory: Path, text: str, approved_at: datetime) -> Path:
    """Store the approved brief as ``<UTC timestamp>.md``: exactly its canonical UTF-8 bytes, no
    front matter, read-only. Archiving the same bytes again returns the same file (a resumed
    `finalize`); other bytes in the same second get `-2`, `-3`, ... The file appears complete or
    not at all."""
    data = canonical_text(text).encode("utf-8")
    directory.mkdir(parents=True, exist_ok=True)
    stem = approved_at.astimezone(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    number = 1
    while True:
        path = directory / (f"{stem}.md" if number == 1 else f"{stem}-{number}.md")
        if path.exists():
            if path.read_bytes() == data:
                return path
            number += 1
            continue
        tmp = _write_temp(directory, stem, data)
        try:
            os.link(tmp, path)  # atomic, and fails if the name is taken
        except FileExistsError:
            continue  # lost a race for the name: look at it again
        finally:
            tmp.unlink(missing_ok=True)
        _fsync_dir(directory)
        return path


def write_draft(directory: Path, session_id: str, text: str) -> Path:
    """The current draft of a saved session, `<session id>.md`, replaced on each save."""
    if not _SESSION_ID.fullmatch(session_id):
        raise ValueError(f"invalid session id {session_id!r}")
    path = directory / f"{session_id}.md"
    write_text(path, canonical_text(text), scrub=False)
    return path
