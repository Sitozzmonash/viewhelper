"""Relay configuration via pydantic-settings.

Environment variables (case-insensitive):
- RELAY_SECRET: shared secret for client auth. Defaults to "dev-secret" for local
  development only; a loud warning is logged when the default is in use.
- REDIS_URL: when set, the relay uses Redis Pub/Sub + Redis presence keys so that
  multiple instances (e.g. Vercel functions) agree on routing and presence.
- PRESENCE_TIMEOUT_SEC: PC is considered offline after this many seconds without
  a heartbeat (protocol default: 15).
- HEARTBEAT_INTERVAL_SEC: how often the PC client should send heartbeats
  (protocol default: 5). Informational on the relay side.
- PRESENCE_SWEEP_INTERVAL_SEC: how often the presence sweeper runs (~3s).

Secrets are never logged.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("relay.config")

DEV_SECRET_DEFAULT = "dev-secret"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    relay_secret: str = DEV_SECRET_DEFAULT
    redis_url: str | None = None
    presence_timeout_sec: float = 15.0
    heartbeat_interval_sec: float = 5.0
    presence_sweep_interval_sec: float = 3.0
    host: str = "0.0.0.0"
    port: int = 8000

    def warn_if_insecure(self) -> None:
        """Log a loud warning when running with the development default secret.

        Never prints the secret value itself.
        """
        if self.relay_secret == DEV_SECRET_DEFAULT:
            logger.warning(
                "RELAY_SECRET is using the insecure development default ('%s'). "
                "Set a long random RELAY_SECRET environment variable in production!",
                DEV_SECRET_DEFAULT,
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
