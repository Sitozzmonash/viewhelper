"""Share the four heavy FunASR ``AutoModel`` instances between the pipelines.

Both audio pipelines (microphone + WASAPI loopback) run the same four models
(VAD, streaming, offline, punctuation). Building one ``AutoModel`` per pipeline
duplicates every weight in host RAM *and* in VRAM for no benefit, so pipelines
go through :class:`SharedAsrModels`: one instance per distinct configuration.

Sharing is only safe because a :class:`SharedModel` also carries a lock:
``funasr.AutoModel.generate`` is not thread safe -- it resets and mutates
``self.kwargs`` in place before every call (``_reset_runtime_configs`` +
``deep_update``) -- so two threads running inference on the same instance at
the same time would overwrite each other's ``cache`` / ``is_final`` /
``chunk_size`` and garble recognition. Callers hold the lock for exactly one
``generate`` call.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.util.log import get_logger

__all__ = ["SharedModel", "SharedAsrModels"]

_logger = get_logger(__name__)


@dataclass(slots=True)
class SharedModel:
    """A shared ``AutoModel`` (``None`` when it could not be built) and its lock."""

    model: Any | None
    lock: threading.Lock = field(default_factory=threading.Lock)


class SharedAsrModels:
    """Lazily built ``AutoModel`` instances keyed by model name + load options."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[tuple[Any, ...], SharedModel] = {}

    def acquire(
        self,
        *,
        name: str,
        device: str,
        disable_update: bool,
        extra: dict[str, Any] | None,
        build: Callable[[], Any | None],
    ) -> SharedModel:
        """Return the entry for this configuration, building it on first use.

        A failed build is remembered (``model is None``) so a broken model is
        not retried by every caller.
        """
        key = (
            name,
            device,
            bool(disable_update),
            tuple(sorted((k, str(v)) for k, v in (extra or {}).items())),
        )
        with self._guard:
            entry = self._entries.get(key)
            if entry is not None:
                _logger.debug("reusing shared ASR model %r (device=%s)", name, device)
                return entry
            _logger.info("building shared ASR model %r (device=%s)", name, device)
            entry = SharedModel(model=build())
            self._entries[key] = entry
            return entry
