"""FastAPI application factory + uvicorn entrypoint for the ViewHelper relay.

Run locally with either::

    uvicorn app.main:app --host 0.0.0.0 --port 8000
    python -m app.main

The relay only does auth, presence, routing and pub/sub. It never stores chat
history, screenshots or answers.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings, get_settings
from .presence import PresenceTracker
from .pubsub import PubSub, create_pubsub
from .websocket import ws_endpoint

logger = logging.getLogger("relay.main")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.warn_if_insecure()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pubsub: PubSub = create_pubsub(settings)
        presence = PresenceTracker(pubsub, settings)
        app.state.pubsub = pubsub
        app.state.presence = presence
        presence.start()
        logger.info("relay started (presence timeout=%.1fs)", settings.presence_timeout_sec)
        try:
            yield
        finally:
            logger.info("relay shutting down")
            await presence.stop()
            await pubsub.aclose()

    app = FastAPI(title="ViewHelper Relay", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings

    # Personal tool: CORS fully open.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.websocket("/ws")
    async def websocket_route(websocket: WebSocket) -> None:
        await ws_endpoint(websocket)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
