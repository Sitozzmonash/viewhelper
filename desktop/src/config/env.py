"""``.env`` / environment configuration.

Resolution order (highest priority first):

1. real environment variables;
2. ``desktop/.env``;
3. repository root ``.env`` (the user already keeps legacy names there).

Legacy names ``MODEL_API_KEY`` / ``MODEL_BASE_URL`` / ``MODEL_NAME`` are accepted
as fallbacks for ``LLM_API_KEY`` / ``LLM_BASE_URL`` / ``LLM_MODEL``. Vision
settings fall back to the conversation LLM settings (PRD 8.1).

Values are never logged -- only their presence and length (see
:meth:`ProviderCredentials.describe`).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config.paths import DESKTOP_DIR, env_candidate_paths
from src.util.log import get_logger

__all__ = [
    "ProviderCredentials",
    "RelayCredentials",
    "EnvConfig",
    "prime_environment",
    "load_env_config",
]

_logger = get_logger(__name__)

_LEGACY_ALIASES: dict[str, tuple[str, ...]] = {
    "LLM_API_KEY": ("MODEL_API_KEY",),
    "LLM_BASE_URL": ("MODEL_BASE_URL",),
    "LLM_MODEL": ("MODEL_NAME",),
    "VISION_API_KEY": ("MODEL_API_KEY",),
    "VISION_BASE_URL": ("MODEL_BASE_URL",),
    "VISION_MODEL": ("MODEL_NAME",),
}


def prime_environment(base_dir: Path | None = None) -> list[Path]:
    """Load ``.env`` files into ``os.environ`` without overriding existing values.

    Files are applied highest-priority-first with ``override=False``, which yields
    env vars > desktop/.env > repo root .env. Returns the files actually loaded.
    """
    candidates = env_candidate_paths(base_dir or DESKTOP_DIR)
    loaded: list[Path] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            # override=False keeps the priority order: env > desktop/.env > root .env
            load_dotenv(candidate, override=False)
            loaded.append(candidate)
        except OSError as exc:
            _logger.warning("could not read %s: %s", candidate, exc)
    # Resolve legacy aliases so pydantic-settings sees canonical names.
    for canonical, fallbacks in _LEGACY_ALIASES.items():
        if os.environ.get(canonical):
            continue
        for fallback in fallbacks:
            value = os.environ.get(fallback)
            if value:
                os.environ[canonical] = value
                _logger.debug("using legacy env %s for %s", fallback, canonical)
                break
    return loaded


class ProviderCredentials(BaseModel):
    """OpenAI-compatible provider endpoint credentials."""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    timeout_sec: float = Field(default=180.0, gt=0)
    connect_timeout_sec: float = Field(default=15.0, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = None
    include_usage: bool = True

    @property
    def is_configured(self) -> bool:
        """True when base_url + model + api_key are all present."""
        return bool(self.api_key and self.base_url and self.model)

    @property
    def endpoint(self) -> str:
        """``{base_url}/chat/completions`` (base_url trailing slash tolerant)."""
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def describe(self) -> str:
        """Log-safe description: never contains the key itself."""
        key = f"set(len={len(self.api_key)})" if self.api_key else "MISSING"
        return f"model={self.model or 'MISSING'} base_url={self.base_url or 'MISSING'} api_key={key}"


class RelayCredentials(BaseModel):
    """Cloud WebSocket relay connection settings."""

    url: str = ""
    device_id: str = "my-pc"
    room: str = "my-pc"
    secret: str = ""

    @property
    def is_configured(self) -> bool:
        """True when a relay URL and a secret are present."""
        return bool(self.url and self.secret)

    def describe(self) -> str:
        """Log-safe description (secret length only)."""
        return (
            f"url={self.url or 'MISSING'} device_id={self.device_id} room={self.room} "
            f"secret={'set(len=%d)' % len(self.secret) if self.secret else 'MISSING'}"
        )


class EnvConfig(BaseModel):
    """Aggregated environment configuration."""

    llm: ProviderCredentials = Field(default_factory=ProviderCredentials)
    vision: ProviderCredentials = Field(default_factory=ProviderCredentials)
    relay: RelayCredentials = Field(default_factory=RelayCredentials)
    log_level: str = "INFO"
    loaded_files: tuple[Path, ...] = ()

    def provider_for(self, mode: str) -> ProviderCredentials:
        """Return the credentials for ``conversation`` or ``screenshot`` mode."""
        return self.vision if mode == "screenshot" else self.llm


class _RawEnv(BaseSettings):
    """Flat view over the merged environment (validated by pydantic-settings)."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    model_api_key: str = ""
    model_base_url: str = ""
    model_name: str = ""
    vision_api_key: str = ""
    vision_base_url: str = ""
    vision_model: str = ""
    relay_url: str = ""
    relay_device_id: str = "my-pc"
    relay_room: str = "my-pc"
    relay_secret: str = ""
    log_level: str = "INFO"

    llm_timeout_sec: float = 180.0
    llm_connect_timeout_sec: float = 15.0
    llm_max_tokens: int | None = None
    llm_temperature: float | None = None
    vision_timeout_sec: float = 180.0
    vision_connect_timeout_sec: float = 15.0
    vision_max_tokens: int | None = None
    vision_temperature: float | None = None

    def _first(self, *values: str) -> str:
        for value in values:
            cleaned = (value or "").strip()
            if cleaned:
                return cleaned
        return ""

    def to_config(self, loaded_files: tuple[Path, ...]) -> EnvConfig:
        """Map the flat env onto :class:`EnvConfig` applying every fallback."""
        llm = ProviderCredentials(
            api_key=self._first(self.llm_api_key, self.model_api_key),
            base_url=self._first(self.llm_base_url, self.model_base_url),
            model=self._first(self.llm_model, self.model_name),
            timeout_sec=self.llm_timeout_sec,
            connect_timeout_sec=self.llm_connect_timeout_sec,
            max_tokens=self.llm_max_tokens,
            temperature=self.llm_temperature,
        )
        vision = ProviderCredentials(
            # PRD 8.1: vision falls back to the conversation provider.
            api_key=self._first(self.vision_api_key, llm.api_key),
            base_url=self._first(self.vision_base_url, llm.base_url),
            model=self._first(self.vision_model, llm.model),
            timeout_sec=self.vision_timeout_sec,
            connect_timeout_sec=self.vision_connect_timeout_sec,
            max_tokens=self.vision_max_tokens,
            temperature=self.vision_temperature,
        )
        relay = RelayCredentials(
            url=self._first(self.relay_url),
            device_id=self._first(self.relay_device_id) or "my-pc",
            room=self._first(self.relay_room) or self._first(self.relay_device_id) or "my-pc",
            secret=self.relay_secret.strip(),
        )
        return EnvConfig(
            llm=llm,
            vision=vision,
            relay=relay,
            log_level=self._first(self.log_level) or "INFO",
            loaded_files=loaded_files,
        )


def load_env_config(base_dir: Path | None = None, *, prime: bool = True) -> EnvConfig:
    """Load the environment configuration.

    Never raises for missing values: callers check ``is_configured`` and degrade
    gracefully (the app must keep running without an LLM key, PRD 27).
    """
    loaded = prime_environment(base_dir) if prime else []
    raw = _RawEnv()
    config = raw.to_config(tuple(loaded))
    _logger.info(
        "env loaded from %s",
        ", ".join(str(path) for path in loaded) if loaded else "process environment only",
    )
    _logger.info("conversation provider: %s", config.llm.describe())
    _logger.info("vision provider: %s", config.vision.describe())
    _logger.info("relay: %s", config.relay.describe())
    return config
