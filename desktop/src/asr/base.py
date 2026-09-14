"""Shared plumbing for FunASR based components.

Every heavy import (``funasr`` -> ``torch`` -> ``modelscope``) happens inside
:func:`load_auto_model`, i.e. at first use and never at import time. A missing or
broken model therefore disables that single component instead of crashing the
process (PRD 27).
"""

from __future__ import annotations

import importlib.util
from typing import Any

from src.util.log import get_logger

__all__ = [
    "funasr_importable",
    "load_auto_model",
    "extract_text",
    "FunasrComponent",
]

_logger = get_logger(__name__)


def funasr_importable() -> bool:
    """Whether ``funasr`` can be imported at all (does not import it)."""
    try:
        return importlib.util.find_spec("funasr") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken installs
        return False


def load_auto_model(
    model: str,
    *,
    device: str = "cpu",
    disable_update: bool = True,
    extra: dict[str, Any] | None = None,
) -> Any | None:
    """Lazily build a ``funasr.AutoModel``; returns ``None`` when unavailable."""
    try:
        from funasr import AutoModel  # noqa: PLC0415 - heavy, lazy import
    except Exception as exc:  # noqa: BLE001 - funasr/torch missing is not fatal
        _logger.warning(
            "funasr unavailable (%s: %s); ASR component %r stays idle",
            type(exc).__name__,
            exc,
            model,
        )
        return None
    try:
        return AutoModel(model=model, device=device, disable_update=disable_update, **(extra or {}))
    except Exception as exc:  # noqa: BLE001 - download/load failure
        _logger.error("failed to load ASR model %r: %s: %s", model, type(exc).__name__, exc)
        return None


def extract_text(results: Any) -> str:
    """Pull the recognised text out of a FunASR ``generate`` result."""
    if not results:
        return ""
    items = results if isinstance(results, (list, tuple)) else [results]
    parts: list[str] = []
    for item in items:
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        elif isinstance(item, str) and item:
            parts.append(item)
    return "".join(parts).strip()


class FunasrComponent:
    """Base class for lazily loaded FunASR models."""

    #: Human readable component name used in logs.
    component_name: str = "funasr"

    def __init__(
        self,
        model: str,
        *,
        device: str = "cpu",
        disable_update: bool = True,
        sample_rate: int = 16000,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._model_name = model
        self._device = device
        self._disable_update = disable_update
        self._sample_rate = sample_rate
        self._extra = dict(extra or {})
        self._model: Any | None = None

    @property
    def name(self) -> str:
        """``component(model)`` identifier for logs."""
        return f"{self.component_name}({self._model_name})"

    @property
    def available(self) -> bool:
        """True once the model is loaded and usable."""
        return self._model is not None

    @property
    def model_name(self) -> str:
        """Configured FunASR model id."""
        return self._model_name

    def load(self) -> bool:
        """Load the model (idempotent). Returns availability."""
        if self._model is not None:
            return True
        self._model = load_auto_model(
            self._model_name,
            device=self._device,
            disable_update=self._disable_update,
            extra=self._extra,
        )
        if self._model is not None:
            _logger.info("loaded %s", self.name)
        return self._model is not None

    def unload(self) -> None:
        """Release the model (frees torch memory on shutdown)."""
        self._model = None

    def _generate(self, **kwargs: Any) -> Any | None:
        """Guarded ``model.generate`` call: errors are logged, never raised."""
        if self._model is None:
            return None
        try:
            return self._model.generate(**kwargs)
        except Exception as exc:  # noqa: BLE001 - one bad chunk must not kill ASR
            _logger.warning("%s inference failed: %s: %s", self.name, type(exc).__name__, exc)
            return None
