"""The brief as text. The model fills a `BriefDraft`; code renders every structural part, so the
Output section, the list of open items and the research questions are guaranteed (PRD M4, AD6)."""

import hashlib
import re
from dataclasses import dataclass

from app.brief.labels import DE, EN, Labels, labels_for, language_name, sources_heading
from app.brief.models import CHECKLIST_ORDER, ChecklistItem, SessionSettings
from app.brief.parse import parse_brief
from app.brief.schemas import BriefDraft
from app.pipeline.profiles import FormatRange
from app.templates import ReportTemplate
from app.text import normalize_for_match

_LEADING_HASH = re.compile(r"^( {0,3})#")
_REGISTER = re.compile(r"^- \*\*Register:\*\* (?P<register>.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class BriefContext:
    """Everything besides the draft that goes into a brief."""

    settings: SessionSettings
    template: ReportTemplate
    fmt: FormatRange
    rounds: int
    missing: tuple[ChecklistItem, ...]
    unknown_questions: tuple[str, ...]
    upload_digest: str
    interview_language: str


def canonical_text(text: str) -> str:
    """LF line endings, no leading or trailing blank lines, one final newline. Nothing else is
    touched, so the approved bytes are the owner's text."""
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    return unified.strip("\n") + "\n"


def brief_sha256(text: str) -> str:
    """The approval hash: sha256 of the canonical UTF-8 bytes."""
    return hashlib.sha256(canonical_text(text).encode("utf-8")).hexdigest()


def _line(text: str) -> str:
    return " ".join(text.split())


def _flow(text: str) -> str:
    """Free text with `#` at a line start escaped, so model text can never add a heading."""
    return "\n".join(_LEADING_HASH.sub(r"\1\\#", line) for line in text.strip().splitlines())


def _bullets(items: list[str]) -> list[str]:
    return [f"- {_line(item)}" for item in items if _line(item)]


def _section(heading: str, body: str) -> str:
    return f"## {heading}\n\n{body}"


def _ordered(missing: tuple[ChecklistItem, ...]) -> list[ChecklistItem]:
    return [item for item in CHECKLIST_ORDER if item in missing]


def _method_line(labels: Labels, rounds: int, unclear: list[str], *, verbatim: bool) -> str:
    parts: list[str] = []
    if rounds or not verbatim:
        parts.append(labels.rounds_one if rounds == 1 else labels.rounds_many.format(n=rounds))
    if unclear:
        parts.append(labels.unclear_method.format(items=", ".join(unclear)))
    if verbatim:
        parts.append(labels.verbatim)
    return "Method: " + "; ".join(parts)


def _questions(draft: BriefDraft, unknown: tuple[str, ...]) -> list[str]:
    """The draft's questions, plus every 'don't know' question the draft does not already hold."""
    questions = [_line(q) for q in draft.research_questions if _line(q)]
    for candidate in map(_line, unknown):
        norm = normalize_for_match(candidate)
        if candidate and not any(normalize_for_match(q) == norm for q in questions):
            questions.append(candidate)
    return questions or [_line(draft.question)]


def render_output(ctx: BriefContext, register: str = "") -> str:
    """The Output section, from the session settings and configuration only."""
    labels = labels_for(ctx.interview_language)
    report = language_name(ctx.settings.report_language, labels.code)
    lines = [
        labels.report_language.format(language=report),
        labels.sources.format(language=report),
        labels.quotes,
    ]
    if _line(register):
        lines.append(labels.register.format(register=_line(register)))
    low, high = ctx.fmt.words
    lines.append(labels.format.format(name=ctx.settings.response_format, low=low, high=high))
    lines.append(labels.citations.format(sources=sources_heading(ctx.settings.report_language)))
    template = ctx.template
    if template.derived_headings:
        lines.append(labels.template_auto)
    else:
        lines.append(
            labels.template.format(
                name=template.name, id=template.id, headings="; ".join(template.headings)
            )
        )
    if not template.derived_headings and template.language != ctx.settings.report_language:
        lines.append(
            labels.notice.format(
                template_language=language_name(template.language, labels.code),
                report_language=report,
            )
        )
    return _section(labels.output, "\n".join(f"- {line}" for line in lines))


def _header(draft: BriefDraft, labels: Labels, ctx: BriefContext) -> str:
    unclear = [labels.items[item] for item in _ordered(ctx.missing)]
    lines = [_method_line(labels, ctx.rounds, unclear, verbatim=False)]
    parts = [
        f"{label}: {_line(value)}"
        for label, value in ((labels.audience, draft.audience), (labels.decision, draft.decision))
        if _line(value)
    ]
    if parts:
        lines.append(" · ".join(parts))
    return "\n".join(lines)


def _scope(draft: BriefDraft, labels: Labels) -> str | None:
    rows = (
        (labels.in_scope, draft.in_scope),
        (labels.out_of_scope, draft.out_of_scope),
        (labels.non_negotiable, draft.non_negotiables),
    )
    lines = [f"- {label}: {'; '.join(map(_line, items))}" for label, items in rows if items]
    return _section(labels.scope, "\n".join(lines)) if lines else None


def _assumptions(draft: BriefDraft, labels: Labels, ctx: BriefContext) -> str | None:
    lines = _bullets(draft.assumptions)
    lines += [f"- {labels.not_clarified}: {labels.items[item]}" for item in _ordered(ctx.missing)]
    return _section(labels.assumptions, "\n".join(lines)) if lines else None


def render_brief(draft: BriefDraft, ctx: BriefContext) -> str:
    """The full brief text, already in canonical form. Empty sections are left out."""
    labels = labels_for(ctx.interview_language)
    numbered = [f"{n}. {q}" for n, q in enumerate(_questions(draft, ctx.unknown_questions), 1)]
    prose = (
        (labels.background, draft.background),
        (labels.uploads, ctx.upload_digest),
    )
    blocks = [f"# {_line(draft.question)}", _header(draft, labels, ctx)]
    blocks += [_section(head, _flow(body)) for head, body in prose if body.strip()]
    if draft.goal.strip():
        blocks.append(_section(labels.goal, _flow(draft.goal)))
    blocks.append(_section(labels.questions, "\n".join(numbered)))
    blocks += [b for b in (_scope(draft, labels), _assumptions(draft, labels, ctx)) if b]
    if draft.good_answer.strip():
        blocks.append(_section(labels.good_answer, _flow(draft.good_answer)))
    blocks.append(render_output(ctx, draft.tone))
    return canonical_text("\n\n".join(blocks))


def replace_output(text: str, ctx: BriefContext, register: str = "") -> str:
    """``text`` with its Output section rendered anew from ``ctx`` and everything else untouched,
    for a settings change after the owner edited the brief by hand. A brief without an Output
    section gets one appended."""
    headings = {f"## {DE.output}".casefold(), f"## {EN.output}".casefold()}
    lines = canonical_text(text).rstrip("\n").split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip().casefold() in headings), None)
    new = render_output(ctx, register)
    if start is None:
        return canonical_text("\n".join(lines) + "\n\n" + new)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    parts = ["\n".join(lines[:start]).rstrip("\n"), new, "\n".join(lines[end:]).strip("\n")]
    return canonical_text("\n\n".join(part for part in parts if part))


def extract_register(text: str) -> str:
    """The register line of the Output section, or "" if the brief has none."""
    found = _REGISTER.search(text)
    return found["register"] if found else ""


def render_verbatim(pasted: str, ctx: BriefContext) -> str:
    """A pasted finished prompt, byte for byte, between a Method line and the Output section.

    Raises `BriefParseError` if the prompt has no title or numbered questions."""
    body = canonical_text(pasted)
    parse_brief(body)
    labels = labels_for(ctx.interview_language)
    method = _method_line(labels, ctx.rounds, [], verbatim=True)
    return canonical_text("\n\n".join([method, body.rstrip("\n"), render_output(ctx)]))
