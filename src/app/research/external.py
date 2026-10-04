"""A brief written outside Phase 1 (`udr run --brief`, `POST /runs` in M6): code adds the Method
line and renders the Output section from the chosen settings; the rest stays as written."""

from app.brief.errors import InvalidInput
from app.brief.labels import labels_for
from app.brief.models import SessionSettings
from app.brief.parse import BriefParseError, parse_brief
from app.brief.render import BriefContext, canonical_text, replace_output
from app.pipeline.profiles import FormatRange
from app.templates import ReportTemplate


def _with_method(text: str, method: str) -> str:
    lines = text.split("\n")
    if any(line.startswith("Method:") for line in lines):
        return text
    title = next(i for i, line in enumerate(lines) if line.startswith("# "))
    return "\n".join([*lines[: title + 1], "", f"Method: {method}", *lines[title + 1 :]])


def prepare_external_brief(
    text: str, *, template: ReportTemplate, fmt_name: str, fmt: FormatRange, language: str
) -> str:
    """The brief as it will be archived and approved. Raises `InvalidInput` if Phase 2 could not
    read it (no title or no numbered research questions)."""
    body = canonical_text(text)
    try:
        parse_brief(body)
    except BriefParseError as exc:
        raise InvalidInput(str(exc)) from exc
    ctx = BriefContext(
        settings=SessionSettings(
            report_language=language,
            response_format=fmt_name,  # type: ignore[arg-type]  # validated by SessionSettings
            template_id=template.id,
        ),
        template=template,
        fmt=fmt,
        rounds=0,
        missing=(),
        unknown_questions=(),
        upload_digest="",
        interview_language=language,
    )
    return replace_output(_with_method(body, labels_for(language).external), ctx)
