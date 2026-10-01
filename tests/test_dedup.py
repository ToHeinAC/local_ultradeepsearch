import random
import subprocess
import sys

import pytest

from app.pipeline.dedup import NUM_PERM, THRESHOLD, NearDupIndex, signature, similarity

VOCABULARY = [f"term{n}" for n in range(600)]


def article(seed: int, words: int = 700) -> str:
    rng = random.Random(seed)
    return " ".join(rng.choice(VOCABULARY) for _ in range(words))


def edited(text: str, every: int) -> str:
    """The same text with every ``every``-th word replaced."""
    parts = text.split()
    for i in range(0, len(parts), every):
        parts[i] = f"changed{i}"
    return " ".join(parts)


def sig(text: str) -> bytes:
    result = signature(text)
    assert result is not None
    return result


def test_constants_follow_the_prd() -> None:
    assert (NUM_PERM, THRESHOLD) == (128, 0.6)


def test_signatures_are_stable_and_fixed_size() -> None:
    first, second = sig(article(1)), sig(article(1))
    assert first == second
    assert len(first) == len(sig(article(2)))
    assert similarity(first, second) == 1.0


def test_signatures_ignore_case_spacing_punctuation_quotes() -> None:
    text = "Der Rückbau kerntechnischer Anlagen erfordert eine Genehmigung. " * 40
    dash = chr(0x2013)
    variant = text.upper().replace(" ", "  ").replace("Genehmigung.", f"Genehmigung {dash}")
    assert similarity(sig(text), sig(variant)) > 0.95


def test_a_lightly_edited_copy_is_a_near_duplicate() -> None:
    base = article(1)
    assert similarity(sig(base), sig(edited(base, every=20))) >= THRESHOLD  # 5 % of the words


def test_a_heavily_edited_copy_is_not() -> None:
    base = article(1)
    assert similarity(sig(base), sig(edited(base, every=5))) < THRESHOLD  # 20 % of the words


def test_different_articles_are_far_apart() -> None:
    assert similarity(sig(article(1)), sig(article(2))) < 0.1


def test_the_index_finds_near_duplicates_only() -> None:
    base = article(1)
    index = NearDupIndex()
    index.add("n0001", sig(base))
    index.add("n0002", sig(article(2)))
    assert index.find(sig(edited(base, every=20))) == "n0001"
    assert index.find(sig(article(3))) is None
    assert index.find(sig(edited(base, every=5))) is None
    assert len(index) == 2


def test_the_earliest_match_wins() -> None:
    base = article(1)
    index = NearDupIndex()
    index.add("n0007", sig(base))
    index.add("n0003", sig(edited(base, every=40)))  # insertion order, not id order, decides
    assert index.find(sig(base)) == "n0007"


def test_an_index_rebuilt_from_stored_bytes_answers_the_same() -> None:
    texts = {f"n{i:04d}": article(i) for i in range(1, 6)}
    live = NearDupIndex()
    for note_id, text in texts.items():
        live.add(note_id, sig(text))
    rebuilt = NearDupIndex((note_id, sig(text)) for note_id, text in texts.items())
    probe = sig(edited(texts["n0003"], every=20))
    assert live.find(probe) == rebuilt.find(probe) == "n0003"


def test_the_threshold_is_configurable() -> None:
    base = article(1)
    probe = sig(edited(base, every=20))
    assert NearDupIndex([("n0001", sig(base))], threshold=0.6).find(probe) == "n0001"
    assert NearDupIndex([("n0001", sig(base))], threshold=0.95).find(probe) is None


def test_texts_without_words_have_no_signature() -> None:
    assert signature("") is None
    assert signature("   \n\t ") is None
    assert signature("!!! ??? ---") is None


def test_very_short_texts_still_get_a_signature() -> None:
    assert signature("one") is not None
    assert signature("one two") is not None
    assert similarity(sig("one two"), sig("one two")) == 1.0
    assert similarity(sig("one two"), sig("three four")) < 0.5


def test_foreign_or_damaged_signatures_are_rejected_or_ignored() -> None:
    good = sig(article(1))
    with pytest.raises(ValueError, match="signature"):
        similarity(good, b"not a signature")
    with pytest.raises(ValueError, match="signature"):
        similarity(good, good[:-4])
    index = NearDupIndex([("n0001", b"garbage"), ("n0002", good)])
    assert len(index) == 1
    assert index.find(good) == "n0002"


def test_signatures_are_identical_across_processes() -> None:
    """A resumed run computes signatures in a new process; they must agree with stored ones."""
    code = (
        "import sys; sys.path.insert(0, 'tests')\n"
        "from test_dedup import article, sig\n"
        "sys.stdout.write(sig(article(7)).hex())"
    )
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert other.stdout == sig(article(7)).hex()
