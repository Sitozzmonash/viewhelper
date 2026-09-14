"""Thin deployment shim for single-project (Vercel) hosting.

The ViewHelper relay is a FastAPI app that lives in ``backend/app``. When the
mobile web frontend is deployed on its own (e.g. a Vercel project rooted at
``frontend/``), serverless Python functions are picked up from this ``api/``
directory. This module simply exposes the existing relay ASGI ``app`` so a
function route such as ``/api/ws`` can serve the same WebSocket endpoint the
standalone backend does.

It performs no logic of its own: it puts the sibling ``backend`` directory on
``sys.path`` and re-exports ``app`` from ``app.main``.

Best-effort note: WebSockets on serverless platforms depend on the runtime
actually supporting long-lived upgrade connections. If the host cannot sustain
WebSockets, run the relay from ``backend/`` directly (``uvicorn app.main:app``)
and point ``NEXT_PUBLIC_RELAY_URL`` at it.

Local layout assumed::

    viewhelper/
      backend/app/main.py   # defines: app = create_app()
      frontend/api/ws.py    # this file
"""

from __future__ import annotations

import sys
from pathlib import Path

# .../frontend/api/ws.py -> parents[0]=api, [1]=frontend, [2]=viewhelper root.
_HERE = Path(__file__).resolve()
_API_DIR = _HERE.parent
_VENDORED = _API_DIR / "app"            # synced copy (scripts/sync-relay.ps1)
_BACKEND = _HERE.parents[2] / "backend"  # local monorepo source

if (_VENDORED / "main.py").exists():
    # Deployed layout (e.g. Vercel): the relay package is vendored next to this
    # file because only frontend/ is uploaded.
    if str(_API_DIR) not in sys.path:
        sys.path.insert(0, str(_API_DIR))
elif str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Re-export the relay ASGI application unchanged.
from app.main import app  # noqa: E402  (import must follow sys.path setup)

__all__ = ["app"]
