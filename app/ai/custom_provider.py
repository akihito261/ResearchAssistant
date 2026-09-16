from __future__ import annotations

import logging
from collections.abc import Mapping

from app.ai.base import (
    AIAuthenticationError,
    AIError,
    AIModelUnavailableError,
    AINetworkError,
    AIProviderUnavailableError,
    AIRateLimitError,
)
from app.ai.deepseek_provider import DeepSeekProvider


LOGGER = logging.getLogger(__name__)


def _status(error: Exception) -> int | None:
    value = getattr(error, "status_code", None)
    if value is None:
        value = getattr(getattr(error, "response", None), "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class CustomAPIProvider(DeepSeekProvider):
    """User-configured OpenAI-compatible endpoint using local PDF text."""

    name = "custom"
    reusable_document_reference = False
    _MAX_TOKENS = 8192

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        display_name: str = "Custom API",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        from openai import OpenAI

        endpoint = str(base_url).strip().rstrip("/")
        if not endpoint:
            raise ValueError("Configure the Custom API Base URL in Settings.")
        self.base_url = endpoint
        self.display_name = " ".join(str(display_name).split()).strip() or "Custom API"
        self.client = OpenAI(
            api_key=api_key,
            base_url=endpoint,
            default_headers={str(k): str(v) for k, v in (headers or {}).items()},
            max_retries=0,
            timeout=90.0,
        )

    def test_connection(self) -> tuple[str, list[str]]:
        models = self.list_models()
        if not models:
            raise AIModelUnavailableError(
                f"{self.display_name} connected, but /models returned no models."
            )
        return (
            f"Connected to {self.display_name} ({len(models)} models available).",
            models,
        )

    def _classify_error(
        self,
        error: Exception,
        *,
        operation: str,
        model: str = "",
    ):
        status = _status(error)
        detail = str(error).replace("\r", " ").replace("\n", " ").strip()[:2000]
        LOGGER.error(
            "provider=CustomAPI name=%s operation=%s model=%s HTTP status=%s message=%s",
            self.display_name,
            operation,
            model or "(none)",
            status if status is not None else "unknown",
            detail or repr(error),
            exc_info=True,
        )
        if status == 401:
            return AIAuthenticationError(
                f"{self.display_name} API key is invalid. Update it in Settings."
            )
        if status == 403:
            return AIError(f"{self.display_name} denied access: {detail}")
        if status == 404:
            return AIModelUnavailableError(
                f"{self.display_name} endpoint or model was not found: {detail}"
            )
        if status == 429:
            return AIRateLimitError(
                f"{self.display_name} rate limit or quota was reached: {detail}"
            )
        if status is not None and 500 <= status <= 599:
            return AIProviderUnavailableError(
                f"{self.display_name} is temporarily unavailable: {detail}"
            )
        if status is not None:
            return AIError(f"{self.display_name} returned HTTP {status}: {detail}")
        return AINetworkError(
            f"Could not reach {self.display_name}. Check its Base URL and network. {detail}"
        )
