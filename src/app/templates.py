"""Report templates (PRD M5): a Markdown file with front matter and one H2 per report section.

```
---
id: literaturuebersicht
name: Literaturübersicht
description: ...
language: de
default_response_format: argumentative
---

## Kurzantwort
<!-- what this section must contain -->
```

Built-in templates live in `templates/`, uploads in `data/templates/`. The template `auto` has no
sections: its headings are derived from the question in step 1.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

from app.pipeline.profiles import ResponseFormatName

MIN_SECTIONS = 2
MAX_SECTIONS = 15
AUTO = "auto"
RESERVED_TITLES = ("quellen", "sources")
RESERVED_FIRST_WORDS = ("anhang", "appendix")
REQUIRED_KEYS = ("id", "name", "description", "language", "default_response_format")
OPTIONAL_KEYS = ("reference_docx",)

_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_LANGUAGE = re.compile(r"[a-z]{2}")
_FRONT = re.compile(r"\A---\n(?P<front>.*?)\n---[ \t]*(?:\n(?P<body>.*))?\Z", re.DOTALL)
_H2 = re.compile(r"^## (?P<title>.+?)[ \t]*$", re.MULTILINE)
_COMMENT = re.compile(r"<!--(?P<text>.*?)-->", re.DOTALL)
_FIRST_WORD = re.compile(r"[^\W\d_]+")


class TemplateError(ValueError):
    """A template file is invalid; the message names the file and the rule."""


@dataclass(frozen=True)
class Section:
    heading: str
    instructions: str


@dataclass(frozen=True)
class ReportTemplate:
    id: str
    name: str
    description: str
    language: str
    default_response_format: ResponseFormatName
    reference_docx: str | None
    sections: tuple[Section, ...]

    @property
    def headings(self) -> tuple[str, ...]:
        return tuple(section.heading for section in self.sections)

    @property
    def derived_headings(self) -> bool:
        """True for `auto`: the headings come from step 1, not from the template."""
        return not self.sections


def _normalize(heading: str) -> str:
    return " ".join(heading.split()).casefold()


def is_reserved_heading(heading: str) -> bool:
    norm = _normalize(heading)
    first = _FIRST_WORD.match(norm)
    return norm in RESERVED_TITLES or (first is not None and first.group() in RESERVED_FIRST_WORDS)


def heading_problem(headings: Sequence[str]) -> str | None:
    """Why ``headings`` cannot be a report's H2 list (the template rules), or None if they can."""
    if not MIN_SECTIONS <= len(headings) <= MAX_SECTIONS:
        return f"needs {MIN_SECTIONS} to {MAX_SECTIONS} headings, has {len(headings)}"
    seen: set[str] = set()
    for heading in headings:
        if not heading.strip():
            return "a heading is empty"
        if is_reserved_heading(heading):
            return f"heading {heading!r} is reserved (Sources/Appendix)"
        if _normalize(heading) in seen:
            return f"duplicate heading {heading!r}"
        seen.add(_normalize(heading))
    return None


def _front_fields(front: str, source: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in front.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(": ")
        key = key.strip()
        if not separator or not key:
            raise TemplateError(f"{source}: front matter line {line!r} is not 'key: value'")
        if key in fields:
            raise TemplateError(f"{source}: front matter key {key!r} appears twice")
        if key not in (*REQUIRED_KEYS, *OPTIONAL_KEYS):
            raise TemplateError(f"{source}: unknown front matter key {key!r}")
        fields[key] = value.strip()
    missing = [key for key in REQUIRED_KEYS if not fields.get(key)]
    if missing:
        raise TemplateError(f"{source}: missing front matter key(s): {', '.join(missing)}")
    return fields


def _check_fields(fields: dict[str, str], source: str) -> None:
    if not _ID.fullmatch(fields["id"]):
        raise TemplateError(
            f"{source}: invalid id {fields['id']!r} (lowercase letters, digits, '-')"
        )
    if not _LANGUAGE.fullmatch(fields["language"]):
        raise TemplateError(
            f"{source}: language must be a two-letter code, got {fields['language']!r}"
        )
    formats = get_args(ResponseFormatName)
    if fields["default_response_format"] not in formats:
        raise TemplateError(
            f"{source}: default_response_format must be one of {', '.join(formats)}, "
            f"got {fields['default_response_format']!r}"
        )
    docx = fields.get("reference_docx")
    if docx is not None and not (docx.endswith(".docx") and "/" not in docx and "\\" not in docx):
        raise TemplateError(
            f"{source}: reference_docx must be a plain .docx file name, got {docx!r}"
        )


def _sections(body: str, source: str) -> tuple[Section, ...]:
    matches = list(_H2.finditer(body))
    sections: list[Section] = []
    seen: set[str] = set()
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        comment = _COMMENT.search(body[match.end() : end])
        heading = match["title"].strip()
        if is_reserved_heading(heading):
            raise TemplateError(f"{source}: heading {heading!r} is reserved (Sources/Appendix)")
        if _normalize(heading) in seen:
            raise TemplateError(f"{source}: duplicate heading {heading!r}")
        seen.add(_normalize(heading))
        sections.append(Section(heading, comment["text"].strip() if comment else ""))
    return tuple(sections)


def parse_template(text: str, source: str) -> ReportTemplate:
    """Parse and validate one template; ``source`` (a file name) appears in every error."""
    match = _FRONT.match(text.replace("\r\n", "\n"))
    if match is None:
        raise TemplateError(f"{source}: the file must start with a '---' front matter block")
    fields = _front_fields(match["front"], source)
    _check_fields(fields, source)
    sections = _sections(match["body"] or "", source)
    if fields["id"] == AUTO and sections:
        raise TemplateError(
            f"{source}: template 'auto' must not define sections (they are derived)"
        )
    if fields["id"] != AUTO and not MIN_SECTIONS <= len(sections) <= MAX_SECTIONS:
        raise TemplateError(
            f"{source}: needs {MIN_SECTIONS} to {MAX_SECTIONS} sections, has {len(sections)}"
        )
    return ReportTemplate(
        id=fields["id"],
        name=fields["name"],
        description=fields["description"],
        language=fields["language"],
        default_response_format=fields["default_response_format"],  # type: ignore[arg-type]  # checked above
        reference_docx=fields.get("reference_docx"),
        sections=sections,
    )


def load_templates(directories: Sequence[Path]) -> dict[str, ReportTemplate]:
    """All `*.md` templates of ``directories`` in order (files sorted by name within a directory).

    An id that appears twice is an error: an upload can never replace a built-in template.
    """
    templates: dict[str, ReportTemplate] = {}
    origin: dict[str, str] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise TemplateError(f"{path.name}: not valid UTF-8") from exc
            template = parse_template(text, path.name)
            if template.id in templates:
                first = origin[template.id]
                raise TemplateError(
                    f"{path.name}: template {template.id!r} is already defined by {first}"
                )
            templates[template.id] = template
            origin[template.id] = path.name
    return templates


def get_template(templates: Mapping[str, ReportTemplate], template_id: str) -> ReportTemplate:
    try:
        return templates[template_id]
    except KeyError:
        raise TemplateError(f"unknown template {template_id!r}") from None
