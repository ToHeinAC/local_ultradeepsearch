"""The ship gate G1-G12 (PRD §3.10): each check has a passing fixture and failing ones."""

import hashlib
import math
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support import make_settings

from app.pipeline.profiles import RunRules, load_run_rules
from app.research.gate import (
    FULL_ARTIFACTS,
    LIGHT_ARTIFACTS,
    GateInput,
    GateNote,
    GateResult,
    run_gate,
)
from app.research.markdown import body_of, body_words
from app.research.report import (
    ApprovedBrief,
    ReportSource,
    SectionText,
    appendix_heading,
    render_report,
)

RULES: RunRules = load_run_rules(make_settings().config_dir)
BRIEF = "# Wie lange dauert der Rückbau?\n\n## Forschungsfragen\n\n1. Wie lange?\n"
BRIEF_SHA = hashlib.sha256(BRIEF.encode("utf-8")).hexdigest()
APPROVED = ApprovedBrief(BRIEF, datetime(2026, 10, 2, 9, 30, tzinfo=UTC), "data/briefs/x.md")
HEADINGS = ("Erster Teil", "Zweiter Teil", "Dritter Teil")
QUOTE = "Der Rückbau kerntechnischer Anlagen erfordert eine Genehmigung nach dem Atomgesetz"
SOURCE_TEXT = f"{QUOTE}. Weitere Angaben folgen im Anhang des Berichts."
SENTENCE = (
    "Der Rückbau kerntechnischer Anlagen dauert nach den vorliegenden Angaben mehrere Jahre "
    "und erfordert in Deutschland eine umfangreiche Genehmigung durch die zuständige Behörde, "
    "die den Ablauf der einzelnen Arbeitsschritte sehr genau prüft"
)


def paragraph(n: int, key: str = "S1") -> str:
    return " ".join(f"{SENTENCE} [{key}]." for _ in range(n))


def source(note_id: str) -> ReportSource:
    return ReportSource(
        note_id=note_id,
        url=f"https://{note_id}.example.org/a",
        title="Ein Titel",
        authors=("A. Autor",),
        publisher=None,
        year=2020,
        retrieved="2026-10-01",
        retracted=False,
    )


KEYS = {"S1": source("n1"), "S2": source("n2")}
NOTES = {
    "n1": GateNote("n1", ("https://n1.example.org/a",), SOURCE_TEXT, retracted=False),
    "n2": GateNote("n2", ("https://n2.example.org/a",), "Anderer Text zum Thema.", retracted=False),
}


def report(texts: tuple[str, ...] | None = None, **kwargs: object) -> str:
    texts = texts or (paragraph(2), paragraph(2, "S2"), paragraph(2))
    sections = [SectionText(h, t) for h, t in zip(HEADINGS, texts, strict=True)]
    language = str(kwargs.get("language", "de"))
    return render_report("Der Titel", sections, KEYS, language, APPROVED).markdown


def gate_input(**overrides: object) -> GateInput:
    fields: dict[str, object] = {
        "report": report(),
        "brief_sha256": BRIEF_SHA,
        "required_headings": HEADINGS,
        "language": "de",
        "words_range": (100, 300),
        "tier": "light",
        "notes": NOTES,
        "artifacts": LIGHT_ARTIFACTS,
        "rules": RULES,
    }
    return GateInput(**{**fields, **overrides})  # type: ignore[arg-type]


def check(result: GateResult, check_id: str) -> tuple[bool, str]:
    (found,) = [c for c in result.checks if c.id == check_id]
    return found.passed, found.detail


def failed(**overrides: object) -> list[str]:
    return run_gate(gate_input(**overrides)).failed


def test_a_good_report_passes_all_twelve_checks() -> None:
    result = run_gate(gate_input())
    assert [c.id for c in result.checks] == [f"G{i}" for i in range(1, 13)]
    assert result.failed == [], [(c.id, c.detail) for c in result.checks if not c.passed]
    assert result.passed


def test_the_result_serialises_for_gate_json() -> None:
    data = run_gate(gate_input()).to_dict()
    assert data["passed"] is True
    assert data["checks"][0] == {
        "id": "G1",
        "name": "report-exists",
        "passed": True,
        "detail": "",
        "warnings": [],
    }


# ---- G1, G2 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [None, "", "  \n"])
def test_g1_needs_a_non_empty_report(text: str | None) -> None:
    assert "G1" in failed(report=text)


def test_g2_headings_must_match_exactly_and_in_order() -> None:
    assert "G2" not in failed()
    assert "G2" in failed(required_headings=("Zweiter Teil", "Erster Teil", "Dritter Teil"))
    assert "G2" in failed(required_headings=(*HEADINGS, "Vierter Teil"))
    assert "G2" in failed(required_headings=HEADINGS[:2])
    assert "G2" in failed(required_headings=("Erster Teil", "Zweiter teil", "Dritter Teil"))


def test_g2_counts_the_sources_and_appendix_headings_and_ignores_the_brief_inside_the_fence() -> (
    None
):
    text = report()
    assert "## Ausgabe" not in text  # the brief has none; a brief heading must never count
    with_heading = report().replace("1. Wie lange?", "1. Wie lange?\n\n## Ausgabe")
    assert "G2" not in failed(report=with_heading.replace(BRIEF, BRIEF + "\n## Ausgabe\n"))
    wrong = text.replace(appendix_heading("de"), "Anhang B")
    assert "G2" in failed(report=wrong)
    english = report(language="en")
    assert "G2" in failed(report=english)  # English headings for a German report


# ---- G3, G4 ---------------------------------------------------------------------------------


def test_g3_length_is_judged_on_the_body_against_the_widened_range() -> None:
    assert check(run_gate(gate_input()), "G3")[0]
    big = report((paragraph(8), paragraph(8), paragraph(8)))
    assert "G3" in failed(report=big)  # far above 1.2 x 300
    small = report((paragraph(1), "Kurz [S1].", "Kurz [S2]."))
    assert "G3" in failed(report=small)  # below 0.8 x 100


def test_g3_the_margins_are_exact() -> None:
    words = body_words(body_of(report()))
    high = math.ceil(words / 1.2)
    low = math.floor(words / 0.8)
    assert "G3" not in failed(words_range=(1, high))
    assert "G3" in failed(words_range=(1, high - 1))
    assert "G3" not in failed(words_range=(low, 100_000))
    assert "G3" in failed(words_range=(low + 1, 100_000))


def test_g3_words_in_sources_and_appendix_do_not_count() -> None:
    long_brief = BRIEF + "\n".join(f"{i}. Frage {'wort ' * 40}" for i in range(2, 30))
    sections = [SectionText(h, t) for h, t in zip(HEADINGS, (paragraph(2),) * 3, strict=True)]
    big_appendix = render_report(
        "Der Titel", sections, KEYS, "de", ApprovedBrief(long_brief, APPROVED.approved_at, "x")
    ).markdown
    assert "G3" not in failed(
        report=big_appendix, brief_sha256=hashlib.sha256(long_brief.encode()).hexdigest()
    )


def test_g4_citation_density_per_thousand_body_words() -> None:
    assert "G4" not in failed()
    plain = paragraph(2).replace(" [S1]", "")
    sparse = report((plain, plain, f"{plain} [S1]."))
    passed, detail = check(run_gate(gate_input(report=sparse)), "G4")
    assert not passed
    assert "per 1000" in detail


def test_g4_repeated_citations_all_count() -> None:
    assert "G4" not in failed(report=report((paragraph(2), paragraph(2), paragraph(2))))


# ---- G5 -------------------------------------------------------------------------------------


def test_g5_every_cited_number_needs_a_source_entry_that_maps_to_a_note() -> None:
    assert "G5" not in failed()
    assert "G5" in failed(
        report=report().replace("Dritter Teil\n\n", "Dritter Teil\n\nKein Eintrag [7]. ")
    )


def test_g5_an_entry_whose_url_is_no_note_of_the_run_fails() -> None:
    other = {
        "n1": GateNote("n1", ("https://elsewhere.example.org/a",), SOURCE_TEXT, retracted=False)
    }
    assert "G5" in failed(notes=other)


def test_g5_a_url_match_ignores_www_tracking_and_trailing_slash() -> None:
    alt = {
        "n1": GateNote("n1", ("https://www.n1.example.org/a/?utm_source=x",), SOURCE_TEXT, False),
        "n2": NOTES["n2"],
    }
    assert "G5" not in failed(notes=alt)


def test_g5_a_listed_url_is_compared_in_canonical_form_too() -> None:
    text = report().replace("https://n1.example.org/a ", "https://www.n1.example.org/a/?utm_x=1 ")
    assert "G5" not in failed(report=text)


def test_g5_an_unused_entry_only_warns() -> None:
    text = report().replace(
        "## Anhang",
        "[9] Z. Titel. 2020. https://n2.example.org/a (abgerufen 2026-10-01)\n\n## Anhang",
        1,
    )
    result = run_gate(gate_input(report=text))
    assert "G5" not in result.failed
    (g5,) = [c for c in result.checks if c.id == "G5"]
    assert any("[9]" in w for w in g5.warnings)


def test_g5_an_entry_without_a_parsable_url_resolves_nothing() -> None:
    text = report().replace("https://n1.example.org/a (abgerufen 2026-10-01)", "ohne Adresse")
    assert "G5" in failed(report=text)


# ---- G6 -------------------------------------------------------------------------------------


def with_quote(quote: str, key: str = "S1") -> str:
    return report(
        (f"{paragraph(2)} Er schreibt „{quote}“ [{key}].", paragraph(2, "S2"), paragraph(2))
    )


def test_g6_a_verbatim_quote_of_the_cited_note_passes() -> None:
    assert "G6" not in failed(report=with_quote(QUOTE))


def test_g6_an_invented_or_miscited_quote_fails() -> None:
    assert "G6" in failed(report=with_quote("Der Rückbau dauert niemals länger als ein Jahr"))
    assert "G6" in failed(report=with_quote(QUOTE, "S2"))
    detail = check(
        run_gate(gate_input(report=with_quote("Frei erfundenes Zitat mit vielen Worten"))), "G6"
    )[1]
    assert "Frei erfundenes Zitat" in detail


def test_g6_short_quotes_are_not_checked() -> None:
    assert "G6" not in failed(report=with_quote("frei erfunden mit vier"))


def test_g6_quotes_in_the_appendix_are_not_the_reports() -> None:
    brief = BRIEF + "\nSie sagt „ein erfundenes langes Zitat im Brief hier“ ohne Beleg.\n"
    sections = [SectionText(h, t) for h, t in zip(HEADINGS, (paragraph(2),) * 3, strict=True)]
    text = render_report(
        "T", sections, KEYS, "de", ApprovedBrief(brief, APPROVED.approved_at, "x")
    ).markdown
    assert "G6" not in failed(report=text, brief_sha256=hashlib.sha256(brief.encode()).hexdigest())


# ---- G7 -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leak",
    [
        "Siehe Locus 3 dazu.",
        "Tension 2 bleibt offen.",
        "Vgl. comparisons.md hierzu.",
        "Das ist nur interim gültig.",
        "Eine cross-locus Betrachtung folgt.",
        "Laut scaffold gilt das.",
        "Mit hyperresearch erstellt.",
        "Siehe [[note-1]] dazu.",
        "<think>Ich überlege</think> Text.",
    ],
)
def test_g7_pipeline_vocabulary_in_the_body_fails(leak: str) -> None:
    text = report((f"{paragraph(2)} {leak}", paragraph(2, "S2"), paragraph(2)))
    assert "G7" in failed(report=text)


def test_g7_front_matter_and_scaffold_headers_fail() -> None:
    assert "G7" in failed(report="---\ntitle: x\n---\n" + report())
    assert "G7" in failed(
        report=report().replace("## Erster Teil", "## Run config\n\n## Erster Teil")
    )
    assert "G7" in failed(
        report=report().replace("## Erster Teil", "## User Prompt\n\n## Erster Teil")
    )


def test_g7_words_are_matched_as_words_and_outside_sources_title_and_appendix_are_exempt() -> None:
    assert "G7" not in failed(
        report=report(
            (f"{paragraph(2)} Das Zwischeninterimslager.", paragraph(2, "S2"), paragraph(2))
        )
    )
    keys = {
        "S1": ReportSource(
            "n1", "https://n1.example.org/a", "Interim Report", (), None, 2020, "2026-10-01", False
        ),
        "S2": KEYS["S2"],
    }
    sections = [
        SectionText(h, t)
        for h, t in zip(HEADINGS, (paragraph(2), paragraph(2, "S2"), paragraph(2)), strict=True)
    ]
    text = render_report(
        "Der interim Bericht",
        sections,
        keys,
        "de",
        ApprovedBrief(BRIEF + "\ninterim\n", APPROVED.approved_at, "x"),
    ).markdown
    assert "G7" not in failed(
        report=text, brief_sha256=hashlib.sha256((BRIEF + "\ninterim\n").encode()).hexdigest()
    )


# ---- G8 -------------------------------------------------------------------------------------


def test_g8_the_appendix_must_hash_to_the_approved_brief() -> None:
    assert "G8" not in failed()
    assert "G8" in failed(brief_sha256="0" * 64)
    assert "G8" in failed(report=report().replace("1. Wie lange?", "1. Wie lang?"))
    assert "G8" in failed(report=report().split("## Anhang")[0])


# ---- G9 -------------------------------------------------------------------------------------

RETRACTED = {
    "n1": GateNote("n1", ("https://n1.example.org/a",), SOURCE_TEXT, retracted=True),
    "n2": NOTES["n2"],
}


def retracted_report(sentence: str) -> str:
    return report((paragraph(2, "S2"), sentence, paragraph(2, "S2")))


def test_g9_a_citation_of_a_retracted_note_needs_an_acknowledgement_nearby() -> None:
    assert "G9" not in failed()  # nothing retracted
    assert "G9" in failed(notes=RETRACTED)  # the good report cites n1 without a notice
    for word in ("zurückgezogen", "Retraction", "retracted"):
        text = retracted_report(f"Die Studie ist {word}, belegt aber den Ablauf [S1].")
        assert "G9" not in failed(report=text, notes=RETRACTED), word
    assert "G9" in failed(
        report=retracted_report("Die Studie belegt den Ablauf [S1]."), notes=RETRACTED
    )


def test_g9_an_acknowledgement_too_far_away_does_not_count() -> None:
    far = "Die Studie wurde zurückgezogen. " + "Text " * 100 + "[S1]."
    assert "G9" in failed(report=retracted_report(far), notes=RETRACTED)


def test_g9_the_notice_may_follow_the_citation() -> None:
    text = retracted_report("Die Studie [S1] wurde zurückgezogen.")
    assert "G9" not in failed(report=text, notes=RETRACTED)


# ---- G10, G11 -------------------------------------------------------------------------------


def test_g10_light_needs_the_polish_log_and_the_readability_decisions() -> None:
    assert frozenset({"polish-log.json", "readability-decisions.json"}) == LIGHT_ARTIFACTS
    assert "G10" not in failed()
    assert "G10" in failed(artifacts=frozenset({"polish-log.json"}))
    assert "G10" in failed(artifacts=frozenset())


def test_g10_full_also_needs_critics_patch_log_and_cite_check() -> None:
    assert LIGHT_ARTIFACTS < FULL_ARTIFACTS
    assert "G10" in failed(tier="full", artifacts=LIGHT_ARTIFACTS)
    assert "G10" not in failed(tier="full", artifacts=FULL_ARTIFACTS)


def test_g11_does_not_apply_to_light_and_counts_open_criticals_in_full() -> None:
    assert check(run_gate(gate_input(criticals_open=3)), "G11")[0]  # light: not applicable
    assert "G11" not in failed(tier="full", artifacts=FULL_ARTIFACTS)
    assert "G11" in failed(tier="full", artifacts=FULL_ARTIFACTS, criticals_open=2)


# ---- G12 ------------------------------------------------------------------------------------

ENGLISH = (
    "The decommissioning of nuclear facilities takes several years according to the available "
    "information and requires an extensive permit from the competent authority, which examines "
    "the individual work steps very closely and in great detail [S1]."
)


def test_g12_the_body_must_be_in_the_report_language() -> None:
    assert "G12" not in failed()
    english = report((ENGLISH * 2, ENGLISH * 2, ENGLISH * 2))
    assert "G12" in failed(report=english)
    in_english = report((ENGLISH * 2, ENGLISH * 2, ENGLISH * 2), language="en")
    assert "G12" not in failed(report=in_english, language="en")


def test_g12_one_foreign_sample_is_enough_to_fail() -> None:
    mixed = report((paragraph(2), ENGLISH * 2, paragraph(2)))
    assert "G12" in failed(report=mixed)


def test_g12_a_report_without_text_fails_instead_of_passing_by_default() -> None:
    assert "G12" in failed(report="# T\n\n## Quellen\n\nx\n")


def test_the_result_lists_the_failed_ids_in_order() -> None:
    result = run_gate(gate_input(report="", artifacts=frozenset()))
    assert result.failed[0] == "G1"
    assert "G10" in result.failed
    assert not result.passed


def test_gate_json_round_trips_to_a_file(tmp_path: Path) -> None:
    import json

    path = tmp_path / "gate.json"
    path.write_text(json.dumps(run_gate(gate_input()).to_dict()), encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["passed"] is True
