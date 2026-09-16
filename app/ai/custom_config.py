from __future__ import annotations

from collections.abc import Mapping
from typing import Any


DEFAULT_CUSTOM_API_NAME = "Vilao"
DEFAULT_CUSTOM_API_BASE_URL = "https://api.vilao.ai/v1"
DEFAULT_CUSTOM_API_FORMAT = "openai_compatible"


def load_custom_api_config(settings_repository: Any) -> dict[str, object]:
    """Load saved generic-compatible settings or fresh-install UX defaults."""
    name = str(settings_repository.get("custom_api_name", "") or "").strip()
    base_url = str(
        settings_repository.get("custom_api_base_url", "") or ""
    ).strip()
    api_format = str(
        settings_repository.get(
            "custom_api_format", DEFAULT_CUSTOM_API_FORMAT
        )
        or DEFAULT_CUSTOM_API_FORMAT
    ).strip()
    try:
        raw_headers = settings_repository.get_json("custom_api_headers", {})
    except (TypeError, ValueError):
        raw_headers = {}
    headers = (
        {
            str(key): str(value)
            for key, value in raw_headers.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if isinstance(raw_headers, Mapping)
        else {}
    )

    # Both blank means that no endpoint has been configured. A partial or full
    # saved configuration is user-owned and is never filled from vendor defaults.
    if not name and not base_url:
        name = DEFAULT_CUSTOM_API_NAME
        base_url = DEFAULT_CUSTOM_API_BASE_URL
    return {
        "name": name,
        "base_url": base_url,
        "format": api_format,
        "headers": headers,
    }
