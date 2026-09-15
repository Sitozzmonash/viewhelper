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

#: ``LLM_PRIMARY`` tokens that keep the MODEL_* block first (the default).
_PRIMARY_MAIN_TOKENS = frozenset({"", "model", "main", "primary", "deepseek", "1"})
#: ``LLM_PRIMARY`` tokens that promote the BK_MODEL_* block to first.
_PRIMARY_BACKUP_TOKENS = frozenset({"bk", "backup", "fallback", "secondary", "kimi", "2"})


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
    #: Max seconds to wait for the next streamed event (incl. the first token)
    #: before treating the provider as stalled. Per-provider on purpose: a fast
    #: primary should fail quickly and hand off, a slow backup needs more room.
    stall_timeout_sec: float = Field(default=30.0, gt=0)
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
    #: Optional backup endpoints. When set, the LLM runner automatically retries
    #: a request on the fallback provider if the primary errors or stalls.
    llm_fallback: ProviderCredentials = Field(default_factory=ProviderCredentials)
    vision_fallback: ProviderCredentials = Field(default_factory=ProviderCredentials)
    relay: RelayCredentials = Field(default_factory=RelayCredentials)
    log_level: str = "INFO"
    loaded_files: tuple[Path, ...] = ()

    def provider_for(self, mode: str) -> ProviderCredentials:
        """Return the credentials for ``conversation`` or ``screenshot`` mode."""
        return self.vision if mode == "screenshot" else self.llm

    def fallback_for(self, mode: str) -> ProviderCredentials | None:
        """Return the backup credentials for a mode, or ``None`` when unset.

        A fallback identical to the primary (or not configured) is suppressed so
        the runner never retries the same dead endpoint twice.
        """
        primary = self.provider_for(mode)
        backup = self.vision_fallback if mode == "screenshot" else self.llm_fallback
        if not backup.is_configured:
            return None
        if (backup.base_url, backup.model, backup.api_key) == (
            primary.base_url,
            primary.model,
            primary.api_key,
        ):
            return None
        return backup


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
    # Backup providers (BK_MODEL_* is the legacy alias, like MODEL_* for LLM_*).
    bk_llm_api_key: str = ""
    bk_llm_base_url: str = ""
    bk_llm_model: str = ""
    bk_model_api_key: str = ""
    bk_model_base_url: str = ""
    bk_model_name: str = ""
    bk_vision_api_key: str = ""
    bk_vision_base_url: str = ""
    bk_vision_model: str = ""
    relay_url: str = ""
    relay_device_id: str = "my-pc"
    relay_room: str = "my-pc"
    relay_secret: str = ""
    log_level: str = "INFO"

    #: Which provider block is tried FIRST for conversation answers. Accepts the
    #: structural tokens ``model``/``main``/``primary`` (the MODEL_* block, the
    #: default) or ``bk``/``backup``/``fallback`` (the BK_MODEL_* block); any other
    #: value is matched against the backup model name (e.g. ``kimi``), so the user
    #: can pick a provider by name. Only the conversation LLM swaps -- vision is
    #: pinned to whichever VISION_* names (DeepSeek cannot see images).
    llm_primary: str = ""

    llm_timeout_sec: float = 180.0
    llm_connect_timeout_sec: float = 15.0
    llm_stall_timeout_sec: float = 30.0
    llm_max_tokens: int | None = None
    llm_temperature: float | None = None
    vision_timeout_sec: float = 180.0
    vision_connect_timeout_sec: float = 15.0
    vision_stall_timeout_sec: float = 30.0
    vision_max_tokens: int | None = None
    vision_temperature: float | None = None
    bk_llm_timeout_sec: float = 180.0
    bk_llm_connect_timeout_sec: float = 15.0
    bk_llm_stall_timeout_sec: float = 30.0
    bk_vision_timeout_sec: float = 180.0
    bk_vision_connect_timeout_sec: float = 15.0
    bk_vision_stall_timeout_sec: float = 30.0

    def _first(self, *values: str) -> str:
        for value in values:
            cleaned = (value or "").strip()
            if cleaned:
                return cleaned
        return ""

    def _backup_is_primary(self, backup_model: str) -> bool:
        """True when ``LLM_PRIMARY`` selects the backup block as the primary.

        Structural tokens (``bk``/``backup``/``model``/``main``...) are honoured
        directly; any other value is treated as a model-name hint and matched
        against the backup model (so ``LLM_PRIMARY=kimi`` picks the kimi block).
        """
        pref = (self.llm_primary or "").strip().lower()
        if pref in _PRIMARY_MAIN_TOKENS:
            return False
        if pref in _PRIMARY_BACKUP_TOKENS:
            return True
        backup = (backup_model or "").strip().lower()
        return bool(pref) and bool(backup) and pref in backup

    def to_config(self, loaded_files: tuple[Path, ...]) -> EnvConfig:
        """Map the flat env onto :class:`EnvConfig` applying every fallback."""
        llm = ProviderCredentials(
            api_key=self._first(self.llm_api_key, self.model_api_key),
            base_url=self._first(self.llm_base_url, self.model_base_url),
            model=self._first(self.llm_model, self.model_name),
            timeout_sec=self.llm_timeout_sec,
            connect_timeout_sec=self.llm_connect_timeout_sec,
            stall_timeout_sec=self.llm_stall_timeout_sec,
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
            stall_timeout_sec=self.vision_stall_timeout_sec,
            max_tokens=self.vision_max_tokens,
            temperature=self.vision_temperature,
        )
        relay = RelayCredentials(
            url=self._first(self.relay_url),
            device_id=self._first(self.relay_device_id) or "my-pc",
            room=self._first(self.relay_room) or self._first(self.relay_device_id) or "my-pc",
            secret=self.relay_secret.strip(),
        )
        llm_fallback = ProviderCredentials(
            api_key=self._first(self.bk_llm_api_key, self.bk_model_api_key),
            base_url=self._first(self.bk_llm_base_url, self.bk_model_base_url),
            model=self._first(self.bk_llm_model, self.bk_model_name),
            timeout_sec=self.bk_llm_timeout_sec,
            connect_timeout_sec=self.bk_llm_connect_timeout_sec,
            stall_timeout_sec=self.bk_llm_stall_timeout_sec,
        )
        vision_fallback = ProviderCredentials(
            api_key=self._first(self.bk_vision_api_key, llm_fallback.api_key),
            base_url=self._first(self.bk_vision_base_url, llm_fallback.base_url),
            model=self._first(self.bk_vision_model, llm_fallback.model),
            timeout_sec=self.bk_vision_timeout_sec,
            connect_timeout_sec=self.bk_vision_connect_timeout_sec,
            stall_timeout_sec=self.bk_vision_stall_timeout_sec,
        )
        # LLM_PRIMARY lets the user pick which conversation provider is tried
        # first; the other becomes the automatic fallback. Each block keeps its
        # own stall/timeout tuning, so swapping the objects is safe.
        if self._backup_is_primary(llm_fallback.model):
            llm, llm_fallback = llm_fallback, llm
        return EnvConfig(
            llm=llm,
            vision=vision,
            llm_fallback=llm_fallback,
            vision_fallback=vision_fallback,
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
    if config.llm_fallback.is_configured:
        _logger.info("conversation fallback: %s", config.llm_fallback.describe())
    if config.vision_fallback.is_configured:
        _logger.info("vision fallback: %s", config.vision_fallback.describe())
    _logger.info(
        "conversation order: %s -> %s",
        config.llm.model or "none",
        config.llm_fallback.model or "none",
    )
    _logger.info("relay: %s", config.relay.describe())
    return config
