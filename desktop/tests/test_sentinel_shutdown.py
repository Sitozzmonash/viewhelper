"""Stop-file sentinel: unit tests plus a full graceful-shutdown integration run.

Contract under test (used later by the PowerShell management scripts):

* ``<base_dir>/runtime/app.stop`` created -> same graceful shutdown as SIGTERM;
* stale stop file is deleted at startup;
* the file is deleted again right before exit when it triggered the stop;
* exit code is 0 in that case;
* ``runtime/`` is created automatically.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import src.main as main_module
from src.config.paths import runtime_dir, stop_file_path
from src.main import DesktopApplication, RuntimeOptions
from src.util.sentinel import StopFileSentinel

# ----------------------------------------------------------------- unit tests


def test_prepare_creates_runtime_dir_and_removes_stale_stop_file(tmp_path: Path) -> None:
    sentinel = StopFileSentinel(stop_file_path(tmp_path))
    assert not runtime_dir(tmp_path).exists()

    # stale file from a previous run
    runtime_dir(tmp_path).mkdir(parents=True)
    stop_file_path(tmp_path).write_text("stop", encoding="utf-8")

    assert sentinel.prepare() is True  # a stale file was removed
    assert runtime_dir(tmp_path).is_dir()
    assert not stop_file_path(tmp_path).exists()
    assert sentinel.prepare() is False  # idempotent, nothing left to remove


async def test_watch_sets_stop_event_when_the_file_appears(tmp_path: Path) -> None:
    sentinel = StopFileSentinel(stop_file_path(tmp_path), interval_sec=0.05)
    sentinel.prepare()
    stop_event = asyncio.Event()

    task = asyncio.create_task(sentinel.watch(stop_event))
    await asyncio.sleep(0.12)  # a few polls with no file
    assert not stop_event.is_set() and not sentinel.triggered

    stop_file_path(tmp_path).write_text("", encoding="utf-8")
    triggered = await asyncio.wait_for(task, timeout=2.0)

    assert triggered is True
    assert sentinel.triggered is True
    assert stop_event.is_set()


async def test_watch_ends_without_trigger_when_stopped_by_other_means(tmp_path: Path) -> None:
    sentinel = StopFileSentinel(stop_file_path(tmp_path), interval_sec=0.05)
    sentinel.prepare()
    stop_event = asyncio.Event()

    task = asyncio.create_task(sentinel.watch(stop_event))
    await asyncio.sleep(0.05)
    stop_event.set()  # e.g. SIGTERM arrived first
    triggered = await asyncio.wait_for(task, timeout=2.0)

    assert triggered is False
    assert sentinel.triggered is False


async def test_watch_on_stop_callback_runs_once(tmp_path: Path) -> None:
    calls: list[bool] = []
    sentinel = StopFileSentinel(
        stop_file_path(tmp_path), interval_sec=0.05, on_stop=lambda: calls.append(True)
    )
    sentinel.prepare()
    stop_file_path(tmp_path).touch()

    assert await asyncio.wait_for(sentinel.watch(asyncio.Event()), timeout=2.0) is True
    assert calls == [True]


def test_cleanup_only_consumes_the_file_when_it_triggered(tmp_path: Path) -> None:
    sentinel = StopFileSentinel(stop_file_path(tmp_path))
    sentinel.prepare()
    stop_file_path(tmp_path).touch()

    # Not triggered: the file belongs to whoever created it, leave it alone.
    assert sentinel.cleanup() is False
    assert stop_file_path(tmp_path).exists()

    # Simulate a sentinel-triggered stop, then cleanup right before exit.
    sentinel._triggered = True
    assert sentinel.cleanup() is True
    assert not stop_file_path(tmp_path).exists()
    assert sentinel.triggered is False


# ------------------------------------------------------------- integration


async def test_app_starts_headless_and_stops_via_stop_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full DesktopApplication run: degraded subsystems, stop file -> exit 0."""
    # No signal handler side effects and no os._exit watchdog inside pytest.
    monkeypatch.setattr(main_module, "install_signal_handlers", lambda callback: None)
    monkeypatch.setattr(main_module, "arm_hard_exit_watchdog", lambda *a, **k: None)

    base = tmp_path / "desktop"
    base.mkdir()
    options = RuntimeOptions(
        base_dir=base,
        enable_audio=False,
        enable_relay=False,
        sentinel_interval_sec=0.05,
        status_interval_sec=3600.0,
    )

    # A stale stop file must not prevent the fresh run.
    runtime_dir(base).mkdir(parents=True)
    stop_file_path(base).write_text("stale", encoding="utf-8")

    app = DesktopApplication(options)
    run_task = asyncio.create_task(app.run())
    await asyncio.sleep(0.5)

    assert app.stopping is False
    assert not stop_file_path(base).exists()  # stale file removed at startup
    assert app.database is not None  # built and running

    # External stop request.
    stop_file_path(base).write_text("", encoding="utf-8")
    exit_code = await asyncio.wait_for(run_task, timeout=10.0)

    assert exit_code == 0
    assert app.stopping is True
    assert app.database is None  # SQLite flushed and closed
    assert not stop_file_path(base).exists()  # consumed right before exit
    assert runtime_dir(base).is_dir()


async def test_app_request_stop_shuts_down_without_stop_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signal path (request_stop) uses the same graceful shutdown."""
    monkeypatch.setattr(main_module, "install_signal_handlers", lambda callback: None)
    monkeypatch.setattr(main_module, "arm_hard_exit_watchdog", lambda *a, **k: None)

    base = tmp_path / "desktop"
    base.mkdir()
    app = DesktopApplication(
        RuntimeOptions(
            base_dir=base,
            enable_audio=False,
            enable_relay=False,
            sentinel_interval_sec=0.05,
            status_interval_sec=3600.0,
        )
    )
    run_task = asyncio.create_task(app.run())
    await asyncio.sleep(0.5)

    app.request_stop()  # what SIGINT/SIGTERM handlers call
    exit_code = await asyncio.wait_for(run_task, timeout=10.0)

    assert exit_code == 0
    assert not stop_file_path(base).exists()  # nothing to consume
    assert app.sentinel is not None and app.sentinel.triggered is False
