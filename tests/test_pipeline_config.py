from pathlib import Path

import pytest
from pydantic import ValidationError
from support import make_settings

from app.pipeline.profiles import (
    load_gate_config,
    load_phase1,
    load_profile,
    load_readability_config,
    load_research_config,
    load_response_formats,
)
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


PHASE1 = """
[phase1]
max_rounds = 5
max_questions_per_round = 5
max_files = 10
max_file_mb = 50
max_total_pages = 500
ocr_min_chars = 50
ocr_dpi = 200
upload_digest_words = 500
pseudo_page_chars = 3000
max_facts_per_part = 25
language_min_chars = 20
language_min_probability = 0.9
"""
FORMATS = """
[response_formats.short]
words = [500, 2000]
citations = [15, 30]
[response_formats.structured]
words = [2000, 5000]
citations = [40, 80]
[response_formats.argumentative]
words = [5000, 10000]
citations = [80, 150]
"""


SECTIONS = """
[research]
coverage_iterations = 3
section_weight_bounds = [0.5, 2.0]
section_min_words = 80
passages_per_note = 2
passage_chars = 1200
max_citations_per_bracket = 3
hunk_max_old_chars = 1200
[readability]
merge_max_chars = 300
paragraph_target_chars = [500, 1000]
break_min_chars = 1500
split_min_chars = 150
added_words_max = 5
connector_words_max = 3
[gate]
fix_rounds = 3
length_tolerance = [0.8, 1.2]
citation_density_min = 9
quote_min_words = 5
retraction_window_chars = 200
language_sample_chars = 1000
"""


def write_profiles(
    tmp_path: Path, text: str, *, rest: str = PHASE1 + FORMATS, sections: str = SECTIONS
) -> Path:
    (tmp_path / "profiles.toml").write_text(text + rest + sections, encoding="utf-8")
    return tmp_path


TIER_BODY = """credit_cap = 1
source_analysis_cap = 1
long_source_words = 1
planned_searches = [8, 20]
adversarial_min = 5
results_per_query = 10
candidate_urls = [20, 40]
deduped_urls = [15, 30]
wave2_urls = 10
wave2_queries_per_item = 3
fetch_waves = 2
sources_min = 10
sources_target = [15, 25]
thin_sources = 1
must_read_notes = [8, 15]
readability_cap = 50
"""
TIERS = f"[light]\n{TIER_BODY}[full]\n{TIER_BODY}"


def test_a_misspelled_profile_key_is_rejected(tmp_path: Path) -> None:
    directory = write_profiles(
        tmp_path, f"[light]\n{TIER_BODY}credit_capp = 2\n[full]\n{TIER_BODY}"
    )
    with pytest.raises(ValidationError, match="credit_capp"):
        load_profile("light", directory)


def test_a_missing_profile_key_is_rejected(tmp_path: Path) -> None:
    directory = write_profiles(tmp_path, TIERS.replace("thin_sources = 1\n", "", 1))
    with pytest.raises(ValidationError, match="thin_sources"):
        load_profile("light", directory)


def test_non_positive_budgets_are_rejected(tmp_path: Path) -> None:
    body = TIER_BODY.replace("credit_cap = 1", "credit_cap = 0")
    directory = write_profiles(tmp_path, f"[light]\n{TIER_BODY}[full]\n{body}")
    with pytest.raises(ValidationError):
        load_profile("full", directory)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("planned_searches = [8, 20]", "planned_searches = [20, 8]"),
        ("must_read_notes = [8, 15]", "must_read_notes = [0, 15]"),
        ("fetch_waves = 2", "fetch_waves = 0"),
    ],
)
def test_bad_tier_numbers_are_rejected(tmp_path: Path, old: str, new: str) -> None:
    light = TIER_BODY.replace(old, new)
    directory = write_profiles(tmp_path, f"[light]\n{light}[full]\n{TIER_BODY}")
    with pytest.raises(ValidationError):
        load_profile("light", directory)


def test_tier_research_numbers_match_the_plan() -> None:
    light = load_profile("light", config_dir())
    full = load_profile("full", config_dir())
    assert (light.planned_searches, light.adversarial_min, light.results_per_query) == (
        (8, 20),
        5,
        10,
    )
    assert (light.candidate_urls, light.deduped_urls, light.wave2_urls) == ((20, 40), (15, 30), 10)
    assert (light.wave2_queries_per_item, light.fetch_waves, light.sources_min) == (3, 2, 10)
    assert (light.sources_target, light.thin_sources, light.must_read_notes) == (
        (15, 25),
        1,
        (8, 15),
    )
    assert light.readability_cap == 50
    assert (full.planned_searches, full.candidate_urls, full.deduped_urls) == (
        (40, 100),
        (80, 120),
        (60, 100),
    )
    assert (full.wave2_urls, full.fetch_waves, full.sources_min, full.sources_target) == (
        40,
        3,
        45,
        (55, 80),
    )
    assert full.must_read_notes == (20, 50)


def test_research_readability_and_gate_numbers_match_the_plan() -> None:
    research = load_research_config(config_dir())
    assert (research.coverage_iterations, research.section_weight_bounds) == (3, (0.5, 2.0))
    assert (research.section_min_words, research.passages_per_note) == (80, 2)
    assert (research.passage_chars, research.max_citations_per_bracket) == (1200, 3)
    assert research.hunk_max_old_chars == 1200
    readability = load_readability_config(config_dir())
    assert (readability.merge_max_chars, readability.paragraph_target_chars) == (300, (500, 1000))
    assert (readability.break_min_chars, readability.split_min_chars) == (1500, 150)
    assert (readability.added_words_max, readability.connector_words_max) == (5, 3)
    gate = load_gate_config(config_dir())
    assert (gate.fix_rounds, gate.length_tolerance, gate.citation_density_min) == (
        3,
        (0.8, 1.2),
        9,
    )
    assert (gate.quote_min_words, gate.retraction_window_chars) == (5, 200)
    assert gate.language_sample_chars == 1000


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("section_weight_bounds = [0.5, 2.0]", "section_weight_bounds = [2.0, 0.5]"),
        ("coverage_iterations = 3", "coverage_iterations = 0"),
        ("length_tolerance = [0.8, 1.2]", "length_tolerance = [1.2, 0.8]"),
        ("paragraph_target_chars = [500, 1000]", "paragraph_target_chars = [1000, 500]"),
        ("fix_rounds = 3\n", ""),
    ],
)
def test_bad_section_numbers_are_rejected(tmp_path: Path, old: str, new: str) -> None:
    directory = write_profiles(tmp_path, TIERS, sections=SECTIONS.replace(old, new))
    with pytest.raises(ValidationError):
        load_research_config(directory)


def test_a_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_profile("light", tmp_path)


def test_phase1_limits_match_the_plan() -> None:
    limits = load_phase1(config_dir())
    assert (limits.max_rounds, limits.max_questions_per_round) == (5, 5)
    assert (limits.max_files, limits.max_file_mb, limits.max_total_pages) == (10, 50, 500)
    assert (limits.ocr_min_chars, limits.ocr_dpi) == (50, 200)
    assert (limits.upload_digest_words, limits.pseudo_page_chars) == (500, 3000)
    assert limits.max_facts_per_part == 25
    assert (limits.language_min_chars, limits.language_min_probability) == (20, 0.9)


def test_response_formats_match_the_prd() -> None:
    formats = load_response_formats(config_dir())
    assert (formats.short.words, formats.short.citations) == ((500, 2000), (15, 30))
    assert (formats.structured.words, formats.structured.citations) == ((2000, 5000), (40, 80))
    assert (formats.argumentative.words, formats.argumentative.citations) == (
        (5000, 10000),
        (80, 150),
    )
    assert formats.named("short") is formats.short
    assert formats.named("argumentative") is formats.argumentative


def test_an_unknown_response_format_name_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown response format 'essay'"):
        load_response_formats(config_dir()).named("essay")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("max_rounds = 5", "max_rounds = 0"),
        ("max_rounds = 5", "max_roundz = 5"),
        ("language_min_probability = 0.9", "language_min_probability = 1.5"),
        ("language_min_probability = 0.9", "language_min_probability = 0"),
        ("ocr_dpi = 200", "ocr_dpi = 30"),
    ],
)
def test_bad_phase1_values_are_rejected(tmp_path: Path, old: str, new: str) -> None:
    directory = write_profiles(tmp_path, TIERS, rest=PHASE1.replace(old, new) + FORMATS)
    with pytest.raises(ValidationError):
        load_phase1(directory)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("words = [500, 2000]", "words = [2000, 500]"),
        ("words = [500, 2000]", "words = [0, 2000]"),
        ("citations = [15, 30]", "citations = [30, 15]"),
        ("words = [500, 2000]", "words = [500]"),
    ],
)
def test_bad_response_format_ranges_are_rejected(tmp_path: Path, old: str, new: str) -> None:
    directory = write_profiles(tmp_path, TIERS, rest=PHASE1 + FORMATS.replace(old, new))
    with pytest.raises(ValidationError):
        load_response_formats(directory)


def test_a_range_may_be_a_single_value(tmp_path: Path) -> None:
    formats = FORMATS.replace("words = [500, 2000]", "words = [800, 800]")
    loaded = load_response_formats(write_profiles(tmp_path, TIERS, rest=PHASE1 + formats))
    assert loaded.short.words == (800, 800)


def test_an_extra_response_format_is_rejected(tmp_path: Path) -> None:
    extra = FORMATS + "[response_formats.essay]\nwords = [1, 2]\ncitations = [1, 2]\n"
    with pytest.raises(ValidationError, match="essay"):
        load_response_formats(write_profiles(tmp_path, TIERS, rest=PHASE1 + extra))


def test_a_profiles_file_without_phase1_or_formats_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="phase1"):
        load_profile("light", write_profiles(tmp_path, TIERS, rest=FORMATS))
    with pytest.raises(ValidationError, match="response_formats"):
        load_profile("light", write_profiles(tmp_path, TIERS, rest=PHASE1))


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
