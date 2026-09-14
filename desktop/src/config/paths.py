"""Filesystem layout of the desktop app.

Everything writable lives below ``desktop/data`` (gitignored) unless overridden
by ``VIEWHELPER_DATA_DIR``.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "DESKTOP_DIR",
    "REPO_ROOT",
    "desktop_dir",
    "repo_root",
    "data_dir",
    "runtime_dir",
    "stop_file_path",
    "config_path",
    "example_config_path",
    "env_candidate_paths",
    "resolve_under_data",
]

# src/config/paths.py -> src/config -> src -> desktop
DESKTOP_DIR: Path = Path(__file__).resolve().parents[2]
REPO_ROOT: Path = DESKTOP_DIR.parent


def desktop_dir() -> Path:
    """Absolute path of ``desktop/``."""
    return DESKTOP_DIR


def repo_root() -> Path:
    """Absolute path of the repository root (parent of ``desktop/``)."""
    return REPO_ROOT


def data_dir(base: Path | None = None) -> Path:
    """Directory for SQLite database, screenshots and other runtime artefacts."""
    override = os.environ.get("VIEWHELPER_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (base or DESKTOP_DIR) / "data"


def runtime_dir(base: Path | None = None) -> Path:
    """Directory for process-lifecycle artefacts (stop-file sentinel; gitignored)."""
    return (base or DESKTOP_DIR) / "runtime"


def stop_file_path(base: Path | None = None) -> Path:
    """Stop-file sentinel: management scripts create it to request shutdown.

    Creating ``desktop/runtime/app.stop`` triggers the same graceful shutdown
    path as SIGTERM; the app removes a stale file at startup and deletes it
    again right before exiting (exit code 0) when it caused the stop.
    """
    return runtime_dir(base) / "app.stop"


def config_path(base: Path | None = None) -> Path:
    """Active ``config.yaml`` (created from the example on first run)."""
    override = os.environ.get("VIEWHELPER_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    return (base or DESKTOP_DIR) / "config.yaml"


def example_config_path(base: Path | None = None) -> Path:
    """Committed template config."""
    return (base or DESKTOP_DIR) / "config.example.yaml"


def env_candidate_paths(base: Path | None = None) -> list[Path]:
    """``.env`` lookup order, highest priority first: desktop/ then repo root."""
    desktop = base or DESKTOP_DIR
    return [desktop / ".env", desktop.parent / ".env"]


def resolve_under_data(relative: str | Path, *, base: Path | None = None) -> Path:
    """Resolve a config-relative path (``data/...``) against the data root.

    Absolute paths are returned unchanged so users can point the database or the
    screenshot directory at another drive.
    """
    candidate = Path(relative)
    if candidate.is_absolute():
        return candidate
    root = data_dir(base)
    # "data/screenshots" -> <data_dir>/screenshots ; "shots" -> <data_dir>/shots
    parts = candidate.parts
    if parts and parts[0] == "data":
        parts = parts[1:]
    return root.joinpath(*parts) if parts else root
