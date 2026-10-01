from pathlib import Path

import pytest

from app.adapters.outbound.denylist import Denylist, fold_variants, tokens


def fullwidth(text: str) -> str:
    """ASCII text in fullwidth forms (U+FF01..), spaces as ideographic spaces."""
    return "".join(
        chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else "\u3000" if c == " " else c for c in text
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Müller", ("mueller", "muller")),
        ("STRAẞE", ("strasse", "strasse")),
        ("Café Crème", ("cafe creme", "cafe creme")),
        (fullwidth("ACME"), ("acme", "acme")),  # NFKC folds fullwidth letters
        ("Øresund Æblegård", ("oresund aeblegard", "oresund aeblegard")),
        ("Łódź", ("lodz", "lodz")),
    ],
)
def test_fold_variants(text: str, expected: tuple[str, str]) -> None:
    assert fold_variants(text) == expected


def test_tokens_split_on_everything_but_letters_and_digits() -> None:
    assert tokens("acme-corp_2026 (x.y)/z") == ["acme", "corp", "2026", "x", "y", "z"]
    assert tokens("Привет, мир") == ["привет", "мир"]


# Variants of a term that MUST be caught (PRD M2 AC1).
TERM = "Müller-Werke"
MUST_MATCH = [
    "Müller-Werke",
    "müller-werke",
    "MÜLLER WERKE",
    "Mueller Werke",
    "Muller Werke",
    "MuellerWerke",
    "Müller   -   Werke",
    "Müller_Werke",
    "Müller.Werke",
    "about mueller-werke gmbh revenue",
    "https://example.com/search?q=M%C3%BCller-Werke",  # percent-encoded umlaut
    "https://example.com/?q=Mueller+Werke",
    fullwidth("MUELLER") + " " + fullwidth("WERKE"),
    "Müller Werke",  # decomposed umlaut
]
MUST_NOT_MATCH = [
    "Müller",  # only part of the term
    "Werke Müller",  # wrong order
    "Müllerwerkstatt",  # inside a longer word
    "Bergmüller-Werkeln",
    "",
]


@pytest.mark.parametrize("text", MUST_MATCH)
def test_term_variants_are_found(text: str) -> None:
    assert Denylist([TERM]).find(text) == [TERM]


@pytest.mark.parametrize("text", MUST_NOT_MATCH)
def test_non_matches(text: str) -> None:
    assert Denylist([TERM]).find(text) == []


def test_short_terms_respect_word_boundaries() -> None:
    deny = Denylist(["AG"])
    assert deny.find("Tagung in Hamburg") == []
    assert deny.find("Beispiel AG, Hamburg") == ["AG"]


def test_single_token_terms_match_joined_and_split_text() -> None:
    deny = Denylist(["Projekt Kranich"])
    assert deny.find("projektkranich.pdf") == ["Projekt Kranich"]
    assert deny.find("Projekt-Kranich Abschlussbericht") == ["Projekt Kranich"]


def test_all_matching_terms_are_reported_once_in_list_order() -> None:
    deny = Denylist(["Acme", "Projekt Kranich", "Zeta"])
    hits = deny.find("acme acme and projekt kranich")
    assert hits == ["Acme", "Projekt Kranich"]


def test_terms_without_letters_or_digits_are_rejected() -> None:
    deny = Denylist([])
    with pytest.raises(ValueError, match="letters or digits"):
        deny.add(" -- ")
    with pytest.raises(ValueError, match="letters or digits"):
        Denylist(["!!"])


def test_add_dedupes_by_folded_form_and_remove_by_variant() -> None:
    deny = Denylist(["Müller-Werke"])
    assert deny.add("MUELLER WERKE") is False
    assert deny.add("Acme") is True
    assert deny.terms == ("Müller-Werke", "Acme")
    assert deny.remove("mueller werke") is True
    assert deny.remove("never added") is False
    assert deny.terms == ("Acme",)


def test_file_round_trip_with_comments_and_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "data" / "denylist.txt"
    assert Denylist.load(path).terms == ()  # a missing file is an empty list
    path.parent.mkdir(parents=True)
    path.write_text("# clients\nAcme Corp\n\n  Projekt Kranich  \n# end\n", encoding="utf-8")
    deny = Denylist.load(path)
    assert deny.terms == ("Acme Corp", "Projekt Kranich")
    deny.add("Zeta")
    deny.save(path)
    assert Denylist.load(path).terms == ("Acme Corp", "Projekt Kranich", "Zeta")
    assert path.read_text(encoding="utf-8").startswith("#")


def test_contains_any() -> None:
    deny = Denylist(["Acme"])
    assert deny.contains_any("ACME report")
    assert not deny.contains_any("acmeist")
