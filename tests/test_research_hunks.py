"""The patch engine (PRD AD5, D10): a model proposes `{old, new}` hunks, code applies them."""

import pytest

from app.research.hunks import (
    CATEGORY_ORDER,
    Hunk,
    Recommendation,
    apply_hunk,
    apply_polish,
    apply_recommendations,
    check_hunk,
    check_polish,
    check_recommendation,
)

MAX = 1200


# ---- every hunk -----------------------------------------------------------------------------


def test_a_hunk_must_name_text_that_occurs_exactly_once() -> None:
    text = "Eins zwei drei. Eins zwei vier."
    assert check_hunk(text, Hunk("zwei drei", "x"), MAX) is None
    assert check_hunk(text, Hunk("Eins zwei", "x"), MAX) == "ambiguous"
    assert check_hunk(text, Hunk("fünf", "x"), MAX) == "not_found"
    assert check_hunk(text, Hunk("", "x"), MAX) == "empty_old"


def test_old_text_is_limited_in_length() -> None:
    text = "wort " * 400
    assert check_hunk(text, Hunk(text.strip(), ""), 5000) is None
    assert check_hunk(text, Hunk(text.strip(), ""), MAX) == "too_long"


def test_old_text_of_exactly_the_limit_is_allowed() -> None:
    assert check_hunk("a" * 20, Hunk("a" * 20, ""), 20) is None
    assert check_hunk("a" * 21, Hunk("a" * 21, ""), 20) == "too_long"


def test_a_hunk_never_touches_a_heading() -> None:
    text = "Text.\n\n## Überschrift\n\nMehr."
    assert check_hunk(text, Hunk("Text.\n\n## Überschrift", "x"), MAX) == "touches_heading"
    assert check_hunk("Text.", Hunk("Text.", "Text.\n\n## Neu"), MAX) == "touches_heading"
    assert check_hunk("Text.", Hunk("Text.", "Text.\n\n# Neu"), MAX) == "touches_heading"
    assert check_hunk("Text.", Hunk("Text.", "Text #1."), MAX) is None  # a hash is not a heading


def test_apply_replaces_only_the_matched_text() -> None:
    assert apply_hunk("a b c", Hunk("b", "X")) == "a X c"


# ---- polish: cut only -----------------------------------------------------------------------


def test_polish_may_cut_but_never_add_characters() -> None:
    assert check_polish(Hunk("Es ist wichtig anzumerken, dass der", "Der")) is None
    assert check_polish(Hunk("abc", "abcd")) == "adds_text"
    assert check_polish(Hunk("abc", "abc")) is None  # equal length is not an addition
    assert check_polish(Hunk("abc", "xyz")) is None


def test_polish_keeps_every_citation_marker() -> None:
    assert check_polish(Hunk("Das gilt [S1] und [S2].", "Gilt [S1][S2].")) is None
    assert check_polish(Hunk("Das gilt [S1] und [S2].", "Das gilt [S1].")) == "drops_citation"
    assert check_polish(Hunk("Gilt [S1].", "Gilt [S1] [S1].")) == "adds_text"


def test_a_marker_that_occurs_twice_must_stay_twice() -> None:
    assert check_polish(Hunk("A [S1] und B [S1].", "A [S1] und B.")) == "drops_citation"
    assert check_polish(Hunk("A [S1] und B [S1].", "A [S1] B [S1].")) is None


def test_polish_keeps_every_number() -> None:
    assert check_polish(Hunk("Etwa 12 Jahre, also 12.5 Monate.", "12 Jahre, 12.5 Monate.")) is None
    assert check_polish(Hunk("Es dauert 12 Jahre lang.", "Es dauert Jahre lang.")) == "drops_number"
    assert check_polish(Hunk("Wert 1.5 und 1.5.", "Wert 1.5.")) == "drops_number"


def test_polish_applies_hunks_in_order_and_reports_what_it_rejected() -> None:
    text = "Natürlich ist der Rückbau teuer [S1]. Wie gesagt dauert er 12 Jahre."
    hunks = [
        Hunk("Natürlich ist", "Ist"),  # cut
        Hunk("Wie gesagt dauert", "Dauert"),  # cut
        Hunk("er 12 Jahre", "er Jahre"),  # drops a number
        Hunk("Ist der Rückbau teuer [S1]", "Der Rückbau ist teuer [S1] und wichtig"),  # adds text
        Hunk("kommt nicht vor", ""),
    ]
    out = apply_polish(text, hunks, MAX)
    assert out.text == "Ist der Rückbau teuer [S1]. Dauert er 12 Jahre."
    assert [h.old for h in out.applied] == ["Natürlich ist", "Wie gesagt dauert"]
    assert [(r.hunk.old[:10], r.reason) for r in out.rejected] == [
        ("er 12 Jahr", "drops_number"),
        ("Ist der Rü", "adds_text"),
        ("kommt nich", "not_found"),
    ]
    assert out.net_chars == len(out.text) - len(text) < 0


# ---- readability ----------------------------------------------------------------------------


def rec(category: str, current: str, recommended: str, rid: str = "r1") -> Recommendation:
    return Recommendation(rid, category, current, recommended)


def check(category: str, current: str, recommended: str, text: str | None = None) -> str | None:
    return check_recommendation(text or current, rec(category, current, recommended), MAX)


def test_the_allowed_categories_and_their_order() -> None:
    assert CATEGORY_ORDER == (
        "remove-hr",
        "merge-paragraphs",
        "break-paragraph",
        "make-list",
        "make-table",
        "bold-keyterms",
        "add-whitespace",
    )


def test_split_sentence_and_unknown_categories_are_not_allowed() -> None:
    assert check("split-sentence", "A, und B.", "A. B.") == "category_not_allowed"
    assert check("rewrite", "A.", "B.") == "category_not_allowed"


def test_remove_hr_only_removes_a_horizontal_rule() -> None:
    assert check("remove-hr", "Text\n\n---\n\nMehr", "Text\n\nMehr") is None
    assert check_recommendation("A\n\n---\n\nB", rec("remove-hr", "---", ""), MAX) is None
    assert check_recommendation("A\n\n***\n\nB", rec("remove-hr", "***", ""), MAX) is None
    assert check_recommendation("A --- B", rec("remove-hr", "A ---", "A"), MAX) == "bad_change"
    assert (
        check_recommendation("A\n\n---\n\nB", rec("remove-hr", "---", "Neu"), MAX) == "bad_change"
    )


def test_merge_joins_paragraphs_without_changing_a_word() -> None:
    current = "Der Rückbau dauert lange.\n\nEr ist teuer [S1]."
    assert (
        check("merge-paragraphs", current, "Der Rückbau dauert lange. Er ist teuer [S1].") is None
    )
    assert (
        check("merge-paragraphs", current, "Der Rückbau dauert lange. Er ist sehr teuer [S1].")
        == "bad_change"
    )
    assert (
        check("merge-paragraphs", current, "Der Rückbau dauert lange. Er ist teuer.")
        == "bad_change"
    )
    assert check("merge-paragraphs", "Ein Absatz.", "Ein Absatz.") == "bad_change"  # merges nothing
    assert check("merge-paragraphs", current, current.replace("\n\n", "\n\n\n")) == "bad_change"


def test_break_splits_a_paragraph_without_changing_a_word() -> None:
    current = "Erster Satz hier. Zweiter Satz da [S2]."
    assert check("break-paragraph", current, "Erster Satz hier.\n\nZweiter Satz da [S2].") is None
    assert (
        check("break-paragraph", current, "Erster Satz hier.\n\nZweiter Satz [S2].") == "bad_change"
    )
    assert check("break-paragraph", current, current) == "bad_change"


def test_list_and_table_keep_the_word_multiset_but_may_change_markup() -> None:
    prose = "Folgende Punkte: Rückbau, Genehmigung, Lagerung."
    listed = "Folgende Punkte:\n\n- Rückbau\n- Genehmigung\n- Lagerung"
    assert check("make-list", prose, listed) is None
    assert (
        check("make-list", prose, "Folgende Punkte:\n\n1. Rückbau\n2. Genehmigung\n3. Lagerung")
        is None
    )
    assert check("make-list", prose, "Folgende Punkte:\n\n- Rückbau\n- Genehmigung") == "bad_change"
    assert (
        check("make-list", prose, "Folgende Punkte: Rückbau, Genehmigung, Lagerung.")
        == "bad_change"
    )
    flat = "Phase Dauer Rückbau zwei Lagerung drei"
    table = "| Phase | Dauer |\n|---|---|\n| Rückbau | zwei |\n| Lagerung | drei |"
    assert check("make-table", flat, table) is None
    assert check("make-table", flat, table.replace("zwei", "vier")) == "bad_change"
    assert check("make-table", flat, "Phase Dauer Rückbau zwei Lagerung drei") == "bad_change"


def test_a_list_needs_at_least_two_items() -> None:
    prose = "Folgender Punkt: Rückbau."
    assert check("make-list", prose, "Folgender Punkt:\n\n- Rückbau") == "bad_change"


def test_bold_only_adds_emphasis() -> None:
    same = "Die **Genehmigung** ist nötig."
    assert check("bold-keyterms", same, same) == "bad_change"
    assert check("bold-keyterms", "Die Genehmigung ist.", "Die  Genehmigung ist.") == "bad_change"
    assert (
        check("bold-keyterms", "Die Genehmigung ist nötig.", "Die **Genehmigung** ist nötig.")
        is None
    )
    assert (
        check("bold-keyterms", "Die Genehmigung ist nötig.", "Die **Erlaubnis** ist nötig.")
        == "bad_change"
    )


def test_whitespace_changes_keep_the_words() -> None:
    assert check("add-whitespace", "Satz eins.\nSatz zwei.", "Satz eins.\n\nSatz zwei.") is None
    assert (
        check("add-whitespace", "Satz eins.\nSatz zwei.", "Satz eins.\nSatz drei.") == "bad_change"
    )
    assert check("add-whitespace", "Satz.", "Satz.") == "bad_change"  # nothing changes


def test_every_recommendation_keeps_citations_and_stays_off_headings() -> None:
    assert check("break-paragraph", "A [S1]. B [S2].", "A [S1].\n\nB.") == "bad_change"
    heading = "Text\n\n## Titel\n\nMehr"
    assert (
        check_recommendation(heading, rec("merge-paragraphs", heading, "Text Titel Mehr"), MAX)
        == "touches_heading"
    )
    assert (
        check_recommendation("a b", rec("add-whitespace", "a b", "a\n## b"), MAX)
        == "touches_heading"
    )
    long = ("wort " * 300).strip()
    assert (
        check_recommendation(long, rec("add-whitespace", long, long.replace(" ", "  ")), MAX)
        == "too_long"
    )
    assert check_recommendation("abc", rec("add-whitespace", "xyz", "x y z"), MAX) == "not_found"


def test_recommendations_are_applied_in_the_category_order() -> None:
    text = "Eins zwei.\n\n---\n\nDrei vier.\n\nFünf sechs."
    recs = [
        rec("bold-keyterms", "vier.\n\nFünf", "vier.\n\n**Fünf**", "bold"),
        rec("merge-paragraphs", "Drei vier.\n\nFünf sechs.", "Drei vier. Fünf sechs.", "merge"),
        rec("remove-hr", "---", "", "hr"),
    ]
    out = apply_recommendations(text, recs, MAX)
    # remove-hr first, then merge; the bold edit's text no longer exists after the merge
    assert out.text == "Eins zwei.\n\n\n\nDrei vier. Fünf sechs."
    assert [(d.id, d.status) for d in out.decisions] == [
        ("hr", "applied"),
        ("merge", "applied"),
        ("bold", "edit_failure"),
    ]


def test_skipped_and_failed_recommendations_are_logged_with_a_reason() -> None:
    text = "Satz eins. Satz zwei."
    recs = [
        rec("split-sentence", "Satz eins.", "Satz. eins.", "a"),
        rec("add-whitespace", "Satz eins. Satz zwei.", "Satz eins.\n\nSatz zwei.", "b"),
        rec("add-whitespace", "gibt es nicht", "gibt  es nicht", "c"),
    ]
    out = apply_recommendations(text, recs, MAX)
    assert out.text == "Satz eins.\n\nSatz zwei."
    assert [(d.id, d.status, d.reason) for d in out.decisions] == [
        ("b", "applied", ""),
        ("c", "edit_failure", "not_found"),
        ("a", "skipped", "category_not_allowed"),
    ]
    assert out.net_chars == 1


@pytest.mark.parametrize("category", CATEGORY_ORDER)
def test_every_category_is_checked_by_some_rule(category: str) -> None:
    assert check(category, "A.", "A.") == "bad_change"
