"""Screenshot capture, preview delivery and vision answers (PRD 13).

Flow (identical for the global hotkey and the mobile ``capture_screen``)::

    grab primary monitor (mss, in a thread)
      -> keep the original PNG under data/screenshots (gitignored)
      -> persist metadata in SQLite
      -> screenshot_created {screenshot_id, preview (data url), captured_at}
      -> automatic vision request (mode="screenshot") through the LLM runner

Deleting/clearing removes the SQLite rows **and** the image files. The vision
image is never written to disk: it is re-encoded from the original on demand.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from src.config.env import EnvConfig
from src.config.paths import DESKTOP_DIR, resolve_under_data
from src.config.settings import ConfigStore
from src.llm.vision import VisionPayloadError, build_vision_messages, describe_vision_request
from src.screenshot.capture import (
    CaptureError,
    CaptureOptions,
    CaptureResult,
    PreviewOptions,
    capture_screen,
    image_data_url_from_file,
)
from src.services.base import ServiceBase
from src.services.llm_runner import LlmJob, LlmRunner
from src.storage.models import ScreenshotRecord
from src.storage.sqlite import Database
from src.transport.protocol import Envelope, EventType
from src.util.files import delete_files
from src.util.log import get_logger, redact

__all__ = ["ScreenshotService", "NO_SCREENSHOT", "CaptureFn"]

_logger = get_logger("services.screenshot")

NO_SCREENSHOT = "no screenshot stored on the PC"

#: Injectable capture entry point (tests replace it with a stub).
CaptureFn = Callable[[CaptureOptions], Awaitable[CaptureResult]]

#: Upper bound for the in-memory vision image (never written to disk).
_VISION_TARGET_BYTES = 3_000_000


class ScreenshotService(ServiceBase):
    """Owns the whole screenshot lifecycle."""

    def __init__(
        self,
        *,
        db: Database,
        config: ConfigStore,
        env: EnvConfig,
        runner: LlmRunner,
        transport: Any = None,
        capture_fn: CaptureFn | None = None,
        base_dir: Path | None = None,
    ) -> None:
        super().__init__(transport=transport, logger_name="services.screenshot")
        self._db = db
        self._config = config
        self._env = env
        self._runner = runner
        self._capture_fn: CaptureFn = capture_fn or capture_screen
        self._base_dir = base_dir or DESKTOP_DIR
        self._captures = 0

    # -- introspection --------------------------------------------------
    @property
    def captures(self) -> int:
        """Number of successful captures (diagnostics)."""
        return self._captures

    def capture_options(self) -> CaptureOptions:
        """Translate the current config into capture parameters."""
        cfg = self._config.config.screenshot
        return CaptureOptions(
            directory=resolve_under_data(cfg.dir, base=self._base_dir),
            monitor_index=cfg.monitor_index,
            preview=PreviewOptions(
                max_width=cfg.preview.max_width,
                quality=cfg.preview.quality,
                format=cfg.preview.format,
                target_bytes=cfg.preview.target_bytes,
            ),
            vision=self.vision_options(),
        )

    def vision_options(self) -> PreviewOptions:
        """Encoding parameters for the image handed to the vision model."""
        cfg = self._config.config.screenshot.vision
        return PreviewOptions(
            max_width=cfg.max_width,
            quality=cfg.quality,
            format="jpeg",
            target_bytes=_VISION_TARGET_BYTES,
        )

    # -- triggers -------------------------------------------------------
    def trigger_from_hotkey(self) -> bool:
        """Called from the pynput thread; schedules the capture on the loop."""
        return self.spawn_threadsafe(
            lambda: self.capture_and_analyze("hotkey"), name="capture-hotkey"
        )

    async def handle_capture_screen(self, envelope: Envelope) -> None:
        """Mobile ``capture_screen``: same flow as the hotkey."""
        _logger.debug("capture_screen requested by %s", envelope.device_id or "mobile")
        self.spawn(lambda: self.capture_and_analyze("mobile"), name="capture-mobile")

    # -- capture --------------------------------------------------------
    async def capture_and_analyze(self, trigger: str = "hotkey") -> CaptureResult | None:
        """Capture, persist, notify the mobile client and start the vision answer."""
        options = self.capture_options()
        try:
            result = await self._capture_fn(options)
        except CaptureError as exc:
            await self._capture_failed(trigger, str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 - PRD 27: never kill the app
            _logger.exception("unexpected capture failure (%s)", trigger)
            await self._capture_failed(trigger, f"{type(exc).__name__}: {exc}")
            return None
        self._captures += 1

        try:
            await asyncio.to_thread(
                self._persist, result, trigger
            )
        except Exception as exc:  # noqa: BLE001 - keep the preview flowing even if SQLite fails
            _logger.error("cannot persist screenshot %s: %s", result.screenshot_id, exc)

        await self.emit(
            EventType.SCREENSHOT_CREATED,
            {
                "screenshot_id": result.screenshot_id,
                "preview": result.preview_data_url,
                "captured_at": result.captured_at,
                "width": result.width,
                "height": result.height,
                "trigger": trigger,
            },
        )
        _logger.info("screenshot_created sent (%s, trigger=%s)", result.describe(), trigger)

        await self.analyze_image(
            screenshot_id=result.screenshot_id,
            request_id=f"req-{result.screenshot_id}",
            image_data_url=result.vision_data_url,
        )
        return result

    def _persist(self, result: CaptureResult, trigger: str) -> ScreenshotRecord:
        return self._db.insert_screenshot(
            local_path=result.original_path,
            screenshot_id=result.screenshot_id,
            preview_path=result.preview_path,
            preview_mime=result.preview_mime,
            preview_bytes=result.preview_bytes,
            width=result.width,
            height=result.height,
            model=None,
            trigger=trigger,
            created_at=result.captured_at,
        )

    async def _capture_failed(self, trigger: str, message: str) -> None:
        _logger.error("screen capture failed (trigger=%s): %s", trigger, redact(message))
        await self.emit_error(redact(message) or "screen capture failed", code="capture_failed")

    # -- vision requests ------------------------------------------------
    async def start_screenshot_request(
        self, request_id: str, screenshot_id: str | None = None
    ) -> bool:
        """Answer a screenshot: the stored one, or the latest when unspecified."""
        try:
            record = await asyncio.to_thread(self._load_record, screenshot_id)
        except Exception as exc:  # noqa: BLE001 - storage failure -> llm_error
            _logger.exception("cannot load screenshot for %s", request_id)
            await self._fail(request_id, redact(f"cannot load screenshot: {exc}"))
            return False

        if record is None:
            await self._fail(request_id, NO_SCREENSHOT)
            return False

        try:
            data_url = await image_data_url_from_file(record.local_path, self.vision_options())
        except CaptureError as exc:
            await self._fail(request_id, redact(str(exc)))
            return False
        return await self.analyze_image(
            screenshot_id=record.id, request_id=request_id, image_data_url=data_url
        )

    async def analyze_image(
        self, *, screenshot_id: str, request_id: str, image_data_url: str
    ) -> bool:
        """Build the vision messages and submit them to the runner."""
        cfg = self._config.config
        provider = self._env.provider_for("screenshot")
        try:
            messages = build_vision_messages(
                system_prompt=cfg.prompts.screenshot, image_data_url=image_data_url
            )
        except VisionPayloadError as exc:
            await self._fail(request_id, str(exc))
            return False

        _logger.info(
            "screenshot request %s for %s: %s",
            request_id,
            screenshot_id,
            describe_vision_request(image_data_url, provider.model),
        )
        job = LlmJob(
            request_id=request_id,
            mode="screenshot",
            messages=messages,
            provider=provider,
            target_id=screenshot_id,
        )
        result = await self._runner.submit(job)
        if not result.accepted:
            await self._fail(request_id, result.reason or "request rejected")
            return False
        try:
            await asyncio.to_thread(self._db.set_screenshot_model, screenshot_id, provider.model)
        except Exception as exc:  # noqa: BLE001 - cosmetic metadata only
            _logger.debug("cannot record model for %s: %s", screenshot_id, exc)
        return True

    # -- delete / clear -------------------------------------------------
    async def handle_screenshot_delete(self, envelope: Envelope) -> None:
        """Mobile ``screenshot_delete``: drop the row and the image files."""
        screenshot_id = _clean(envelope.get("screenshot_id"))
        if not screenshot_id:
            await self.emit_error("screenshot_id is required", code="bad_request")
            return
        record = await asyncio.to_thread(self._db.delete_screenshot, screenshot_id)
        if record is None:
            _logger.info("screenshot_delete for unknown id %s", screenshot_id)
            return
        removed = await asyncio.to_thread(delete_files, self._db.screenshot_files([record]))
        _logger.info("deleted screenshot %s (%d file(s) removed)", screenshot_id, removed)

    async def handle_screenshot_clear(self, envelope: Envelope) -> None:
        """Mobile ``screenshot_clear``: wipe rows, answers and files, then ack."""
        del envelope  # payload is empty per protocol
        records = await asyncio.to_thread(self._db.clear_screenshots)
        answers = await asyncio.to_thread(self._db.clear_ai_answers, "screenshot")
        removed = await asyncio.to_thread(delete_files, self._db.screenshot_files(records))
        _logger.info(
            "cleared %d screenshot(s), %d answer(s), %d file(s)", len(records), answers, removed
        )
        await self.emit(EventType.SCREENSHOT_CLEARED, {"count": len(records)})

    # -- helpers --------------------------------------------------------
    def _load_record(self, screenshot_id: str | None) -> ScreenshotRecord | None:
        """Blocking lookup executed in a worker thread."""
        if screenshot_id:
            return self._db.get_screenshot(screenshot_id)
        return self._db.latest_screenshot()

    async def _fail(self, request_id: str, message: str) -> None:
        _logger.warning("screenshot request %s rejected: %s", request_id, message)
        await self.emit(
            EventType.LLM_ERROR, {"request_id": request_id, "message": message or "request failed"}
        )


def _clean(value: Any) -> str:
    """Coerce a wire value into a stripped string."""
    return "" if value is None else str(value).strip()
