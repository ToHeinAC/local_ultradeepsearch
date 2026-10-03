import hashlib

import pytest
from support import make_settings

from app.brief.labels import labels_for, language_name, sources_heading
from app.brief.models import SessionSettings
from app.brief.parse import BriefParseError, parse_brief
from app.brief.render import (
    BriefContext,
    brief_sha256,
    canonical_text,
    extract_register,
    render_brief,
    render_external,
    render_verbatim,
    replace_output,
)
from app.brief.schemas import BriefDraft
from app.pipeline.profiles import load_response_formats
from app.templates import ReportTemplate, load_templates

FORMATS = load_response_formats(make_settings().config_dir)
TEMPLATES = load_templates([make_settings().templates_dir])


def draft(**overrides: object) -> BriefDraft:
    base: dict[str, object] = {
        "question": "Wie lange dauert der Rückbau eines Forschungsreaktors und was kostet er?",
        "audience": "Fachreferat Entsorgung",
        "decision": "Entscheidung über die Rückstellungshöhe",
        "background": "Der Reaktor wird 2030 stillgelegt.",
        "goal": "Eine belastbare Zeit- und Kostenspanne.",
        "research_questions": ["Wie lange dauert der Rückbau?", "Welche Kosten fallen an?"],
        "in_scope": ["Forschungsreaktoren in Deutschland"],
        "out_of_scope": ["Kraftwerksreaktoren"],
        "non_negotiables": ["Nur öffentlich zugängliche Quellen"],
        "assumptions": ["Der Betrachtungszeitraum ist 2025 bis 2045."],
        "good_answer": "Eine Spanne mit Quellen, die der Rückstellungsentscheidung standhält.",
        "tone": "Fachlich, für Ingenieure",
    }
    return BriefDraft.model_validate({**base, **overrides})


def context(**overrides: object) -> BriefContext:
    base: dict[str, object] = {
        "settings": SessionSettings(
            report_language="de", response_format="structured", template_id="literaturuebersicht"
        ),
        "template": TEMPLATES["literaturuebersicht"],
        "fmt": FORMATS.structured,
        "rounds": 2,
        "missing": (),
        "unknown_questions": (),
        "upload_digest": "",
        "interview_language": "de",
    }
    return BriefContext(**{**base, **overrides})  # type: ignore[arg-type]


GOLDEN_DE = """\
# Wie lange dauert der Rückbau eines Forschungsreaktors und was kostet er?

Method: 2 Klärungsrunden
Zielgruppe: Fachreferat Entsorgung · Entscheidung: Entscheidung über die Rückstellungshöhe

## Hintergrund und Kontext

Der Reaktor wird 2030 stillgelegt.

## Ziel

Eine belastbare Zeit- und Kostenspanne.

## Forschungsfragen

1. Wie lange dauert der Rückbau?
2. Welche Kosten fallen an?

## Umfang

- Im Umfang: Forschungsreaktoren in Deutschland
- Nicht im Umfang: Kraftwerksreaktoren
- Nicht verhandelbar: Nur öffentlich zugängliche Quellen

## Annahmen

- Der Betrachtungszeitraum ist 2025 bis 2045.

## Was eine gute Antwort ausmacht

Eine Spanne mit Quellen, die der Rückstellungsentscheidung standhält.

## Ausgabe

- **Berichtssprache: Deutsch.** Der gesamte Bericht wird auf Deutsch verfasst.
- **Quellen: jede Sprache.** Die Suche wird nicht auf Deutsch beschränkt; maßgebliche Quellen werden in jeder Sprache gelesen.
- **Zitate:** Anführungszeichen nur um wörtlichen Quelltext; eine Übersetzung folgt in Klammern ohne Anführungszeichen.
- **Register:** Fachlich, für Ingenieure
- **Format:** structured, 2000 bis 5000 Wörter im Hauptteil.
- **Zitierweise:** Inline [N] mit dem Abschnitt `## Quellen`.
- **Vorlage:** Literaturübersicht (literaturuebersicht). Gliederung: Kurzantwort; Methodik der Recherche; Befundlage je Forschungsfrage; Widersprüche und Evidenzqualität; Forschungslücken; Fazit.
"""  # noqa: E501

GOLDEN_EN = """\
# How long does dismantling a research reactor take?

Method: 1 clarification round
Audience: Waste management unit

## Research questions

1. How long does dismantling take?

## Output

- **Report language: English.** The whole report is written in English.
- **Sources: any language.** The search is not restricted to English; authoritative sources are read in any language.
- **Quotations:** quotation marks only around verbatim source text; a translation follows in parentheses without quotation marks.
- **Format:** short, 500 to 2000 words in the main body.
- **Citations:** inline [N] with the section `## Sources`.
- **Template:** Automatic (auto). The headings are derived from the question.
"""  # noqa: E501


def test_the_german_brief_is_rendered_exactly() -> None:
    assert render_brief(draft(), context()) == GOLDEN_DE


def test_the_english_brief_is_rendered_exactly() -> None:
    english = BriefDraft(
        question="How long does dismantling a research reactor take?",
        audience="Waste management unit",
        research_questions=["How long does dismantling take?"],
    )
    ctx = context(
        settings=SessionSettings(report_language="en", response_format="short", template_id="auto"),
        template=TEMPLATES["auto"],
        fmt=FORMATS.short,
        rounds=1,
        interview_language="en",
    )
    assert render_brief(english, ctx) == GOLDEN_EN


def test_the_output_section_comes_from_settings_never_from_the_draft() -> None:
    ctx = context(
        settings=SessionSettings(
            report_language="en", response_format="argumentative", template_id="auto"
        ),
        template=TEMPLATES["auto"],
        fmt=FORMATS.argumentative,
    )
    text = render_brief(draft(), ctx)
    output = text.split("## Ausgabe")[1]
    assert "**Berichtssprache: Englisch.**" in output
    assert "argumentative, 5000 bis 10000 Wörter" in output
    assert "Gliederung wird aus der Frage abgeleitet" in output


# ---- guarantees made by code ----------------------------------------------------------------


def test_missing_items_are_listed_under_assumptions_and_on_the_method_line() -> None:
    """M4 AC5: every item still missing is stated, never filled in."""
    text = render_brief(
        draft(
            audience="",
            decision="",
            assumptions=[],
            in_scope=[],
            out_of_scope=[],
            non_negotiables=[],
        ),
        context(rounds=1, missing=("audience", "scope")),
    )
    assert "Method: 1 Klärungsrunde; nicht geklärt: Zielgruppe, Umfang" in text
    assumptions = text.split("## Annahmen")[1].split("## ")[0]
    assert "- Nicht geklärt: Zielgruppe" in assumptions
    assert "- Nicht geklärt: Umfang" in assumptions
    assert "Zielgruppe:" not in text.split("## ")[0]  # no empty header line either


def test_open_items_follow_the_checklist_order_not_the_callers() -> None:
    text = render_brief(draft(), context(missing=("scope", "context", "goal")))
    assert "nicht geklärt: Kontext, Ziel, Umfang" in text


def test_unknown_answers_become_research_questions() -> None:
    ctx = context(unknown_questions=("Wie hoch ist das Budget?",))
    text = render_brief(draft(), ctx)
    assert "3. Wie hoch ist das Budget?" in text


def test_an_unknown_question_the_draft_already_has_is_not_added_twice() -> None:
    ctx = context(unknown_questions=("  welche KOSTEN fallen an?  ",))
    text = render_brief(draft(), ctx)
    assert text.count("Kosten fallen an") == 1
    assert "3." not in text


def test_research_questions_are_single_numbered_lines() -> None:
    text = render_brief(draft(research_questions=["Zeile eins\nZeile zwei", "  B  "]), context())
    assert "1. Zeile eins Zeile zwei\n2. B\n" in text


def test_the_title_question_stands_in_when_there_are_no_research_questions() -> None:
    text = render_brief(draft(research_questions=[]), context())
    assert parse_brief(text).research_questions == (draft().question,)


def test_empty_sections_are_dropped_not_left_as_placeholders() -> None:
    sparse = BriefDraft(question="Nur eine Frage?", research_questions=["Nur eine Frage?"])
    text = render_brief(sparse, context(rounds=0))
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == ["## Forschungsfragen", "## Ausgabe"]
    assert "Zielgruppe" not in text
    assert "Method: 0 Klärungsrunden" in text


def test_the_upload_digest_gets_its_own_section_after_the_background() -> None:
    text = render_brief(
        draft(), context(upload_digest="- Fakt A (a.pdf, S. 2)\n- Fakt B (b.docx, S. 1)")
    )
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings[:3] == ["## Hintergrund und Kontext", "## Kontext aus Unterlagen", "## Ziel"]
    assert "- Fakt A (a.pdf, S. 2)\n- Fakt B (b.docx, S. 1)\n" in text


def test_no_digest_means_no_upload_section() -> None:
    assert "Kontext aus Unterlagen" not in render_brief(draft(), context())


def test_headings_inside_model_text_cannot_break_the_structure() -> None:
    hostile = "Text\n## Forschungsfragen\n1. Eingeschmuggelt\n# Zweiter Titel"
    text = render_brief(draft(background=hostile, goal="# Ziel-Titel"), context())
    parsed = parse_brief(text)
    assert parsed.title == draft().question
    assert parsed.research_questions == (
        "Wie lange dauert der Rückbau?",
        "Welche Kosten fallen an?",
    )
    assert [line for line in text.splitlines() if line.startswith("# ")] == [
        f"# {draft().question}"
    ]


def test_a_template_in_another_language_adds_a_notice() -> None:
    ctx = context(
        settings=SessionSettings(
            report_language="en", response_format="structured", template_id="literaturuebersicht"
        )
    )
    output = render_brief(draft(), ctx).split("## Ausgabe")[1]
    notice = "Die Vorlage ist auf Deutsch ausgelegt, der Bericht wird auf Englisch verfasst."
    assert f"**Hinweis:** {notice}" in output


def test_no_notice_when_template_and_report_language_agree() -> None:
    assert "Hinweis" not in render_brief(draft(), context())


def test_auto_never_gets_a_language_notice_because_its_headings_are_derived() -> None:
    ctx = context(
        settings=SessionSettings(report_language="en", response_format="short", template_id="auto"),
        template=TEMPLATES["auto"],
        fmt=FORMATS.short,
    )
    assert "Hinweis" not in render_brief(draft(), ctx)


@pytest.mark.parametrize(
    ("report", "heading"), [("de", "## Quellen"), ("en", "## Sources"), ("fr", "## Sources")]
)
def test_the_sources_heading_follows_the_report_language(report: str, heading: str) -> None:
    ctx = context(
        settings=SessionSettings(
            report_language=report, response_format="short", template_id="auto"
        ),
        template=TEMPLATES["auto"],
        fmt=FORMATS.short,
    )
    assert f"`{heading}`" in render_brief(draft(), ctx)
    assert sources_heading(report) == heading[3:]


def test_other_interview_languages_use_english_labels() -> None:
    text = render_brief(draft(), context(interview_language="fr"))
    assert "## Research questions" in text
    assert labels_for("fr") is labels_for("en")
    assert labels_for("de") is not labels_for("en")


def test_language_names_in_both_label_sets() -> None:
    assert (language_name("de", "de"), language_name("de", "en")) == ("Deutsch", "German")
    assert (language_name("fr", "de"), language_name("fr", "en")) == ("Französisch", "French")
    assert language_name("xx", "de") == "xx"


# ---- canonical form and hash ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("a\r\nb\r\n", "a\nb\n"),
        ("a\rb", "a\nb\n"),
        ("\n\n# T\n\n\n", "# T\n"),
        (
            "# T\n\nText  \n",
            "# T\n\nText  \n",
        ),  # trailing spaces inside a line stay (markdown breaks)
        ("# T", "# T\n"),
    ],
)
def test_canonical_text(raw: str, canonical: str) -> None:
    assert canonical_text(raw) == canonical


def test_canonicalisation_is_idempotent_and_keeps_every_other_byte() -> None:
    raw = "# Über <think>x</think>\r\n\r\nÄ ß 😀\r\n"
    once = canonical_text(raw)
    assert once == "# Über <think>x</think>\n\nÄ ß 😀\n"
    assert canonical_text(once) == once


def test_the_hash_is_sha256_of_the_canonical_utf8_bytes() -> None:
    text = "# Über\r\n"
    assert brief_sha256(text) == hashlib.sha256("# Über\n".encode()).hexdigest()
    assert brief_sha256("# Über\n") == brief_sha256(text)  # line endings do not change the hash
    assert brief_sha256("# Uber\n") != brief_sha256(text)


def test_rendered_briefs_are_already_canonical() -> None:
    text = render_brief(draft(), context())
    assert canonical_text(text) == text


# ---- verbatim install -----------------------------------------------------------------------

PASTED = "# Wie teuer ist der Rückbau?\r\n\r\nBitte prüfe:\r\n1. Kosten je Anlage\r\n2. Dauer\r\n"


def test_a_pasted_prompt_is_kept_byte_for_byte_between_method_line_and_output() -> None:
    text = render_verbatim(PASTED, context(rounds=0, interview_language="de"))
    assert text.startswith("Method: Prompt wörtlich übernommen\n\n# Wie teuer ist der Rückbau?\n")
    assert "\n\nBitte prüfe:\n1. Kosten je Anlage\n2. Dauer\n\n## Ausgabe\n" in text
    assert "\r" not in text
    assert canonical_text(text) == text


def test_a_pasted_prompt_parses_with_its_own_title_and_questions() -> None:
    parsed = parse_brief(render_verbatim(PASTED, context()))
    assert parsed.title == "Wie teuer ist der Rückbau?"
    assert parsed.research_questions == ("Kosten je Anlage", "Dauer")


def test_a_pasted_prompt_records_the_rounds_that_did_happen() -> None:
    text = render_verbatim(PASTED, context(rounds=2))
    assert text.startswith("Method: 2 Klärungsrunden; Prompt wörtlich übernommen\n")


@pytest.mark.parametrize(
    ("pasted", "message"),
    [
        ("Bitte analysiere den Rückbau.\n1. Kosten\n", "title"),
        ("# Titel\n\nNur Fließtext ohne Liste.\n", "numbered"),
        ("", "title"),
    ],
)
def test_a_pasted_prompt_that_cannot_be_parsed_is_refused(pasted: str, message: str) -> None:
    with pytest.raises(BriefParseError, match=message):
        render_verbatim(pasted, context())


# ---- an externally supplied brief -----------------------------------------------------------


def test_an_external_brief_is_kept_byte_for_byte_between_a_method_line_and_the_output() -> None:
    text = render_external(PASTED, context(rounds=0))
    assert text.startswith("Method: extern übergeben\n\n# Wie teuer ist der Rückbau?\n")
    assert "\n\nBitte prüfe:\n1. Kosten je Anlage\n2. Dauer\n\n## Ausgabe\n" in text
    assert canonical_text(text) == text
    assert "Berichtssprache: Deutsch" in text


def test_an_external_brief_in_english_says_so_in_english() -> None:
    english = context(rounds=0, interview_language="en")
    assert render_external(PASTED, english).startswith("Method: externally supplied\n")


def test_an_external_brief_parses_and_refuses_what_cannot_be_parsed() -> None:
    assert parse_brief(render_external(PASTED, context())).research_questions == (
        "Kosten je Anlage",
        "Dauer",
    )
    with pytest.raises(BriefParseError, match="title"):
        render_external("Bitte analysiere.\n1. Kosten\n", context())
    with pytest.raises(BriefParseError, match="numbered"):
        render_external("# Titel\n\nNur Text.\n", context())


# ---- parsing --------------------------------------------------------------------------------


def test_the_german_brief_parses_back() -> None:
    parsed = parse_brief(GOLDEN_DE)
    assert parsed.title == draft().question
    assert parsed.research_questions == (
        "Wie lange dauert der Rückbau?",
        "Welche Kosten fallen an?",
    )


def test_the_english_brief_parses_back() -> None:
    parsed = parse_brief(GOLDEN_EN)
    assert parsed.research_questions == ("How long does dismantling take?",)


def test_questions_are_found_under_either_label() -> None:
    for heading in ("Forschungsfragen", "Research questions"):
        assert parse_brief(f"# T\n\n## {heading}\n\n1. A\n2) B\n").research_questions == ("A", "B")


@pytest.mark.parametrize("heading", ["Forschungsfragen", "Research questions"])
def test_the_questions_heading_beats_numbered_items_elsewhere(heading: str) -> None:
    text = f"# T\n\n## Ziel\n\n1. Stray\n\n## {heading}\n\n1. Echt\n"
    assert parse_brief(text).research_questions == ("Echt",)


def test_only_numbered_items_under_the_questions_heading_count() -> None:
    text = (
        "# T\n\n## Ziel\n\n1. keine Frage\n\n"
        "## Forschungsfragen\n\n- Aufzählung\n1. Echt\n\n"
        "## Umfang\n\n2. Nein\n"
    )
    assert parse_brief(text).research_questions == ("Echt",)


def test_without_a_questions_heading_any_numbered_items_before_the_output_count() -> None:
    text = "# T\n\n1. Eins\n2. Zwei\n\n## Ausgabe\n\n1. nicht diese\n"
    assert parse_brief(text).research_questions == ("Eins", "Zwei")


def test_the_first_h1_is_the_title() -> None:
    assert parse_brief("Method: x\n\n# Erster\n\n# Zweiter\n\n1. Q\n").title == "Erster"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("kein Titel\n1. Q\n", "title"),
        ("## nur H2\n1. Q\n", "title"),
        ("# Titel\n\nkeine Fragen\n", "numbered"),
        ("# Titel\n\n## Forschungsfragen\n\n(leer)\n", "numbered"),
        ("#Titel ohne Leerzeichen\n1. Q\n", "title"),
        ("# \n1. Q\n", "title"),
    ],
)
def test_an_unparseable_brief_is_rejected_with_a_reason(text: str, message: str) -> None:
    with pytest.raises(BriefParseError, match=message):
        parse_brief(text)


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_parsing_accepts_every_line_ending(newline: str) -> None:
    text = newline.join(["# T", "", "## Forschungsfragen", "", "1. Q", ""])
    parsed = parse_brief(text)
    assert (parsed.title, parsed.research_questions) == ("T", ("Q",))


def test_a_template_object_is_all_the_renderer_needs_for_headings() -> None:
    template: ReportTemplate = TEMPLATES["technische-stellungnahme"]
    ctx = context(template=template)
    assert "Gliederung: Management Summary; Fragestellung und Abgrenzung;" in render_brief(
        draft(), ctx
    )


# ---- re-rendering only the Output section ---------------------------------------------------


def test_replacing_the_output_section_changes_nothing_else() -> None:
    text = render_brief(draft(), context())
    english = context(
        settings=SessionSettings(report_language="en", response_format="short", template_id="auto"),
        template=TEMPLATES["auto"],
        fmt=FORMATS.short,
    )
    result = replace_output(text, english, "Fachlich, für Ingenieure")
    assert result.split("## Ausgabe")[0] == text.split("## Ausgabe")[0]
    assert "**Berichtssprache: Englisch.**" in result
    assert "short, 500 bis 2000 Wörter" in result
    assert "Literaturübersicht" not in result
    assert "**Register:** Fachlich, für Ingenieure" in result
    assert canonical_text(result) == result


def test_replacing_the_output_with_the_same_settings_is_a_no_op() -> None:
    text = render_brief(draft(), context())
    assert replace_output(text, context(), "Fachlich, für Ingenieure") == text


def test_sections_after_the_output_section_are_kept() -> None:
    text = render_brief(draft(), context()) + "\n## Eigener Abschnitt\n\nVom Eigentümer ergänzt.\n"
    result = replace_output(text, context(), "Fachlich, für Ingenieure")
    assert result.endswith("## Eigener Abschnitt\n\nVom Eigentümer ergänzt.\n")
    assert result.count("## Ausgabe") == 1


def test_a_brief_without_an_output_section_gets_one_appended() -> None:
    result = replace_output("# T\n\n1. Q\n", context())
    assert result.startswith("# T\n\n1. Q\n\n## Ausgabe\n")
    assert canonical_text(result) == result


def test_the_english_output_heading_is_found_too() -> None:
    text = render_brief(draft(), context(interview_language="en"))
    result = replace_output(text, context(interview_language="en"), "Fachlich, für Ingenieure")
    assert result.count("## Output") == 1


def test_the_register_is_read_back_from_a_brief() -> None:
    assert extract_register(render_brief(draft(), context())) == "Fachlich, für Ingenieure"
    assert extract_register(render_brief(draft(tone=""), context())) == ""
    assert extract_register("# T\n\n1. Q\n") == ""
