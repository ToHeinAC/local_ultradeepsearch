"""Source tiers and per-domain hints from `config/source_strategies.toml` (PRD §3.3)."""

import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

Tier = Literal["ground_truth", "institutional", "practitioner", "commentary", "unknown"]
DomainName = Literal["tech_standards", "regulation_de_eu", "science_medicine", "business_markets"]


class HostRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    suffix: str
    tier: Tier


class DomainStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scholarly_first: bool
    authoritative: tuple[str, ...]
    preferred: tuple[str, ...]
    include_domains: tuple[str, ...]


class SourceStrategies(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tier_weights: dict[Tier, float]
    host_rules: tuple[HostRule, ...] = ()
    domains: dict[DomainName, DomainStrategy] = {}

    def rules(self) -> tuple[HostRule, ...]:
        """Explicit rules plus every domain's authoritative hosts as ground_truth."""
        authoritative = (
            HostRule(suffix=host, tier="ground_truth")
            for domain in self.domains.values()
            for host in domain.authoritative
        )
        return (*self.host_rules, *authoritative)


def load_strategies(config_dir: Path) -> SourceStrategies:
    raw = tomllib.loads((config_dir / "source_strategies.toml").read_text(encoding="utf-8"))
    return SourceStrategies.model_validate(raw)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").rstrip(".").lower()


def tier_for(
    url: str,
    strategies: SourceStrategies,
    *,
    has_doi: bool = False,
    scholarly: bool = False,
) -> Tier:
    """The longest matching host rule wins; else a DOI or scholarly origin; else unknown."""
    host = _host(url)
    matches = [
        rule
        for rule in strategies.rules()
        if host == rule.suffix.lower() or host.endswith(f".{rule.suffix.lower()}")
    ]
    if matches:
        return max(matches, key=lambda rule: len(rule.suffix)).tier
    return "institutional" if has_doi or scholarly else "unknown"
