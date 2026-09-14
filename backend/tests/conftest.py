"""Shared test helpers: app factory with test settings, envelope builders."""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import FastAPI

from app.config import Settings
from app.main import create_app

TEST_SECRET = "test-secret-not-logged"
TEST_ROOM = "room-1"


def make_app(**overrides: Any) -> FastAPI:
    """Create a relay app with test settings (no .env, no Redis)."""
    settings = Settings(
        relay_secret=TEST_SECRET,
        redis_url=None,
        _env_file=None,
        **overrides,
    )
    return create_app(settings)


def auth_message(role: str, room: str = TEST_ROOM, token: str = TEST_SECRET) -> dict[str, Any]:
    """Bare auth message as shown in shared/protocol/README.md."""
    return {"type": "auth", "payload": {"role": role, "room": room, "token": token}}


def envelope(
    type_: str,
    payload: dict[str, Any] | None = None,
    device_id: str = "test-device",
) -> dict[str, Any]:
    """Full protocol envelope (see shared/protocol/events.schema.json)."""
    return {
        "type": type_,
        "event_id": str(uuid.uuid4()),
        "device_id": device_id,
        "timestamp": int(time.time() * 1000),
        "payload": payload or {},
    }
