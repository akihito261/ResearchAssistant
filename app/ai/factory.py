from __future__ import annotations

from app.ai.base import AIConfigurationError, AIProvider


def create_provider(
    provider: str,
    api_key: str,
    *,
    custom_config: dict[str, object] | None = None,
) -> AIProvider:
    name = provider.strip().lower()
    if not api_key.strip():
        display_name = (
            str((custom_config or {}).get("name") or "Custom API")
            if name == "custom"
            else name.title()
        )
        raise AIConfigurationError(
            f"No {display_name} API key is configured. Open Settings > AI Providers."
        )
    if name == "gemini":
        from app.ai.gemini_provider import GeminiProvider

        return GeminiProvider(api_key.strip())
    if name == "openai":
        from app.ai.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key.strip())
    if name == "claude":
        from app.ai.claude_provider import ClaudeProvider

        return ClaudeProvider(api_key.strip())
    if name == "deepseek":
        from app.ai.deepseek_provider import DeepSeekProvider

        return DeepSeekProvider(api_key.strip())
    if name == "custom":
        from app.ai.custom_provider import CustomAPIProvider

        config = custom_config or {}
        return CustomAPIProvider(
            api_key.strip(),
            base_url=str(config.get("base_url") or ""),
            display_name=str(config.get("name") or "Custom API"),
            headers=config.get("headers") if isinstance(config.get("headers"), dict) else {},
        )
    raise AIConfigurationError(f"Unsupported AI provider: {provider}")
