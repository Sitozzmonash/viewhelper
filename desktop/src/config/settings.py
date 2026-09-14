"""``config.yaml`` schema, loader and persisting store.

The store is the single place that reads/writes the YAML file. Mobile
``settings_update`` payloads are merged into it, persisted atomically and pushed
to subscribers (e.g. the ASR pipelines pick up new hotwords immediately).

API keys never live here -- they belong to :mod:`src.config.env`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from src.config.paths import DESKTOP_DIR, config_path, example_config_path
from src.util.files import atomic_write_text, ensure_dir
from src.util.log import get_logger

__all__ = [
    "AppConfig",
    "ConfigStore",
    "PromptsConfig",
    "ConversationConfig",
    "AsrConfig",
    "AudioConfig",
    "ScreenshotConfig",
    "StorageConfig",
    "TransportConfig",
    "WIRE_SETTINGS_KEYS",
    "DEFAULT_CONVERSATION_PROMPT",
    "DEFAULT_SCREENSHOT_PROMPT",
    "deep_merge",
]

_logger = get_logger(__name__)

DEFAULT_CONVERSATION_PROMPT = (
    "你是一个实时对话助手。\n"
    "根据当前对话和上下文，直接给出用户现在最适合说出的回答。\n"
    "回答简洁、自然，不要解释过程。\n"
)
DEFAULT_SCREENSHOT_PROMPT = (
    "你是一个截图分析助手。\n"
    "分析截图内容并直接回答最重要的问题。\n"
    "如果截图中包含题目，直接给答案并简要解释。\n"
)

# Keys of shared/protocol events.schema.json#$defs/settings.
WIRE_SETTINGS_KEYS: tuple[str, ...] = (
    "conversation_prompt",
    "screenshot_prompt",
    "context_messages",
    "hotwords",
    "show_partial",
)
# Extra (non-secret) keys accepted from the mobile client.
_EXTRA_WIRE_KEYS: tuple[str, ...] = ("input_device", "loopback_device")


class PromptsConfig(BaseModel):
    """Two strictly independent prompts (PRD 8.2, AC-14)."""

    conversation: str = DEFAULT_CONVERSATION_PROMPT
    screenshot: str = DEFAULT_SCREENSHOT_PROMPT


class ConversationConfig(BaseModel):
    """Conversation context window."""

    context_messages: int = Field(default=10, ge=1, le=50)


class AsrModelsConfig(BaseModel):
    """FunASR model identifiers."""

    vad: str = "fsmn-vad"
    streaming: str = "paraformer-zh-streaming"
    offline: str = "paraformer-zh"
    punctuation: str = "ct-punc"
    device: str = "cpu"
    disable_update: bool = True
    # Trailing silence FSMN-VAD waits before closing an utterance (FunASR
    # default: 800). Lower = faster finals, higher risk of cutting pauses.
    max_end_silence_ms: int = Field(default=800, ge=100, le=5000)


class EnergyVadConfig(BaseModel):
    """Fallback VAD used when funasr/torch are missing."""

    threshold_rms: float = Field(default=300.0, ge=0)
    min_speech_ms: int = Field(default=250, ge=0)
    end_silence_ms: int = Field(default=700, ge=50)


class AsrConfig(BaseModel):
    """ASR pipeline tuning."""

    enabled: bool = True
    hotwords: str = ""
    show_partial: bool = True
    partial_interval_ms: int = Field(default=300, ge=0)
    max_utterance_sec: float = Field(default=20.0, gt=0)
    preroll_ms: int = Field(default=320, ge=0)
    models: AsrModelsConfig = Field(default_factory=AsrModelsConfig)
    energy_vad: EnergyVadConfig = Field(default_factory=EnergyVadConfig)


class AudioConfig(BaseModel):
    """Device selection and framing (PRD 6.1)."""

    sample_rate: int = Field(default=16000, gt=0)
    block_ms: int = Field(default=100, ge=10, le=2000)
    queue_size: int = Field(default=200, ge=4)
    input_device: str | int | None = None
    loopback_device: str | int | None = None


class PreviewConfig(BaseModel):
    """Compressed preview sent to the mobile client (PRD 13.3)."""

    max_width: int = Field(default=1280, ge=64)
    quality: int = Field(default=75, ge=10, le=100)
    format: Literal["jpeg", "webp", "png"] = "jpeg"
    target_bytes: int = Field(default=500_000, ge=10_000)


class VisionImageConfig(BaseModel):
    """Image handed to the vision model (kept in memory only)."""

    max_width: int = Field(default=1568, ge=64)
    quality: int = Field(default=85, ge=10, le=100)


class ScreenshotConfig(BaseModel):
    """Screenshot capture settings (PRD 13)."""

    hotkey: str = "<ctrl>+<shift>+<space>"
    debounce_sec: float = Field(default=2.0, ge=0)
    monitor_index: int = Field(default=0, ge=0)
    dir: str = "data/screenshots"
    preview: PreviewConfig = Field(default_factory=PreviewConfig)
    vision: VisionImageConfig = Field(default_factory=VisionImageConfig)


class StorageConfig(BaseModel):
    """SQLite locations and history defaults (PRD 19)."""

    database: str = "data/viewhelper.sqlite3"
    history_limit: int = Field(default=100, ge=1, le=1000)


class ReconnectConfig(BaseModel):
    """Exponential backoff 1,2,4,8,16,30,30... (PRD 21)."""

    initial_sec: float = Field(default=1.0, gt=0)
    factor: float = Field(default=2.0, ge=1.0)
    max_sec: float = Field(default=30.0, gt=0)
    jitter_sec: float = Field(default=0.0, ge=0)


class TransportConfig(BaseModel):
    """Relay transport tuning."""

    heartbeat_interval_sec: float = Field(default=5.0, gt=0)
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)
    send_queue_size: int = Field(default=256, ge=1)


class LoggingConfig(BaseModel):
    """Log level for the desktop process."""

    level: str = "INFO"


class AppConfig(BaseModel):
    """Root of ``config.yaml``."""

    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    # Fixed personal background injected into BOTH conversation and screenshot
    # requests. Kept local only: never sent to the mobile client or the relay.
    resume_context: str = ""
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    screenshot: ScreenshotConfig = Field(default_factory=ScreenshotConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    transport: TransportConfig = Field(default_factory=TransportConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    # -- wire mapping ---------------------------------------------------
    def to_wire_settings(self) -> dict[str, Any]:
        """Settings payload for ``settings_updated`` (never contains secrets)."""
        return {
            "conversation_prompt": self.prompts.conversation,
            "screenshot_prompt": self.prompts.screenshot,
            "context_messages": self.conversation.context_messages,
            "hotwords": self.asr.hotwords,
            "show_partial": self.asr.show_partial,
            "input_device": self.audio.input_device,
            "loopback_device": self.audio.loopback_device,
        }


def deep_merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge *patch* onto *base* (``None`` values are ignored)."""
    merged = dict(base)
    for key, value in patch.items():
        if value is None:
            continue
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def wire_settings_to_patch(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a protocol ``settings`` payload into nested config paths."""
    patch: dict[str, Any] = {}
    prompts: dict[str, Any] = {}
    if isinstance(payload.get("conversation_prompt"), str):
        prompts["conversation"] = payload["conversation_prompt"]
    if isinstance(payload.get("screenshot_prompt"), str):
        prompts["screenshot"] = payload["screenshot_prompt"]
    if prompts:
        patch["prompts"] = prompts

    conversation: dict[str, Any] = {}
    if isinstance(payload.get("context_messages"), int):
        conversation["context_messages"] = int(payload["context_messages"])
    if conversation:
        patch["conversation"] = conversation

    asr: dict[str, Any] = {}
    if isinstance(payload.get("hotwords"), str):
        asr["hotwords"] = payload["hotwords"]
    if isinstance(payload.get("show_partial"), bool):
        asr["show_partial"] = payload["show_partial"]
    if asr:
        patch["asr"] = asr

    audio: dict[str, Any] = {}
    for key in ("input_device", "loopback_device"):
        if key in payload and payload[key] is not None:
            audio[key] = payload[key]
    if audio:
        patch["audio"] = audio

    return patch


class ConfigStore:
    """Loads, caches, mutates and persists :class:`AppConfig`."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        base_dir: Path | None = None,
        bootstrap_from_example: bool = True,
    ) -> None:
        self._base_dir = base_dir or DESKTOP_DIR
        self._path = Path(path) if path else config_path(self._base_dir)
        self._lock = threading.RLock()
        self._listeners: list[Callable[[AppConfig], None]] = []
        self._bootstrap_from_example = bootstrap_from_example
        self._config: AppConfig = AppConfig()
        self._config = self._load_sync()

    # -- access ---------------------------------------------------------
    @property
    def path(self) -> Path:
        """File backing this store."""
        return self._path

    @property
    def config(self) -> AppConfig:
        """Current cached configuration."""
        with self._lock:
            return self._config

    def snapshot(self) -> AppConfig:
        """Deep copy of the current configuration (safe to hand to threads)."""
        with self._lock:
            return self._config.model_copy(deep=True)

    def subscribe(self, listener: Callable[[AppConfig], None]) -> Callable[[], None]:
        """Register *listener*; returns an unsubscribe callable."""
        with self._lock:
            self._listeners.append(listener)

        def _unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsubscribe

    # -- load / save ----------------------------------------------------
    def reload(self) -> AppConfig:
        """Re-read the YAML file from disk."""
        with self._lock:
            self._config = self._load_sync()
            self._notify_locked()
            return self._config

    def _load_sync(self) -> AppConfig:
        if not self._path.is_file():
            if self._bootstrap_from_example and not self._bootstrap_from_template():
                _logger.warning("no %s found, using built-in defaults", self._path)
                return AppConfig()
        raw_text = ""
        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            _logger.error("cannot read %s (%s), using built-in defaults", self._path, exc)
            return AppConfig()
        try:
            data = yaml.safe_load(raw_text) or {}
        except yaml.YAMLError as exc:
            _logger.error("invalid YAML in %s (%s), using built-in defaults", self._path, exc)
            return AppConfig()
        if not isinstance(data, dict):
            _logger.error("%s must contain a mapping, using built-in defaults", self._path)
            return AppConfig()
        try:
            config = AppConfig.model_validate(data)
        except ValidationError as exc:
            _logger.error(
                "invalid config %s (%d problem(s)), falling back to defaults",
                self._path,
                exc.error_count(),
            )
            return AppConfig()
        _logger.info("loaded config %s", self._path)
        return config

    def _bootstrap_from_template(self) -> bool:
        """Create ``config.yaml`` from ``config.example.yaml`` on first run."""
        template = example_config_path(self._base_dir)
        if not template.is_file():
            return False
        try:
            ensure_dir(self._path.parent)
            atomic_write_text(self._path, template.read_text(encoding="utf-8"))
        except OSError as exc:
            _logger.warning("could not create %s from template: %s", self._path, exc)
            return False
        _logger.info("created %s from %s", self._path, template.name)
        return True

    def save(self, config: AppConfig | None = None) -> Path:
        """Persist the configuration as YAML (atomic replace)."""
        with self._lock:
            if config is not None:
                self._config = config
            payload = self._config.model_dump(mode="json")
            text = (
                "# Managed by viewhelper-desktop. Editable while the app is stopped;\n"
                "# prompts/hotwords/context are also editable from the mobile settings page.\n"
                + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=1000)
            )
            ensure_dir(self._path.parent)
            atomic_write_text(self._path, text)
            _logger.info("saved config %s", self._path)
            return self._path

    # -- mutation -------------------------------------------------------
    def update(self, patch: Mapping[str, Any], *, persist: bool = True) -> AppConfig:
        """Merge a nested patch, persist it and notify listeners.

        Raises :class:`pydantic.ValidationError` when the patch produces an
        invalid configuration (the previous config stays in effect).
        """
        with self._lock:
            current = self._config.model_dump(mode="python")
            merged = deep_merge(current, dict(patch))
            candidate = AppConfig.model_validate(merged)
            self._config = candidate
            if persist:
                self.save(candidate)
            self._notify_locked()
            return candidate

    def update_from_wire(self, payload: Mapping[str, Any], *, persist: bool = True) -> AppConfig:
        """Apply a protocol ``settings_update`` payload."""
        return self.update(wire_settings_to_patch(payload), persist=persist)

    def _notify_locked(self) -> None:
        config = self._config
        for listener in list(self._listeners):
            try:
                listener(config)
            except Exception as exc:  # noqa: BLE001 - listener bugs must not break config
                _logger.exception("config listener failed: %s", exc)
