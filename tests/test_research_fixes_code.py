"""The ship gate's fixes that code makes alone (PRD §3.10, D11): no model, no invention."""

import pytest

from app.research.fixes_code import (
    RETRACTION_NOTICE,
    acknowledge_retractions,
    citation_density,
    citations_only_change,
    keyed_quote_failures,
    unquote,
)
from app.research.leakage import leak_sentences, leaks, scrub
from app.research.quotes import QuoteFailure
from app.text import normalize_for_match

SOURCE = "Der Rückbau kerntechnischer Anlagen erfordert eine Genehmigung nach dem Atomgesetz."
NOTES = {"n1": normalize_for_match(SOURCE)}
KEYS = {"S1": "n1"}
OPEN, CLOSE = chr(0x201E), chr(0x201C)  # German low and high double quotation marks


def test_unquote_removes_the_marks_of_the_failing_spans_only() -> None:
    good = f"{OPEN}Der Rückbau kerntechnischer Anlagen erfordert{CLOSE}"
    bad = f"{OPEN}Das hat die Quelle so nie gesagt hier{CLOSE}"
    text = f"A: {good} [S1]. B: {bad} [S1]."
    failures = keyed_quote_failures(text, KEYS, NOTES, 5)
    assert [f.span for f in failures] == ["Das hat die Quelle so nie gesagt hier"]
    fixed = unquote(text, failures)
    assert fixed == f"A: {good} [S1]. B: Das hat die Quelle so nie gesagt hier [S1]."
    assert keyed_quote_failures(fixed, KEYS, NOTES, 5) == []


def test_unquote_handles_every_occurrence_and_other_styles() -> None:
    span = "ein erfundenes Zitat mit sechs Worten"
    failure = QuoteFailure('"', span, '"', "uncited")
    text = f'Erst "{span}" und nochmal "{span}".'
    assert unquote(text, [failure]) == f"Erst {span} und nochmal {span}."


def test_an_uncited_quote_fails_in_keyed_text_too() -> None:
    text = f"Er sagt {OPEN}Der Rückbau kerntechnischer Anlagen erfordert{CLOSE} ohne Beleg."
    assert [f.reason for f in keyed_quote_failures(text, KEYS, NOTES, 5)] == ["uncited"]


def test_a_key_without_a_note_resolves_to_nothing() -> None:
    text = f"{OPEN}Der Rückbau kerntechnischer Anlagen erfordert{CLOSE} [S9]."
    assert [f.reason for f in keyed_quote_failures(text, KEYS, NOTES, 5)] == ["uncited"]


# ---- retractions ----------------------------------------------------------------------------


def ack(text: str, window: int = 200, language: str = "de") -> str:
    return acknowledge_retractions(text, {"S2"}, RETRACTION_NOTICE[language], window)


def test_a_notice_follows_the_citation_of_a_retracted_source() -> None:
    assert ack("Der Befund [S2] gilt.") == "Der Befund [S2] (zurückgezogen) gilt."
    assert ack("Der Befund [S2] gilt.", language="en") == "Der Befund [S2] (retracted) gilt."


def test_a_group_with_a_retracted_key_gets_one_notice() -> None:
    assert ack("Belegt [S1, S2].") == "Belegt [S1, S2] (zurückgezogen)."
    assert ack("Belegt [S1][S2].") == "Belegt [S1][S2] (zurückgezogen)."


def test_other_sources_and_already_acknowledged_citations_are_left_alone() -> None:
    assert ack("Belegt [S1].") == "Belegt [S1]."
    done = "Die Studie wurde zurückgezogen. Der Befund [S2] gilt."
    assert ack(done) == done
    assert (
        ack("Retraction notice: Der Befund [S2] gilt.")
        == "Retraction notice: Der Befund [S2] gilt."
    )


def test_a_notice_after_the_citation_counts_too() -> None:
    text = "Der Befund [S2] gilt. Die Studie wurde zurückgezogen."
    assert ack(text) == text


def test_an_acknowledgement_beyond_the_window_does_not_count() -> None:
    text = "Zurückgezogen. " + "wort " * 80 + "Der Befund [S2] gilt."
    assert ack(text, window=50).endswith("[S2] (zurückgezogen) gilt.")
    assert ack(text, window=1000) == text


def test_every_unacknowledged_citation_gets_its_notice() -> None:
    fixed = ack("Eins [S2]. " + "wort " * 100 + "Zwei [S2].", window=50)
    assert fixed.count("(zurückgezogen)") == 2


# ---- citation repair ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "ok"),
    [
        ("Der Rückbau dauert zehn Jahre.", "Der Rückbau dauert zehn Jahre [S1].", True),
        ("Der Rückbau dauert zehn Jahre.", "Der Rückbau [S1] dauert zehn Jahre.", True),
        ("Satz [S1].", "Satz [S1, S2].", True),
        ("Satz [S1].", "Satz [S1][S2].", True),
        ("Der Rückbau dauert zehn Jahre.", "Der Rückbau dauert elf Jahre [S1].", False),
        ("Satz [S1].", "Satz.", False),
        ("Satz.", "Satz [S9].", False),
        ("Satz.", "Satz", False),
    ],
)
def test_a_citation_repair_may_only_add_known_keys(old: str, new: str, ok: bool) -> None:
    assert citations_only_change(old, new, {"S1", "S2"}) is ok


def test_citation_density_counts_markers_per_thousand_words_of_the_text() -> None:
    text = " ".join(["wort"] * 96) + " [S1] [S2] [S1, S3]."
    assert citation_density(text) == pytest.approx(4 / 96 * 1000)
    assert citation_density("") == 0.0


# ---- leakage --------------------------------------------------------------------------------


def test_scrub_removes_front_matter_thinking_and_scaffold_headers() -> None:
    raw = "---\ntitle: x\n---\n<think>nein</think>Text A.\n\n## Run config\n\nText B."
    assert scrub(raw) == "Text A.\n\nText B."
    assert leaks(scrub(raw)) == []


def test_scrub_leaves_clean_text_alone() -> None:
    assert scrub("Ein Satz [S1].\n\nNoch einer.") == "Ein Satz [S1].\n\nNoch einer."


def test_the_vocabulary_is_found_by_sentence() -> None:
    text = (
        "Alles gut hier. Das ist nur interim gültig [S1]. Siehe Locus 3 dazu.\n\nCross-locus Fazit."
    )
    assert leak_sentences(text) == [
        "Das ist nur interim gültig [S1].",
        "Siehe Locus 3 dazu.",
        "Cross-locus Fazit.",
    ]
    assert leak_sentences("Siehe Locus 3 dazu. Siehe Locus 3 dazu.") == ["Siehe Locus 3 dazu."]
    assert leak_sentences("Alles sauber.") == []
