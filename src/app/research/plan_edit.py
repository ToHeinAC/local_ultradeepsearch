"""Editing the search plan as text (PRD §3.2): one line per query in an editor, parsed back into
edits. A changed or new query goes through the denylist and sanitizer again; an unchanged one
keeps its result, so only what the owner touched is checked anew."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import get_args

from app.research.errors import InvalidEdit
from app.research.models import Lens, PlannedQuery, SearchPlan
from app.research.plan import QueryPreparer, kind_of, prepare

HEADER = (
    "# One query per line: id | item | lens | query\n"
    "# Change the text to have it checked again, delete a line to drop the query, and use - as\n"
    "# the id for a new one. Lenses: A breadth, B scholarly, C adversarial, D period-pinned.\n"
    "# Lines starting with # are ignored.\n"
)
_ID = re.compile(r"q(\d+)")
_NEW = ("-", "new")


@dataclass(frozen=True)
class EditedQuery:
    query_id: str | None  # None: a new query
    item: str
    lens: Lens
    text: str


def render_lines(plan: SearchPlan) -> str:
    """The plan as editable text; what the sanitizer made of each query is shown as a comment."""
    lines = [HEADER.rstrip("\n")]
    for query in plan.queries:
        lines.append(f"{query.query_id} | {query.item} | {query.lens} | {query.original}")
        if query.blocked:
            lines.append(f"#   BLOCKED: {query.blocked}")
        elif query.removed_terms:
            lines.append(f"#   sent: {query.sent} (removed: {', '.join(query.removed_terms)})")
        elif query.sent != query.original:
            lines.append(f"#   sent: {query.sent}")
    return "\n".join(lines) + "\n"


def _line(number: int, raw: str) -> EditedQuery:
    parts = [part.strip() for part in raw.split("|", 3)]
    if len(parts) != 4:
        raise InvalidEdit(f"line {number}: expected 'id | item | lens | query'")
    query_id, item, lens, text = parts
    if query_id not in _NEW and not _ID.fullmatch(query_id):
        raise InvalidEdit(f"line {number}: the id must be q<number>, - or new, got {query_id!r}")
    if lens not in get_args(Lens):
        raise InvalidEdit(f"line {number}: lens must be A, B, C or D, got {lens!r}")
    if not text:
        raise InvalidEdit(f"line {number}: the query is empty")
    return EditedQuery(None if query_id in _NEW else query_id, item, lens, text)  # type: ignore[arg-type]  # checked above


def parse_lines(text: str) -> list[EditedQuery]:
    """The edits in ``text``; blank lines and lines starting with # are skipped."""
    return [
        _line(number, line.strip())
        for number, line in enumerate(text.split("\n"), 1)
        if line.strip() and not line.strip().startswith("#")
    ]


def apply_edits(
    plan: SearchPlan,
    edits: Sequence[EditedQuery],
    *,
    preparer: QueryPreparer,
    known_items: set[str],
) -> SearchPlan:
    """The plan the edits describe, in their order. Lines left out are deleted. Ids of deleted
    queries are never given out again."""
    existing = {query.query_id: query for query in plan.queries}
    next_number = max((int(query_id[1:]) for query_id in existing), default=0) + 1
    seen: set[str] = set()
    result: list[PlannedQuery] = []
    for edit in edits:
        if edit.item not in known_items:
            raise InvalidEdit(f"unknown item {edit.item}")
        text = " ".join(edit.text.split())
        if edit.query_id is None:
            query_id, old = f"q{next_number:02d}", None
            next_number += 1
        else:
            query_id, old = edit.query_id, existing.get(edit.query_id)
            if old is None:
                raise InvalidEdit(f"unknown query {query_id}")
            if query_id in seen:
                raise InvalidEdit(f"{query_id} appears twice")
        seen.add(query_id)
        result.append(_planned(query_id, edit, text, old, preparer))
    return SearchPlan(queries=tuple(result))


def _planned(
    query_id: str,
    edit: EditedQuery,
    text: str,
    old: PlannedQuery | None,
    preparer: QueryPreparer,
) -> PlannedQuery:
    """A new or changed (or blocked) query is checked again; the others keep their result."""
    if old is None or old.original != text or old.blocked:
        return prepare(preparer, query_id, edit.item, edit.lens, text)
    return old.model_copy(update={"item": edit.item, "lens": edit.lens, "kind": kind_of(edit.lens)})
