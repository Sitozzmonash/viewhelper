"""Desktop application orchestrator (PRD 24: ``src/main.py``).

Wires everything together and owns the process lifetime::

    logging -> .env -> config.yaml -> SQLite -> services -> relay transport
                                     |
        supervised subsystems: relay, asr(mic), asr(loopback), hotkey, status

Design rules enforced here:

* no blocking work on the event loop (audio/ASR/capture run in threads);
* every long-running subsystem is wrapped in a :class:`~src.util.supervisor.Supervisor`
  so a crash restarts only that subsystem (PRD 27);
* heavy optional dependencies are probed with ``find_spec`` before use, so a
  machine without ``sounddevice`` / ``PyAudioWPatch`` / ``pynput`` / ``mss`` /
  ``funasr`` still runs;
* shutdown is graceful: stop the sources, drain pending finals, cancel the LLM
  stream (which closes the upstream HTTP connection), close the socket, end the
  SQLite session.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from src import __version__
from src.audio.loopback import LoopbackCapture
from src.audio.microphone import MicrophoneCapture
from src.config.env import EnvConfig, RelayCredentials, load_env_config
from src.config.paths import DESKTOP_DIR, resolve_under_data, stop_file_path
from src.config.settings import AppConfig, ConfigStore, TransportConfig
from src.screenshot.hotkey import ScreenshotHotkey
from src.services.conversation_service import ConversationService
from src.services.history_service import HistoryService
from src.services.llm_dispatcher import LlmDispatcher
from src.services.llm_runner import LlmRunner
from src.services.screenshot_service import ScreenshotService
from src.services.settings_service import SettingsService
from src.services.transcription_service import TranscriptionService
from src.storage.sqlite import Database
from src.transport.protocol import EventType
from src.transport.reconnect import BackoffPolicy
from src.transport.websocket import CommandHandler, RelayTransport
from src.util.log import get_logger, setup_logging
from src.util.sentinel import StopFileSentinel
from src.util.supervisor import SubsystemFactory, Supervisor

__all__ = [
    "DesktopApplication",
    "RuntimeOptions",
    "build_handlers",
    "build_transport",
    "dependency_available",
    "install_signal_handlers",
    "cli",
    "main",
    "SHUTDOWN_TIMEOUT_SEC",
    "HARD_EXIT_GRACE_SEC",
]

_logger = get_logger("main")

#: Optional dependencies; when one is missing only its feature is disabled.
_OPTIONAL_DEPENDENCIES: dict[str, str] = {
    "sounddevice": "microphone capture (pip install sounddevice)",
    "pyaudiowpatch": "system loopback capture (pip install PyAudioWPatch)",
    "pynput": "global screenshot hotkey (pip install pynput)",
    "mss": "screen capture (pip install mss)",
    "funasr": "local ASR models (uv sync --extra asr)",
}

_STATUS_INTERVAL_SEC = 60.0

#: Whole graceful shutdown must finish inside this budget (each stage is also
#: wrapped in ``asyncio.wait_for``; see :meth:`DesktopApplication.shutdown`).
SHUTDOWN_TIMEOUT_SEC = 10.0

#: Last-resort hard exit (``os._exit``) when even the executor/loop teardown
#: hangs after the graceful shutdown budget elapsed.
HARD_EXIT_GRACE_SEC = SHUTDOWN_TIMEOUT_SEC + 2.0

_T = TypeVar("_T")


def arm_hard_exit_watchdog(delay_sec: float = HARD_EXIT_GRACE_SEC, code: int = 0) -> threading.Timer:
    """Daemon timer that forces ``os._exit(code)`` if the process is still alive.

    Armed once when shutdown is requested; a normally exiting process takes the
    timer thread down with it, so it only fires when teardown itself hangs
    (e.g. a stuck audio/hotkey thread blocking the executor join).
    """

    def _force_exit() -> None:
        try:
            _logger.error("graceful shutdown exceeded %.0fs; forcing exit %d", delay_sec, code)
            logging.shutdown()
        finally:
            os._exit(code)

    timer = threading.Timer(delay_sec, _force_exit)
    timer.daemon = True
    timer.start()
    return timer


def _require(value: _T | None, what: str) -> _T:
    """Unwrap a component created by :meth:`DesktopApplication.build`."""
    if value is None:
        raise RuntimeError(f"{what} is not initialised; build() must run first")
    return value


def dependency_available(module_name: str) -> bool:
    """Whether *module_name* can be imported (probes without importing it)."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


class RuntimeOptions:
    """Command line options (plain object so tests can build one directly)."""

    __slots__ = (
        "config_path",
        "log_level",
        "base_dir",
        "enable_audio",
        "enable_relay",
        "status_interval_sec",
        "sentinel_interval_sec",
    )

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        log_level: str | None = None,
        base_dir: Path | None = None,
        enable_audio: bool = True,
        enable_relay: bool = True,
        status_interval_sec: float = _STATUS_INTERVAL_SEC,
        sentinel_interval_sec: float = 1.0,
    ) -> None:
        self.config_path = config_path
        self.log_level = log_level
        self.base_dir = base_dir or DESKTOP_DIR
        self.enable_audio = enable_audio
        self.enable_relay = enable_relay
        self.status_interval_sec = status_interval_sec
        self.sentinel_interval_sec = sentinel_interval_sec


def build_handlers(
    dispatcher: LlmDispatcher,
    screenshot: ScreenshotService,
    history: HistoryService,
    settings: SettingsService,
) -> dict[str, CommandHandler]:
    """Map the nine mobile->pc commands onto their service handlers (PRD 23)."""
    return {
        EventType.LLM_REQUEST: dispatcher.handle_llm_request,
        EventType.LLM_CANCEL: dispatcher.handle_llm_cancel,
        EventType.CAPTURE_SCREEN: screenshot.handle_capture_screen,
        EventType.SCREENSHOT_DELETE: screenshot.handle_screenshot_delete,
        EventType.SCREENSHOT_CLEAR: screenshot.handle_screenshot_clear,
        EventType.CONVERSATION_CLEAR: history.handle_conversation_clear,
        EventType.HISTORY_SYNC_REQUEST: history.handle_history_sync_request,
        EventType.SETTINGS_GET: settings.handle_settings_get,
        EventType.SETTINGS_UPDATE: settings.handle_settings_update,
    }


def build_transport(relay: RelayCredentials, cfg: TransportConfig) -> RelayTransport:
    """Create the relay transport from credentials + tuning config."""
    reconnect = cfg.reconnect
    return RelayTransport(
        credentials=relay,
        heartbeat_interval=cfg.heartbeat_interval_sec,
        backoff=BackoffPolicy(
            initial_sec=reconnect.initial_sec,
            factor=reconnect.factor,
            max_sec=reconnect.max_sec,
            jitter_sec=reconnect.jitter_sec,
        ),
        send_queue_size=cfg.send_queue_size,
    )


def install_signal_handlers(callback: Callable[..., None]) -> None:
    """Wire SIGINT/SIGTERM to *callback* on both POSIX and Windows."""
    loop: asyncio.AbstractEventLoop | None = None
    with contextlib.suppress(RuntimeError):
        loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        if loop is not None:
            try:
                loop.add_signal_handler(sig, callback, sig)
                continue
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # Windows proactor loop: fall back to signal.signal
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, lambda *_args: callback())


class DesktopApplication:
    """The whole desktop program."""

    def __init__(self, options: RuntimeOptions | None = None) -> None:
        self.options = options or RuntimeOptions()
        self._stop_event = asyncio.Event()
        self._env: EnvConfig | None = None
        self._config: ConfigStore | None = None
        self._db: Database | None = None
        self._transport: RelayTransport | None = None
        self._runner: LlmRunner | None = None
        self._transcription: TranscriptionService | None = None
        self._conversation: ConversationService | None = None
        self._screenshot: ScreenshotService | None = None
        self._history: HistoryService | None = None
        self._settings: SettingsService | None = None
        self._dispatcher: LlmDispatcher | None = None
        self._hotkey: ScreenshotHotkey | None = None
        self._conversation_hotkey: ScreenshotHotkey | None = None
        self._sentinel: StopFileSentinel | None = None
        self._supervisors: list[Supervisor] = []
        self._unsubscribe_config: Callable[[], None] | None = None
        self._started = False

    # -- introspection --------------------------------------------------
    @property
    def config(self) -> AppConfig | None:
        """Current configuration (``None`` before :meth:`build`)."""
        return self._config.config if self._config else None

    @property
    def env(self) -> EnvConfig | None:
        """Loaded ``.env`` configuration (``None`` before :meth:`build`)."""
        return self._env

    @property
    def database(self) -> Database | None:
        """SQLite handle (``None`` before :meth:`build` and after shutdown)."""
        return self._db

    @property
    def transport(self) -> RelayTransport | None:
        """Relay transport (``None`` before :meth:`build`)."""
        return self._transport

    @property
    def sentinel(self) -> StopFileSentinel | None:
        """Stop-file sentinel (``None`` before :meth:`build`)."""
        return self._sentinel

    @property
    def services(self) -> dict[str, Any]:
        """Built services by name (diagnostics/tests)."""
        return {
            "transcription": self._transcription,
            "conversation": self._conversation,
            "screenshot": self._screenshot,
            "history": self._history,
            "settings": self._settings,
            "llm": self._dispatcher,
            "runner": self._runner,
        }

    @property
    def stopping(self) -> bool:
        """Whether shutdown has been requested."""
        return self._stop_event.is_set()

    def request_stop(self, *_args: object) -> None:
        """Ask the application to shut down (signal handler signature)."""
        if not self._stop_event.is_set():
            _logger.info("shutdown requested")
        self._stop_event.set()

    # -- wiring ---------------------------------------------------------
    def build(self) -> None:
        """Synchronous wiring: logging, ``.env``, ``config.yaml``, SQLite, services."""
        if self._started:
            return
        env = load_env_config(self.options.base_dir)
        setup_logging(self.options.log_level or env.log_level)

        config = ConfigStore(self.options.config_path, base_dir=self.options.base_dir)
        # config.yaml wins unless --log-level was given explicitly.
        setup_logging(self.options.log_level or config.config.logging.level)

        db = Database(
            resolve_under_data(config.config.storage.database, base=self.options.base_dir),
            session_title=f"viewhelper-desktop {__version__} pid={os.getpid()}",
        )
        # The transport exists before the services (the runner needs it); its
        # handler map is registered once they are built.
        transport = build_transport(env.relay, config.config.transport)
        runner = LlmRunner(transport=transport, db=db)
        transcription = TranscriptionService(db=db, config=config, transport=transport)
        conversation = ConversationService(
            db=db, config=config, env=env, runner=runner, transport=transport
        )
        screenshot = ScreenshotService(
            db=db,
            config=config,
            env=env,
            runner=runner,
            transport=transport,
            base_dir=self.options.base_dir,
        )
        history = HistoryService(db=db, config=config, transport=transport)
        settings = SettingsService(config=config, transport=transport)
        dispatcher = LlmDispatcher(
            conversation=conversation, screenshot=screenshot, transport=transport
        )
        transport.register_many(build_handlers(dispatcher, screenshot, history, settings))
        hotkey = ScreenshotHotkey(
            hotkey=config.config.screenshot.hotkey,
            debounce_sec=config.config.screenshot.debounce_sec,
            on_trigger=screenshot.trigger_from_hotkey,
        )
        # Second global hotkey: answer the latest conversation turn (Shift+Ctrl+Enter),
        # the keyboard equivalent of tapping a bubble's ask dot on the phone.
        conversation_hotkey: ScreenshotHotkey | None = None
        if config.config.conversation.hotkey_enabled:
            conversation_hotkey = ScreenshotHotkey(
                hotkey=config.config.conversation.hotkey,
                debounce_sec=config.config.conversation.hotkey_debounce_sec,
                on_trigger=conversation.trigger_from_hotkey,
            )
        # Stop-file sentinel (<base_dir>/runtime/app.stop): management scripts
        # create the file to ask for the same graceful shutdown as SIGTERM.
        # A stale file from a previous run is removed before we start watching.
        sentinel = StopFileSentinel(
            stop_file_path(self.options.base_dir),
            interval_sec=self.options.sentinel_interval_sec,
            on_stop=self.request_stop,
        )
        sentinel.prepare()

        self._env, self._config, self._db = env, config, db
        self._transport, self._runner = transport, runner
        self._transcription, self._conversation = transcription, conversation
        self._screenshot, self._history, self._settings = screenshot, history, settings
        self._dispatcher, self._hotkey = dispatcher, hotkey
        self._conversation_hotkey = conversation_hotkey
        self._sentinel = sentinel
        self._unsubscribe_config = transcription.subscribe_config()
        self._started = True
        self._log_startup_summary(env, config, db)

    def _log_startup_summary(self, env: EnvConfig, config: ConfigStore, db: Database) -> None:
        cfg = config.config
        _logger.info(
            "viewhelper-desktop %s starting (python %s, pid %d)",
            __version__,
            sys.version.split()[0],
            os.getpid(),
        )
        _logger.info("config: %s", config.path)
        _logger.info("database: %s (session %s)", db.path, db.session_id)
        _logger.info(
            "conversation provider: %s",
            env.llm.describe() if env.llm.is_configured else "NOT CONFIGURED",
        )
        _logger.info(
            "vision provider: %s",
            env.vision.describe() if env.vision.is_configured else "NOT CONFIGURED",
        )
        _logger.info(
            "relay: %s",
            env.relay.describe() if env.relay.url else "NOT CONFIGURED (mobile UI unreachable)",
        )
        _logger.info(
            "asr: enabled=%s show_partial=%s hotwords=%r context_messages=%d",
            cfg.asr.enabled,
            cfg.asr.show_partial,
            cfg.asr.hotwords,
            cfg.conversation.context_messages,
        )
        _logger.info(
            "hotkey: %s (debounce %.1fs)", cfg.screenshot.hotkey, cfg.screenshot.debounce_sec
        )
        if cfg.conversation.hotkey_enabled:
            _logger.info(
                "conversation hotkey: %s (debounce %.1fs, context %d)",
                cfg.conversation.hotkey,
                cfg.conversation.hotkey_debounce_sec,
                cfg.conversation.hotkey_context_messages,
            )
        else:
            _logger.info("conversation hotkey: disabled")
        for module, purpose in _OPTIONAL_DEPENDENCIES.items():
            if not dependency_available(module):
                _logger.warning("optional dependency missing: %-14s -> %s", module, purpose)

    # -- subsystems -----------------------------------------------------
    def _supervised_tasks(self) -> set[asyncio.Task[None]]:
        """Create one supervised task per subsystem."""
        cfg = _require(self._config, "config").config
        tasks: set[asyncio.Task[None]] = set()

        if not self.options.enable_relay:
            _logger.info("relay disabled by command line flag")
        elif self._env and self._env.relay.url:
            tasks.add(self._supervise("relay", self._run_transport))
        else:
            _logger.warning("RELAY_URL is not set; running without the mobile connection")

        if not cfg.asr.enabled:
            _logger.info("ASR disabled by configuration")
        elif not self.options.enable_audio:
            _logger.info("audio/ASR disabled by command line flag")
        else:
            tasks.add(self._supervise("asr-mic", self._run_microphone))
            tasks.add(self._supervise("asr-loopback", self._run_loopback))

        tasks.add(self._supervise("hotkey", self._run_hotkey))
        if self._conversation_hotkey is not None:
            tasks.add(self._supervise("conversation-hotkey", self._run_conversation_hotkey))
        tasks.add(self._supervise("status", self._run_status_logger))
        # The sentinel watcher never restarts: it either triggers the shutdown
        # or ends when the stop event was set by another source.
        sentinel = self._sentinel
        if sentinel is not None:
            tasks.add(asyncio.create_task(self._run_sentinel(sentinel), name="sentinel"))
        return tasks

    async def _run_sentinel(self, sentinel: StopFileSentinel) -> None:
        """Poll ``runtime/app.stop`` every ~1s (external stop request)."""
        await sentinel.watch(self._stop_event)

    def _supervise(self, name: str, factory: SubsystemFactory) -> asyncio.Task[None]:
        supervisor = Supervisor(
            name,
            stop_event=self._stop_event,
            backoff=BackoffPolicy(initial_sec=1.0, factor=2.0, max_sec=30.0),
        )
        self._supervisors.append(supervisor)
        return asyncio.create_task(supervisor.run(factory), name=f"supervisor-{name}")

    async def _run_transport(self) -> None:
        await _require(self._transport, "transport").run()

    async def _run_microphone(self) -> None:
        cfg = _require(self._config, "config").config
        transcription = _require(self._transcription, "transcription service")
        if not dependency_available("sounddevice"):
            _logger.warning("sounddevice missing; microphone transcription stays idle")
            await self._stop_event.wait()
            return
        frames = MicrophoneCapture(
            device=cfg.audio.input_device,
            sample_rate=cfg.audio.sample_rate,
            block_ms=cfg.audio.block_ms,
            queue_size=cfg.audio.queue_size,
        )
        pipeline = transcription.create_pipeline(frames, speaker="me", source="mic", config=cfg)
        await pipeline.run()

    async def _run_loopback(self) -> None:
        cfg = _require(self._config, "config").config
        transcription = _require(self._transcription, "transcription service")
        if not dependency_available("pyaudiowpatch"):
            _logger.warning("PyAudioWPatch missing; system audio transcription stays idle")
            await self._stop_event.wait()
            return
        frames = LoopbackCapture(
            device=cfg.audio.loopback_device,
            sample_rate=cfg.audio.sample_rate,
            block_ms=cfg.audio.block_ms,
            queue_size=cfg.audio.queue_size,
        )
        pipeline = transcription.create_pipeline(
            frames, speaker="other", source="system", config=cfg
        )
        await pipeline.run()

    async def _run_hotkey(self) -> None:
        await _require(self._hotkey, "hotkey").run(self._stop_event)

    async def _run_conversation_hotkey(self) -> None:
        await _require(self._conversation_hotkey, "conversation hotkey").run(self._stop_event)

    async def _run_status_logger(self) -> None:
        """Periodic one-line health summary (this is a background process)."""
        interval = max(5.0, self.options.status_interval_sec)
        while not self._stop_event.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            if self._stop_event.is_set():
                return
            _logger.info("status: %s", self.status())

    def status(self) -> dict[str, Any]:
        """Compact runtime summary for logs (no secrets, no transcript text)."""
        info: dict[str, Any] = {
            "relay_connected": bool(self._transport and self._transport.connected),
            "relay": self._transport.stats if self._transport else {},
            "llm": self._runner.stats if self._runner else {},
            "hotkey_triggers": self._hotkey.triggers if self._hotkey else 0,
            "restarts": {item.name: item.restarts for item in self._supervisors},
        }
        if self._transcription is not None:
            summary = self._transcription.stats()
            info["partials"] = summary["partials"]
            info["finals"] = summary["finals"]
            info["pipelines"] = {
                source: {key: stats.get(key) for key in ("vad", "models", "utterances", "finals")}
                for source, stats in summary["pipelines"].items()
            }
        db = self._db
        if db is not None:
            with contextlib.suppress(Exception):
                info["messages"] = db.count_messages()
                info["screenshots"] = db.count_screenshots()
        return info

    # -- lifetime -------------------------------------------------------
    async def run(self) -> int:
        """Run until a signal, the stop file (or :meth:`request_stop`) asks for shutdown."""
        self.build()
        for service in self.services.values():
            if hasattr(service, "bind_loop"):
                service.bind_loop()
        install_signal_handlers(self.request_stop)

        tasks = self._supervised_tasks()
        waiter = asyncio.create_task(self._stop_event.wait(), name="await-stop")
        try:
            await asyncio.wait(tasks | {waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.request_stop()
            # Last-resort guarantee: even if a cleanup stage or the loop/executor
            # teardown hangs, the process exits with code 0 after the budget.
            arm_hard_exit_watchdog()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.shutdown(), timeout=SHUTDOWN_TIMEOUT_SEC)
            waiter.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(waiter, *tasks, return_exceptions=True)
        sentinel = self._sentinel
        if sentinel is not None and sentinel.triggered:
            # The stop file caused this shutdown: consume it right before exit.
            sentinel.cleanup()
        _logger.info("shutdown complete")
        return 0

    async def shutdown(self) -> None:
        """Stop every subsystem in dependency order (idempotent).

        Each stage is individually bounded with :func:`asyncio.wait_for` so one
        hung component (audio thread, hotkey hook, LLM stream, SQLite) can never
        stall the whole shutdown; :meth:`run` additionally bounds the sum.
        """
        self._stop_event.set()

        # 1. Global hotkey listener (pynput thread; stop() joins it).
        hotkey = self._hotkey
        if hotkey is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.to_thread(hotkey.stop), timeout=2.0)
        conversation_hotkey = self._conversation_hotkey
        if conversation_hotkey is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.to_thread(conversation_hotkey.stop), timeout=2.0)

        # 2. ASR pipelines: signal their worker threads to finish, then let
        #    in-flight finals complete. The workers exit within one block-read
        #    timeout (~0.25s); the waits below are the "join with timeout".
        if self._transcription is not None:
            self._transcription.stop_pipelines()

        # 3. Active LLM request task: cancel it (``aclosing`` in the runner
        #    closes the upstream httpx stream) and close cached HTTP clients.
        runner = self._runner
        if runner is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(runner.shutdown(), timeout=6.0)

        # 4. Service tasks: give them a moment, then cancel whatever is left.
        for service in (
            self._screenshot,
            self._conversation,
            self._history,
            self._settings,
            self._dispatcher,
        ):
            if service is None:
                continue
            with contextlib.suppress(Exception):
                await asyncio.wait_for(service.wait_tasks(timeout=1.0), timeout=2.0)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(service.cancel_tasks(timeout=1.0), timeout=2.0)

        # 5. Transcription tasks + frame sources (stop() closes audio streams).
        if self._transcription is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._transcription.shutdown(), timeout=4.0)

        # 6. Relay transport: best-effort drain, then close the socket.
        transport = self._transport
        if transport is not None:
            with contextlib.suppress(Exception):
                await transport.drain(timeout=2.0)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(transport.stop(), timeout=3.0)

        # 7. Config listeners.
        if self._unsubscribe_config is not None:
            with contextlib.suppress(Exception):
                self._unsubscribe_config()
            self._unsubscribe_config = None

        # 8. SQLite: flush (end the session row) and close.
        db = self._db
        if db is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.to_thread(db.end_session), timeout=2.0)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.to_thread(db.close), timeout=2.0)
            self._db = None


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    """Command line interface definition."""
    parser = argparse.ArgumentParser(
        prog="viewhelper-desktop",
        description=(
            "viewhelper PC companion: microphone + system audio transcription, "
            "screenshot Q&A and the relay link to the mobile UI."
        ),
    )
    parser.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    parser.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING / ERROR")
    parser.add_argument("--no-audio", action="store_true", help="do not start capture/ASR")
    parser.add_argument("--no-relay", action="store_true", help="do not connect to the relay")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate .env + config.yaml, print a summary (no secrets) and exit",
    )
    parser.add_argument(
        "--list-audio-devices",
        action="store_true",
        help="list input and loopback devices and exit",
    )
    parser.add_argument("--version", action="version", version=f"viewhelper-desktop {__version__}")
    return parser


async def _run_check(options: RuntimeOptions) -> int:
    """``--check``: build everything, report, close. Returns the exit code."""
    app = DesktopApplication(options)
    try:
        app.build()
    except Exception as exc:  # noqa: BLE001 - reporting the problem is the point
        _logger.error("startup check failed: %s: %s", type(exc).__name__, exc)
        return 1
    _logger.info("startup check OK: %s", app.status())
    await app.shutdown()
    return 0


def _list_audio_devices() -> int:
    """``--list-audio-devices``: print what the audio layer can see."""
    from src.audio.base import describe_devices  # noqa: PLC0415 - CLI only
    from src.audio.loopback import list_loopback_devices  # noqa: PLC0415 - CLI only
    from src.audio.microphone import list_input_devices  # noqa: PLC0415 - CLI only

    for label, lister in (("input", list_input_devices), ("loopback", list_loopback_devices)):
        try:
            devices = lister()
        except Exception as exc:  # noqa: BLE001 - missing library/driver
            print(f"{label} devices: unavailable ({type(exc).__name__}: {exc})")
            continue
        if not devices:
            print(f"{label} devices: none found")
            continue
        print(f"{label} devices:")
        for device in describe_devices(devices):
            print(f"  [{device.get('index')}] {device.get('name')} ({device.get('channels')} ch)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m src.main`` and the console script."""
    args = build_parser().parse_args(argv)
    options = RuntimeOptions(
        config_path=args.config,
        log_level=args.log_level,
        enable_audio=not args.no_audio,
        enable_relay=not args.no_relay,
    )
    if args.list_audio_devices:
        setup_logging(options.log_level or "WARNING")
        return _list_audio_devices()
    if args.check:
        return asyncio.run(_run_check(options))
    try:
        return asyncio.run(DesktopApplication(options).run())
    except KeyboardInterrupt:
        _logger.info("interrupted by user")
        return 130


def cli() -> None:  # pragma: no cover - console-script shim
    """Console script entry point (``viewhelper-desktop``)."""
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    cli()
