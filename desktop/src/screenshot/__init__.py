"""Screenshot layer: capture/compression and the global hotkey."""

from __future__ import annotations

from src.screenshot.capture import (
    CaptureError,
    CaptureOptions,
    CaptureResult,
    PreviewOptions,
    capture_screen,
    capture_screen_sync,
    encode_preview,
)
from src.screenshot.hotkey import DEFAULT_HOTKEY, HotkeyDebouncer, ScreenshotHotkey

__all__ = [
    "CaptureError",
    "CaptureOptions",
    "CaptureResult",
    "PreviewOptions",
    "capture_screen",
    "capture_screen_sync",
    "encode_preview",
    "DEFAULT_HOTKEY",
    "HotkeyDebouncer",
    "ScreenshotHotkey",
]
