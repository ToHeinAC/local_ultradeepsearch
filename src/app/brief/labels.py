"""Labels of the brief in the interview language. German and English exist; any other interview
language uses the English labels."""

from dataclasses import dataclass

from app.brief.models import ChecklistItem


@dataclass(frozen=True)
class Labels:
    code: str
    rounds_one: str
    rounds_many: str  # {n}
    unclear_method: str  # {items}
    verbatim: str
    audience: str
    decision: str
    background: str
    uploads: str
    goal: str
    questions: str
    scope: str
    in_scope: str
    out_of_scope: str
    non_negotiable: str
    assumptions: str
    not_clarified: str
    good_answer: str
    output: str
    items: dict[ChecklistItem, str]
    report_language: str  # {language}
    sources: str  # {language}
    quotes: str
    register: str
    format: str  # {name} {low} {high}
    citations: str  # {sources}
    template: str  # {name} {id} {headings}
    template_auto: str
    notice: str  # {template_language} {report_language}


DE = Labels(
    code="de",
    rounds_one="1 Klärungsrunde",
    rounds_many="{n} Klärungsrunden",
    unclear_method="nicht geklärt: {items}",
    verbatim="Prompt wörtlich übernommen",
    audience="Zielgruppe",
    decision="Entscheidung",
    background="Hintergrund und Kontext",
    uploads="Kontext aus Unterlagen",
    goal="Ziel",
    questions="Forschungsfragen",
    scope="Umfang",
    in_scope="Im Umfang",
    out_of_scope="Nicht im Umfang",
    non_negotiable="Nicht verhandelbar",
    assumptions="Annahmen",
    not_clarified="Nicht geklärt",
    good_answer="Was eine gute Antwort ausmacht",
    output="Ausgabe",
    items={
        "question": "Fragestellung",
        "context": "Kontext",
        "goal": "Ziel",
        "audience": "Zielgruppe",
        "scope": "Umfang",
        "output": "Ausgabe",
        "depth": "Tiefe",
    },
    report_language=(
        "**Berichtssprache: {language}.** Der gesamte Bericht wird auf {language} verfasst."
    ),
    sources=(
        "**Quellen: jede Sprache.** Die Suche wird nicht auf {language} beschränkt; "
        "maßgebliche Quellen werden in jeder Sprache gelesen."
    ),
    quotes=(
        "**Zitate:** Anführungszeichen nur um wörtlichen Quelltext; eine Übersetzung folgt in "
        "Klammern ohne Anführungszeichen."
    ),
    register="**Register:** {register}",
    format="**Format:** {name}, {low} bis {high} Wörter im Hauptteil.",
    citations="**Zitierweise:** Inline [N] mit dem Abschnitt `## {sources}`.",
    template="**Vorlage:** {name} ({id}). Gliederung: {headings}.",
    template_auto="**Vorlage:** Automatisch (auto). Die Gliederung wird aus der Frage abgeleitet.",
    notice=(
        "**Hinweis:** Die Vorlage ist auf {template_language} ausgelegt, "
        "der Bericht wird auf {report_language} verfasst."
    ),
)

EN = Labels(
    code="en",
    rounds_one="1 clarification round",
    rounds_many="{n} clarification rounds",
    unclear_method="not clarified: {items}",
    verbatim="prompt installed verbatim",
    audience="Audience",
    decision="Decision",
    background="Background and context",
    uploads="Context from uploaded files",
    goal="Goal",
    questions="Research questions",
    scope="Scope",
    in_scope="In scope",
    out_of_scope="Out of scope",
    non_negotiable="Non-negotiable",
    assumptions="Assumptions",
    not_clarified="Not clarified",
    good_answer="What a good answer looks like",
    output="Output",
    items={
        "question": "question",
        "context": "context",
        "goal": "goal",
        "audience": "audience",
        "scope": "scope",
        "output": "output",
        "depth": "depth",
    },
    report_language=("**Report language: {language}.** The whole report is written in {language}."),
    sources=(
        "**Sources: any language.** The search is not restricted to {language}; "
        "authoritative sources are read in any language."
    ),
    quotes=(
        "**Quotations:** quotation marks only around verbatim source text; a translation "
        "follows in parentheses without quotation marks."
    ),
    register="**Register:** {register}",
    format="**Format:** {name}, {low} to {high} words in the main body.",
    citations="**Citations:** inline [N] with the section `## {sources}`.",
    template="**Template:** {name} ({id}). Headings: {headings}.",
    template_auto="**Template:** Automatic (auto). The headings are derived from the question.",
    notice=(
        "**Notice:** The template is written for {template_language}, "
        "the report is written in {report_language}."
    ),
)

_LANGUAGE_NAMES: dict[str, tuple[str, str]] = {  # code -> (German, English)
    "de": ("Deutsch", "German"),
    "en": ("Englisch", "English"),
    "fr": ("Französisch", "French"),
    "es": ("Spanisch", "Spanish"),
    "it": ("Italienisch", "Italian"),
    "nl": ("Niederländisch", "Dutch"),
    "pl": ("Polnisch", "Polish"),
    "pt": ("Portugiesisch", "Portuguese"),
    "ru": ("Russisch", "Russian"),
    "tr": ("Türkisch", "Turkish"),
    "zh": ("Chinesisch", "Chinese"),
    "ja": ("Japanisch", "Japanese"),
}


def labels_for(interview_language: str) -> Labels:
    return DE if interview_language == "de" else EN


def language_name(code: str, in_language: str) -> str:
    """Language ``code`` named in ``in_language`` (German or English); unknown codes stay as is."""
    names = _LANGUAGE_NAMES.get(code)
    if names is None:
        return code
    return names[0] if in_language == "de" else names[1]


def sources_heading(report_language: str) -> str:
    """The references heading: `Quellen` for German reports, else `Sources` (PRD §3.9)."""
    return "Quellen" if report_language == "de" else "Sources"
