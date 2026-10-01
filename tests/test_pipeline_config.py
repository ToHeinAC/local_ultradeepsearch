from pathlib import Path

import pytest
from pydantic import ValidationError
from support import make_settings

from app.pipeline.profiles import load_profile
from app.pipeline.strategies import (
    DomainStrategy,
    HostRule,
    SourceStrategies,
    load_strategies,
    tier_for,
)


def config_dir() -> Path:
    return make_settings().config_dir


def test_the_default_config_dir_holds_the_repository_files() -> None:
    directory = config_dir()
    assert (directory / "profiles.toml").is_file()
    assert (directory / "source_strategies.toml").is_file()


def test_config_dir_can_be_overridden(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UDR_CONFIG_DIR", str(tmp_path))
    assert make_settings().config_dir == tmp_path


# ---- profiles -------------------------------------------------------------------------------


def test_profile_numbers_match_the_prd() -> None:
    light = load_profile("light", config_dir())
    full = load_profile("full", config_dir())
    assert (light.credit_cap, light.source_analysis_cap, light.long_source_words) == (60, 6, 5000)
    assert (full.credit_cap, full.source_analysis_cap, full.long_source_words) == (300, 6, 5000)


def test_an_unknown_profile_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown profile 'dissertation'"):
        load_profile("dissertation", config_dir())


def write_profiles(tmp_path: Path, text: str) -> Path:
    (tmp_path / "profiles.toml").write_text(text, encoding="utf-8")
    return tmp_path


def test_a_misspelled_profile_key_is_rejected(tmp_path: Path) -> None:
    body = "credit_cap = 1\nsource_analysis_cap = 1\nlong_source_words = 1\n"
    directory = write_profiles(tmp_path, f"[light]\n{body}credit_capp = 2\n[full]\n{body}")
    with pytest.raises(ValidationError, match="credit_capp"):
        load_profile("light", directory)


def test_non_positive_budgets_are_rejected(tmp_path: Path) -> None:
    body = "source_analysis_cap = 1\nlong_source_words = 1\n"
    directory = write_profiles(
        tmp_path, f"[light]\ncredit_cap = 0\n{body}[full]\ncredit_cap = 1\n{body}"
    )
    with pytest.raises(ValidationError):
        load_profile("full", directory)


def test_a_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_profile("light", tmp_path)


# ---- source strategies ----------------------------------------------------------------------


def test_strategies_load_with_the_prd_tier_weights_and_four_domains() -> None:
    strategies = load_strategies(config_dir())
    assert strategies.tier_weights == {
        "ground_truth": 1.0,
        "institutional": 0.85,
        "practitioner": 0.7,
        "commentary": 0.4,
        "unknown": 0.6,
    }
    assert set(strategies.domains) == {
        "tech_standards",
        "regulation_de_eu",
        "science_medicine",
        "business_markets",
    }
    for domain in strategies.domains.values():
        assert domain.authoritative
        assert domain.include_domains
    assert strategies.domains["science_medicine"].scholarly_first is True
    assert strategies.domains["regulation_de_eu"].scholarly_first is False


@pytest.mark.parametrize(
    ("url", "tier"),
    [
        ("https://www.sec.gov/cgi-bin/browse-edgar", "ground_truth"),
        ("https://www.gesetze-im-internet.de/atg/__7.html", "ground_truth"),
        ("https://eur-lex.europa.eu/eli/reg/2016/679", "ground_truth"),
        ("https://www.bmuv.bund.de/themen", "ground_truth"),
        ("https://www.iso.org/standard/1.html", "ground_truth"),
        ("https://www.din.de/de", "ground_truth"),
        ("https://www.iaea.org/publications", "ground_truth"),
        ("https://en.wikipedia.org/wiki/Reactor", "institutional"),
        ("https://arxiv.org/abs/2401.00001", "institutional"),
        ("https://ocw.mit.edu/x", "institutional"),
        ("https://medium.com/@someone/post", "commentary"),
        ("https://stackoverflow.com/questions/1", "practitioner"),
        ("https://some-random-blog.example/post", "unknown"),
        ("HTTPS://WWW.SEC.GOV:443/x", "ground_truth"),
    ],
)
def test_tier_for_hosts(url: str, tier: str) -> None:
    assert tier_for(url, load_strategies(config_dir())) == tier


@pytest.mark.parametrize(
    "url",
    [
        "https://evilsec.gov.example.com/x",  # the suffix must end the host
        "https://notiso.org/x",
        "https://sec.gov.attacker.net/x",
    ],
)
def test_look_alike_hosts_do_not_inherit_a_tier(url: str) -> None:
    assert tier_for(url, load_strategies(config_dir())) == "unknown"


def test_a_doi_or_scholarly_provider_lifts_an_unknown_host_to_institutional() -> None:
    strategies = load_strategies(config_dir())
    assert tier_for("https://journal.example/a", strategies, has_doi=True) == "institutional"
    assert tier_for("https://journal.example/a", strategies, scholarly=True) == "institutional"
    # but a host rule still wins
    assert tier_for("https://medium.com/x", strategies, has_doi=True) == "commentary"


def test_the_longest_matching_suffix_wins() -> None:
    strategies = SourceStrategies(
        tier_weights={
            "ground_truth": 1,
            "institutional": 1,
            "practitioner": 1,
            "commentary": 1,
            "unknown": 1,
        },
        host_rules=(
            HostRule(suffix="gov", tier="ground_truth"),
            HostRule(suffix="blog.gov", tier="commentary"),
        ),
        domains={
            name: DomainStrategy(
                scholarly_first=False,
                authoritative=("x.example",),
                preferred=(),
                include_domains=("x.example",),
            )
            for name in (
                "tech_standards",
                "regulation_de_eu",
                "science_medicine",
                "business_markets",
            )
        },
    )
    assert tier_for("https://news.blog.gov/a", strategies) == "commentary"
    assert tier_for("https://agency.gov/a", strategies) == "ground_truth"
    assert (
        tier_for("https://x.example/a", strategies) == "ground_truth"
    )  # authoritative lists count


def test_unknown_keys_and_tiers_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "source_strategies.toml").write_text(
        "[tier_weights]\nground_truth = 1\ninstitutional = 1\npractitioner = 1\ncommentary = 1\n"
        'unknown = 1\n[[host_rules]]\nsuffix = "x.org"\ntier = "gold"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_strategies(tmp_path)
