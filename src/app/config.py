"""Settings from `UDR_*` environment variables and `.env`. No I/O beyond reading those."""

import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    model_config = SettingsConfigDict(env_prefix="UDR_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")

    shared_ollama_url: str = "http://127.0.0.1:11434"
    own_ollama_enabled: bool = True
    own_ollama_port: int = Field(11436, ge=1, le=65535)
    own_ollama_gpu: int = Field(1, ge=0)
    own_ollama_startup_timeout_s: float = Field(30.0, gt=0)
    ollama_binary: str = "ollama"
    ollama_models_dir: Path | None = None

    model_reason: str = Field("qwen3.8-27b:latest", min_length=1)
    model_extract: str = Field("LiquidAI/lfm2.5-1.2b-instruct:latest", min_length=1)
    model_summarize: str = Field("gemma4:e4b", min_length=1)
    model_ocr: str = Field("deepseek-ocr:3b", min_length=1)
    num_ctx_extract: int = Field(8192, ge=1024)
    num_ctx_summarize: int = Field(16384, ge=1024)
    num_ctx_ocr: int = Field(8192, ge=1024)

    llm_timeout_s: float = Field(900.0, gt=0)
    min_free_disk_gb: float = Field(20.0, ge=0)

    @field_validator("shared_ollama_url")
    @classmethod
    def _loopback_only(cls, url: str) -> str:
        if not is_loopback_url(url):
            raise ValueError(f"Ollama URL must be a loopback http(s) URL, got {url!r}")
        return url

    @property
    def own_ollama_url(self) -> str:
        return f"http://127.0.0.1:{self.own_ollama_port}"
