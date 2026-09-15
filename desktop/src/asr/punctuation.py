"""Punctuation restoration (CT-Transformer / ct-punc, PRD 7 pipeline).

Applied once per final utterance -- never on streaming partials -- so the live
text stays stable and cheap.
"""

from __future__ import annotations

from src.asr.base import FunasrComponent, extract_text
from src.asr.shared_models import SharedAsrModels
from src.util.log import get_logger

__all__ = ["PunctuationRestorer"]

_logger = get_logger(__name__)


class PunctuationRestorer(FunasrComponent):
    """Adds punctuation to an ASR transcript."""

    component_name = "ct-punc"

    def __init__(
        self,
        model: str = "ct-punc",
        *,
        device: str = "cpu",
        disable_update: bool = True,
        shared: SharedAsrModels | None = None,
    ) -> None:
        super().__init__(model, device=device, disable_update=disable_update, shared=shared)

    def restore(self, text: str) -> str:
        """Return *text* with punctuation; falls back to the input on failure."""
        cleaned = (text or "").strip()
        if not cleaned or not self.available:
            return cleaned
        result = extract_text(self._generate(input=cleaned))
        return result or cleaned
