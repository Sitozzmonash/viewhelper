"""WebSocket connection handling for the relay.

Flow:
1. Client connects to ``/ws`` and must send ``auth`` as the first message.
2. On success the relay replies ``auth_ok`` (with current ``pc_online``); on
   failure it sends ``error {code: "auth_failed"}`` and closes with code 4401.
3. Messages are then routed per shared/protocol/README.md:
   - pc -> mobile events are published to the room (fan-out to all mobiles),
   - mobile -> pc events are forwarded to the room's PC client,
   - ``heartbeat`` is consumed by the presence tracker, never forwarded,
   - unknown types are ignored (logged only).

The relay is stateless w.r.t. content: chat history, screenshots and answers
are only ever forwarded, never stored. Tokens/secrets are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import WebSocket
from pydantic import BaseModel, Field, ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .auth import validate_auth
from .presence import PresenceTracker, make_envelope
from .pubsub import PubSub

logger = logging.getLogger("relay.ws")

AUTH_TIMEOUT_SEC = 10.0
AUTH_FAILED_CLOSE_CODE = 4401
BAD_MESSAGE_CLOSE_CODE = 4400
INTERNAL_ERROR_CLOSE_CODE = 1011

# Routing table from shared/protocol/README.md
PC_TO_MOBILE = frozenset(
    {
        "asr_partial",
        "asr_final",
        "screenshot_created",
        "llm_started",
        "llm_chunk",
        "llm_stats",
        "llm_done",
        "llm_cancelled",
        "llm_error",
        "history_sync_response",
        "conversation_cleared",
        "screenshot_cleared",
        "settings_updated",
    }
)

MOBILE_TO_PC = frozenset(
    {
        "llm_request",
        "llm_cancel",
        "capture_screen",
        "screenshot_delete",
        "conversation_clear",
        "screenshot_clear",
        "history_sync_request",
        "settings_get",
        "settings_update",
    }
)

RELAY_TO_MOBILE = frozenset({"presence"})


class Envelope(BaseModel):
    """Protocol envelope; see shared/protocol/events.schema.json."""

    type: str
    event_id: str
    device_id: str
    timestamp: int
    payload: dict[str, Any] = Field(default_factory=dict)


class FirstMessage(BaseModel):
    """Lenient model for the very first message.

    The README shows ``auth`` as a bare ``{type, payload}`` object while the
    JSON schema requires full envelope fields; both forms are accepted here.
    """

    type: str
    event_id: str | None = None
    device_id: str | None = None
    timestamp: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    with contextlib.suppress(Exception):
        await websocket.send_json(make_envelope("error", {"code": code, "message": message}))


async def _close_quietly(websocket: WebSocket, code: int) -> None:
    with contextlib.suppress(Exception):
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=code)


async def _authenticate(websocket: WebSocket, relay_secret: str) -> tuple[str, str] | None:
    """Wait for the first message and validate it as ``auth``.

    Returns ``(role, room)`` on success, or None after replying with an error
    event and closing the socket.
    """
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.info("auth timeout, closing socket")
        await _send_error(websocket, "auth_failed", "authentication timeout")
        await _close_quietly(websocket, AUTH_FAILED_CLOSE_CODE)
        return None
    except (WebSocketDisconnect, RuntimeError):
        return None

    try:
        first = FirstMessage.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError):
        logger.info("first message is not a valid envelope")
        await _send_error(websocket, "auth_failed", "first message must be a valid auth message")
        await _close_quietly(websocket, AUTH_FAILED_CLOSE_CODE)
        return None

    result = validate_auth(first.model_dump(), relay_secret)
    if not result.ok:
        await _send_error(websocket, "auth_failed", "authentication failed")
        await _close_quietly(websocket, AUTH_FAILED_CLOSE_CODE)
        return None

    assert result.role is not None and result.room is not None
    return result.role, result.room


async def _subscriber_loop(
    websocket: WebSocket,
    pubsub: PubSub,
    room: str,
    role: str,
) -> None:
    """Deliver room events to this socket, filtered by role.

    Mobiles receive pc->mobile events plus relay presence; the PC receives
    mobile->pc events. Anything else (including our own publications) is
    dropped. Cancellation and disconnects end the loop quietly.
    """
    allowed = (PC_TO_MOBILE | RELAY_TO_MOBILE) if role == "mobile" else MOBILE_TO_PC
    try:
        async for envelope in pubsub.subscribe(room):
            if envelope.get("type") not in allowed:
                continue
            await websocket.send_json(envelope)
    except asyncio.CancelledError:
        raise
    except WebSocketDisconnect:
        logger.debug("subscriber: client disconnected room=%s role=%s", room, role)
    except Exception:
        # The receive loop owns the socket lifecycle; just log and exit.
        logger.exception("subscriber loop ended room=%s role=%s", room, role)


async def _route_message(
    raw: str,
    websocket: WebSocket,
    pubsub: PubSub,
    presence: PresenceTracker,
    room: str,
    role: str,
) -> None:
    try:
        envelope = Envelope.model_validate_json(raw)
    except ValidationError as exc:
        # Log error kinds/locations only - never message contents (may carry user data).
        details = [(e.get("loc"), e.get("type")) for e in exc.errors()]
        logger.warning("invalid envelope from role=%s: %s", role, details)
        await _send_error(websocket, "invalid_message", "message does not match the protocol envelope")
        return

    event_type = envelope.type
    data = envelope.model_dump()

    if role == "pc":
        if event_type == "heartbeat":
            # Consumed by the relay, never forwarded.
            await presence.pc_heartbeat(room)
        elif event_type in PC_TO_MOBILE:
            await pubsub.publish(room, data)
        else:
            logger.info("ignoring unexpected type from pc: %s", event_type)
    else:  # mobile
        if event_type in MOBILE_TO_PC:
            await pubsub.publish(room, data)
        else:
            logger.info("ignoring unexpected type from mobile: %s", event_type)


async def ws_endpoint(websocket: WebSocket) -> None:
    settings = websocket.app.state.settings
    pubsub: PubSub = websocket.app.state.pubsub
    presence: PresenceTracker = websocket.app.state.presence

    await websocket.accept()

    role: str | None = None
    room: str | None = None
    subscriber: asyncio.Task[None] | None = None
    try:
        authed = await _authenticate(websocket, settings.relay_secret)
        if authed is None:
            return
        role, room = authed

        presence.watch(room)
        if role == "pc":
            await presence.pc_connected(room)
            pc_online = True
        else:
            pc_online = await pubsub.get_pc_online(room)

        await websocket.send_json(
            make_envelope("auth_ok", {"room": room, "role": role, "pc_online": pc_online})
        )
        logger.info("client authenticated role=%s room=%s", role, room)

        subscriber = asyncio.create_task(
            _subscriber_loop(websocket, pubsub, room, role),
            name=f"subscriber-{role}-{room}",
        )

        while True:
            raw = await websocket.receive_text()
            await _route_message(raw, websocket, pubsub, presence, room, role)
    except WebSocketDisconnect:
        logger.info("client disconnected role=%s room=%s", role, room)
    except asyncio.CancelledError:
        logger.info("connection handler cancelled role=%s room=%s", role, room)
        raise
    except Exception:
        # A single broken client must never crash the server.
        logger.exception("connection handler error role=%s room=%s", role, room)
        await _close_quietly(websocket, INTERNAL_ERROR_CLOSE_CODE)
    finally:
        if subscriber is not None:
            subscriber.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await subscriber
        if role is not None and room is not None:
            presence.unwatch(room)
            if role == "pc":
                with contextlib.suppress(Exception):
                    await presence.pc_disconnected(room)
