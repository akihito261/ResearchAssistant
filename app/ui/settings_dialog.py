from __future__ import annotations

import logging
import json
import re
import sqlite3
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.database.settings_repository import SettingsRepository
from app.ai.custom_config import load_custom_api_config
from app.services.ai_chat_service import AIChatService
from app.services.ai_credential_service import AICredentialError, AICredentialService
from app.services.background_worker import FunctionWorker
from app.services.library_path_service import MANAGED_PAPERS_ROOT
from app.ui.icons import set_widget_icon
from app.ui.ui_styles import MODERN_SCROLLBAR_QSS


LOGGER = logging.getLogger(__name__)
_CREDENTIAL_INDICATOR = "•" * 15

PROVIDERS = (
    ("Gemini", "gemini"),
    ("OpenAI", "openai"),
    ("Claude", "claude"),
    ("DeepSeek", "deepseek"),
)
CUSTOM_PROVIDER = ("Custom API", "custom")
ALL_PROVIDERS = (*PROVIDERS, CUSTOM_PROVIDER)

LANGUAGES = (
    ("Vietnamese", "vi"),
    ("English", "en"),
    ("French", "fr"),
    ("German", "de"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
    ("Chinese (Simplified)", "zh-CN"),
)


def _configure_dropdown(combo: QComboBox, *, visible_rows: int = 10) -> None:
    combo.setMaxVisibleItems(visible_rows)
    combo.view().setVerticalScrollBarPolicy(
        Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    combo.view().setMaximumHeight(visible_rows * 30)


class CredentialLineEdit(QLineEdit):
    """Masked visual indicator that never contains the stored credential."""

    stored_credential_delete_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self._showing_indicator = False
        self.textEdited.connect(self._on_text_edited)

    def show_stored_indicator(self) -> None:
        self._showing_indicator = True
        self.setText(_CREDENTIAL_INDICATOR)

    def clear_stored_indicator(self) -> None:
        self._showing_indicator = False
        self.clear()

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        if self._showing_indicator:
            self.selectAll()

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        if self._showing_indicator:
            self.selectAll()

    def keyPressEvent(self, event) -> None:
        if self._showing_indicator and event.key() in {
            Qt.Key.Key_Backspace,
            Qt.Key.Key_Delete,
        }:
            self.clear_stored_indicator()
            self.stored_credential_delete_requested.emit()
            event.accept()
            return
        replacing_with_paste = event.matches(QKeySequence.StandardKey.Paste)
        typed_text = bool(event.text()) and not (
            event.modifiers()
            & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.MetaModifier
            )
        )
        if self._showing_indicator and (replacing_with_paste or typed_text):
            self.clear_stored_indicator()
        super().keyPressEvent(event)

    def _on_text_edited(self, _text: str) -> None:
        self._showing_indicator = False

    def replacement_secret(self) -> str:
        if self._showing_indicator and self.text() == _CREDENTIAL_INDICATOR:
            return ""
        return self.text().strip()


class SettingsDialog(QDialog):
    settings_saved = Signal()

    def __init__(
        self,
        parent=None,
        *,
        repository: type[SettingsRepository] = SettingsRepository,
        library_path: Path = MANAGED_PAPERS_ROOT,
        ai_chat_service: AIChatService | None = None,
        credential_service: type[AICredentialService] = AICredentialService,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.library_path = library_path.resolve(strict=False)
        self.ai_chat_service = ai_chat_service or AIChatService()
        self.credential_service = credential_service
        self._ai_workers: set[FunctionWorker] = set()
        self._model_options: dict[str, list[str]] = {
            provider: [] for _label, provider in ALL_PROVIDERS
        }
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self._setup_ui()
        self._load()
        self._apply_style()
        self._clamp_to_screen()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(12)

        title = QLabel("Settings")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setProperty("modernScroll", True)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("settingsContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 8, 0)
        content_layout.setSpacing(14)

        storage_group = QGroupBox("Storage")
        storage_form = QFormLayout(storage_group)
        self.library_input = QLineEdit(str(self.library_path))
        self.library_input.setReadOnly(True)
        self.library_input.setToolTip(
            "The managed library location is read-only in this version."
        )
        backup_row = QHBoxLayout()
        self.backup_input = QLineEdit()
        self.backup_input.setPlaceholderText("Choose a folder for backups")
        browse_button = QPushButton("Browse…")
        set_widget_icon(browse_button, "folder", size=16)
        browse_button.clicked.connect(self._browse_backup)
        backup_row.addWidget(self.backup_input, 1)
        backup_row.addWidget(browse_button)
        storage_form.addRow("Library path:", self.library_input)
        storage_form.addRow("Backup folder:", backup_row)
        content_layout.addWidget(storage_group)

        language_group = QGroupBox("Language")
        language_form = QFormLayout(language_group)
        self.language_input = QComboBox()
        self.language_input.setEditable(True)
        for label, code in LANGUAGES:
            self.language_input.addItem(f"{label} ({code})", code)
        _configure_dropdown(self.language_input, visible_rows=8)
        language_form.addRow("Output language:", self.language_input)
        content_layout.addWidget(language_group)

        ai_group = QGroupBox("AI Providers")
        ai_group.setObjectName("aiProvidersGroup")
        ai_form = QFormLayout(ai_group)

        official_label = QLabel("Official")
        official_label.setObjectName("settingsSectionLabel")
        ai_form.addRow(official_label)

        self.key_inputs: dict[str, CredentialLineEdit] = {}
        for label, provider in PROVIDERS:
            editor = CredentialLineEdit()
            editor.setPlaceholderText(self._credential_placeholder(provider))
            editor.stored_credential_delete_requested.connect(
                lambda name=provider, target=editor: self._delete_stored_api_key(
                    name, target
                )
            )
            button = QPushButton("Connection")
            button.clicked.connect(
                lambda _checked=False, name=provider, target=button: self._test_ai_connection(
                    name, target
                )
            )
            row = QHBoxLayout()
            row.addWidget(editor, 1)
            row.addWidget(button)
            self.key_inputs[provider] = editor
            ai_form.addRow(f"{label} API key:", row)

        self.gemini_key_input = self.key_inputs["gemini"]
        self.openai_key_input = self.key_inputs["openai"]
        self.claude_key_input = self.key_inputs["claude"]
        self.deepseek_key_input = self.key_inputs["deepseek"]

        security_note = QLabel(
            "Keys are stored in the operating-system credential vault, never in the project database."
        )
        security_note.setObjectName("settingsHint")
        security_note.setWordWrap(True)
        ai_form.addRow("", security_note)

        custom_divider = QFrame()
        custom_divider.setObjectName("settingsSectionDivider")
        custom_divider.setFrameShape(QFrame.Shape.HLine)
        ai_form.addRow(custom_divider)
        custom_label = QLabel("Custom · Custom API")
        custom_label.setObjectName("settingsSectionLabel")
        ai_form.addRow(custom_label)
        self.custom_name_input = QLineEdit()
        self.custom_name_input.setPlaceholderText("Provider name")
        self.custom_base_url_input = QLineEdit()
        self.custom_base_url_input.setPlaceholderText("https://api.example.com/v1")
        self.custom_format_input = QComboBox()
        self.custom_format_input.addItem("OpenAI Compatible", "openai_compatible")
        self.custom_headers_input = QLineEdit()
        self.custom_headers_input.setPlaceholderText('{"X-Header": "value"} (optional)')
        self.custom_advanced_button = QPushButton("Advanced")
        self.custom_advanced_button.setObjectName("advancedToggle")
        self.custom_advanced_button.setCheckable(True)
        set_widget_icon(self.custom_advanced_button, "chevron-down", size=13)
        self.custom_advanced_button.toggled.connect(
            self.custom_headers_input.setVisible
        )
        self.custom_headers_input.hide()
        ai_form.addRow("Provider name:", self.custom_name_input)
        ai_form.addRow("Base URL:", self.custom_base_url_input)
        ai_form.addRow("API format:", self.custom_format_input)
        ai_form.addRow(self.custom_advanced_button, self.custom_headers_input)

        custom_editor = CredentialLineEdit()
        custom_editor.setPlaceholderText(self._credential_placeholder("custom"))
        custom_editor.stored_credential_delete_requested.connect(
            lambda: self._delete_stored_api_key("custom", custom_editor)
        )
        custom_button = QPushButton("Connection")
        custom_button.clicked.connect(
            lambda _checked=False: self._test_ai_connection("custom", custom_button)
        )
        custom_key_row = QHBoxLayout()
        custom_key_row.addWidget(custom_editor, 1)
        custom_key_row.addWidget(custom_button)
        self.key_inputs["custom"] = custom_editor
        self.custom_key_input = custom_editor
        ai_form.addRow("API key:", custom_key_row)
        content_layout.addWidget(ai_group)
        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _clamp_to_screen(self) -> None:
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(680, 720)
            return
        available = screen.availableGeometry()
        maximum_width = max(240, int(available.width() * 0.90))
        maximum_height = max(240, int(available.height() * 0.90))
        self.setMaximumSize(maximum_width, maximum_height)
        if available.width() < self.minimumWidth():
            self.setMinimumWidth(max(240, min(maximum_width, available.width() - 24)))
        width = min(700, max(self.minimumWidth(), available.width() - 48))
        width = min(width, maximum_width)
        height = min(760, max(360, available.height() - 80))
        height = min(height, maximum_height)
        self.resize(width, height)

    def _load(self) -> None:
        try:
            self.backup_input.setText(self.repository.get("backup_path", "") or "")
            target = self.repository.get("translation_target", "vi") or "vi"
            custom_config = load_custom_api_config(self.repository)
            self.custom_name_input.setText(str(custom_config["name"]))
            self.custom_base_url_input.setText(str(custom_config["base_url"]))
            raw_headers = custom_config["headers"]
            self.custom_headers_input.setText(
                json.dumps(raw_headers, ensure_ascii=False)
                if isinstance(raw_headers, dict) and raw_headers
                else ""
            )
            self.custom_advanced_button.setChecked(
                bool(self.custom_headers_input.text().strip())
            )
            self._model_options = {
                name: self._stored_model_options(name)
                for _label, name in ALL_PROVIDERS
            }
        except (sqlite3.Error, OSError, ValueError) as error:
            LOGGER.exception("Could not load settings")
            QMessageBox.warning(self, "Settings", f"Could not load settings:\n{error}")
            target = "vi"
            self._model_options = {name: [] for _label, name in ALL_PROVIDERS}

        language_index = self.language_input.findData(target)
        if language_index >= 0:
            self.language_input.setCurrentIndex(language_index)
        else:
            self.language_input.setEditText(str(target))

        for name, editor in self.key_inputs.items():
            if self.credential_service.has_stored_api_key(name):
                editor.show_stored_indicator()
            editor.setPlaceholderText(self._credential_placeholder(name))

    def _credential_placeholder(self, provider: str) -> str:
        environment_name = self.credential_service.ENVIRONMENT_KEYS[provider]
        if self.credential_service.has_environment_api_key(provider):
            return f"Available from {environment_name}"
        return f"Enter key or use {environment_name}"

    def _delete_stored_api_key(
        self, provider: str, editor: CredentialLineEdit
    ) -> None:
        try:
            self.credential_service.delete_api_key(provider)
        except AICredentialError as error:
            LOGGER.exception("Could not delete stored AI credential for %s", provider)
            editor.show_stored_indicator()
            QMessageBox.warning(self, "Settings", str(error))
            return
        editor.setPlaceholderText(self._credential_placeholder(provider))

    def _stored_model_options(self, provider: str) -> list[str]:
        try:
            raw = self.repository.get_json(f"ai_models_{provider}", [])
        except (TypeError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        return list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))

    def _test_ai_connection(self, provider: str, button: QPushButton) -> None:
        typed_key = self.key_inputs[provider].replacement_secret() or None
        provider_config = None
        if provider == "custom":
            try:
                headers = json.loads(self.custom_headers_input.text().strip() or "{}")
                if not isinstance(headers, dict) or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in headers.items()
                ):
                    raise ValueError
            except (TypeError, ValueError, json.JSONDecodeError):
                QMessageBox.warning(
                    self, "Custom headers", "Custom headers must be a JSON object of string values."
                )
                return
            provider_config = {
                "name": self.custom_name_input.text().strip() or "Custom API",
                "base_url": self.custom_base_url_input.text().strip(),
                "headers": headers,
            }
        button.setEnabled(False)
        button.setText("Testing…")
        worker = (
            FunctionWorker(
                self.ai_chat_service.test_connection,
                provider,
                typed_key,
                provider_config,
            )
            if provider == "custom"
            else FunctionWorker(
                self.ai_chat_service.test_connection,
                provider,
                typed_key,
            )
        )
        self._ai_workers.add(worker)

        def succeeded(result: object) -> None:
            message, models = (
                result if isinstance(result, tuple) else (str(result), [])
            )
            available = [str(model) for model in models]
            self._model_options[provider] = available
            QMessageBox.information(self, "AI connection", str(message))

        worker.signals.result.connect(succeeded)
        worker.signals.error.connect(
            lambda error: QMessageBox.warning(self, "AI connection", str(error))
        )
        worker.signals.finished.connect(
            lambda: (button.setEnabled(True), button.setText("Connection"))
        )
        worker.signals.finished.connect(
            lambda current=worker: self._ai_workers.discard(current)
        )
        QThreadPool.globalInstance().start(worker)

    def _browse_backup(self) -> None:
        start = self.backup_input.text().strip() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose Backup Folder",
            start,
        )
        if selected:
            self.backup_input.setText(selected)

    def _translation_code(self) -> str:
        data = self.language_input.currentData()
        if data and self.language_input.currentText() == self.language_input.itemText(
            self.language_input.currentIndex()
        ):
            return str(data)
        text = self.language_input.currentText().strip()
        match = re.search(r"\(([A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?)\)$", text)
        return match.group(1) if match else text

    def _save(self) -> None:
        target = self._translation_code()
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?", target):
            QMessageBox.warning(
                self,
                "Invalid language",
                "Use a language code such as vi, en, or zh-CN.",
            )
            return
        try:
            custom_headers = json.loads(
                self.custom_headers_input.text().strip() or "{}"
            )
            if not isinstance(custom_headers, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in custom_headers.items()
            ):
                raise ValueError("Custom headers must be a JSON object of string values.")
            custom_base_url = self.custom_base_url_input.text().strip().rstrip("/")
            if custom_base_url and not re.match(r"^https?://", custom_base_url, re.I):
                raise ValueError("Custom API Base URL must start with http:// or https://.")
            for provider, editor in self.key_inputs.items():
                replacement = editor.replacement_secret()
                if replacement:
                    self.credential_service.set_api_key(provider, replacement)
            self.repository.set("backup_path", self.backup_input.text().strip())
            self.repository.set("translation_target", target)
            self.repository.set(
                "custom_api_name",
                self.custom_name_input.text().strip(),
            )
            self.repository.set("custom_api_base_url", custom_base_url)
            self.repository.set("custom_api_format", "openai_compatible")
            self.repository.set_json("custom_api_headers", custom_headers)
            for _label, provider in ALL_PROVIDERS:
                self.repository.set_json(
                    f"ai_models_{provider}",
                    self._model_options.get(provider, []),
                )
        except (AICredentialError, sqlite3.Error, OSError, ValueError) as error:
            LOGGER.exception("Could not save settings")
            QMessageBox.critical(self, "Settings", f"Could not save settings:\n{error}")
            return
        self.settings_saved.emit()
        self.accept()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QDialog { background: #F7F8FA; }
            #settingsScroll, #settingsContent { border: none; background: transparent; }
            #dialogTitle { font-size: 22px; font-weight: 700; color: #202328; }
            QGroupBox {
                background: white; border: 1px solid #E1E4E8; border-radius: 9px;
                margin-top: 10px; padding: 12px; font-weight: 600;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; }
            #settingsSectionLabel {
                color: #657181; font-size: 11px; font-weight: 650;
                padding: 4px 0 2px 0;
            }
            #settingsSectionDivider {
                border: none; border-top: 1px solid #E6E9ED;
                min-height: 1px; max-height: 1px; margin: 8px 0 3px 0;
            }
            QLineEdit, QComboBox {
                min-height: 34px; border: 1px solid #DDE1E6; border-radius: 7px;
                background: white; padding: 0 9px; font-weight: 400;
            }
            QLineEdit[readOnly="true"] { background: #F1F3F5; color: #666C75; }
            #settingsHint { color: #6A717B; font-size: 11px; font-weight: 400; }
            #advancedToggle {
                border: none; background: transparent; color: #66717F;
                text-align: left; padding: 2px 0; min-height: 24px;
            }
            #advancedToggle:hover { color: #2F5F9E; }
            QPushButton { min-height: 34px; padding: 0 13px; }
            """ + MODERN_SCROLLBAR_QSS
        )
