from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.ai.base import (
    AIAuthenticationError,
    AIError,
    AIModelUnavailableError,
    AIProviderUnavailableError,
    AIRateLimitError,
)
from app.ai.custom_provider import CustomAPIProvider
from app.ai.factory import create_provider
from app.ui.ai_chat_panel import PROVIDERS as CHAT_PROVIDERS
from app.ui.settings_dialog import ALL_PROVIDERS as SETTINGS_PROVIDERS


class _HTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class CustomProviderTests(unittest.TestCase):
    def test_provider_registry_has_one_generic_custom_slot(self) -> None:
        expected = ["gemini", "openai", "claude", "deepseek", "custom"]
        self.assertEqual([value for _label, value in CHAT_PROVIDERS], expected)
        self.assertEqual([value for _label, value in SETTINGS_PROVIDERS], expected)
        self.assertNotIn("vilao", expected)

    def test_factory_requires_runtime_configuration(self) -> None:
        with patch("openai.OpenAI") as client_type:
            provider = create_provider(
                "custom",
                "secret",
                custom_config={
                    "name": "Research Gateway",
                    "base_url": "https://gateway.example/v1",
                    "headers": {"X-Lab": "paper"},
                },
            )
        self.assertIsInstance(provider, CustomAPIProvider)
        self.assertEqual(provider.name, "custom")
        self.assertEqual(provider.display_name, "Research Gateway")
        client_type.assert_called_once()
        kwargs = client_type.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "https://gateway.example/v1")
        self.assertEqual(kwargs["default_headers"], {"X-Lab": "paper"})

    def test_models_are_loaded_from_compatible_endpoint(self) -> None:
        with patch("openai.OpenAI"):
            provider = CustomAPIProvider(
                "secret",
                base_url="https://gateway.example/v1",
                display_name="Lab API",
            )
        provider.client = SimpleNamespace(
            models=SimpleNamespace(
                list=lambda: SimpleNamespace(
                    data=[SimpleNamespace(id="model-b"), SimpleNamespace(id="model-a")]
                )
            )
        )
        message, models = provider.test_connection()
        self.assertEqual(models, ["model-a", "model-b"])
        self.assertIn("Lab API", message)

    def test_errors_keep_custom_provider_identity(self) -> None:
        with patch("openai.OpenAI"):
            provider = CustomAPIProvider(
                "secret",
                base_url="https://gateway.example/v1",
                display_name="Lab API",
            )
        cases = (
            (401, AIAuthenticationError),
            (403, AIError),
            (404, AIModelUnavailableError),
            (429, AIRateLimitError),
            (503, AIProviderUnavailableError),
        )
        for status, expected in cases:
            with self.subTest(status=status):
                classified = provider._classify_error(
                    _HTTPError(status, f"gateway error {status}"),
                    operation="chat",
                    model="test-model",
                )
                self.assertIsInstance(classified, expected)
                self.assertIn("Lab API", str(classified))


if __name__ == "__main__":
    unittest.main()
