"""The ship gate with its fix rounds (PRD §3.10, D11).

The report is rendered from the section files and judged by G1-G12. For up to the configured
number of rounds, every failed check gets its fix and the report is judged again. The fixes
that are only allowed once (compress, expand, redraft) are remembered in `temp/gate-state.json`,
so a resumed gate never repeats them. What the gate could not fix leaves the run `blocked`:
`gate.json` lists every round and the checks still failing.
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.artifacts import write_json
from app.events import EventSink
from app.pipeline.profiles import RunRules
from app.research.draft import Drafter, DraftPlan, words_per_section
from app.research.evidence import EvidenceKeys
from app.research.fixes_code import (
    RETRACTION_NOTICE,
    acknowledge_retractions,
    citation_density,
    keyed_quote_failures,
    section_words,
    strip_markers,
    unquote,
)
from app.research.fixes_model import ModelFixes
from app.research.gate import GateInput, GateNote, GateResult, run_gate
from app.research.language import MIN_SAMPLE_CHARS, detect_code
from app.research.leakage import scrub
from app.research.markdown import body_of, body_words
from app.research.report import ApprovedBrief
from app.research.sections import load_section, save_section, write_report
from app.store.models import Note
from app.store.vault import Vault
from app.text import normalize_for_match

GATE_FILE = "gate.json"
STATE_FILE = Path("temp") / "gate-state.json"
SUMMARY_CHARS = 200


@dataclass(frozen=True)
class GateContext:
    """What the gate needs to know about the run."""

    run_dir: Path
    tier: str
    brief: ApprovedBrief
    brief_sha256: str
    plan: DraftPlan  # title, sections, questions, language, format, must-read, shim

    @property
    def headings(self) -> list[str]:
        return [section.heading for section in self.plan.sections]


@dataclass(frozen=True)
class GateOutcome:
    passed: bool
    result: GateResult
    rounds: list[dict[str, Any]]


def gate_notes(vault: Vault) -> dict[str, GateNote]:
    """The run's sources as the gate sees them: where they live, their text, retraction."""
    notes: dict[str, GateNote] = {}
    for note in vault.notes(kind="source"):
        urls = tuple(dict.fromkeys(u for u in (note.url, note.final_url, note.canonical_url) if u))
        notes[note.note_id] = GateNote(
            note.note_id, urls, note.body, note.meta.get("is_retracted") is True
        )
    return notes


def _load_state(run_dir: Path) -> dict[str, Any]:
    path = run_dir / STATE_FILE
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"rounds": [], "expanded": [], "compressed": [], "redrafted": []}


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ShipGate:
    def __init__(
        self,
        vault: Vault,
        keys: EvidenceKeys,
        rules: RunRules,
        events: EventSink,
        fixes: ModelFixes,
        drafter: Drafter,
    ) -> None:
        self._vault = vault
        self._keys = keys
        self._rules = rules
        self._events = events
        self._fixes = fixes
        self._drafter = drafter

    def run(self, ctx: GateContext) -> GateOutcome:
        """Judge, fix and judge again, at most `gate_fix_rounds` times; write `gate.json`."""
        state = _load_state(ctx.run_dir)
        while True:
            report, result = self._judge(ctx)
            rounds: list[dict[str, Any]] = state["rounds"]
            stuck = bool(rounds) and not rounds[-1]["fixes"]  # the last round changed nothing
            if result.passed or len(rounds) >= self._rules.gate_fix_rounds or stuck:
                break
            done = self._fix(ctx, result.failed, report, state)
            number = len(rounds) + 1
            rounds.append({"round": number, "failed": result.failed, "fixes": done})
            write_json(ctx.run_dir / STATE_FILE, state)
            self._events.emit("gate_round", round=number, failed=result.failed, fixes=len(done))
        outcome = GateOutcome(result.passed, result, state["rounds"])
        write_json(ctx.run_dir / GATE_FILE, {**result.to_dict(), "rounds": state["rounds"]})
        return outcome

    def _judge(self, ctx: GateContext) -> tuple[str, GateResult]:
        rendered = write_report(
            ctx.run_dir,
            title=ctx.plan.title,
            headings=ctx.headings,
            keys=self._keys.mapping(),
            vault=self._vault,
            language=ctx.plan.language,
            brief=ctx.brief,
            events=self._events,
        )
        files = frozenset(p.name for p in ctx.run_dir.iterdir() if p.is_file())
        result = run_gate(
            GateInput(
                report=rendered.markdown,
                brief_sha256=ctx.brief_sha256,
                required_headings=tuple(ctx.headings),
                language=ctx.plan.language,
                words_range=ctx.plan.fmt.words,
                tier=ctx.tier,
                notes=gate_notes(self._vault),
                artifacts=files,
                rules=self._rules,
            )
        )
        return rendered.markdown, result

    # ---- fixes ----------------------------------------------------------------------------

    def _fix(
        self, ctx: GateContext, failed: list[str], report: str, state: dict[str, Any]
    ) -> list[str]:
        handlers: Mapping[str, Callable[[], list[str]]] = {
            "G3": lambda: self._fix_length(ctx, report, state),
            "G4": lambda: self._fix_density(ctx),
            "G6": lambda: self._fix_quotes(ctx),
            "G7": lambda: self._fix_leakage(ctx),
            "G9": lambda: self._fix_retractions(ctx),
            "G12": lambda: self._fix_language(ctx, state),
        }
        done: list[str] = []
        for check in failed:
            if check in handlers:
                done += handlers[check]()
        return done

    def _sections(self, ctx: GateContext) -> list[tuple[int, str, str]]:
        return [
            (index, heading, load_section(ctx.run_dir, index) or "")
            for index, heading in enumerate(ctx.headings, 1)
        ]

    def _fix_quotes(self, ctx: GateContext) -> list[str]:
        texts = {n.note_id: normalize_for_match(n.body) for n in self._vault.notes(kind="source")}
        done: list[str] = []
        for index, _, text in self._sections(ctx):
            failures = keyed_quote_failures(
                text, self._keys.mapping(), texts, self._rules.quote_min_words
            )
            if failures:
                save_section(ctx.run_dir, index, unquote(text, failures))
                done.append(f"G6: removed quotation marks in section {index}")
        return done

    def _retracted(self) -> set[str]:
        by_id: dict[str, Note] = {n.note_id: n for n in self._vault.notes(kind="source")}
        return {
            key
            for key, note_id in self._keys.mapping().items()
            if note_id in by_id and by_id[note_id].meta.get("is_retracted") is True
        }

    def _fix_retractions(self, ctx: GateContext) -> list[str]:
        notice = RETRACTION_NOTICE.get(ctx.plan.language, RETRACTION_NOTICE["en"])
        retracted = self._retracted()
        done: list[str] = []
        for index, _, text in self._sections(ctx):
            fixed = acknowledge_retractions(
                text, retracted, notice, self._rules.retraction_window_chars
            )
            if fixed != text:
                save_section(ctx.run_dir, index, fixed)
                done.append(f"G9: marked retracted sources in section {index}")
        return done

    def _fix_leakage(self, ctx: GateContext) -> list[str]:
        done: list[str] = []
        for index, heading, text in self._sections(ctx):
            clean = scrub(text)
            if clean != text:
                save_section(ctx.run_dir, index, clean)
                done.append(f"G7: removed front matter or headers in section {index}")
            reworded = self._fixes.reword_leaks(ctx.run_dir, index, heading)
            if reworded:
                done.append(f"G7: reworded {reworded} sentences in section {index}")
        return done

    def _fix_language(self, ctx: GateContext, state: dict[str, Any]) -> list[str]:
        done: list[str] = []
        for index, _, text in self._sections(ctx):
            plain = strip_markers(text)
            wrong = len(plain) >= MIN_SAMPLE_CHARS and detect_code(plain) != ctx.plan.language
            if wrong and index not in state["redrafted"]:
                state["redrafted"].append(index)
                self._drafter.redraft_in_language(ctx.run_dir, ctx.plan, index)
                done.append(f"G12: wrote section {index} again in the report language")
        return done

    def _fix_density(self, ctx: GateContext) -> list[str]:
        sources = {
            key: f"{note.title}: {' '.join(note.summary.split())[:SUMMARY_CHARS]}"
            for key, note_id in self._keys.mapping().items()
            if (note := self._vault.get_note(note_id)) is not None
        }
        done: list[str] = []
        for index, heading, text in self._sections(ctx):
            if text and citation_density(text) < self._rules.citation_density_min:
                added = self._fixes.add_citations(ctx.run_dir, index, heading, sources)
                if added:
                    done.append(f"G4: added citations ({added} edits) in section {index}")
        return done

    def _fix_length(self, ctx: GateContext, report: str, state: dict[str, Any]) -> list[str]:
        """Too long: shorten the long sections. Too short: extend the short ones. Each section
        is tried once in each direction, whatever came of it."""
        too_long = body_words(body_of(report)) > ctx.plan.fmt.words[1]
        budget = words_per_section(ctx.plan.fmt, len(ctx.plan.sections))
        done: list[str] = []
        for index, heading, text in self._sections(ctx):
            size = section_words(text)
            if too_long and size > budget * self._rules.section_over_factor:
                done += self._shorten(ctx, state["compressed"], index, heading, budget)
            elif not too_long and size < budget * self._rules.section_under_factor:
                done += self._extend(ctx, state["expanded"], index, budget)
        return done

    def _shorten(
        self, ctx: GateContext, tried: list[int], index: int, heading: str, words: int
    ) -> list[str]:
        if index in tried:
            return []
        tried.append(index)
        ok = self._fixes.compress(ctx.run_dir, index, heading, words)
        return [f"G3: shortened section {index}"] if ok else []

    def _extend(self, ctx: GateContext, tried: list[int], index: int, words: int) -> list[str]:
        if index in tried:
            return []
        tried.append(index)
        ok = self._fixes.expand(ctx.run_dir, ctx.plan, index, words)
        return [f"G3: extended section {index}"] if ok else []
