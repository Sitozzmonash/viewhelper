"""Screenshot capture and preview compression (PRD 13).

Flow: grab the primary monitor with ``mss`` -> keep the original PNG locally
under ``desktop/data/screenshots`` (gitignored, never uploaded) -> build a
compressed preview (max width 1280, JPEG/WebP q75, target ~500KB) as a data URL
for the mobile client -> build a higher-quality data URL for the vision model.

``mss`` and ``Pillow`` are imported lazily so importing this module never fails,
and the blocking work is executed via :func:`asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.util.clock import utc_now_ms
from src.util.files import bytes_to_data_url, ensure_dir, human_bytes
from src.util.log import get_logger

__all__ = [
    "CaptureError",
    "PreviewOptions",
    "CaptureOptions",
    "CaptureResult",
    "capture_screen",
    "capture_screen_sync",
    "encode_preview",
    "encode_file_image",
    "image_data_url_from_file",
    "scale_to_width",
    "MIME_BY_FORMAT",
]

_logger = get_logger(__name__)

MIME_BY_FORMAT: dict[str, str] = {
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "png": "image/png",
}
_PIL_FORMAT_BY_FORMAT: dict[str, str] = {"jpeg": "JPEG", "webp": "WEBP", "png": "PNG"}
_QUALITY_STEPS = (0, 10, 20, 30)
_MAX_SHRINK_ROUNDS = 3
_SHRINK_FACTOR = 0.85


class CaptureError(RuntimeError):
    """Screen capture or image encoding failed."""


@dataclass(frozen=True, slots=True)
class PreviewOptions:
    """Compression parameters for one output image."""

    max_width: int = 1280
    quality: int = 75
    format: str = "jpeg"
    target_bytes: int = 500_000

    @property
    def mime(self) -> str:
        """MIME type of the encoded image."""
        return MIME_BY_FORMAT.get(self.format.lower(), "image/jpeg")

    @property
    def pil_format(self) -> str:
        """Pillow format name."""
        return _PIL_FORMAT_BY_FORMAT.get(self.format.lower(), "JPEG")


@dataclass(frozen=True, slots=True)
class CaptureOptions:
    """Everything needed for one capture."""

    directory: Path
    monitor_index: int = 0
    preview: PreviewOptions = PreviewOptions()
    vision: PreviewOptions = PreviewOptions(max_width=1568, quality=85, format="jpeg", target_bytes=3_000_000)
    save_preview_file: bool = True


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """Outcome of a successful capture."""

    screenshot_id: str
    captured_at: int
    original_path: Path
    preview_path: Path | None
    width: int
    height: int
    preview_bytes: int
    preview_mime: str
    preview_data_url: str
    vision_data_url: str
    vision_bytes: int

    def describe(self) -> str:
        """Log-safe summary (never contains base64 data)."""
        return (
            f"id={self.screenshot_id} {self.width}x{self.height} "
            f"original={self.original_path.name} preview={human_bytes(self.preview_bytes)} "
            f"vision={human_bytes(self.vision_bytes)}"
        )


def _import_mss() -> Any:
    try:
        import mss  # noqa: PLC0415 - lazy by design
        import mss.exception  # noqa: PLC0415 - lazy by design
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise CaptureError("mss is not installed; screenshot capture unavailable") from exc
    return mss


def _import_pillow() -> Any:
    try:
        from PIL import Image  # noqa: PLC0415 - lazy by design
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise CaptureError("Pillow is not installed; screenshot capture unavailable") from exc
    return Image


def scale_to_width(image: Any, max_width: int) -> Any:
    """Downscale *image* to *max_width* keeping the aspect ratio."""
    if max_width <= 0 or image.width <= max_width:
        return image
    ratio = max_width / float(image.width)
    new_size = (max_width, max(1, int(round(image.height * ratio))))
    resample = getattr(image, "Resample", None)
    filter_value = getattr(resample, "LANCZOS", None) if resample is not None else None
    if filter_value is None:  # pragma: no cover - very old Pillow
        return image.resize(new_size)
    return image.resize(new_size, filter_value)


def encode_preview(image: Any, options: PreviewOptions) -> tuple[bytes, str]:
    """Encode *image* under the size/target constraints of *options*.

    Strategy: scale to ``max_width``, then step the quality down, then shrink the
    image further, until the payload fits ``target_bytes`` (or the options are
    exhausted). Returns ``(bytes, mime)``.
    """
    fmt = options.format.lower()
    mime = options.mime
    pil_format = options.pil_format
    working = image
    if fmt in ("jpeg", "jpg") and working.mode not in ("RGB", "L"):
        working = working.convert("RGB")

    best: bytes = b""
    for round_index in range(_MAX_SHRINK_ROUNDS + 1):
        if round_index:
            width = max(64, int(working.width * _SHRINK_FACTOR))
            working = scale_to_width(working, width)
        candidate = scale_to_width(working, options.max_width)
        for step in _QUALITY_STEPS:
            quality = max(20, options.quality - step)
            buffer = io.BytesIO()
            save_kwargs: dict[str, Any] = {"format": pil_format}
            if pil_format in ("JPEG", "WEBP"):
                save_kwargs.update(quality=quality, optimize=True, progressive=pil_format == "JPEG")
            candidate.save(buffer, **save_kwargs)
            data = buffer.getvalue()
            best = data
            if len(data) <= options.target_bytes:
                return data, mime
    _logger.warning(
        "preview still %s after compression (target %s)",
        human_bytes(len(best)),
        human_bytes(options.target_bytes),
    )
    return best, mime


async def capture_screen(options: CaptureOptions) -> CaptureResult:
    """Capture the screen without blocking the event loop."""
    return await asyncio.to_thread(capture_screen_sync, options)


def capture_screen_sync(options: CaptureOptions) -> CaptureResult:
    """Blocking capture: grab, save the original, build preview + vision images."""
    mss = _import_mss()
    Image = _import_pillow()

    captured_at = utc_now_ms()
    screenshot_id = f"ss-{captured_at}-{uuid.uuid4().hex[:6]}"
    directory = ensure_dir(options.directory)

    try:
        # A fresh mss instance per call: mss objects are bound to their thread.
        with mss.mss() as sct:
            monitors = list(sct.monitors)
            physical = monitors[1:] if len(monitors) > 1 else monitors
            if not physical:
                raise CaptureError("no monitor reported by mss")
            index = min(max(0, options.monitor_index), len(physical) - 1)
            shot = sct.grab(physical[index])
    except CaptureError:
        raise
    except Exception as exc:  # noqa: BLE001 - driver / permission problems
        raise CaptureError(f"screen grab failed: {exc}") from exc

    try:
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    except Exception as exc:  # noqa: BLE001 - unexpected pixel layout
        raise CaptureError(f"cannot convert screenshot buffer: {exc}") from exc

    original_path = directory / f"{screenshot_id}.png"
    try:
        image.save(original_path, format="PNG", optimize=False, compress_level=1)
    except OSError as exc:
        raise CaptureError(f"cannot save original screenshot: {exc}") from exc

    preview_data, preview_mime = encode_preview(image, options.preview)
    preview_path: Path | None = None
    if options.save_preview_file:
        extension = "jpg" if options.preview.format.lower() in ("jpeg", "jpg") else options.preview.format
        preview_path = ensure_dir(directory / "preview") / f"{screenshot_id}.{extension}"
        try:
            preview_path.write_bytes(preview_data)
        except OSError as exc:
            _logger.warning("cannot persist preview file: %s", exc)
            preview_path = None

    vision_data, _vision_mime = encode_preview(image, options.vision)

    result = CaptureResult(
        screenshot_id=screenshot_id,
        captured_at=captured_at,
        original_path=original_path,
        preview_path=preview_path,
        width=int(image.width),
        height=int(image.height),
        preview_bytes=len(preview_data),
        preview_mime=preview_mime,
        preview_data_url=bytes_to_data_url(preview_data, preview_mime),
        vision_data_url=bytes_to_data_url(vision_data, _vision_mime),
        vision_bytes=len(vision_data),
    )
    _logger.info("screenshot captured: %s", result.describe())
    return result


def encode_file_image(path: Path | str, options: PreviewOptions) -> str:
    """Re-encode an already stored screenshot as a data URL (blocking).

    Used when the mobile client asks to analyse an older screenshot again: the
    original PNG stays on disk, only the vision-sized JPEG is rebuilt in memory.
    """
    Image = _import_pillow()
    file_path = Path(path)
    try:
        with Image.open(file_path) as image:
            image.load()
            data, mime = encode_preview(image, options)
    except FileNotFoundError as exc:
        raise CaptureError(f"screenshot file is missing: {file_path.name}") from exc
    except OSError as exc:
        raise CaptureError(f"cannot read screenshot file: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - corrupt/unreadable image
        raise CaptureError(f"cannot decode screenshot file: {exc}") from exc
    return bytes_to_data_url(data, mime)


async def image_data_url_from_file(path: Path | str, options: PreviewOptions) -> str:
    """Async flavour of :func:`encode_file_image` (never blocks the event loop)."""
    return await asyncio.to_thread(encode_file_image, path, options)
