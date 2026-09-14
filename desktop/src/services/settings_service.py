"""Settings read/write over the wire (PRD 8.3, 22; ``settings_*`` events).

The mobile client can read and edit the five settings defined by
``shared/protocol/events.schema.json#$defs/settings``. Values are merged into
``config.yaml`` through :class:`~src.config.settings.ConfigStore`, which notifies
subscribers (the ASR pipelines pick up hotwords/``show_partial`` immediately).

API keys are **never** part of these payloads: they only live in ``.env``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from src.config.settings import ConfigStore
from src.services.base import ServiceBase
from src.transport.protocol import Envelope, EventType
from src.util.log import get_logger

__all__ = ["SettingsService"]

_logger = get_logger("services.settings")


class SettingsService(ServiceBase):
    """Serves ``settings_get`` / ``settings_update``.

    Changes are applied through :class:`ConfigStore`, which notifies its own
    subscribers (e.g. the ASR pipelines), so no extra observer hook is needed.
    """

    def __init__(self, *, config: ConfigStore, transport: Any = None) -> None:
        super().__init__(transport=transport, logger_name="services.settings")
        self._config = config

    # -- protocol handlers ---------------------------------------------
    async def handle_settings_get(self, envelope: Envelope) -> None:
        """Reply with the current settings (``settings_updated`` doubles as the ack)."""
        del envelope  # payload is empty per protocol
        await self.push()

    async def handle_settings_update(self, envelope: Envelope) -> None:
        """Merge, persist and acknowledge a settings payload."""
        payload = envelope.payload if isinstance(envelope.payload, Mapping) else {}
        _logger.info("settings_update: %s", sorted(payload))
        try:
            await asyncio.to_thread(self._config.update_from_wire, dict(payload))
        except ValidationError as exc:
            _logger.warning("rejected invalid settings: %d problem(s)", exc.error_count())
            await self.emit_error(
                f"invalid settings: {_first_problem(exc)}", code="invalid_settings"
            )
            await self.push()  # resync the mobile form with what is actually stored
            return
        except OSError as exc:
            _logger.error("cannot persist settings: %s", exc)
            await self.emit_error(f"cannot persist settings: {exc}", code="storage_error")
            await self.push()
            return
        except Exception as exc:  # noqa: BLE001 - never let a bad payload kill the app
            _logger.exception("settings_update failed")
            await self.emit_error(f"cannot apply settings: {exc}", code="internal_error")
            await self.push()
            return

        await self.push()

    # -- outgoing -------------------------------------------------------
    async def push(self) -> bool:
        """Send the current settings (schema ``$defs/settings`` + device hints)."""
        return await self.emit(EventType.SETTINGS_UPDATED, self._config.config.to_wire_settings())

    def snapshot(self) -> dict[str, Any]:
        """Current settings payload (diagnostics/tests)."""
        return self._config.config.to_wire_settings()


def _first_problem(exc: ValidationError) -> str:
    """Compact, user-readable description of the first validation error."""
    errors = exc.errors()
    if not errors:
        return "invalid value"
    problem = errors[0]
    location = ".".join(str(part) for part in problem.get("loc", ())) or "value"
    return f"{location}: {problem.get('msg', 'invalid')}"
