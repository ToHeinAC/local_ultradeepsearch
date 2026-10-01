from pathlib import Path

import pytest
from support import make_settings

from app.pipeline.scoring import (
    RETRACTED_CAP,
    authority_percentiles,
    quality,
    quality_scores,
)
from app.pipeline.strategies import load_strategies
from app.store.models import NewSource, SourceMeta
from app.store.vault import Vault

# Weights (PRD AC4): tier .35, utility .20 (value utility / 18), authority .25; renormalised over
# the components that are present.


@pytest.mark.parametrize(
    ("tier", "utility", "authority", "expected"),
    [
        # tier only: the score is the tier weight itself
        (0.85, None, None, 0.85),
        (0.6, None, None, 0.6),
        # tier + utility: (.35*tier + .20*u/18) / .55
        (1.0, 18, None, 1.0),
        (1.0, 9, None, 0.45 / 0.55),
        (0.4, 0, None, 0.14 / 0.55),
        # tier + authority: (.35*tier + .25*a) / .60
        (0.6, None, 0.8, 0.41 / 0.60),
        (1.0, None, 0.0, 0.35 / 0.60),
        # all three: (.35*tier + .20*u/18 + .25*a) / .80
        (0.85, 12, 0.5, (0.2975 + 0.2 * 12 / 18 + 0.125) / 0.80),
        (1.0, 18, 1.0, 1.0),
        (0.0, 0, 0.0, 0.0),
    ],
)
def test_quality_is_the_renormalised_weighted_sum(
    tier: float, utility: float | None, authority: float | None, expected: float
) -> None:
    assert quality(tier, utility, authority, retracted=False) == pytest.approx(expected)


def test_utility_and_authority_are_clamped_into_range() -> None:
    assert quality(1.0, 99, 5.0, retracted=False) == pytest.approx(1.0)
    assert quality(0.0, -4, -1.0, retracted=False) == pytest.approx(0.0)


def test_a_retracted_source_never_scores_above_the_floor() -> None:
    assert RETRACTED_CAP == 0.05
    assert quality(1.0, 18, 1.0, retracted=True) == pytest.approx(0.05)
    assert quality(0.0, 0, 0.0, retracted=True) == pytest.approx(0.0)  # a lower score stays lower


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({"a": 10, "b": 20, "c": 30}, {"a": 0.5 / 3, "b": 1.5 / 3, "c": 2.5 / 3}),
        ({"a": 7}, {"a": 0.5}),
        ({"a": 5, "b": 5, "c": 5, "d": 5}, {"a": 0.5, "b": 0.5, "c": 0.5, "d": 0.5}),
        ({"a": 1, "b": 1, "c": 9}, {"a": 1.0 / 3, "b": 1.0 / 3, "c": 2.5 / 3}),  # ties share a rank
        ({"a": 0, "b": 100}, {"a": 0.25, "b": 0.75}),
        ({}, {}),
    ],
)
def test_authority_percentiles_use_midranks(
    counts: dict[str, int], expected: dict[str, float]
) -> None:
    result = authority_percentiles(counts)
    assert result.keys() == expected.keys()
    for note_id, value in expected.items():
        assert result[note_id] == pytest.approx(value)


# ---- scores for a whole run -----------------------------------------------------------------


def source(n: int, tier: str, **meta: object) -> NewSource:
    return NewSource(
        url=f"https://example.org/p{n}",
        final_url=f"https://example.org/p{n}",
        canonical_url=f"https://example.org/p{n}",
        doi=None,
        title=f"Page {n}",
        content_type="text/html",
        via="local",
        body=f"Body {n}",
        pages=(),
        word_count=2,
        source_tier=tier,
        derivative_of=None,
        minhash=None,
        links=(),
        meta=SourceMeta(**meta),  # type: ignore[arg-type]
    )


def test_scores_for_a_run(tmp_path: Path) -> None:
    strategies = load_strategies(make_settings().config_dir)
    vault = Vault(tmp_path / "udr.sqlite", "run-a")
    vault.add_source_note(source(1, "ground_truth", cited_by_count=10))
    vault.add_source_note(source(2, "institutional", cited_by_count=20))
    vault.add_source_note(source(3, "commentary"))  # no citation count: no authority component
    vault.add_source_note(source(4, "ground_truth", cited_by_count=30, is_retracted=True))
    note = vault.get_note("n0001")
    assert note is not None
    vault.save_extraction(note.note_id, "s", [], dropped=0, failed=False)
    vault.add_analysis_note(note.note_id, "Analysis", "body")  # analysis notes are not scored
    scores = quality_scores(vault, strategies)
    assert set(scores) == {"n0001", "n0002", "n0003", "n0004"}
    # n0001: tier 1.0, authority percentile 1/6 among {10, 20, 30}
    assert scores["n0001"] == pytest.approx((0.35 * 1.0 + 0.25 * (0.5 / 3)) / 0.60)
    assert scores["n0002"] == pytest.approx((0.35 * 0.85 + 0.25 * (1.5 / 3)) / 0.60)
    assert scores["n0003"] == pytest.approx(0.4)
    assert scores["n0004"] == pytest.approx(0.05)


def test_an_unknown_tier_name_scores_as_unknown(tmp_path: Path) -> None:
    strategies = load_strategies(make_settings().config_dir)
    vault = Vault(tmp_path / "udr.sqlite", "run-a")
    vault.add_source_note(source(1, "mystery-tier"))
    assert quality_scores(vault, strategies)["n0001"] == pytest.approx(
        strategies.tier_weights["unknown"]
    )


def test_an_empty_run_has_no_scores(tmp_path: Path) -> None:
    strategies = load_strategies(make_settings().config_dir)
    assert quality_scores(Vault(tmp_path / "udr.sqlite", "run-a"), strategies) == {}
