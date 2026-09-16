from __future__ import annotations

from abc import ABC, abstractmethod


class TranslationError(RuntimeError):
    """A recoverable provider or network error."""


class TranslationProvider(ABC):
    @abstractmethod
    def translate(
        self,
        text: str,
        *,
        source_language: str = "en",
        target_language: str = "vi",
    ) -> str:
        """Translate text or raise TranslationError on a recoverable failure."""
