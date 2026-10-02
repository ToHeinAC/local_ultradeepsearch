from pathlib import Path

import pytest
from support import make_settings

from app.templates import (
    MAX_SECTIONS,
    MIN_SECTIONS,
    TemplateError,
    get_template,
    load_templates,
    parse_template,
)

BUILT_IN = make_settings().templates_dir
IDS = [
    "auto",
    "technische-stellungnahme",
    "regulatorische-analyse",
    "literaturuebersicht",
    "markt-unternehmensanalyse",
]
HEADINGS = {
    "technische-stellungnahme": (
        "Management Summary",
        "Fragestellung und Abgrenzung",
        "Stand der Technik und Normenlage",
        "Technische Bewertung",
        "Empfehlungen",
        "Offene Punkte und Unsicherheiten",
    ),
    "regulatorische-analyse": (
        "Management Summary",
        "Rechtsrahmen (DE/EU)",
        "Anforderungen im Einzelnen",
        "Auslegung und Behördenpraxis",
        "Lücken, Konflikte und Risiken",
        "Handlungsbedarf",
    ),
    "literaturuebersicht": (
        "Kurzantwort",
        "Methodik der Recherche",
        "Befundlage je Forschungsfrage",
        "Widersprüche und Evidenzqualität",
        "Forschungslücken",
        "Fazit",
    ),
    "markt-unternehmensanalyse": (
        "Kurzantwort",
        "Ausgangslage",
        "Kennzahlen und Entwicklung",
        "Treiber und Risiken",
        "Szenarien",
        "Bewertung und Empfehlung",
    ),
}


def template_text(
    *,
    template_id: str = "demo",
    headings: tuple[str, ...] = ("Eins", "Zwei"),
    extra_front: str = "",
    comment: str = "<!-- Was hier stehen soll. -->",
) -> str:
    front = (
        f"---\nid: {template_id}\nname: Demo Vorlage\ndescription: Nur ein Test: mit Doppelpunkt\n"
        f"language: de\ndefault_response_format: structured\n{extra_front}---\n"
    )
    body = "".join(f"\n## {h}\n{comment}\n" for h in headings)
    return front + body


# ---- the built-ins --------------------------------------------------------------------------


def test_the_five_built_in_templates_load_in_file_name_order() -> None:
    templates = load_templates([BUILT_IN])
    assert list(templates) == sorted(IDS)  # "auto" sorts first


@pytest.mark.parametrize("template_id", IDS[1:])
def test_built_in_headings_match_the_prd(template_id: str) -> None:
    template = load_templates([BUILT_IN])[template_id]
    assert template.headings == HEADINGS[template_id]
    assert all(section.instructions for section in template.sections)  # each is explained
    assert template.language == "de"


def test_auto_derives_its_headings() -> None:
    auto = load_templates([BUILT_IN])["auto"]
    assert auto.sections == ()
    assert auto.derived_headings is True
    assert (auto.id, auto.language) == ("auto", "de")


def test_the_other_templates_have_fixed_headings() -> None:
    assert not load_templates([BUILT_IN])["literaturuebersicht"].derived_headings


def test_default_formats_are_valid_names() -> None:
    formats = {t.default_response_format for t in load_templates([BUILT_IN]).values()}
    assert formats <= {"short", "structured", "argumentative"}


# ---- parsing --------------------------------------------------------------------------------


def test_front_matter_and_sections_are_parsed() -> None:
    template = parse_template(template_text(extra_front="reference_docx: styles.docx\n"), "demo.md")
    assert (template.id, template.name) == ("demo", "Demo Vorlage")
    assert template.description == "Nur ein Test: mit Doppelpunkt"  # split on the first colon only
    assert (template.language, template.default_response_format) == ("de", "structured")
    assert template.reference_docx == "styles.docx"
    assert [(s.heading, s.instructions) for s in template.sections] == [
        ("Eins", "Was hier stehen soll."),
        ("Zwei", "Was hier stehen soll."),
    ]


def test_multiline_instructions_keep_their_lines() -> None:
    text = template_text(headings=("Eins", "Zwei"), comment="<!-- Zeile eins.\nZeile zwei. -->")
    assert parse_template(text, "d.md").sections[0].instructions == "Zeile eins.\nZeile zwei."


def test_a_heading_without_a_comment_has_empty_instructions() -> None:
    text = template_text(comment="")
    assert [s.instructions for s in parse_template(text, "d.md").sections] == ["", ""]


def test_other_markdown_in_the_body_is_ignored() -> None:
    text = template_text() + "\n### Unterpunkt\nText.\n"
    assert parse_template(text, "d.md").headings == ("Eins", "Zwei")


def test_windows_line_endings_parse_the_same() -> None:
    text = template_text().replace("\n", "\r\n")
    assert parse_template(text, "d.md").headings == ("Eins", "Zwei")


# ---- validation -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("no front matter\n## A\n## B\n", "front matter"),
        (template_text().replace("name: Demo Vorlage\n", ""), "name"),
        (template_text().replace("language: de", "language: deutsch"), "language"),
        (template_text().replace("structured", "essay"), "default_response_format"),
        (template_text(template_id="Demo Vorlage"), "id"),
        (template_text(template_id="../evil"), "id"),
        (template_text(extra_front="colour: blue\n"), "colour"),
        (template_text(extra_front="reference_docx: ../x.docx\n"), "reference_docx"),
        (template_text(headings=("Nur eins",)), f"{MIN_SECTIONS}"),
        (
            template_text(headings=tuple(f"H{i}" for i in range(MAX_SECTIONS + 1))),
            f"{MAX_SECTIONS}",
        ),
        (template_text(headings=("Eins", "eins")), "duplicate"),
        (template_text(headings=("Zwei  Wörter", "zwei wörter")), "duplicate"),
        (template_text().replace("name: Demo Vorlage\n", "name: A\nname: B\n"), "twice"),
    ],
)
def test_invalid_templates_are_rejected_with_a_reason(text: str, message: str) -> None:
    with pytest.raises(TemplateError, match=message):
        parse_template(text, "demo.md")


@pytest.mark.parametrize(
    "reserved", ["Quellen", "Sources", "Anhang A — Recherche-Brief", "appendix", "SOURCES"]
)
def test_reserved_headings_are_rejected(reserved: str) -> None:
    with pytest.raises(TemplateError, match="reserved"):
        parse_template(template_text(headings=("Eins", reserved)), "demo.md")


def test_bounds_are_inclusive() -> None:
    assert len(parse_template(template_text(headings=("A", "B")), "d.md").sections) == MIN_SECTIONS
    many = tuple(f"H{i}" for i in range(MAX_SECTIONS))
    assert len(parse_template(template_text(headings=many), "d.md").sections) == MAX_SECTIONS


def test_auto_must_have_no_sections_and_others_must_have_some() -> None:
    with pytest.raises(TemplateError, match="auto"):
        parse_template(template_text(template_id="auto"), "auto.md")
    assert parse_template(template_text(template_id="auto", headings=()), "auto.md").sections == ()


def test_errors_name_the_source_file() -> None:
    with pytest.raises(TemplateError, match=r"my-file\.md"):
        parse_template("broken", "my-file.md")


# ---- loading --------------------------------------------------------------------------------


def test_user_templates_are_added_after_the_built_ins(tmp_path: Path) -> None:
    (tmp_path / "meine.md").write_text(template_text(template_id="meine"), encoding="utf-8")
    templates = load_templates([BUILT_IN, tmp_path])
    assert list(templates) == [*sorted(IDS), "meine"]


def test_a_user_template_cannot_replace_a_built_in(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text(
        template_text(template_id="literaturuebersicht"), encoding="utf-8"
    )
    with pytest.raises(TemplateError, match=r"literaturuebersicht.*already"):
        load_templates([BUILT_IN, tmp_path])


def test_a_missing_user_directory_is_fine(tmp_path: Path) -> None:
    assert list(load_templates([BUILT_IN, tmp_path / "nope"])) == sorted(IDS)


def test_the_templates_directory_is_a_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert BUILT_IN.name == "templates"
    assert (BUILT_IN / "auto.md").is_file()
    monkeypatch.setenv("UDR_TEMPLATES_DIR", str(tmp_path))
    assert make_settings().templates_dir == tmp_path


def test_get_template_by_id_and_unknown_id() -> None:
    templates = load_templates([BUILT_IN])
    assert get_template(templates, "auto").id == "auto"
    with pytest.raises(TemplateError, match="unknown template 'nope'"):
        get_template(templates, "nope")


def test_a_broken_file_is_reported_by_name(tmp_path: Path) -> None:
    (tmp_path / "kaputt.md").write_text("nothing useful", encoding="utf-8")
    with pytest.raises(TemplateError, match=r"kaputt\.md"):
        load_templates([BUILT_IN, tmp_path])


def test_non_markdown_files_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not a template", encoding="utf-8")
    assert list(load_templates([BUILT_IN, tmp_path])) == sorted(IDS)
