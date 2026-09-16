from __future__ import annotations

from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from app.ui.settings_dialog import SettingsDialog
from app.services.ai_credential_service import AICredentialService


class _Repository:
    values: dict[str, object] = {}

    @classmethod
    def get(cls, key: str, default=None):
        return cls.values.get(key, default)

    @classmethod
    def get_json(cls, key: str, default=None):
        return cls.values.get(key, default)

    @classmethod
    def set(cls, key: str, value: object) -> None:
        cls.values[key] = value

    @classmethod
    def set_json(cls, key: str, value: object) -> None:
        cls.values[key] = value


class _Credentials:
    ENVIRONMENT_KEYS = {
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "claude": "ANTHROPIC_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "custom": "CUSTOM_API_KEY",
    }
    stored: dict[str, str] = {}
    environment: set[str] = set()

    @classmethod
    def has_stored_api_key(cls, provider: str) -> bool:
        return bool(cls.stored.get(provider))

    @classmethod
    def has_environment_api_key(cls, provider: str) -> bool:
        return provider in cls.environment

    @classmethod
    def delete_api_key(cls, provider: str) -> None:
        cls.stored.pop(provider, None)

    @classmethod
    def set_api_key(cls, provider: str, value: str) -> None:
        cls.stored[provider] = value


class SettingsCredentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        _Repository.values = {}
        _Credentials.stored = {"gemini": "old-secret"}
        _Credentials.environment = {"gemini"}

    def _dialog(self) -> SettingsDialog:
        return SettingsDialog(
            repository=_Repository,
            credential_service=_Credentials,
        )

    def test_backspace_removes_indicator_and_stored_key_immediately(self) -> None:
        dialog = self._dialog()
        editor = dialog.key_inputs["gemini"]
        self.assertTrue(editor.text())
        self.assertNotEqual(editor.text(), "old-secret")
        editor.setFocus()
        QTest.keyClick(editor, Qt.Key.Key_Backspace)
        self.assertEqual(editor.text(), "")
        self.assertNotIn("gemini", _Credentials.stored)
        self.assertIn("GEMINI_API_KEY", editor.placeholderText())
        dialog.close()

        reopened = self._dialog()
        self.assertEqual(reopened.key_inputs["gemini"].text(), "")
        reopened.close()

    def test_typing_replaces_indicator_and_connection_labels_are_compact(self) -> None:
        dialog = self._dialog()
        editor = dialog.key_inputs["gemini"]
        editor.setFocus()
        QTest.keyClicks(editor, "new-secret")
        self.assertEqual(editor.text(), "new-secret")
        self.assertEqual(_Credentials.stored["gemini"], "old-secret")
        connection_buttons = [
            button
            for button in dialog.findChildren(QPushButton)
            if button.text() == "Connection"
        ]
        self.assertEqual(len(connection_buttons), 5)
        dialog._save()
        self.assertEqual(_Credentials.stored["gemini"], "new-secret")

    def test_custom_key_uses_the_same_secure_credential_flow(self) -> None:
        _Credentials.stored["custom"] = "old-custom-secret"
        dialog = self._dialog()
        editor = dialog.key_inputs["custom"]
        self.assertTrue(editor.text())
        self.assertNotEqual(editor.text(), "old-custom-secret")
        editor.setFocus()
        QTest.keyClicks(editor, "new-custom-secret")
        dialog._save()
        self.assertEqual(_Credentials.stored["custom"], "new-custom-secret")
        self.assertNotIn("vilao", dialog.key_inputs)

    def test_legacy_provider_key_is_moved_to_custom_vault_identity(self) -> None:
        legacy_name = "vilao"
        values = {
            AICredentialService._username(legacy_name): "legacy-secret"
        }

        def get_password(_service: str, username: str):
            return values.get(username)

        def set_password(_service: str, username: str, secret: str) -> None:
            values[username] = secret

        def delete_password(_service: str, username: str) -> None:
            values.pop(username, None)

        fake_keyring = SimpleNamespace(
            get_password=get_password,
            set_password=set_password,
            delete_password=delete_password,
        )
        with patch.dict(sys.modules, {"keyring": fake_keyring}):
            self.assertEqual(
                AICredentialService.get_stored_api_key("custom"),
                "legacy-secret",
            )
        self.assertEqual(
            values[AICredentialService._username("custom")],
            "legacy-secret",
        )
        self.assertNotIn(AICredentialService._username(legacy_name), values)


if __name__ == "__main__":
    unittest.main()
