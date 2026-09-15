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
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

import src.main as main_module
from src.config.paths import runtime_dir, stop_file_path
from src.config.settings import ConfigStore
from src.main import DesktopApplication, RuntimeOptions
from src.services.llm_runner import SubmitResult
from src.transport.protocol import EventType
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


def test_build_parser_close_flag() -> None:
    parser = main_module.build_parser()
    assert parser.parse_args(["--close"]).no_audio is True
    assert parser.parse_args(["--no-audio"]).no_audio is True
    assert parser.parse_args([]).no_audio is False
    assert RuntimeOptions().enable_audio is True


@pytest.mark.parametrize(
    ("enable_audio", "config_enabled"), [(True, True), (False, True), (True, False), (False, False)]
)
async def test_audio_mode_keeps_screenshot_services_and_selects_subsystems(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_config,
    enable_audio: bool,
    config_enabled: bool,
) -> None:
    base = tmp_path / "desktop"
    base.mkdir()
    store = ConfigStore(base_dir=base, bootstrap_from_example=False)
    store.update({"asr": {"enabled": config_enabled}, "interview_notes": {"enabled": False}})
    before = store.path.read_bytes()
    monkeypatch.setattr(main_module, "load_env_config", lambda *_: env_config)
    app = DesktopApplication(RuntimeOptions(base_dir=base, enable_audio=enable_audio))
    selected: set[str] = set()

    def supervise(name, factory):
        selected.add(name)
        return asyncio.create_task(app._stop_event.wait(), name=name)

    monkeypatch.setattr(app, "_supervise", supervise)
    tasks = set()
    try:
        app.build()
        create_pipeline = Mock(side_effect=AssertionError("must not create ASR pipelines"))
        monkeypatch.setattr(app.services["transcription"], "create_pipeline", create_pipeline)
        tasks = app._supervised_tasks()
        expected = {"relay", "hotkey", "status"}
        if enable_audio and config_enabled:
            expected.update({"asr-mic", "asr-loopback", "conversation-hotkey"})
        assert selected == expected
        assert (app._conversation_hotkey is not None) == (enable_audio and config_enabled)
        assert app.status()["pipelines"] == {}
        assert app.status()["partials"] == app.status()["finals"] == 0
        create_pipeline.assert_not_called()
        screenshot = app.services["screenshot"]
        assert app._hotkey is not None
        assert (
            main_module.build_handlers(
                app.services["llm"], screenshot, app.services["history"], app.services["settings"]
            )[EventType.CAPTURE_SCREEN]
            == screenshot.handle_capture_screen
        )
        submit = AsyncMock(return_value=SubmitResult(accepted=True))
        monkeypatch.setattr(app.services["runner"], "submit", submit)
        assert await screenshot.analyze_image(
            screenshot_id="test-shot",
            request_id="test-request",
            image_data_url="data:image/jpeg;base64,QUJD",
        )
        assert submit.await_args.args[0].mode == "screenshot"
        assert app.config.asr.enabled == config_enabled
        assert store.path.read_bytes() == before
    finally:
        await app.shutdown()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_importing_desktop_does_not_load_asr_dependencies() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import src.main; "
            "assert not {'torch', 'funasr', 'sounddevice', 'pyaudiowpatch'} & sys.modules.keys()",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires Windows PowerShell")
@pytest.mark.parametrize("script_name", ["start.ps1", "restart.ps1"])
@pytest.mark.parametrize("flag", ["", "-Close", "--close", "--clos"])
def test_powershell_mode_arguments(script_name: str, flag: str) -> None:
    path = Path(__file__).resolve().parents[1] / script_name
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$null, [ref]$null); "
        '$attributes = ($ast.ParamBlock.Attributes | ForEach-Object { $_.Extent.Text }) -join "`n"; '
        '$probe = [scriptblock]::Create($attributes + "`n" + $ast.ParamBlock.Extent.Text + '
        "'\n$Close.IsPresent -or ($ExtraArgs -contains ''--close'')'); "
        f"& $probe {flag}"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        timeout=30,
    )
    if flag == "--clos":
        assert result.returncode != 0
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == (b"True" if flag else b"False")
