"""Step 1 (PRD M5): the approved brief broken into the items a search plan and a report must
cover, checked against the brief's own phrases, and rendered into the shims and the scaffold.

The questions come from the brief, parsed by code (AD6); the model adds entities, time periods,
domains, the voice and (for the template `auto`) the section headings. The owner's tier choice is
binding: the model's recommendation is only recorded.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.artifacts import write_json, write_text
from app.brief.labels import language_name
from app.brief.parse import parse_brief
from app.brief.render import extract_register
from app.events import EventSink
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.profiles import RunRules
from app.prompts import research as prompts
from app.research.manifest import RunSettings
from app.research.models import (
    AtomicItem,
    CoverageMatrix,
    Decomposition,
    DecompositionDraft,
    MatrixRow,
    atomic_items,
)
from app.research.report import fence_for
from app.templates import MAX_SECTIONS, MIN_SECTIONS, ReportTemplate, is_reserved_heading

FALLBACK_HEADING_CHARS = 80


@dataclass(frozen=True)
class DecomposeResult:
    decomposition: Decomposition
    matrix: tuple[MatrixRow, ...]
    gaps: tuple[str, ...]  # phrases of the brief that no item covers, after the last iteration


@dataclass(frozen=True)
class _Attempt:
    draft: DecompositionDraft
    decomposition: Decomposition
    matrix: tuple[MatrixRow, ...]
    gaps: tuple[str, ...]


def clean_headings(raw: Sequence[str], questions: Sequence[str]) -> list[str]:
    """Derived headings made fit for a report: plain text, no duplicates, none reserved, at most
    the template maximum. With fewer than the template minimum the research questions become the
    headings, so the structure is never empty."""
    seen: set[str] = set()
    headings: list[str] = []
    for heading in raw:
        title = " ".join(heading.lstrip("#").split())
        key = title.casefold()
        if title and key not in seen and not is_reserved_heading(title):
            seen.add(key)
            headings.append(title)
    if len(headings) >= MIN_SECTIONS:
        return headings[:MAX_SECTIONS]
    fallback = [" ".join(q.split()).rstrip("?.!:")[:FALLBACK_HEADING_CHARS] for q in questions]
    return list(dict.fromkeys(h for h in fallback if h))[:MAX_SECTIONS]


def _numbered(lines: Sequence[str]) -> str:
    return "\n".join(f"{n}. {line}" for n, line in enumerate(lines, 1))


def _items_block(items: Sequence[AtomicItem]) -> str:
    return "\n".join(f"{item.id}: {item.kind}: {item.text}" for item in items)


def _is_gap(row: MatrixRow, known: set[str]) -> bool:
    return not row.scope_ok or not any(item in known for item in row.items)


class Decomposer:
    def __init__(self, service: LLMService, rules: RunRules, events: EventSink) -> None:
        self._service = service
        self._rules = rules
        self._events = events

    def decompose(
        self, *, brief: str, settings: RunSettings, template: ReportTemplate
    ) -> DecomposeResult:
        """Break ``brief`` down, then check coverage and re-break for gaps, up to the configured
        number of checks. A model failure propagates: the step runs again on resume."""
        questions = list(parse_brief(brief).research_questions)
        feedback = ""
        for attempt in range(1, self._rules.coverage_matrix_max_iterations):
            found = self._attempt(brief, questions, settings, template, feedback)
            if not found.gaps:
                return self._done(found, template)
            feedback = prompts.DECOMPOSE_FEEDBACK.format(
                gaps="\n".join(f"- {g}" for g in found.gaps)
            )
            self._events.emit("coverage_gap", attempt=attempt, phrases=list(found.gaps))
        return self._done(self._attempt(brief, questions, settings, template, feedback), template)

    def _attempt(
        self,
        brief: str,
        questions: list[str],
        settings: RunSettings,
        template: ReportTemplate,
        feedback: str,
    ) -> _Attempt:
        draft = self._draft(brief, questions, settings, template, feedback)
        decomposition = self._build(draft, questions, settings, template)
        matrix = self._matrix(brief, decomposition)
        known = {item.id for item in atomic_items(decomposition)}
        gaps = tuple(row.phrase for row in matrix if _is_gap(row, known))
        return _Attempt(draft, decomposition, tuple(matrix), gaps)

    def _done(self, found: _Attempt, template: ReportTemplate) -> DecomposeResult:
        self._report(found.draft, found.decomposition, template, found.gaps)
        return DecomposeResult(found.decomposition, found.matrix, found.gaps)

    def _draft(
        self,
        brief: str,
        questions: list[str],
        settings: RunSettings,
        template: ReportTemplate,
        feedback: str,
    ) -> DecompositionDraft:
        if template.derived_headings:
            headings = prompts.HEADINGS_DERIVE.format(
                min_headings=MIN_SECTIONS, max_headings=MAX_SECTIONS
            )
        else:
            headings = prompts.HEADINGS_FIXED
        user = prompts.DECOMPOSE_USER.format(
            brief=brief,
            questions=_numbered(questions),
            language=language_name(settings.report_language, "en"),
            response_format=settings.response_format,
            headings=headings,
            feedback=feedback,
        )
        messages: list[Message] = [
            {"role": "system", "content": prompts.DECOMPOSE_SYSTEM},
            {"role": "user", "content": user},
        ]
        return self._service.structured(Role.REASON, messages, DecompositionDraft, think=True)

    @staticmethod
    def _build(
        draft: DecompositionDraft,
        questions: list[str],
        settings: RunSettings,
        template: ReportTemplate,
    ) -> Decomposition:
        headings = (
            clean_headings(draft.required_section_headings, questions)
            if template.derived_headings
            else list(template.headings)
        )
        return Decomposition(
            sub_questions=questions,
            entities=draft.entities,
            required_formats=draft.required_formats,
            required_sections=draft.required_sections,
            required_section_headings=headings,
            time_horizons=draft.time_horizons,
            time_periods=draft.time_periods,
            scope_conditions=draft.scope_conditions,
            domains=draft.domains,
            pipeline_tier=settings.tier,
            tier_recommendation=draft.tier_recommendation,
            tier_rationale=draft.tier_rationale,
            response_format=settings.response_format,
            modality=draft.modality,
            levers=draft.levers,
        )

    def _matrix(self, brief: str, decomposition: Decomposition) -> list[MatrixRow]:
        user = prompts.MATRIX_USER.format(
            brief=brief, items=_items_block(atomic_items(decomposition))
        )
        messages: list[Message] = [
            {"role": "system", "content": prompts.MATRIX_SYSTEM},
            {"role": "user", "content": user},
        ]
        return self._service.structured(Role.REASON, messages, CoverageMatrix).rows

    def _report(
        self,
        draft: DecompositionDraft,
        decomposition: Decomposition,
        template: ReportTemplate,
        gaps: tuple[str, ...],
    ) -> None:
        if gaps:
            self._events.emit("coverage_gaps_remaining", level="warning", phrases=list(gaps))
        if decomposition.tier_recommendation != decomposition.pipeline_tier:
            self._events.emit(
                "tier_recommendation_differs",
                chosen=decomposition.pipeline_tier,
                recommended=decomposition.tier_recommendation,
            )
        if not template.derived_headings and draft.required_sections:
            self._events.emit("template_overrides_brief", level="warning", template=template.id)


# ---- the files ------------------------------------------------------------------------------


def _matrix_md(rows: Sequence[MatrixRow], known: set[str]) -> str:
    lines = [
        "## Coverage Matrix — query phrase → atomic item mapping",
        "",
        "| Query phrase (verbatim) | Mapped atomic item(s) | Scope check | Gap? |",
        "|---|---|---|---|",
    ]
    for row in rows:
        phrase = row.phrase.replace("|", "\\|")
        scope = "OK" if row.scope_ok else "NARROWED"
        gap = "**YES**" if _is_gap(row, known) else "No"
        items = ", ".join(i for i in row.items if i in known) or "-"
        lines.append(f"| {phrase} | {items} | {scope} | {gap} |")
    return "\n".join(lines) + "\n"


def _shims(brief: str, decomposition: Decomposition) -> dict[str, str]:
    levers = decomposition.levers
    voice = prompts.VOICE_TEXT[levers.voice]
    owner = extract_register(brief)
    owner_register = prompts.OWNER_REGISTER.format(register=owner) if owner else ""
    return {
        "research": prompts.SHIM_RESEARCH.format(
            domain_notes=levers.domain_notes, depth=prompts.DEPTH_TEXT[levers.inference_depth]
        ),
        "drafting": prompts.SHIM_DRAFTING.format(
            voice=voice, domain_notes=levers.domain_notes, owner_register=owner_register
        ).rstrip()
        + "\n",
        "polish": prompts.SHIM_POLISH.format(voice=voice, owner_register=owner_register).rstrip()
        + "\n",
    }


def _scaffold(
    brief: str,
    settings: RunSettings,
    template: ReportTemplate,
    result: DecomposeResult,
    now: datetime,
) -> str:
    d = result.decomposition
    fence = fence_for(brief)
    parts = [
        "# Scaffold\n",
        f"## User Prompt\n\n{fence}text\n{brief}{fence}\n",
        "## Run config\n\n"
        f"- Created: {now.isoformat()}\n"
        f"- Tier: {settings.tier}\n"
        f"- Template: {template.id}\n"
        f"- Report language: {settings.report_language}\n"
        f"- Response format: {settings.response_format}\n",
        f"## Modality\n\n{d.modality}\n",
        f"## Tier rationale\n\nChosen: {d.pipeline_tier}. Recommended: {d.tier_recommendation}. "
        f"{d.tier_rationale}\n",
    ]
    if result.gaps:
        listed = "\n".join(f"- {phrase}" for phrase in result.gaps)
        parts.append(f"## Coverage gaps remaining\n\n{listed}\n")
    return "\n".join(parts)


def write_artifacts(
    run_dir: Path,
    brief: str,
    settings: RunSettings,
    template: ReportTemplate,
    result: DecomposeResult,
    now: datetime,
) -> None:
    """`prompt-decomposition.json`, `temp/coverage-matrix.md`, `shims/*.md` and `scaffold.md`.
    Writing again replaces them with the same bytes, so a resumed step is harmless."""
    known = {item.id for item in atomic_items(result.decomposition)}
    write_json(run_dir / "prompt-decomposition.json", result.decomposition)
    write_text(run_dir / "temp" / "coverage-matrix.md", _matrix_md(result.matrix, known))
    for name, text in _shims(brief, result.decomposition).items():
        write_text(run_dir / "shims" / f"{name}.md", text)
    write_text(run_dir / "scaffold.md", _scaffold(brief, settings, template, result, now))
