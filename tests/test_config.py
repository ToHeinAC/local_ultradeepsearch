from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, is_loopback_url


def make(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]


def test_defaults_match_the_prd() -> None:
    s = make()
    assert s.data_dir == Path("data")
    assert s.shared_ollama_url == "http://127.0.0.1:11434"
    assert s.own_ollama_enabled is True
    assert (s.own_ollama_port, s.own_ollama_gpu) == (11436, 1)
    assert s.own_ollama_startup_timeout_s == 30
    assert s.model_reason == "qwen3.8-27b:latest"
    assert s.model_extract == "LiquidAI/lfm2.5-1.2b-instruct:latest"
    assert s.model_summarize == "gemma4:e4b"
    assert s.model_ocr == "deepseek-ocr:3b"
    assert (s.num_ctx_extract, s.num_ctx_summarize, s.num_ctx_ocr) == (8192, 16384, 8192)
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
    s = Settings(_env_file=env)  # pyright: ignore[reportCallIssue]
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
