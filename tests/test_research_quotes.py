"""G6 helper: quoted spans of five or more words must come from a note the sentence cites."""

import re

import pytest

from app.research.quotes import QuoteFailure, find_quotes, quote_failures
from app.text import normalize_for_match

SOURCE = "Der Rückbau kerntechnischer Anlagen erfordert eine Genehmigung nach dem Atomgesetz."
NOTES = {"n1": normalize_for_match(SOURCE), "n2": normalize_for_match("Völlig anderer Text hier.")}


def groups(window: str) -> list[str]:
    """Citations `[1]` and `[2]` name notes n1 and n2."""
    return [f"n{n}" for n in map(int, re.findall(r"\[(\d)\]", window))]


def failures(text: str, min_words: int = 5) -> list[QuoteFailure]:
    return quote_failures(text, groups, NOTES, min_words)


@pytest.mark.parametrize(
    ("open_", "close"),
    [
        ("„", "“"),
        ("“", "”"),
        ('"', '"'),
        ("«", "»"),
        ("»", "«"),
        ("‚", "‘"),
    ],
)
def test_every_quotation_style_of_the_prd_is_found(open_: str, close: str) -> None:
    (quote,) = find_quotes(f"Er sagt {open_}eine ganze Menge an Worten{close} dazu.")
    assert (quote.open, quote.span, quote.close) == (open_, "eine ganze Menge an Worten", close)


def test_a_verbatim_quote_of_the_cited_note_passes() -> None:
    text = "Es gilt: „Der Rückbau kerntechnischer Anlagen erfordert eine Genehmigung“ [1]."
    assert failures(text) == []


def test_matching_ignores_case_whitespace_and_quote_style() -> None:
    text = 'Es gilt: "der  RÜCKBAU kerntechnischer Anlagen" [1].'
    assert failures(text) == []


def test_a_quote_that_is_not_in_the_cited_note_fails() -> None:
    (bad,) = failures("Es gilt: „Der Rückbau dauert niemals länger als ein Jahr“ [1].")
    assert (bad.span, bad.reason) == (
        "Der Rückbau dauert niemals länger als ein Jahr",
        "not_in_source",
    )


def test_a_quote_from_another_note_than_the_cited_one_fails() -> None:
    assert [f.reason for f in failures("„Der Rückbau kerntechnischer Anlagen erfordert“ [2].")] == [
        "not_in_source"
    ]


def test_any_of_several_cited_notes_may_hold_the_quote() -> None:
    assert failures("„Der Rückbau kerntechnischer Anlagen erfordert“ [2] [1].") == []


def test_an_uncited_quote_fails() -> None:
    assert [
        f.reason for f in failures("Es gilt: „Der Rückbau kerntechnischer Anlagen erfordert“.")
    ] == ["uncited"]


def test_a_short_quote_is_not_checked() -> None:
    assert failures("Das nennt er „völlig falsch und irreführend“ [1].") == []  # four words
    assert failures("Das nennt er „völlig falsch und irreführend“ [1].", min_words=4) != []


def test_the_citation_must_be_in_the_same_sentence_and_paragraph() -> None:
    other_sentence = "„Der Rückbau kerntechnischer Anlagen erfordert“ ist wichtig. Anderes [1]."
    other_paragraph = "„Der Rückbau kerntechnischer Anlagen erfordert“ ist wichtig.\n\nAnderes [1]."
    assert [f.reason for f in failures(other_sentence)] == ["uncited"]
    assert [f.reason for f in failures(other_paragraph)] == ["uncited"]


def test_a_paragraph_break_ends_the_window_even_without_a_period() -> None:
    quote = "„Der Rückbau kerntechnischer Anlagen erfordert“"
    assert [f.reason for f in failures(f"{quote} ist wichtig\n\nAnderes [1]")] == ["uncited"]
    assert [f.reason for f in failures(f"Vorher [1]\n\n{quote} danach")] == ["uncited"]


def test_the_citation_before_the_quote_in_its_sentence_counts() -> None:
    assert (
        failures("Laut [1] gilt „Der Rückbau kerntechnischer Anlagen erfordert“ ohne Ausnahme.")
        == []
    )


def test_a_period_inside_the_quote_does_not_end_the_sentence() -> None:
    text = "Er schreibt „Der Rückbau kerntechnischer Anlagen erfordert eine Genehmigung.“ [1]"
    assert failures(text) == []


def test_two_quotes_are_judged_one_by_one() -> None:
    text = (
        "Zuerst „Der Rückbau kerntechnischer Anlagen erfordert“ [1] und dann "
        "„völlig erfundene Aussage zum Thema hier“ [1]."
    )
    assert [f.span for f in failures(text)] == ["völlig erfundene Aussage zum Thema hier"]


def test_a_quote_of_a_cited_note_that_is_unknown_fails() -> None:
    assert [f.reason for f in failures("„Der Rückbau kerntechnischer Anlagen erfordert“ [9].")] == [
        "uncited"
    ]
