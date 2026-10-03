"""Reading the report back: headings outside code fences, body, sources, appendix, citations."""

import pytest

from app.research.markdown import (
    appendix_fence,
    body_of,
    body_words,
    citation_numbers,
    h2_list,
    sources_entries,
    strip_citations,
)

FENCED = """# Title

## One

text

## Quellen

[1] A. Title. 2020. https://a.example/x (abgerufen 2026-10-01)

[2] B. Other. o. J. https://b.example/y (abgerufen 2026-10-01)

## Anhang A — Recherche-Brief

```text
# The brief

## Ausgabe

- not a heading of the report
```

Freigegeben am 2026-10-02 09:30:15 UTC. Archiviert unter x.md.
"""


def test_h2_list_ignores_headings_inside_code_fences() -> None:
    assert h2_list(FENCED) == ["One", "Quellen", "Anhang A — Recherche-Brief"]


def test_h2_list_handles_tilde_and_longer_fences() -> None:
    text = (
        "## A\n\n````\n```\n## inside\n```\n## still inside\n````\n\n~~~\n## tilde\n~~~\n\n## B\n"
    )
    assert h2_list(text) == ["A", "B"]


def test_inline_code_with_backticks_does_not_open_a_fence() -> None:
    assert h2_list("```x``` and more\n\n## A\n") == ["A"]


def test_a_deeply_indented_fence_line_does_not_close_a_fence() -> None:
    text = "```\n## hidden\n    ```\n## still hidden\n```\n## visible\n"
    assert h2_list(text) == ["visible"]


def test_an_unclosed_fence_hides_the_rest() -> None:
    assert h2_list("## A\n\n```\n## B\n") == ["A"]


def test_the_body_is_the_text_before_the_sources_heading() -> None:
    assert body_of(FENCED).rstrip().endswith("text")
    assert "Quellen" not in body_of(FENCED)
    assert body_of("# T\n\n## A\n\ntext\n") == "# T\n\n## A\n\ntext\n"  # no sources heading: all


@pytest.mark.parametrize("heading", ["## Sources", "## Quellen"])
def test_both_sources_headings_end_the_body(heading: str) -> None:
    assert body_of(f"# T\n\ntext\n\n{heading}\n\n[1] x\n") == "# T\n\ntext\n\n"


def test_sources_entries_map_numbers_to_the_entry_text() -> None:
    entries = sources_entries(FENCED)
    assert sorted(entries) == [1, 2]
    assert entries[1].startswith("A. Title. 2020. https://a.example/x")


def test_the_appendix_fence_is_the_brief_byte_for_byte() -> None:
    assert appendix_fence(FENCED) == (
        "# The brief\n\n## Ausgabe\n\n- not a heading of the report\n"
    )


def test_a_longer_fence_closes_only_on_a_fence_at_least_as_long() -> None:
    text = "## Appendix A — Research Brief\n\n````text\nline\n```\nmore\n````\n"
    assert appendix_fence(text) == "line\n```\nmore\n"


def test_no_appendix_or_no_fence_gives_none() -> None:
    assert appendix_fence("## One\n\ntext\n") is None
    assert appendix_fence("## Anhang A — Recherche-Brief\n\nno fence\n") is None


def test_citation_numbers_in_order_of_appearance() -> None:
    assert citation_numbers("A [1, 3]. B [2] [1].") == [1, 3, 2, 1]
    assert citation_numbers("array[0] and [text] and [1a]") == [0]  # only pure number lists


def test_strip_citations_and_word_count() -> None:
    assert strip_citations("Ein Satz [1, 2] endet [3].") == "Ein Satz endet."
    assert body_words("Ein Satz [1, 2] endet [3].") == 3
    assert body_words("[1] Anfang") == 1
