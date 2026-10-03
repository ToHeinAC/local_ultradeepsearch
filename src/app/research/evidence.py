"""The evidence a section is written from (PRD M5, D9).

A run reads 8-15 must-read sources. Each gets a stable key (`S1`, `S2`, ...) that the drafting
model cites; code later turns keys into numbered citations. A section's pack holds the summaries
of all must-read sources, their claims ranked by relevance to the section, and passages found by
full-text search. What does not fit the prompt is condensed by the `summarize` role, never cut
silently: every condensing and every drop is an event.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.artifacts import write_json
from app.events import EventSink
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.profiles import RunRules
from app.prompts import research as prompts
from app.prompts.untrusted import fence_untrusted
from app.research.models import CondensedEvidence
from app.store.models import Note
from app.store.vault import Vault
from app.text import normalize_for_match

_WORD = re.compile(r"\w+")
_HEADING = re.compile(r"(?m)^#{1,6}\s*")
MIN_TOKEN = 3
CONDENSED_URL = "condensed"
ItemKind = Literal["claim", "passage", "analysis"]


def eligible(note: Note) -> bool:
    """A note the report may cite: a complete, original, readable source that is not retracted."""
    return (
        note.kind == "source"
        and note.stage == "complete"
        and note.derivative_of is None
        and not note.extract_failed
        and note.meta.get("is_retracted") is not True
    )


def select_must_read(
    notes: Sequence[Note],
    quality: Mapping[str, float],
    provenance: Mapping[str, tuple[str, ...]],
    item_ids: Sequence[str],
    count: tuple[int, int],
) -> list[str]:
    """Up to ``count[1]`` note ids: the items take turns, each giving its best-quality source,
    then the sources no item claims fill what is left, best first."""
    usable = [n for n in notes if eligible(n)]

    def best_first(group: Sequence[Note]) -> list[Note]:
        return sorted(group, key=lambda n: (-quality.get(n.note_id, 0.0), n.note_id))

    per_item = {
        item: best_first([n for n in usable if item in provenance.get(n.note_id, ())])
        for item in item_ids
    }
    cap = count[1]
    chosen: dict[str, None] = {}
    cursor = dict.fromkeys(item_ids, 0)
    while len(chosen) < cap:
        before = len(chosen)
        for item in item_ids:
            ranked = per_item[item]
            while cursor[item] < len(ranked) and ranked[cursor[item]].note_id in chosen:
                cursor[item] += 1
            if cursor[item] < len(ranked) and len(chosen) < cap:
                chosen[ranked[cursor[item]].note_id] = None
        if len(chosen) == before:
            break
    for note in best_first([n for n in usable if n.note_id not in chosen]):
        if len(chosen) >= cap:
            break
        chosen[note.note_id] = None
    return list(chosen)


class EvidenceKeys:
    """Evidence keys of a run in `temp/evidence-keys.json`. Keys are only ever added, so a key
    in a stored section keeps meaning the same note after a restart."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._keys: dict[str, str] = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        )

    def key_for(self, note_id: str) -> str:
        for key, existing in self._keys.items():
            if existing == note_id:
                return key
        key = f"S{len(self._keys) + 1}"
        self._keys[key] = note_id
        write_json(self._path, self._keys)
        return key

    def note_for(self, key: str) -> str | None:
        return self._keys.get(key)

    def mapping(self) -> dict[str, str]:
        return dict(self._keys)


def section_query(heading: str, instructions: str, questions: Sequence[str]) -> str:
    """What a section is about, for ranking claims and searching passages."""
    return " ".join([heading, instructions, *questions]).strip()


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(normalize_for_match(text)) if len(w) >= MIN_TOKEN}


def _relevance(query: set[str], text: str) -> float:
    found = _tokens(text)
    return len(query & found) / (len(found) ** 0.5 + 1)


def _best_paragraph(body: str, query: set[str], limit: int) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    best = max(paragraphs, key=lambda p: len(query & _tokens(p)), default="")
    if len(best) <= limit:
        return best
    return best[:limit].rsplit(" ", 1)[0]


@dataclass(frozen=True)
class Pack:
    text: str  # empty when the run has no evidence
    keys: tuple[str, ...]  # the evidence keys that appear in ``text``


@dataclass(frozen=True)
class _Item:
    key: str
    kind: ItemKind
    text: str
    rank: float


@dataclass
class _Block:
    key: str
    note: Note
    summary: str
    items: list[_Item] = field(default_factory=lambda: [])


def _render_block(block: _Block, included: set[int], items: Sequence[_Item]) -> str:
    lines = [f"[{block.key}] {' '.join(block.note.title.split())}"]
    if block.summary.strip():
        lines.append(f"Summary: {' '.join(block.summary.split())}")
    for kind, label in (("claim", "Claims"), ("analysis", "Analysis"), ("passage", "Passages")):
        chosen = [
            i.text for i in items if id(i) in included and i.kind == kind and i.key == block.key
        ]
        if chosen:
            lines += [f"{label}:", *chosen]
    return fence_untrusted(block.note.url, "\n".join(lines))


class PackBuilder:
    def __init__(
        self,
        vault: Vault,
        keys: EvidenceKeys,
        service: LLMService,
        rules: RunRules,
        events: EventSink,
    ) -> None:
        self._vault = vault
        self._keys = keys
        self._service = service
        self._rules = rules
        self._events = events

    def build(
        self,
        *,
        section: str,
        query: str,
        must_read: Sequence[str],
        budget_chars: int,
        condense_chars: int,
    ) -> Pack:
        """The evidence of one section within ``budget_chars``. The summaries of the must-read
        sources always come; claims, analyses and passages follow by relevance; the rest is
        condensed to ``condense_chars`` per call, and dropped loudly if even that does not fit."""
        wanted = _tokens(query)
        blocks = self._blocks(must_read, wanted)
        for note in self._passage_notes(query, must_read):
            blocks.append(self._passage_block(note, wanted))
        items = sorted((i for b in blocks for i in b.items), key=lambda i: -i.rank)
        included, overflow = self._fit(blocks, items, budget_chars)
        if overflow:  # keep room for the condensed rest
            reserve = int(budget_chars * self._rules.condensed_share)
            included, overflow = self._fit(blocks, items, budget_chars - reserve)
        text = "\n\n".join(_render_block(b, included, items) for b in blocks)
        if overflow:
            text = self._with_condensed(
                text, blocks, overflow, section, budget_chars, condense_chars
            )
        keys = tuple(b.key for b in blocks if f"[{b.key}]" in text)
        return Pack(text, keys)

    # ---- collecting -----------------------------------------------------------------------

    def _blocks(self, must_read: Sequence[str], wanted: set[str]) -> list[_Block]:
        blocks: list[_Block] = []
        for note_id in must_read:
            note = self._vault.get_note(note_id)
            if note is None:
                continue
            block = _Block(self._keys.key_for(note_id), note, note.summary)
            for claim in self._vault.claims(note_id):
                text = f'- {" ".join(claim.claim.split())} — "{claim.quoted_support}"'
                block.items.append(
                    _Item(
                        block.key,
                        "claim",
                        text,
                        _relevance(wanted, claim.claim + claim.quoted_support),
                    )
                )
            analysis = self._vault.find_analysis(note_id)
            if analysis is not None:
                body = _HEADING.sub("", analysis.body).strip()
                block.items.append(_Item(block.key, "analysis", body, _relevance(wanted, body)))
            blocks.append(block)
        return blocks

    def _passage_notes(self, query: str, must_read: Sequence[str]) -> list[Note]:
        limit = self._rules.pack_passages
        hits = self._vault.search(query, limit=limit + len(must_read) + 5)
        found: list[Note] = []
        for hit in hits:
            note = self._vault.get_note(hit.note_id)
            if note and eligible(note) and note.note_id not in must_read:
                found.append(note)
        return found[:limit]

    def _passage_block(self, note: Note, wanted: set[str]) -> _Block:
        block = _Block(self._keys.key_for(note.note_id), note, "")
        passage = _best_paragraph(note.body, wanted, self._rules.passage_chars)
        block.items.append(
            _Item(block.key, "passage", f"- {passage}", 1.0 + _relevance(wanted, passage))
        )
        return block

    # ---- fitting --------------------------------------------------------------------------

    @staticmethod
    def _fit(
        blocks: Sequence[_Block], items: Sequence[_Item], budget: int
    ) -> tuple[set[int], list[_Item]]:
        """All items, then the least relevant dropped one by one until the pack fits ``budget``.
        The summaries stay either way. Returns the kept items' ids and the dropped ones, most
        relevant first."""
        included = {id(item) for item in items}
        overflow: list[_Item] = []
        while included and sum(len(_render_block(b, included, items)) for b in blocks) > budget:
            lowest = next(item for item in reversed(items) if id(item) in included)
            included.discard(id(lowest))
            overflow.insert(0, lowest)
        return included, overflow

    # ---- condensing -----------------------------------------------------------------------

    def _with_condensed(
        self,
        text: str,
        blocks: Sequence[_Block],
        overflow: Sequence[_Item],
        section: str,
        budget: int,
        condense_chars: int,
    ) -> str:
        valid = {b.key for b in blocks}
        lines: list[str] = []
        for chunk in self._chunks(blocks, overflow, condense_chars):
            answer = self._condense(section, chunk)
            lines += [
                f"[{x.key}] {' '.join(x.text.split())}" for x in answer.lines if x.key in valid
            ]
        self._events.emit(
            "evidence_condensed", section=section, items=len(overflow), lines=len(lines)
        )
        kept: list[str] = []
        room = budget - len(text) - len(fence_untrusted(CONDENSED_URL, ""))
        for line in lines:
            if len(line) + 1 > room:
                break
            kept.append(line)
            room -= len(line) + 1
        if len(kept) < len(lines):
            self._events.emit(
                "evidence_dropped", level="warning", section=section, lines=len(lines) - len(kept)
            )
        if not kept:
            return text
        return text + "\n\n" + fence_untrusted(CONDENSED_URL, "\n".join(kept))

    @staticmethod
    def _chunks(
        blocks: Sequence[_Block], overflow: Sequence[_Item], limit: int
    ) -> list[list[tuple[_Block, _Item]]]:
        by_key = {b.key: b for b in blocks}
        chunks: list[list[tuple[_Block, _Item]]] = [[]]
        size = 0
        for item in overflow:
            cost = len(item.text) + len(by_key[item.key].note.url) + 64
            if chunks[-1] and size + cost > limit:
                chunks.append([])
                size = 0
            chunks[-1].append((by_key[item.key], item))
            size += cost
        return chunks

    def _condense(self, section: str, chunk: Sequence[tuple[_Block, _Item]]) -> CondensedEvidence:
        grouped: dict[str, list[str]] = {}
        urls: dict[str, str] = {}
        for block, item in chunk:
            urls[block.key] = block.note.url
            grouped.setdefault(block.key, []).append(f"[{block.key}] {item.text.lstrip('- ')}")
        evidence = "\n".join(fence_untrusted(urls[k], "\n".join(v)) for k, v in grouped.items())
        messages: list[Message] = [
            {"role": "system", "content": prompts.CONDENSE_SYSTEM},
            {
                "role": "user",
                "content": prompts.CONDENSE_USER.format(heading=section, evidence=evidence),
            },
        ]
        return self._service.structured(Role.SUMMARIZE, messages, CondensedEvidence)
