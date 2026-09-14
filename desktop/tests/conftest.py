"""Shared test fixtures.

Everything here runs **without** funasr/torch, without audio hardware and
without network access: the transport, the LLM provider and the screen capture
are all replaced by in-process fakes. Fixtures also make sure tests never touch
the real ``desktop/data`` directory or the repository-root ``.env``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from src.config.env import EnvConfig, ProviderCredentials, RelayCredentials
from src.config.settings import ConfigStore
from src.storage.sqlite import Database
from src.transport.protocol import Envelope, build_envelope

#: Environment variables that could leak real credentials into a test run.
_SECRET_ENV_VARS = (
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "MODEL_API_KEY",
    "MODEL_BASE_URL",
    "MODEL_NAME",
    "VISION_API_KEY",
    "VISION_BASE_URL",
    "VISION_MODEL",
    "RELAY_URL",
    "RELAY_SECRET",
    "RELAY_ROOM",
    "RELAY_DEVICE_ID",
    "RELAY_TOKEN",
)


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the data dir at ``tmp_path`` and drop any inherited secrets."""
    monkeypatch.setenv("VIEWHELPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("VIEWHELPER_CONFIG", raising=False)
    for name in _SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "data"


class RecordingTransport:
    """Duck-typed :class:`~src.transport.websocket.RelayTransport` for tests.

    Implements the only two methods services use (``send_event`` /
    ``send_envelope``) plus ``register*`` so it can be handed to anything that
    expects the real transport. Sent envelopes are recorded, never serialised.
    """

    def __init__(self, *, connected: bool = True, device_id: str = "test-pc") -> None:
        self.device_id_value = device_id
        self.connected = connected
        self.sent: list[Envelope] = []
        self.handlers: dict[str, Any] = {}

    # -- RelayTransport surface ----------------------------------------
    @property
    def device_id(self) -> str:
        return self.device_id_value

    def register(self, event_type: str, handler: Any) -> None:
        self.handlers[event_type] = handler

    def register_many(self, handlers: Mapping[str, Any]) -> None:
        self.handlers.update(handlers)

    async def send_event(self, event_type: str, payload: Mapping[str, Any] | None = None) -> bool:
        envelope = build_envelope(event_type, payload, device_id=self.device_id_value)
        return await self.send_envelope(envelope)

    async def send_envelope(self, envelope: Envelope) -> bool:
        if not self.connected:
            return False
        self.sent.append(envelope)
        return True

    # -- assertions -----------------------------------------------------
    @property
    def types(self) -> list[str]:
        return [envelope.type for envelope in self.sent]

    def payloads(self, event_type: str) -> list[dict[str, Any]]:
        return [envelope.payload for envelope in self.sent if envelope.type == event_type]

    def last(self, event_type: str) -> dict[str, Any] | None:
        matches = self.payloads(event_type)
        return matches[-1] if matches else None

    def count(self, event_type: str) -> int:
        return len(self.payloads(event_type))

    def clear(self) -> None:
        self.sent.clear()


@pytest.fixture
def transport() -> RecordingTransport:
    """A connected, recording transport."""
    return RecordingTransport()


@pytest.fixture
def database(tmp_path: Path) -> Database:
    """A throwaway SQLite database."""
    db = Database(tmp_path / "test.sqlite3", session_title="test-session")
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def config_store(tmp_path: Path) -> ConfigStore:
    """Config store backed by ``tmp_path/config.yaml`` (defaults, no bootstrap)."""
    return ConfigStore(
        tmp_path / "config.yaml", base_dir=tmp_path, bootstrap_from_example=False
    )


@pytest.fixture
def env_config() -> EnvConfig:
    """Fully configured (but fake) providers."""
    llm = ProviderCredentials(
        api_key="test-llm-key",
        base_url="https://llm.example.com/v1",
        model="test-chat-model",
        timeout_sec=5.0,
        connect_timeout_sec=2.0,
    )
    vision = ProviderCredentials(
        api_key="test-vision-key",
        base_url="https://vision.example.com/v1",
        model="test-vision-model",
        timeout_sec=5.0,
        connect_timeout_sec=2.0,
    )
    relay = RelayCredentials(
        url="wss://relay.example.com/ws",
        device_id="test-pc",
        room="test-room",
        secret="test-relay-secret",
    )
    return EnvConfig(llm=llm, vision=vision, relay=relay, log_level="WARNING")


async def wait_for(
    predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.005
) -> bool:
    """Poll *predicate* until it is true (used for thread -> loop handoffs)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def settle(*, delay: float = 0.05) -> None:
    """Let pending callbacks/tasks run (thread handoffs need a real tick)."""
    await asyncio.sleep(delay)


def run_coroutine_threadsafe(
    loop: asyncio.AbstractEventLoop, coroutine: Awaitable[Any]
) -> Any:
    """Helper for tests that mimic an ASR worker thread."""
    return asyncio.run_coroutine_threadsafe(coroutine, loop)  # type: ignore[arg-type]
