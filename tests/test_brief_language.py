from collections.abc import Callable

import pytest
from support import make_settings

from app.brief.language import detect_interview_language
from app.pipeline.profiles import load_phase1

LIMITS = load_phase1(make_settings().config_dir)  # 20 characters, probability 0.9
LONG = "x" * 20


def fake(*candidates: tuple[str, float]) -> Callable[[str], list[tuple[str, float]]]:
    def detect(_text: str) -> list[tuple[str, float]]:
        return list(candidates)

    return detect


def test_a_confident_detection_of_a_long_enough_message_wins() -> None:
    assert detect_interview_language(LONG, LIMITS, detect=fake(("en", 0.97), ("de", 0.03))) == "en"


@pytest.mark.parametrize(("probability", "expected"), [(0.9, "fr"), (0.89, "de")])
def test_the_probability_threshold_is_inclusive(probability: float, expected: str) -> None:
    assert detect_interview_language(LONG, LIMITS, detect=fake(("fr", probability))) == expected


@pytest.mark.parametrize(("length", "expected"), [(20, "en"), (19, "de")])
def test_the_length_threshold_is_inclusive(length: int, expected: str) -> None:
    assert detect_interview_language("x" * length, LIMITS, detect=fake(("en", 0.99))) == expected


def test_surrounding_whitespace_does_not_count_towards_the_length() -> None:
    padded = " " * 30 + "kurz" + " " * 30
    assert detect_interview_language(padded, LIMITS, detect=fake(("en", 0.99))) == "de"


def test_only_the_best_candidate_counts() -> None:
    assert detect_interview_language(LONG, LIMITS, detect=fake(("en", 0.5), ("fr", 0.45))) == "de"


def test_the_best_candidate_wins_whatever_the_order() -> None:
    assert detect_interview_language(LONG, LIMITS, detect=fake(("fr", 0.3), ("en", 0.95))) == "en"


def test_no_candidates_means_german() -> None:
    assert detect_interview_language(LONG, LIMITS, detect=fake()) == "de"


@pytest.mark.parametrize(("code", "expected"), [("zh-cn", "zh"), ("zh-tw", "zh"), ("pt", "pt")])
def test_regional_codes_are_reduced_to_the_language(code: str, expected: str) -> None:
    assert detect_interview_language(LONG, LIMITS, detect=fake((code, 0.99))) == expected


def test_a_detector_failure_means_german() -> None:
    def broken(_text: str) -> list[tuple[str, float]]:
        raise ValueError("no features in text")

    assert detect_interview_language(LONG, LIMITS, detect=broken) == "de"


# ---- the real detector ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Wie lange dauert der Rückbau eines Forschungsreaktors in Deutschland?", "de"),
        ("How long does it take to dismantle a research reactor in Germany?", "en"),
        ("Combien de temps faut-il pour démanteler un réacteur de recherche en France ?", "fr"),
        ("Hoeveel tijd kost het ontmantelen van een onderzoeksreactor in Nederland?", "nl"),
    ],
)
def test_the_real_detector_recognises_clear_sentences(text: str, expected: str) -> None:
    assert detect_interview_language(text, LIMITS) == expected


def test_the_real_detector_is_deterministic() -> None:
    text = "Rückbau costs and Genehmigung für die Stilllegung von reactors"
    assert len({detect_interview_language(text, LIMITS) for _ in range(25)}) == 1


def test_a_short_english_message_still_gets_german() -> None:
    assert detect_interview_language("KKW Rückbau costs?", LIMITS) == "de"


def test_the_detector_is_seeded() -> None:
    from langdetect import DetectorFactory

    assert DetectorFactory.seed == 0
