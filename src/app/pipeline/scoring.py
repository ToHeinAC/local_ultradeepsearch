"""Source quality (PRD M3 step 8): the original's weighted sum without graph centrality.

    score = (w_tier·tier + w_utility·utility/18 + w_authority·authority) / (sum of present weights)

Only components that are known take part, so a source without a citation count is not punished
for it. A retracted source is capped at a floor. Authority is a percentile among the run's
sources that have a citation count, so scores are computed on demand: they move as sources arrive.
"""

from collections.abc import Mapping

from app.pipeline.strategies import SourceStrategies
from app.store.vault import Vault

WEIGHT_TIER = 0.35
WEIGHT_UTILITY = 0.20
WEIGHT_AUTHORITY = 0.25
UTILITY_MAX = 18.0  # six dimensions scored 0-3 (set in M8)
RETRACTED_CAP = 0.05


def _unit(value: float) -> float:
    return max(0.0, min(1.0, value))


def quality(
    tier_weight: float, utility: float | None, authority: float | None, *, retracted: bool
) -> float:
    """The renormalised weighted sum of the components that are present (all in 0..1)."""
    parts = [(WEIGHT_TIER, tier_weight)]
    if utility is not None:
        parts.append((WEIGHT_UTILITY, _unit(utility / UTILITY_MAX)))
    if authority is not None:
        parts.append((WEIGHT_AUTHORITY, _unit(authority)))
    score = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
    return min(score, RETRACTED_CAP) if retracted else score


def authority_percentiles(counts: Mapping[str, int]) -> dict[str, float]:
    """Midrank percentile of each count: (number lower + half the number equal) / n."""
    values = list(counts.values())
    total = len(values)
    return {
        key: (sum(v < count for v in values) + 0.5 * sum(v == count for v in values)) / total
        for key, count in counts.items()
    }


def quality_scores(vault: Vault, strategies: SourceStrategies) -> dict[str, float]:
    """Quality of every source note of the run, keyed by note id."""
    notes = vault.notes(kind="source")
    cited = {
        n.note_id: int(n.meta["cited_by_count"])
        for n in notes
        if isinstance(n.meta.get("cited_by_count"), int)
    }
    authority = authority_percentiles(cited)
    weights = {str(tier): weight for tier, weight in strategies.tier_weights.items()}
    return {
        n.note_id: quality(
            weights.get(n.source_tier, weights["unknown"]),
            n.utility,
            authority.get(n.note_id),
            retracted=n.meta.get("is_retracted") is True,
        )
        for n in notes
    }
