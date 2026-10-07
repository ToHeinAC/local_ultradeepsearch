"""Settings from `UDR_*` environment variables and `.env`. No I/O beyond reading those."""

import ipaddress
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]  # src/app/config.py -> repository root


def is_loopback_url(url: str) -> bool:
    """True for http(s) URLs whose host is `localhost` or a loopback IP (PRD §3.1)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="UDR_", env_file=".env", extra="ignore", populate_by_name=True
    )

    data_dir: Path = Path("data")
    config_dir: Path = REPO_ROOT / "config"
    templates_dir: Path = (
        REPO_ROOT / "templates"
    )  # built-in report templates; uploads: data/templates

    shared_ollama_url: str = "http://127.0.0.1:11434"
    own_ollama_enabled: bool = True
    own_ollama_port: int = Field(default=11436, ge=1, le=65535)
    own_ollama_gpu: int = Field(default=1, ge=0)
    own_ollama_startup_timeout_s: float = Field(default=30.0, gt=0)
    ollama_binary: str = "ollama"
    ollama_models_dir: Path | None = None

    model_reason: str = Field(default="qwen3.8-27b:latest", min_length=1)
    model_extract: str = Field(default="gemma4:e4b", min_length=1)
    model_summarize: str = Field(default="gemma4:e4b", min_length=1)
    model_ocr: str = Field(default="deepseek-ocr:3b", min_length=1)
    num_ctx_extract: int = Field(default=16384, ge=1024)  # = summarize: no reload
    num_ctx_summarize: int = Field(default=16384, ge=1024)
    num_ctx_ocr: int = Field(default=8192, ge=1024)

    api_port: int = Field(default=8541, ge=1, le=65535)  # `udr serve`; it binds to 127.0.0.1 only

    llm_timeout_s: float = Field(default=900.0, gt=0)
    min_free_disk_gb: float = Field(default=20.0, ge=0)

    # Outbound (PRD §3.2, §3.3). Secrets keep their plain names, as listed in the PRD.
    tavily_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("TAVILY_API_KEY", "tavily_api_key")
    )
    openalex_mailto: str | None = Field(
        default=None, validation_alias=AliasChoices("OPENALEX_MAILTO", "openalex_mailto")
    )
    openalex_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("OPENALEX_API_KEY", "openalex_api_key")
    )
    tavily_monthly_limit: int = Field(default=1000, ge=0)
    searxng_url: str | None = None  # our own SearXNG (`deploy/searxng/`); first in the web chain
    internal_domains: Annotated[tuple[str, ...], NoDecode] = ()
    fetch_timeout_s: float = Field(default=30.0, gt=0)
    max_html_mb: float = Field(default=10.0, gt=0)
    max_pdf_mb: float = Field(default=25.0, gt=0)

    @field_validator("shared_ollama_url")
    @classmethod
    def _loopback_only(cls, url: str) -> str:
        if not is_loopback_url(url):
            raise ValueError(f"Ollama URL must be a loopback http(s) URL, got {url!r}")
        return url

    @field_validator("searxng_url")
    @classmethod
    def _searxng_loopback(cls, url: str | None) -> str | None:
        if url and not is_loopback_url(url):
            raise ValueError(f"SearXNG URL must be a loopback http(s) URL, got {url!r}")
        return url or None

    @field_validator("internal_domains", mode="before")
    @classmethod
    def _split_domains(cls, value: Any) -> tuple[str, ...]:
        items = value.split(",") if isinstance(value, str) else list(value or ())
        cleaned = (str(item).strip().strip(".").lower() for item in items)
        return tuple(d for d in cleaned if d)

    @property
    def own_ollama_url(self) -> str:
        return f"http://127.0.0.1:{self.own_ollama_port}"
