"""Step 1 (PRD M5): decompose the brief into atomic items, check coverage, fix the report's
headings and word budgets, and render the shims. Every model answer is kept in a step journal, so
a resumed step 1 makes no call twice (AD10)."""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from app.artifacts import write_json, write_text
from app.brief.labels import labels_for, language_name
from app.events import EventSink
from app.llm.errors import LLMError
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.profiles import FormatRange, ResearchConfig
from app.pipeline.strategies import DomainName, DomainStrategy
from app.prompts import research as prompts
from app.research.journal import Journal
from app.research.models import Item
from app.research.schemas import CoverageMatrix, CoverageRow, Decomposition, Headings
from app.research.shims import compose_shims, write_shims
from app.research.workspace import set_scaffold_section
from app.templates import MAX_SECTIONS, MIN_SECTIONS, ReportTemplate, heading_problem
from app.templates import is_reserved_heading as reserved

_M = TypeVar("_M", bound=BaseModel)  # CI runs 3.11: no PEP 695 generics
_LEADING_HASHES = re.compile(r"^\s*#+\s*")


@dataclass(frozen=True)
class StepOneInput:
    brief: str
    template: ReportTemplate
    tier: str
    response_format: str
    fmt: FormatRange
    report_language: str
    domains: Mapping[DomainName, DomainStrategy]


@dataclass(frozen=True)
class StepOneResult:
    decomposition: Decomposition
    items: list[Item]
    headings: list[str]
    weights: list[float]
    budgets: list[int]
    gaps_left: list[CoverageRow]


# ---- pure helpers ---------------------------------------------------------------------------


def atomic_items(dec: Decomposition) -> list[Item]:
    """The searchable items: sub-questions, then entities, then time periods, ids i01, i02, ..."""
    found = [("sub_question", q) for q in dec.sub_questions]
    found += [("entity", e.name) for e in dec.entities]
    found += [("period", p.period) for p in dec.time_periods]
    return [Item(f"i{n:02d}", kind, text) for n, (kind, text) in enumerate(found, 1)]


def _strip(headings: Sequence[str]) -> list[str]:
    return [_LEADING_HASHES.sub("", h).strip() for h in headings]


def _fallback(dec: Decomposition, language: str) -> list[str]:
    """One heading per distinct sub-question; a background heading first if fewer than two."""
    headings: list[str] = []
    for question in dec.sub_questions:
        key = " ".join(question.split()).casefold()
        if not reserved(question) and key not in {" ".join(h.split()).casefold() for h in headings}:
            headings.append(" ".join(question.split()))
    if len(headings) < MIN_SECTIONS:
        headings.insert(0, labels_for(language).background)
    return headings[:MAX_SECTIONS]


def final_headings(
    template: ReportTemplate,
    dec: Decomposition,
    *,
    events: EventSink,
    repair: Callable[[str], list[str]],
    language: str,
) -> list[str]:
    """A fixed template's headings exactly; for `auto` the derived ones, validated, repaired once,
    else one per sub-question."""
    if not template.derived_headings:
        if dec.required_sections:
            events.emit(
                "brief_sections_overridden", template_id=template.id, sections=dec.required_sections
            )
        return list(template.headings)
    headings = _strip(dec.section_headings)
    problem = heading_problem(headings)
    if problem is None:
        return headings
    try:
        repaired = _strip(repair(problem))
    except LLMError:
        repaired = []
    if heading_problem(repaired) is None:
        return repaired
    events.emit("headings_fallback", level="warning", problem=problem)
    return _fallback(dec, language)


def section_weights(
    raw: Sequence[float], n: int, bounds: tuple[float, float], events: EventSink
) -> list[float]:
    """One weight per heading, clamped to ``bounds``; a wrong count means equal weights."""
    if len(raw) != n:
        events.emit("section_weights_reset", level="warning", got=len(raw), expected=n)
        return [1.0] * n
    low, high = bounds
    return [min(high, max(low, float(w))) for w in raw]


def _shares(weights: Sequence[float], target: int, min_words: int) -> list[float]:
    n = len(weights)
    if min_words * n >= target:
        return [target / n] * n
    fixed: set[int] = set()
    while True:
        rest = target - min_words * len(fixed)
        total = sum(w for i, w in enumerate(weights) if i not in fixed)
        shares = [min_words if i in fixed else rest * w / total for i, w in enumerate(weights)]
        low = {i for i, share in enumerate(shares) if i not in fixed and share < min_words}
        if not low:
            return shares
        fixed |= low


def section_budgets(weights: Sequence[float], target: int, min_words: int) -> list[int]:
    """Word budgets in proportion to ``weights``: integers that add up to ``target``, none below
    ``min_words`` (unless there are too many sections for that, then an even split)."""
    shares = _shares(weights, target, min_words)
    budgets = [int(share) for share in shares]
    by_remainder = sorted(range(len(shares)), key=lambda i: (budgets[i] - shares[i], i))
    for i in by_remainder[: target - sum(budgets)]:
        budgets[i] += 1
    return budgets


# ---- model calls ----------------------------------------------------------------------------


def _items_text(items: Sequence[Item]) -> str:
    return "\n".join(f"{i.item_id} ({i.kind.replace('_', '-')}): {i.text}" for i in items)


class _Calls:
    """The `reason` calls of step 1, each answered once and kept in the journal."""

    def __init__(self, llm: LLMService, journal: Journal, inp: StepOneInput) -> None:
        self._llm = llm
        self._journal = journal
        self._inp = inp
        self._language = language_name(inp.report_language, "en")

    def _ask(self, key: str, system: str, user: str, schema: type[_M]) -> _M:
        kept = self._journal.get(key)
        if kept is not None:
            return schema.model_validate(kept)
        messages: list[Message] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        result = self._llm.structured(Role.REASON, messages, schema, think=True)
        self._journal.put(key, result.model_dump(mode="json"))
        return result

    def decompose(self) -> Decomposition:
        inp = self._inp
        if inp.template.derived_headings:
            template_block = prompts.TEMPLATE_DERIVE
        else:
            listed = "\n".join(f"- {h}" for h in inp.template.headings)
            template_block = prompts.TEMPLATE_FIXED.format(name=inp.template.name, headings=listed)
        domains = "\n".join(f"- {d}: {prompts.DOMAIN_DESCRIPTIONS[d]}" for d in inp.domains)
        system = prompts.DECOMPOSE_SYSTEM.format(
            min_sections=MIN_SECTIONS, max_sections=MAX_SECTIONS
        )
        user = prompts.DECOMPOSE_USER.format(
            tier=inp.tier,
            response_format=inp.response_format,
            language=self._language,
            template_block=template_block,
            domains=domains,
            brief=inp.brief,
        )
        return self._ask("decompose", system, user, Decomposition)

    def coverage(self, n: int, dec: Decomposition) -> CoverageMatrix:
        user = prompts.COVERAGE_USER.format(
            brief=self._inp.brief, items=_items_text(atomic_items(dec))
        )
        return self._ask(f"coverage{n}", prompts.COVERAGE_SYSTEM, user, CoverageMatrix)

    def revise(self, n: int, dec: Decomposition, gaps: Sequence[CoverageRow]) -> Decomposition:
        user = prompts.REVISE_DECOMPOSITION_USER.format(
            brief=self._inp.brief,
            decomposition=json.dumps(dec.model_dump(mode="json"), ensure_ascii=False, indent=1),
            gaps="\n".join(f'- "{g.phrase}": {g.note}' for g in gaps),
        )
        return self._ask(f"revise{n}", prompts.REVISE_DECOMPOSITION_SYSTEM, user, Decomposition)

    def headings(self, dec: Decomposition, problem: str) -> list[str]:
        system = prompts.HEADINGS_SYSTEM.format(
            min_sections=MIN_SECTIONS, max_sections=MAX_SECTIONS
        )
        user = prompts.HEADINGS_USER.format(
            language=self._language,
            problem=problem,
            sub_questions="\n".join(f"- {q}" for q in dec.sub_questions),
            brief=self._inp.brief,
        )
        return self._ask("headings", system, user, Headings).headings


# ---- the step -------------------------------------------------------------------------------


def _coverage_loop(
    calls: _Calls, dec: Decomposition, cfg: ResearchConfig, events: EventSink
) -> tuple[Decomposition, CoverageMatrix | None, list[CoverageRow]]:
    """Check, revise on gaps, check again, up to the configured number of checks."""
    matrix: CoverageMatrix | None = None
    gaps: list[CoverageRow] = []
    for n in range(1, cfg.coverage_iterations + 1):
        try:
            matrix = calls.coverage(n, dec)
        except LLMError as exc:
            events.emit("coverage_check_failed", level="warning", error=type(exc).__name__)
            return dec, matrix, []
        gaps = [row for row in matrix.rows if row.gap]
        if not gaps:
            return dec, matrix, []
        if n < cfg.coverage_iterations:
            dec = calls.revise(n, dec, gaps)
    events.emit("coverage_gaps_left", level="warning", phrases=[g.phrase for g in gaps])
    return dec, matrix, gaps


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def render_matrix(matrix: CoverageMatrix | None) -> str:
    """The upstream coverage table."""
    head = (
        "## Coverage Matrix — query phrase → atomic item mapping\n\n"
        "| Query phrase (verbatim) | Mapped atomic item(s) | Scope check | Gap? |\n"
        "|---|---|---|---|\n"
    )
    if matrix is None:
        return head + "\nNo coverage check completed.\n"
    rows: list[str] = []
    for row in matrix.rows:
        scope = "OK" if row.scope_ok else "NARROWED"
        if row.note:
            scope += f" — {_cell(row.note)}"
        ids = ", ".join(row.item_ids) or "-"
        rows.append(
            f'| "{_cell(row.phrase)}" | {ids} | {scope} | {"**YES**" if row.gap else "No"} |'
        )
    return head + "\n".join(rows) + "\n"


def _write_outputs(run_dir: Path, inp: StepOneInput, result: StepOneResult) -> None:
    dec = result.decomposition
    write_json(
        run_dir / "prompt-decomposition.json",
        {
            **dec.model_dump(mode="json"),
            "items": [asdict(item) for item in result.items],
            "required_section_headings": result.headings,
            "section_weights": result.weights,
            "section_budgets": result.budgets,
            "pipeline_tier": inp.tier,
            "tier_recommendation": dec.tier_recommendation,
            "response_format": inp.response_format,
            "report_language": inp.report_language,
            "citation_style": "inline",
        },
    )
    set_scaffold_section(run_dir, "Modality", dec.modality)
    set_scaffold_section(run_dir, "Tier rationale", dec.tier_rationale or "-")
    if result.gaps_left:
        notes = "\n".join(f'- "{g.phrase}": {g.note}' for g in result.gaps_left)
        set_scaffold_section(run_dir, "Coverage notes", notes)
    write_shims(run_dir, compose_shims(dec.levers))


def run_step_one(
    llm: LLMService, events: EventSink, run_dir: Path, inp: StepOneInput, cfg: ResearchConfig
) -> StepOneResult:
    """Run step 1 and write its artifacts. A model error in decompose or revise is raised."""
    calls = _Calls(llm, Journal(run_dir / "temp" / "step1.json"), inp)
    dec, matrix, gaps = _coverage_loop(calls, calls.decompose(), cfg, events)
    headings = final_headings(
        inp.template,
        dec,
        events=events,
        repair=lambda problem: calls.headings(dec, problem),
        language=inp.report_language,
    )
    weights = section_weights(dec.section_weights, len(headings), cfg.section_weight_bounds, events)
    low, high = inp.fmt.words
    budgets = section_budgets(weights, (low + high) // 2, cfg.section_min_words)
    result = StepOneResult(dec, atomic_items(dec), headings, weights, budgets, gaps)
    write_text(run_dir / "temp" / "coverage-matrix.md", render_matrix(matrix))
    _write_outputs(run_dir, inp, result)
    return result
