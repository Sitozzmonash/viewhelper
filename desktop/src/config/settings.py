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

from src.config.paths import DESKTOP_DIR, REPO_ROOT, config_path, example_config_path
from src.util.files import atomic_write_text, ensure_dir
from src.util.log import get_logger

__all__ = [
    "AppConfig",
    "ConfigStore",
    "PromptsConfig",
    "ConversationConfig",
    "ResumeConfig",
    "InterviewNotesConfig",
    "AsrConfig",
    "AudioConfig",
    "ScreenshotConfig",
    "StorageConfig",
    "TransportConfig",
    "WIRE_SETTINGS_KEYS",
    "DEFAULT_CONVERSATION_PROMPT",
    "DEFAULT_SCREENSHOT_PROMPT",
    "DEFAULT_RESUME_HINT",
    "DEFAULT_NOTES_HINT",
    "deep_merge",
]

_logger = get_logger(__name__)

DEFAULT_CONVERSATION_PROMPT = (
    "你是我的实时面试助手。答案会显示在手机屏幕上，我扫一眼就照着念出去。\n\n"
    "先判断【目标消息】（对方最近说的话）是不是需要我回答的面试问题：\n"
    "- 不是（闲聊、陈述、无关内容）：用 1-2 句大白话简短回应，不要硬套要点格式。\n"
    "- 是面试问题：直接输出要点式答案。\n\n"
    "要点式答案的要求：\n"
    "- 用编号列表（1. 2. 3.）逐条列出，让我一眼看出有几个点；\n"
    "- 通常 3-5 个点，简单问题 2-3 个；每个点 1-2 句话；\n"
    "- 每条先用一句大白话把结论说出来，需要再补半句细节；\n"
    "- 每条里的关键词用 **加粗** 标出（手机端会渲染 Markdown，** 会正常加粗）；\n"
    "- 全文 250 字以内，一眼能扫完；\n"
    "- 不要开场白、不要总结段、不要客套话、不要说“作为一个 AI”。\n\n"
    "说话方式是重中之重——写出来的每一句，都要像饭桌上讲给同事听，不像技术博客：\n"
    "- 句子要短，一句话只说一件事，宁可拆成两句；\n"
    "- 禁止书面腔：“首先、其次、此外、综上、值得注意的是、简而言之”这些词一个都不要出现，"
    "换成“第一点、还有、所以、说白了”；\n"
    "- 专业词第一次出现时，用半句话解释成人话，比如“用了 **MCP**，就是给模型外挂工具的标准接口”；\n"
    "- 能打比方就打比方，能举例就举具体例子（带数字、场景或项目名）；\n"
    "- 不要堆名词和形容词：“实现了高并发的异步处理架构”要说成“请求再多也扛得住”，"
    "“显著提升了准确性”要说成“答得准多了”；\n"
    "- 写完心里默念一遍，念着别扭、绕口、像念稿子的句子，就拆短、改直白。\n\n"
    "反例（绝对不要这样写）：通过引入检索增强生成机制并结合重排序策略，系统的回答准确性得到了显著提升。\n"
    "正例（要这样写）：就是先查一遍资料，再挑最相关的几条喂给模型，这样它答得准多了，也不容易瞎编。\n\n"
    "保持准确、切题；简历里没有的经历不要编造。\n"
)
DEFAULT_SCREENSHOT_PROMPT = (
    "你是一个截图分析助手。\n"
    "分析截图内容并直接回答最重要的问题。\n"
    "如果截图中包含题目，直接给答案并简要解释。\n"
)

#: Optional instruction inserted into BOTH system prompts telling the model to use
#: the resume text already injected above it (the ``【resume_context】`` block) --
#: the model has no filesystem, so naming a file like Summary.md would be useless.
#: Toggleable via ``resume.hint_enabled``.
DEFAULT_RESUME_HINT = "在回答涉及我的经历、项目或能力的问题时，请结合上面【resume_context】中的简历内容来组织答案。"

#: Optional instruction inserted into BOTH system prompts telling the model to
#: prefer the interview notes injected above it (the ``【interview_notes】``
#: block) and to combine them with the resume experience. Toggleable via
#: ``interview_notes.hint_enabled``.
DEFAULT_NOTES_HINT = (
    "回答面试问题时，请优先参考上面【interview_notes】中的八股要点，"
    "并结合【resume_context】里的简历经历组织答案。"
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
    """Conversation context window + the PC-side answer hotkey."""

    context_messages: int = Field(default=10, ge=1, le=50)
    #: Global hotkey that answers the LATEST message (same as tapping the ask dot).
    hotkey: str = "<ctrl>+<shift>+<enter>"
    hotkey_enabled: bool = True
    hotkey_debounce_sec: float = Field(default=2.0, ge=0)
    #: How many preceding messages the hotkey answer carries as context. The user
    #: asked for "last + recent N", counting upward from the newest turn.
    hotkey_context_messages: int = Field(default=10, ge=0, le=50)


class ResumeConfig(BaseModel):
    """Toggle for the resume-reference line injected into both prompts."""

    hint_enabled: bool = True
    hint_text: str = DEFAULT_RESUME_HINT


class InterviewNotesConfig(BaseModel):
    """Bulk interview notes injected into both prompts (local file, read-only).

    The content is loaded from *path* on demand: relative paths are resolved
    against the repository root, absolute paths are used as-is. Like
    ``resume_context`` the text never leaves the PC (never in
    ``to_wire_settings()``, never logged).
    """

    enabled: bool = True
    #: Relative paths are resolved against the repository root (``docs/...``).
    path: str = "docs/大厂面试八股.md"
    #: 0 = keep the whole file; >0 = keep the first N characters and append an
    #: omission marker.
    max_chars: int = Field(default=0, ge=0)
    hint_enabled: bool = True
    hint_text: str = DEFAULT_NOTES_HINT


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
    resume: ResumeConfig = Field(default_factory=ResumeConfig)
    # Interview-notes file injected into BOTH requests right after the resume
    # block. Also local only: the file content is never sent to mobile/relay.
    interview_notes: InterviewNotesConfig = Field(default_factory=InterviewNotesConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    screenshot: ScreenshotConfig = Field(default_factory=ScreenshotConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    transport: TransportConfig = Field(default_factory=TransportConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    # -- derived --------------------------------------------------------
    @property
    def resume_hint(self) -> str:
        """Resume-reference line to inject, or ``""`` when the toggle is off."""
        return self.resume.hint_text.strip() if self.resume.hint_enabled else ""

    @property
    def interview_notes_text(self) -> str:
        """Local interview-notes text, or ``""`` when unavailable.

        Never raises: disabled / missing / unreadable / blank files all yield
        ``""`` (the reason is reported once by the startup summary). The text is
        local only and must never appear in logs, wire payloads or tests.
        """
        notes = self.interview_notes
        if not notes.enabled:
            return ""
        raw_path = notes.path.strip()
        if not raw_path:
            return ""
        path = Path(raw_path)
        if not path.is_absolute():
            path = REPO_ROOT / path
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if not text:
            return ""
        if notes.max_chars > 0 and len(text) > notes.max_chars:
            text = text[: notes.max_chars].rstrip() + "\n…（interview_notes 已截断）"
        return text

    @property
    def interview_notes_hint(self) -> str:
        """Notes-reference line to inject, or ``""`` when the toggle is off."""
        return self.interview_notes.hint_text.strip() if self.interview_notes.hint_enabled else ""

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
