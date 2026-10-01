import re

import pytest

from app.pipeline.chunking import length_class, split_paragraph_chunks, word_count


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def test_empty_input_gives_no_chunks() -> None:
    assert split_paragraph_chunks("", 100) == []
    assert split_paragraph_chunks("  \n\n  \n", 100) == []


def test_a_short_text_is_one_chunk() -> None:
    assert split_paragraph_chunks("One paragraph.", 100) == ["One paragraph."]


def test_paragraphs_are_packed_up_to_the_limit() -> None:
    text = "aaaa\n\nbbbb\n\ncccc\n\ndddd"
    assert split_paragraph_chunks(text, 10) == ["aaaa\n\nbbbb", "cccc\n\ndddd"]
    assert split_paragraph_chunks(text, 4) == ["aaaa", "bbbb", "cccc", "dddd"]
    assert split_paragraph_chunks(text, 1000) == [text]


def test_oversize_paragraphs_split_at_sentence_ends() -> None:
    paragraph = "First sentence here. Second sentence here. Third sentence here."
    chunks = split_paragraph_chunks(paragraph, 45)
    assert chunks == ["First sentence here. Second sentence here.", "Third sentence here."]


def test_a_sentence_longer_than_the_limit_is_hard_split() -> None:
    chunks = split_paragraph_chunks("x" * 250, 100)
    assert [len(c) for c in chunks] == [100, 100, 50]


@pytest.mark.parametrize(
    "text",
    [
        "Alpha beta gamma. " * 200,
        "para one.\n\n" * 50 + "para two without end " * 40,
        ("word " * 30 + "\n\n") * 30,
        "no punctuation at all " * 100,
        "Mixed. Sentences! And questions? " * 30 + "\n\n" + "tail" * 80,
    ],
)
@pytest.mark.parametrize("limit", [50, 120, 400, 5000])
def test_chunks_respect_the_limit_and_lose_nothing(text: str, limit: int) -> None:
    chunks = split_paragraph_chunks(text, limit)
    assert chunks
    assert all(0 < len(c) <= limit for c in chunks)
    assert squash("".join(chunks)) == squash(text)


def test_chunks_follow_the_original_order() -> None:
    text = "\n\n".join(f"paragraph number {i:03d}" for i in range(40))
    joined = "\n\n".join(split_paragraph_chunks(text, 120))
    assert re.findall(r"\d{3}", joined) == [f"{i:03d}" for i in range(40)]


@pytest.mark.parametrize(
    ("words", "label"),
    [
        (0, "short"),
        (1499, "short"),
        (1500, "medium"),
        (4999, "medium"),
        (5000, "long"),
        (80000, "long"),
    ],
)
def test_length_class(words: int, label: str) -> None:
    assert length_class(words) == label


def test_word_count() -> None:
    assert word_count("") == 0
    assert word_count("  one two\nthree\t four  ") == 4
