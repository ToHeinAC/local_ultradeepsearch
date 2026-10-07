from pathlib import Path

import pytest
from pydantic import ValidationError
from support import make_settings

from app.config import Settings, is_loopback_url


def make(**overrides: object) -> Settings:
    return make_settings(**overrides)


def test_defaults_match_the_prd() -> None:
    s = make()
    assert s.data_dir == Path("data")
    assert s.shared_ollama_url == "http://127.0.0.1:11434"
    assert s.own_ollama_enabled is True
    assert (s.own_ollama_port, s.own_ollama_gpu) == (11436, 1)
    assert s.own_ollama_startup_timeout_s == 30
    assert s.model_reason == "qwen3.8-27b:latest"
    assert s.model_extract == "gemma4:e4b"
    assert s.model_summarize == "gemma4:e4b"
    assert s.model_ocr == "deepseek-ocr:3b"
    assert (s.num_ctx_extract, s.num_ctx_summarize, s.num_ctx_ocr) == (16384, 16384, 8192)
    assert s.llm_timeout_s == 900
    assert s.min_free_disk_gb == 20


def test_own_url_is_derived_from_the_port() -> None:
    assert make(own_ollama_port=11500).own_ollama_url == "http://127.0.0.1:11500"


def test_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UDR_MODEL_REASON", "other:1b")
    monkeypatch.setenv("UDR_OWN_OLLAMA_GPU", "0")
    monkeypatch.setenv("UDR_OWN_OLLAMA_ENABLED", "false")
    monkeypatch.setenv("UDR_DATA_DIR", "/tmp/udr-data")
    s = make()
    assert (s.model_reason, s.own_ollama_gpu, s.own_ollama_enabled) == ("other:1b", 0, False)
    assert s.data_dir == Path("/tmp/udr-data")


def test_env_file_is_read(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("UDR_MODEL_SUMMARIZE=gemma4:e2b\nUNRELATED=1\n")
    s = Settings(_env_file=env)  # pyright: ignore[reportCallIssue]  # synthesized __init__
    assert s.model_summarize == "gemma4:e2b"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "https://127.0.0.2",
        "http://[::1]:11434",
        "http://LOCALHOST:1",
    ],
)
def test_accepts_loopback(url: str) -> None:
    assert is_loopback_url(url)
    assert make(shared_ollama_url=url).shared_ollama_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1:11434",
        "http://192.168.1.5:11434",
        "http://example.com",
        "http://localhost.evil.com",
        "http://127.0.0.1.evil.com",
        "ftp://127.0.0.1",
        "127.0.0.1:11434",
        "http://",
        "",
    ],
)
def test_rejects_non_loopback(url: str) -> None:
    assert not is_loopback_url(url)
    with pytest.raises(ValidationError, match="loopback"):
        make(shared_ollama_url=url)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("own_ollama_port", 0),
        ("own_ollama_port", 70000),
        ("api_port", 0),
        ("api_port", 70000),
        ("own_ollama_gpu", -1),
        ("own_ollama_startup_timeout_s", 0),
        ("llm_timeout_s", 0),
        ("num_ctx_extract", 100),
        ("model_reason", ""),
    ],
)
def test_rejects_out_of_range_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make(**{field: value})


# ---- M2 outbound settings ---------------------------------------------------------------------


def test_outbound_defaults() -> None:
    s = make()
    assert s.tavily_api_key is None
    assert s.openalex_mailto is None
    assert s.openalex_api_key is None
    assert s.tavily_monthly_limit == 1000
    assert s.internal_domains == ()
    assert s.fetch_timeout_s == 30
    assert (s.max_html_mb, s.max_pdf_mb) == (10, 25)


def test_secrets_use_their_plain_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret")
    monkeypatch.setenv("OPENALEX_MAILTO", "me@example.org")
    monkeypatch.setenv("OPENALEX_API_KEY", "oa-secret")
    s = make()
    assert s.tavily_api_key is not None
    assert s.tavily_api_key.get_secret_value() == "tvly-secret"
    assert s.openalex_api_key is not None
    assert s.openalex_api_key.get_secret_value() == "oa-secret"
    assert s.openalex_mailto == "me@example.org"
    assert "tvly-secret" not in repr(s)


def test_secrets_can_be_passed_by_field_name() -> None:
    s = make(tavily_api_key="tvly-x")
    assert s.tavily_api_key is not None
    assert s.tavily_api_key.get_secret_value() == "tvly-x"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("brenk.local,corp.example.com", ("brenk.local", "corp.example.com")),
        (" Brenk.Local , .corp.example. ,, ", ("brenk.local", "corp.example")),
        ("", ()),
    ],
)
def test_internal_domains_are_comma_separated_and_normalised(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: tuple[str, ...]
) -> None:
    monkeypatch.setenv("UDR_INTERNAL_DOMAINS", raw)
    assert make().internal_domains == expected


def test_internal_domains_accept_a_list_in_code() -> None:
    assert make(internal_domains=["A.example"]).internal_domains == ("a.example",)


def test_conftest_scrubs_provider_secrets() -> None:
    import os

    assert not {"TAVILY_API_KEY", "OPENALEX_MAILTO", "OPENALEX_API_KEY"} & set(os.environ)


def test_the_api_port_defaults_to_8541() -> None:
    assert make().api_port == 8541


def test_searxng_is_off_by_default_and_loopback_only() -> None:
    assert make().searxng_url is None
    assert make(searxng_url="http://127.0.0.1:8888").searxng_url == "http://127.0.0.1:8888"
    with pytest.raises(ValidationError, match="loopback"):
        make(searxng_url="http://searx.example.com")
