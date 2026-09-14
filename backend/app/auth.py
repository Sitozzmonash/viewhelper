"""Authentication for the relay WebSocket.

The first message on a socket must be::

    {"type": "auth", "payload": {"role": "pc"|"mobile", "room": "<room>", "token": "<secret>"}}

The token is compared against ``RELAY_SECRET`` with :func:`hmac.compare_digest`
(constant time). Tokens are never logged.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("relay.auth")

VALID_ROLES = ("pc", "mobile")
MAX_ROOM_LEN = 128


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    role: str | None = None
    room: str | None = None
    error: str | None = None


def _fail(error: str) -> AuthResult:
    # Log the reason only - never the token, room or full payload.
    logger.info("auth rejected: %s", error)
    return AuthResult(ok=False, error=error)


def validate_auth(message: dict[str, Any], relay_secret: str) -> AuthResult:
    """Validate an ``auth`` message. Returns an :class:`AuthResult`."""
    if not isinstance(message, dict) or message.get("type") != "auth":
        return _fail("first_message_not_auth")

    payload = message.get("payload")
    if not isinstance(payload, dict):
        return _fail("missing_payload")

    role = payload.get("role")
    if role not in VALID_ROLES:
        return _fail("invalid_role")

    room = payload.get("room")
    if not isinstance(room, str) or not room.strip() or len(room) > MAX_ROOM_LEN:
        return _fail("invalid_room")

    token = payload.get("token")
    if not isinstance(token, str):
        return _fail("auth_failed")

    if not hmac.compare_digest(token.encode("utf-8"), relay_secret.encode("utf-8")):
        return _fail("auth_failed")

    return AuthResult(ok=True, role=role, room=room)
