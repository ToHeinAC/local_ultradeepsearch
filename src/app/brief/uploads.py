"""Uploads of a Phase-1 session (PRD M4): validation, storage, reading with OCR, distillation.

Every step saves its progress, so a stopped session continues where it stopped (PRD AD10):
- the files are written (complete or not at all) before their rows, and a failed batch removes
  what it wrote;
- extraction (and OCR) is saved once per file, distillation once per part of a file;
- the digest is rebuilt when a file finished, or when facts exist and the digest is still empty.
Phase 1 makes no outbound requests: this module talks to local models only.
"""

import base64
import hashlib
import math
import os
import re
import shutil
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath

from app.brief.digest import FactLine, build_digest
from app.brief.errors import NotFound, UploadRejected
from app.brief.labels import labels_for
from app.brief.schemas import UploadFacts
from app.documents import (
    DocumentError,
    decode_text,
    docx_text,
    pdf_page_count,
    read_pdf,
    render_page_png,
    split_pages,
)
from app.events import EventSink
from app.llm.errors import LLMError, LLMModelMissingError
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.analysis import MAP_CHUNK_CHARS
from app.pipeline.chunking import split_paragraph_chunks
from app.pipeline.profiles import Phase1Limits
from app.prompts.brief import OCR_PAGE, UPLOAD_FACTS_SYSTEM, UPLOAD_FACTS_USER
from app.prompts.untrusted import fence_untrusted
from app.store.sessions import Fact, NewUpload, SessionStore, UploadKind, UploadRow

PART_CHARS = (
    MAP_CHUNK_CHARS  # text per distillation call (about 9300 tokens of the summarize context)
)
MIB = 1024 * 1024
NAME_MAX = 120
KINDS: dict[str, UploadKind] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class UploadFile:
    name: str
    data: bytes


@dataclass(frozen=True)
class CheckedUpload:
    name: str
    kind: UploadKind
    data: bytes
    pages: int
    sha256: str


@dataclass(frozen=True)
class Part:
    """Consecutive pages (or a piece of one long page) read in one model call."""

    index: int
    pages: tuple[int, ...]
    text: str


# ---- validation -----------------------------------------------------------------------------


def display_name(raw: str) -> str:
    """A plain file name: no directories, no control characters, at most NAME_MAX characters."""
    name = _CONTROL.sub("", raw.replace("\\", "/")).split("/")[-1].strip()
    if not name.strip(". "):
        return "upload"
    if len(name) > NAME_MAX:
        stem, _, extension = name.rpartition(".")
        keep = NAME_MAX - len(extension) - 1
        name = f"{stem[:keep]}.{extension}" if stem and keep > 0 else name[:NAME_MAX]
    return name


def _text_pages(characters: int, limits: Phase1Limits) -> int:
    return max(1, math.ceil(characters / limits.pseudo_page_chars))


def _count_pages(name: str, kind: UploadKind, data: bytes, limits: Phase1Limits) -> int:
    """Pages of a valid file; any problem with its content is a rejection naming the file."""
    magic = {"pdf": b"%PDF", "docx": b"PK"}.get(kind)
    if magic is not None and not data.startswith(magic):
        raise UploadRejected(f"{name}: not a {kind.upper()} file")
    try:
        if kind == "pdf":
            return pdf_page_count(data)
        text = docx_text(data) if kind == "docx" else decode_text(data)
    except DocumentError as exc:
        raise UploadRejected(f"{name}: {exc}") from exc
    return _text_pages(len(text), limits)


def _check_one(file: UploadFile, limits: Phase1Limits) -> CheckedUpload:
    name = display_name(file.name)
    kind = KINDS.get(PurePath(name).suffix.lower())
    if not file.data:
        raise UploadRejected(f"{name}: the file is empty")
    if len(file.data) > limits.max_file_mb * MIB:
        raise UploadRejected(f"{name}: the file is larger than {limits.max_file_mb} MB")
    if kind is None:
        raise UploadRejected(f"{name}: file type not supported (use PDF, DOCX, MD or TXT)")
    pages = _count_pages(name, kind, file.data, limits)
    return CheckedUpload(name, kind, file.data, pages, hashlib.sha256(file.data).hexdigest())


def check_uploads(
    files: Sequence[UploadFile],
    limits: Phase1Limits,
    *,
    existing_files: int = 0,
    existing_pages: int = 0,
) -> list[CheckedUpload]:
    """Validate a whole batch against the session limits; the first violation rejects all of it
    (M4 AC8: nothing is partially stored)."""
    if existing_files + len(files) > limits.max_files:
        raise UploadRejected(
            f"at most {limits.max_files} files per session ({existing_files} already stored)"
        )
    checked = [_check_one(file, limits) for file in files]
    total = existing_pages + sum(c.pages for c in checked)
    if total > limits.max_total_pages:
        raise UploadRejected(
            f"the files add up to {total} pages, over the limit of {limits.max_total_pages}"
        )
    return checked


# ---- parts ----------------------------------------------------------------------------------


def _pack_blocks(blocks: Sequence[tuple[int, str]], chars: int) -> list[Part]:
    parts: list[Part] = []
    pages: list[int] = []
    texts: list[str] = []
    size = 0
    for number, block in blocks:
        extra = len(block) + (2 if texts else 0)
        if texts and size + extra > chars:
            parts.append(Part(len(parts), tuple(pages), "\n\n".join(texts)))
            pages, texts, size, extra = [], [], 0, len(block)
        if number not in pages:
            pages.append(number)
        texts.append(block)
        size += extra
    if texts:
        parts.append(Part(len(parts), tuple(pages), "\n\n".join(texts)))
    return parts


def plan_parts(pages: Sequence[str], chars: int) -> list[Part]:
    """Group pages, each introduced by a `[Page N]` marker, into parts of at most ``chars``.

    A page longer than ``chars`` is cut into pieces under its own number; pages without text are
    skipped but keep their numbers. The result depends only on the pages, so a resumed run finds
    the same parts as the run that stopped."""
    blocks: list[tuple[int, str]] = []
    for number, text in enumerate(pages, start=1):
        marker = f"[Page {number}]\n"
        for piece in split_paragraph_chunks(text.strip(), max(chars - len(marker), 1)):
            blocks.append((number, marker + piece))
    return _pack_blocks(blocks, chars)


# ---- the ingestor ---------------------------------------------------------------------------


class UploadIngestor:
    def __init__(
        self,
        store: SessionStore,
        llm: LLMService,
        limits: Phase1Limits,
        directory: Path,
        events: EventSink,
    ) -> None:
        self._store = store
        self._llm = llm
        self._limits = limits
        self._dir = directory
        self._events = events

    # ---- accepting files ------------------------------------------------------------------

    def _path(self, session_id: str, sha256: str, kind: str) -> Path:
        return self._dir / session_id / f"{sha256}.{kind}"

    def _write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with tmp.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def accept(self, session_id: str, files: Sequence[UploadFile]) -> list[UploadRow]:
        """Validate a batch, store the files, then record all rows in one transaction.

        A violation rejects the batch before anything is written; a failure while recording
        removes the files this call wrote."""
        if not files:
            return []
        known = self._store.uploads(session_id)
        checked = check_uploads(
            files,
            self._limits,
            existing_files=len(known),
            existing_pages=sum(row.pages for row in known),
        )
        written: list[Path] = []
        try:
            for upload in checked:
                path = self._path(session_id, upload.sha256, upload.kind)
                if not path.exists():
                    self._write(path, upload.data)
                    written.append(path)
            rows = self._store.add_uploads(
                session_id,
                [NewUpload(u.name, u.kind, len(u.data), u.pages, u.sha256) for u in checked],
            )
        except BaseException:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        for row in rows:
            self._events.emit(
                "upload_stored",
                session_id=session_id,
                name=row.name,
                kind=row.kind,
                pages=row.pages,
            )
        return rows

    def remove_session_files(self, session_id: str) -> None:
        """Delete every stored file of a session (the session itself was removed or never began)."""
        shutil.rmtree(self._dir / session_id, ignore_errors=True)

    def cleanup_orphans(self, session_id: str) -> int:
        """Delete files of this session that no row refers to (left by a crashed accept)."""
        folder = self._dir / session_id
        if not folder.is_dir():
            return 0
        referenced = {f"{u.sha256}.{u.kind}" for u in self._store.uploads(session_id)}
        orphans = [p for p in folder.iterdir() if p.is_file() and p.name not in referenced]
        for path in orphans:
            path.unlink(missing_ok=True)
        return len(orphans)

    # ---- reading --------------------------------------------------------------------------

    def _ocr(self, data: bytes, index: int) -> str:
        png = render_page_png(data, index, self._limits.ocr_dpi)
        message: Message = {
            "role": "user",
            "content": OCR_PAGE,
            "images": [base64.b64encode(png).decode("ascii")],
        }
        return self._llm.text(Role.OCR, [message]).strip()

    def _pdf_pages(self, name: str, data: bytes) -> tuple[list[str], list[str]]:
        """Text layer per page; a page with too little text is read by the OCR model."""
        pages = list(read_pdf(data).pages)
        warnings: list[str] = []
        ocr_off = False
        for index, layer in enumerate(pages):
            if ocr_off or len(layer) >= self._limits.ocr_min_chars:
                continue
            try:
                text = self._ocr(data, index)
            except LLMModelMissingError:
                warnings.append("ocr_unavailable")
                self._events.emit("upload_ocr_unavailable", level="warning", name=name)
                ocr_off = True  # every further scanned page would fail the same way
                continue
            except (LLMError, DocumentError):
                warnings.append(f"ocr_failed:{index + 1}")
                continue
            if len(text) > len(layer):
                pages[index] = text
        return pages, warnings

    def _read_pages(self, row: UploadRow, data: bytes) -> tuple[list[str], list[str]]:
        try:
            if row.kind == "pdf":
                return self._pdf_pages(row.name, data)
            text = docx_text(data) if row.kind == "docx" else decode_text(data)
        except DocumentError:
            return [], ["unreadable"]
        return split_pages(text, self._limits.pseudo_page_chars), []

    # ---- distilling -----------------------------------------------------------------------

    def _facts(self, row: UploadRow, part: Part, question: str) -> list[Fact]:
        messages: list[Message] = [
            {"role": "system", "content": UPLOAD_FACTS_SYSTEM},
            {
                "role": "user",
                "content": UPLOAD_FACTS_USER.format(
                    question=question,
                    name=row.name,
                    first=part.pages[0],
                    last=part.pages[-1],
                    max_facts=self._limits.max_facts_per_part,
                    fenced=fence_untrusted(row.name, part.text),
                ),
            },
        ]
        try:
            result = self._llm.structured(Role.SUMMARIZE, messages, UploadFacts, think=False)
        except LLMError as exc:
            self._events.emit(
                "upload_part_failed",
                level="warning",
                name=row.name,
                part=part.index,
                error=type(exc).__name__,
            )
            return []
        kept = [(f.fact, f.page) for f in result.facts if f.page in part.pages]
        return kept[: self._limits.max_facts_per_part]

    def _distill(self, session_id: str, row: UploadRow, question: str) -> None:
        done = self._store.parts(session_id, row.file_id)
        for part in plan_parts(row.page_texts, PART_CHARS):
            if part.index not in done:
                facts = self._facts(row, part, question)
                self._store.add_part(session_id, row.file_id, part.index, facts)

    def _row(self, session_id: str, file_id: str) -> UploadRow:
        return next(r for r in self._store.uploads(session_id) if r.file_id == file_id)

    def _advance(self, session_id: str, row: UploadRow, question: str) -> bool:
        """Bring one file to `distilled`; False if its bytes are missing from disk."""
        path = self._path(session_id, row.sha256, row.kind)
        if not path.is_file():
            self._events.emit("upload_file_missing", level="warning", name=row.name)
            return False
        if row.stage == "stored":
            pages, warnings = self._read_pages(row, path.read_bytes())
            self._store.save_pages(session_id, row.file_id, pages, warnings)
            row = self._row(session_id, row.file_id)
        self._distill(session_id, row, question)
        self._store.mark_distilled(session_id, row.file_id)
        return True

    # ---- the digest -----------------------------------------------------------------------

    def _refresh_digest(
        self, session_id: str, question: str, language: str, *, changed: bool
    ) -> bool:
        session = self._store.get(session_id)
        if session is None:
            raise NotFound(session_id)
        rows = [r for r in self._store.uploads(session_id) if r.stage == "distilled"]
        facts = [
            FactLine(row.name, page, fact)
            for row in rows
            for part_facts in self._store.parts(session_id, row.file_id).values()
            for fact, page in part_facts
        ]
        if not changed and not (facts and not session.upload_digest):
            return False
        pages: dict[str, int] = {}
        for row in rows:
            pages[row.name] = max(pages.get(row.name, 0), len(row.page_texts))
        digest = build_digest(
            self._llm,
            self._events,
            facts,
            pages=pages,
            question=question,
            labels=labels_for(language),
            limits=self._limits,
        )
        self._store.set_digest(session_id, digest.text, digest.notice)
        return True

    def process(self, session_id: str, question: str, language: str) -> bool:
        """Read and distil every file that is not finished, then refresh the digest.

        Safe to call again after any interruption; returns whether anything was done."""
        changed = False
        for row in self._store.uploads(session_id):
            if row.stage != "distilled" and self._advance(session_id, row, question):
                changed = True
        return self._refresh_digest(session_id, question, language, changed=changed)
