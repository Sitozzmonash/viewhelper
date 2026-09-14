"""Global screenshot hotkey (PRD 13.1): ``Ctrl+Shift+Space`` with debounce.

``pynput`` is imported lazily; when it is missing (or the platform blocks global
hooks) the hotkey simply stays unavailable and the mobile ``capture_screen``
command remains the only trigger -- the program keeps running (PRD 27).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

from src.util.log import get_logger

__all__ = ["HotkeyDebouncer", "ScreenshotHotkey", "DEFAULT_HOTKEY"]

DEFAULT_HOTKEY = "<ctrl>+<shift>+<space>"

_logger = get_logger(__name__)


class HotkeyDebouncer:
    """Repeat suppression: ignore retriggers inside a time window.

    Key repeat and double taps otherwise produce several screenshots per press.
    """

    def __init__(self, min_interval_sec: float = 2.0, clock: Callable[[], float] | None = None) -> None:
        self._min_interval = max(0.0, min_interval_sec)
        self._clock = clock or time.monotonic
        self._last = -float("inf")
        self._lock = threading.Lock()
        self._suppressed = 0

    @property
    def min_interval_sec(self) -> float:
        """Configured suppression window in seconds."""
        return self._min_interval

    @property
    def suppressed(self) -> int:
        """How many triggers were ignored so far."""
        return self._suppressed

    def allow(self, now: float | None = None) -> bool:
        """True when a trigger should be honoured (and records it)."""
        with self._lock:
            current = self._clock() if now is None else now
            if current - self._last < self._min_interval:
                self._suppressed += 1
                return False
            self._last = current
            return True

    def reset(self) -> None:
        """Forget the previous trigger time."""
        with self._lock:
            self._last = -float("inf")


class ScreenshotHotkey:
    """Global hotkey listener that fires a callback (from a pynput thread)."""

    def __init__(
        self,
        *,
        hotkey: str = DEFAULT_HOTKEY,
        debounce_sec: float = 2.0,
        on_trigger: Callable[[], None],
    ) -> None:
        self._hotkey = hotkey or DEFAULT_HOTKEY
        self._on_trigger = on_trigger
        self._debouncer = HotkeyDebouncer(debounce_sec)
        self._listener: Any | None = None
        self._lock = threading.Lock()
        self._triggers = 0
        self._logger = get_logger("screenshot.hotkey")

    # -- introspection --------------------------------------------------
    @property
    def hotkey(self) -> str:
        """Configured hotkey combination."""
        return self._hotkey

    @property
    def available(self) -> bool:
        """True while the global listener is running."""
        with self._lock:
            return self._listener is not None

    @property
    def triggers(self) -> int:
        """Number of accepted triggers (diagnostics)."""
        return self._triggers

    @property
    def debouncer(self) -> HotkeyDebouncer:
        """Underlying debouncer (tests / diagnostics)."""
        return self._debouncer

    # -- lifecycle ------------------------------------------------------
    def start(self) -> bool:
        """Start listening; returns False when pynput is unavailable."""
        with self._lock:
            if self._listener is not None:
                return True
        try:
            from pynput import keyboard  # noqa: PLC0415 - lazy by design
        except Exception as exc:  # noqa: BLE001 - missing/blocked pynput is not fatal
            self._logger.warning(
                "global hotkey unavailable (%s: %s); use the mobile capture_screen command instead",
                type(exc).__name__,
                exc,
            )
            return False
        try:
            listener = keyboard.GlobalHotKeys({self._hotkey: self._fire})
            listener.daemon = True
            listener.start()
        except Exception as exc:  # noqa: BLE001 - hook installation can fail
            self._logger.warning("cannot register hotkey %r: %s", self._hotkey, exc)
            return False
        with self._lock:
            self._listener = listener
        self._logger.info(
            "listening for %s (debounce %.1fs)", self._hotkey, self._debouncer.min_interval_sec
        )
        return True

    def stop(self) -> None:
        """Stop the listener (idempotent, never raises)."""
        with self._lock:
            listener, self._listener = self._listener, None
        if listener is None:
            return
        try:
            listener.stop()
        except Exception as exc:  # noqa: BLE001 - best effort
            self._logger.debug("hotkey listener stop failed: %s", exc)
        self._logger.info("hotkey listener stopped (%d trigger(s))", self._triggers)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Supervisor-friendly coroutine: listen until *stop_event* is set."""
        started = await asyncio.to_thread(self.start)
        if not started:
            await stop_event.wait()
            return
        try:
            await stop_event.wait()
        finally:
            await asyncio.to_thread(self.stop)

    # -- callback (pynput thread) ---------------------------------------
    def _fire(self) -> None:
        if not self._debouncer.allow():
            self._logger.debug("hotkey repeat suppressed (%d total)", self._debouncer.suppressed)
            return
        self._triggers += 1
        try:
            self._on_trigger()
        except Exception as exc:  # noqa: BLE001 - never raise into the hook thread
            self._logger.exception("hotkey trigger handler failed: %s", exc)
