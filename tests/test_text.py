# ruff: noqa: RUF001  # this file tests look-alike Unicode quotes and dashes on purpose
import pytest

from app.text import contains_quote, normalize_for_match, quote_in_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Hello \n\t World  ", "hello world"),
        ("“quoted” and „quoted“ and «quoted»", '"quoted" and "quoted" and "quoted"'),
        ("it’s ‘fine’", "it's 'fine'"),
        ("pre–war — post‑war", "pre-war - post-war"),
        ("a" + chr(0x2010) + "b " + chr(0x2012) + " c " + chr(0x2015) + " d", "a-b - c - d"),
        ("decommis­sioning", "decommissioning"),  # soft hyphen
        ("eﬃcient ＡＢ", "efficient ab"),  # ligature and fullwidth letters via NFKC
        ("STRAẞE", "strasse"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_for_match(raw: str, expected: str) -> None:
    assert normalize_for_match(raw) == expected


TEXT = (
    "Der Rückbau kerntechnischer Anlagen erfordert eine Genehmigung nach § 7 Abs. 3 AtG.\n"
    "Die Behörde prüft den Antrag – auch bei Forschungsreaktoren."
)


@pytest.mark.parametrize(
    "quote",
    [
        "Der Rückbau kerntechnischer Anlagen erfordert",
        "DER RÜCKBAU KERNTECHNISCHER ANLAGEN",  # case
        "Anlagen erfordert eine Genehmigung nach § 7 Abs. 3 AtG.",
        "AtG. Die Behörde prüft",  # across the line break
        "Die Behörde prüft den Antrag - auch bei",  # hyphen for en dash
        '"Die Behörde prüft den Antrag"',  # the model added quotation marks
        "„Die Behörde prüft den Antrag“",
        "  Die   Behörde  prüft  ",
    ],
)
def test_verbatim_quotes_are_found(quote: str) -> None:
    assert quote_in_text(quote, TEXT)


@pytest.mark.parametrize(
    "quote",
    [
        "Der Rückbau kerntechnischer Anlagen verlangt",  # one word changed
        "Anlagen kerntechnischer",  # reordered
        "kerntechnischer Anlagen und Reaktoren",  # extended beyond the source
        "ckbau kerntechnischer Anlagen",  # starts inside a word
        "Der Rückbau kerntechnischer Anlag",  # ends inside a word
        "",
        "   ",
        '""',
    ],
)
def test_altered_or_partial_quotes_are_rejected(quote: str) -> None:
    assert not quote_in_text(quote, TEXT)


def test_a_quote_may_end_before_punctuation() -> None:
    assert quote_in_text("nach § 7 Abs", TEXT)  # "Abs" is followed by "."
    assert quote_in_text("Genehmigung nach", TEXT)


def test_punctuation_counts_as_a_word_boundary() -> None:
    assert quote_in_text("Abs", "see Abs. 3 above")
    assert quote_in_text("Abs. 3", "see Abs. 3 above")


def test_contains_quote_accepts_prenormalised_text() -> None:
    norm = normalize_for_match(TEXT)
    assert contains_quote(norm, "Der Rückbau kerntechnischer")
    assert not contains_quote(norm, "Der Ausbau kerntechnischer")


def test_every_occurrence_is_considered_for_the_boundary_rule() -> None:
    # first occurrence is inside "reactors", the second is a whole word
    assert quote_in_text("actors", "reactors and actors")
    assert not quote_in_text("actors", "only reactors")
