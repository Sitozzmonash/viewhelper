"""Filesystem helpers: directories, atomic writes and data-url encoding."""

from __future__ import annotations

import base64
import mimetypes
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "ensure_dir",
    "atomic_write_bytes",
    "atomic_write_text",
    "file_to_data_url",
    "bytes_to_data_url",
    "mime_for_path",
    "delete_files",
    "human_bytes",
]


def ensure_dir(path: Path | str) -> Path:
    """Create *path* (and parents) if needed and return it as ``Path``."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    """Write *data* to *path* via a temp file + ``os.replace`` (crash safe)."""
    path = Path(path)
    ensure_dir(path.parent)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as handle_file:
            handle_file.write(data)
            handle_file.flush()
            os.fsync(handle_file.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> Path:
    """Text flavour of :func:`atomic_write_bytes`."""
    return atomic_write_bytes(path, text.encode(encoding))


def mime_for_path(path: Path | str) -> str:
    """Best-effort MIME type for *path* (defaults to ``application/octet-stream``)."""
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def bytes_to_data_url(data: bytes, mime: str) -> str:
    """Encode *data* as an RFC 2397 data URL."""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def file_to_data_url(path: Path | str, mime: str | None = None) -> str | None:
    """Read *path* and return it as a data URL, or ``None`` when unreadable."""
    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except OSError:
        return None
    return bytes_to_data_url(data, mime or mime_for_path(file_path))


def delete_files(paths: Iterable[Path | str]) -> int:
    """Delete every existing file in *paths*; returns how many were removed."""
    removed = 0
    for raw in paths:
        candidate = Path(raw)
        try:
            if candidate.is_file():
                candidate.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def human_bytes(size: int | float | None) -> str:
    """Compact human readable size for logs."""
    if size is None:
        return "n/a"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024.0
    return f"{value:.1f}GB"
