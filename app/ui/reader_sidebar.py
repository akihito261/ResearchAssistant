from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from functools import lru_cache
from typing import Any

from PySide6.QtCore import QPoint, QRectF, QSize, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QAbstractScrollArea,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
)

from app.bridge.pdf_reader_bridge import HIGHLIGHT_COLORS, PdfReaderBridge
from app.database.collection_repository import CollectionRepository
from app.database.ai_repository import AIRepository
from app.database.highlight_repository import HighlightRepository
from app.database.note_repository import NoteRepository
from app.database.paper_repository import PaperRepository
from app.database.project_repository import ProjectRepository
from app.database.settings_repository import SettingsRepository
from app.database.summary_repository import SUMMARY_FIELDS, SummaryRepository
from app.database.tag_repository import TagRepository
from app.ui.icons import (
    DANGER_ICON_COLOR,
    app_icon,
    icon_pixmap,
    populate_reading_status_combo,
    set_widget_icon,
)
from app.ui.ai_chat_panel import (
    AIChatPanel,
    PROVIDERS,
    ProviderCombo,
    provider_logo_icon,
)
from app.ui.citation_widgets import citation_number_map
from app.ui.markdown_math import AutoExpandingMarkdownEdit
from app.ui.project_ai_search_dialog import (
    ProjectSearchConversationController,
    ReaderSearchPanel,
)


DEFAULT_HIGHLIGHT_MEANINGS = {
    "yellow": "Important",
    "blue": "Method",
    "green": "Result",
    "red": "Limitation / Problem",
    "orange": "Unclear / Question",
    "purple": "Personal Idea",
}

HIGHLIGHT_SWATCH_COLORS = {
    "yellow": "#E2B714",
    "blue": "#4D96E8",
    "green": "#35AD72",
    "red": "#DF5B53",
    "orange": "#ED8A35",
    "purple": "#8E70D6",
}


@lru_cache(maxsize=len(HIGHLIGHT_SWATCH_COLORS))
def _highlight_swatch_icon(name: str) -> QIcon:
    color = QColor(HIGHLIGHT_SWATCH_COLORS[name])
    icon = QIcon()
    for pixels in (14, 18, 24, 32):
        pixmap = QPixmap(pixels, pixels)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setBrush(color)
            painter.setPen(QPen(color.darker(155), max(1.0, pixels / 18)))
            margin = max(2.0, pixels * 0.18)
            painter.drawEllipse(
                QRectF(margin, margin, pixels - 2 * margin, pixels - 2 * margin)
            )
        finally:
            painter.end()
        icon.addPixmap(pixmap)
    return icon


def _updated_label(value: Any = None) -> str:
    if value:
        timestamp = str(value).replace("T", " ").removesuffix("Z")
        return f"Updated {timestamp[:16]}"
    return f"Updated {datetime.now().strftime('%Y-%m-%d %H:%M')}"


def _mapping(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return dict(row)
    keys = getattr(row, "keys", None)
    if callable(keys):
        return {key: row[key] for key in keys()}
    return {}


def _location_from_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = row.get("location_data")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class TranslationCard(QFrame):
    save_requested = Signal(str, str, object)
    closed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("translationCard")
        self._location: dict[str, Any] | None = None
        self.request_id = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(7)

        header = QHBoxLayout()
        self.header_layout = header
        title_icon = QLabel()
        title_icon.setPixmap(icon_pixmap("languages", 17))
        title_icon.setToolTip("Translate")
        title = QLabel("Translation")
        title.setObjectName("sidebarSectionTitle")
        close_button = QPushButton()
        close_button.setObjectName("sidebarIconButton")
        set_widget_icon(
            close_button,
            "x",
            size=15,
            tooltip="Close translation",
        )
        close_button.clicked.connect(self._close)
        header.addWidget(title_icon)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(close_button)

        self.original = QPlainTextEdit()
        self.original.setProperty("modernScroll", True)
        self.original.setObjectName("translationOriginal")
        self.original.setReadOnly(True)
        self.original.setMaximumHeight(86)
        self.original.setPlaceholderText("Original")

        self.translation = QPlainTextEdit()
        self.translation.setProperty("modernScroll", True)
        self.translation.setObjectName("translationResult")
        self.translation.setReadOnly(True)
        self.translation.setMaximumHeight(110)
        self.translation.setPlaceholderText("Translation")

        self.status = QLabel("")
        self.status.setObjectName("sidebarStatus")
        self.status.setWordWrap(True)

        actions = QHBoxLayout()
        self.copy_button = QPushButton("Copy translation")
        self.copy_button.setObjectName("sidebarSmallButton")
        set_widget_icon(self.copy_button, "copy", size=15)
        self.copy_button.clicked.connect(self._copy)
        self.save_button = QPushButton("Save as note")
        self.save_button.setObjectName("sidebarPrimaryButton")
        set_widget_icon(
            self.save_button,
            "sticky-note",
            size=15,
            color="#FFFFFF",
            active_color="#FFFFFF",
        )
        self.save_button.clicked.connect(self._save)
        actions.addWidget(self.copy_button)
        actions.addWidget(self.save_button)

        layout.addLayout(header)
        layout.addWidget(QLabel("Original"))
        layout.addWidget(self.original)
        layout.addWidget(QLabel("Translation"))
        layout.addWidget(self.translation)
        layout.addWidget(self.status)
        layout.addLayout(actions)
        self.hide()

    def begin(self, text: str, location: dict[str, Any], request_id: str = "") -> None:
        self._location = location
        self.request_id = request_id
        self.original.setPlainText(text)
        self.translation.clear()
        self.status.setText("Translating…")
        self.copy_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.show()

    def set_result(self, translated_text: str) -> None:
        self.translation.setPlainText(translated_text)
        self.status.setText("Translation ready")
        enabled = bool(translated_text.strip())
        self.copy_button.setEnabled(enabled)
        self.save_button.setEnabled(enabled)

    def set_error(self, message: str) -> None:
        self.translation.clear()
        self.status.setText(message)
        self.copy_button.setEnabled(False)
        self.save_button.setEnabled(False)

    def _copy(self) -> None:
        text = self.translation.toPlainText()
        if text:
            QGuiApplication.clipboard().setText(text)
            self.status.setText("Translation copied")

    def _save(self) -> None:
        translation = self.translation.toPlainText().strip()
        if translation:
            self.save_requested.emit(
                self.original.toPlainText(), translation, self._location
            )

    def _close(self) -> None:
        self.hide()
        self.closed.emit()

    def dismiss(self) -> None:
        self.request_id = ""
        self.hide()


class NoteCard(QFrame):
    navigation_requested = Signal(object)
    deleted = Signal(int)

    def __init__(
        self,
        paper_id: int,
        row: Mapping[str, Any],
        repository: Any,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("noteCard")
        self.paper_id = paper_id
        self.repository = repository
        self.row = dict(row)
        self.note_id = int(self.row["id"])
        self._last_title = str(self.row.get("title") or "")
        self._last_content = str(self.row.get("content") or "")
        self._dirty = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(6)
        top = QHBoxLayout()
        self.title_edit = QLineEdit(self._last_title)
        self.title_edit.setObjectName("noteTitleEdit")
        self.title_edit.setPlaceholderText("Untitled note")
        delete_button = QPushButton()
        delete_button.setObjectName("sidebarDangerButton")
        set_widget_icon(
            delete_button,
            "trash",
            size=15,
            tooltip="Delete note",
            color=DANGER_ICON_COLOR,
            active_color="#7E2525",
        )
        delete_button.clicked.connect(self._delete)
        top.addWidget(self.title_edit, 1)
        top.addWidget(delete_button)
        layout.addLayout(top)

        source_text = str(self.row.get("source_text") or "").strip()
        location = _location_from_row(self.row)
        if source_text or location:
            source_button = QPushButton()
            source_button.setObjectName("noteSourceButton")
            set_widget_icon(source_button, "message-square", size=14)
            page = self.row.get("page_number")
            excerpt = " ".join(source_text.split())
            if len(excerpt) > 150:
                excerpt = excerpt[:147] + "…"
            source_button.setText(
                f"p. {page} · {excerpt}" if page else excerpt or "View source"
            )
            source_button.setToolTip(source_text)
            source_button.setEnabled(location is not None)
            if location is not None:
                source_button.clicked.connect(
                    lambda _checked=False, value=location: self.navigation_requested.emit(value)
                )
            layout.addWidget(source_button)

        self.editor = QPlainTextEdit(self._last_content)
        self.editor.setProperty("modernScroll", True)
        self.editor.setObjectName("noteContentEdit")
        self.editor.setPlaceholderText("Write a note…")
        self.editor.setMinimumHeight(76)
        self.editor.setMaximumHeight(150)
        layout.addWidget(self.editor)

        footer = QHBoxLayout()
        self.status = QLabel(_updated_label(self.row.get("updated_at")))
        self.status.setObjectName("sidebarStatus")
        footer.addWidget(self.status)
        footer.addStretch()
        layout.addLayout(footer)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(650)
        self._timer.timeout.connect(self.save)
        self.title_edit.textChanged.connect(self._changed)
        self.editor.textChanged.connect(self._changed)

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def focus_editor(self) -> None:
        self.editor.setFocus()

    def _changed(self) -> None:
        self._dirty = (
            self.title_edit.text() != self._last_title
            or self.editor.toPlainText() != self._last_content
        )
        if self._dirty:
            self.status.setText("Saving…")
            self._timer.setInterval(650)
            self._timer.start()

    def _owned(self) -> bool:
        row = _mapping(self.repository.get(self.note_id))
        return bool(row) and int(row.get("paper_id", -1)) == self.paper_id

    def save(self) -> bool:
        self._timer.stop()
        if not self._dirty:
            return True
        try:
            if not self._owned():
                raise ValueError("The note does not belong to this paper.")
            changed = self.repository.update(
                self.note_id,
                content=self.editor.toPlainText(),
                title=self.title_edit.text().strip() or None,
            )
            if not changed:
                raise ValueError("The note no longer exists.")
        except (sqlite3.Error, OSError, ValueError) as error:
            self.status.setText("Could not save - retrying…")
            self.status.setToolTip(str(error))
            if self._dirty:
                self._timer.setInterval(2000)
                self._timer.start()
            return False
        self._last_title = self.title_edit.text()
        self._last_content = self.editor.toPlainText()
        self._dirty = False
        self.status.setText(_updated_label())
        self.status.setToolTip("")
        return True

    def _delete(self) -> None:
        try:
            if not self._owned():
                raise ValueError("The note does not belong to this paper.")
            if not self.repository.delete(self.note_id):
                raise ValueError("The note no longer exists.")
        except (sqlite3.Error, OSError, ValueError) as error:
            self.status.setText("Could not delete")
            self.status.setToolTip(str(error))
            return
        self._timer.stop()
        self._dirty = False
        self.deleted.emit(self.note_id)


class SelectionDraftCard(QFrame):
    saved = Signal(int, str)
    discarded = Signal(str)

    def __init__(
        self,
        paper_id: int,
        payload: Mapping[str, Any],
        repository: Any,
        document_version: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("noteCard")
        self.setProperty("draftNote", True)
        self.paper_id = int(paper_id)
        self.payload = dict(payload)
        self.repository = repository
        self.document_version = document_version
        self.request_id = str(payload.get("requestId") or "")
        self._saving = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(6)
        top = QHBoxLayout()
        self.title_edit = QLineEdit("Selection note")
        self.title_edit.setObjectName("noteTitleEdit")
        discard = QPushButton()
        discard.setObjectName("sidebarDangerButton")
        set_widget_icon(
            discard, "x", size=15, tooltip="Discard note draft",
            color=DANGER_ICON_COLOR, active_color="#7E2525",
        )
        discard.clicked.connect(self.discard)
        top.addWidget(self.title_edit, 1)
        top.addWidget(discard)
        layout.addLayout(top)

        excerpt = " ".join(str(payload.get("selectedText") or "").split())
        source = QLabel(excerpt[:180] + ("…" if len(excerpt) > 180 else ""))
        source.setObjectName("highlightQuote")
        source.setWordWrap(True)
        layout.addWidget(source)

        self.editor = QPlainTextEdit()
        self.editor.setProperty("modernScroll", True)
        self.editor.setObjectName("noteContentEdit")
        self.editor.setPlaceholderText("Write a note…")
        self.editor.setMinimumHeight(76)
        self.editor.setMaximumHeight(150)
        layout.addWidget(self.editor)
        self.status = QLabel("Draft — not saved until content is entered")
        self.status.setObjectName("sidebarStatus")
        layout.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(650)
        self._timer.timeout.connect(self.save)
        self.editor.textChanged.connect(self._changed)
        self.title_edit.textChanged.connect(self._changed)

    @property
    def is_dirty(self) -> bool:
        return bool(self.editor.toPlainText().strip())

    def focus_editor(self) -> None:
        self.editor.setFocus()

    def _changed(self) -> None:
        if self._saving:
            return
        if self.editor.toPlainText().strip():
            self.status.setText("Saving…")
            self._timer.start()
        else:
            self._timer.stop()
            self.status.setText("Draft — not saved until content is entered")

    def save(self) -> bool:
        self._timer.stop()
        content = self.editor.toPlainText()
        if not content.strip():
            return True
        location = self.payload.get("location")
        segments = location.get("segments", []) if isinstance(location, dict) else []
        page = segments[0].get("page") if segments else None
        try:
            self._saving = True
            note_id = self.repository.create(
                self.paper_id,
                content=content,
                title=self.title_edit.text().strip() or "Selection note",
                kind="selection",
                source_text=str(self.payload.get("selectedText") or ""),
                page_number=page,
                location_data=json.dumps(location, ensure_ascii=True),
                document_version=self.document_version,
            )
        except (sqlite3.Error, OSError, TypeError, ValueError) as error:
            self._saving = False
            self.status.setText("Could not save - retrying…")
            self.status.setToolTip(str(error))
            self._timer.setInterval(2000)
            self._timer.start()
            return False
        self._saving = False
        self.saved.emit(int(note_id), self.request_id)
        return True

    def discard(self) -> None:
        self._timer.stop()
        self.discarded.emit(self.request_id)


class HighlightCard(QFrame):
    navigation_requested = Signal(object)
    color_requested = Signal(int, str)
    delete_requested = Signal(int)
    note_requested = Signal(int)

    def __init__(self, row: Mapping[str, Any], parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("highlightCard")
        self.row = dict(row)
        highlight_id = int(self.row["id"])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(5)

        top = QHBoxLayout()
        page_button = QPushButton(f"Page {self.row.get('page_number', 1)}")
        page_button.setObjectName("noteSourceButton")
        set_widget_icon(page_button, "highlighter", size=14)
        location = _location_from_row(self.row)
        page_button.setEnabled(location is not None)
        if location is not None:
            page_button.clicked.connect(
                lambda _checked=False, value=location: self.navigation_requested.emit(value)
            )
        self.color = QComboBox()
        self.color.setObjectName("highlightColor")
        self.color.setToolTip("Change highlight color")
        self.color.setIconSize(QSize(14, 14))
        for name in ("yellow", "blue", "green", "red", "orange", "purple"):
            self.color.addItem(_highlight_swatch_icon(name), name.title(), name)
        index = self.color.findData(self.row.get("color", "yellow"))
        self.color.setCurrentIndex(max(0, index))
        self.color.currentIndexChanged.connect(
            lambda _index: self.color_requested.emit(highlight_id, self.color.currentData())
        )
        palette_icon = QLabel()
        palette_icon.setPixmap(icon_pixmap("palette", 15))
        palette_icon.setToolTip("Change highlight color")
        delete_button = QPushButton()
        delete_button.setObjectName("sidebarDangerButton")
        set_widget_icon(
            delete_button,
            "trash",
            size=15,
            tooltip="Delete highlight",
            color=DANGER_ICON_COLOR,
            active_color="#7E2525",
        )
        delete_button.clicked.connect(
            lambda _checked=False: self.delete_requested.emit(highlight_id)
        )
        top.addWidget(page_button)
        top.addStretch()
        top.addWidget(palette_icon)
        top.addWidget(self.color)
        top.addWidget(delete_button)
        layout.addLayout(top)

        quote = QLabel(str(self.row.get("selected_text") or ""))
        quote.setObjectName("highlightQuote")
        quote.setWordWrap(True)
        quote.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(quote)

        note_button = QPushButton(
            "Open linked note" if self.row.get("note_id") else "Add note"
        )
        note_button.setObjectName("sidebarSmallButton")
        set_widget_icon(note_button, "message-square", size=14)
        note_button.clicked.connect(
            lambda _checked=False: self.note_requested.emit(highlight_id)
        )
        if not self.row.get("note_id") and location is None:
            note_button.setEnabled(False)
            note_button.setToolTip(
                "This highlight has no anchor in the active PDF version."
            )
        layout.addWidget(note_button, alignment=Qt.AlignmentFlag.AlignLeft)


class NotesPanel(QWidget):
    navigation_requested = Signal(object)
    selection_note_saved = Signal(int, str)
    transient_closed = Signal(str)
    translation_note_saved = Signal(int)

    def __init__(
        self,
        paper_id: int,
        bridge: PdfReaderBridge,
        parent=None,
        *,
        note_repository: Any = NoteRepository,
        highlight_repository: Any = HighlightRepository,
        highlight_meanings: Mapping[str, str] = DEFAULT_HIGHLIGHT_MEANINGS,
    ) -> None:
        super().__init__(parent)
        self.paper_id = paper_id
        self.bridge = bridge
        self.note_repository = note_repository
        self.highlight_repository = highlight_repository
        self.highlight_meanings = dict(highlight_meanings)
        self.note_cards: dict[int, NoteCard] = {}
        self.draft_card: SelectionDraftCard | None = None
        self._active_note_id: int | None = None
        self.highlight_legend: QLabel | None = None
        self.empty_state: QLabel | None = None
        self._scratchpad_saved = ""
        self._scratchpad_dirty = False

        layout = QVBoxLayout(self)
        self.outer_layout = layout
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.translation_card = TranslationCard()
        self.translation_card.save_requested.connect(self.save_translation_note)
        layout.addWidget(self.translation_card)

        scratch_header = QHBoxLayout()
        scratch_title = QLabel("Scratchpad")
        scratch_title.setObjectName("sidebarSectionTitle")
        self.scratch_status = QLabel("Saved")
        self.scratch_status.setObjectName("sidebarStatus")
        scratch_header.addWidget(scratch_title)
        scratch_header.addStretch()
        scratch_header.addWidget(self.scratch_status)
        self.scratchpad = QPlainTextEdit()
        self.scratchpad.setProperty("modernScroll", True)
        self.scratchpad.setObjectName("scratchpadEditor")
        self.scratchpad.setPlaceholderText("Quick notes for this paper…")
        self.scratchpad.setMaximumHeight(145)
        layout.addLayout(scratch_header)
        layout.addWidget(self.scratchpad)

        notes_header = QHBoxLayout()
        notes_title = QLabel("Notes")
        notes_title.setObjectName("sidebarSectionTitle")
        add_button = QPushButton("New")
        add_button.setObjectName("sidebarSmallButton")
        set_widget_icon(add_button, "plus", size=14)
        add_button.clicked.connect(self.create_manual_note)
        notes_header.addWidget(notes_title)
        notes_header.addStretch()
        notes_header.addWidget(add_button)
        layout.addLayout(notes_header)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("sidebarScroll")
        self.scroll.setProperty("modernScroll", True)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setSizeAdjustPolicy(
            QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored
        )
        self.scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch()
        self.scroll.setWidget(self.list_widget)
        layout.addWidget(self.scroll, 1)

        self._scratch_timer = QTimer(self)
        self._scratch_timer.setSingleShot(True)
        self._scratch_timer.setInterval(700)
        self._scratch_timer.timeout.connect(self.save_scratchpad)
        self._load_scratchpad()
        self.scratchpad.textChanged.connect(self._scratchpad_changed)
        self.reload_cards()

    def _load_scratchpad(self) -> None:
        try:
            row = self.note_repository.get_scratchpad(self.paper_id)
        except (sqlite3.Error, OSError) as error:
            self.scratchpad.setEnabled(False)
            self.scratch_status.setText("Unavailable")
            self.scratch_status.setToolTip(str(error))
            return
        self._scratchpad_saved = str(row["content"]) if row is not None else ""
        self.scratchpad.setPlainText(self._scratchpad_saved)

    def _scratchpad_changed(self) -> None:
        self._scratchpad_dirty = self.scratchpad.toPlainText() != self._scratchpad_saved
        if self._scratchpad_dirty:
            self.scratch_status.setText("Saving…")
            self._scratch_timer.setInterval(700)
            self._scratch_timer.start()

    def save_scratchpad(self) -> bool:
        self._scratch_timer.stop()
        if not self._scratchpad_dirty:
            return True
        content = self.scratchpad.toPlainText()
        try:
            self.note_repository.save_for_paper(self.paper_id, content)
        except (sqlite3.Error, OSError, ValueError) as error:
            self.scratch_status.setText("Could not save - retrying…")
            self.scratch_status.setToolTip(str(error))
            if self._scratchpad_dirty:
                self._scratch_timer.setInterval(2000)
                self._scratch_timer.start()
            return False
        self._scratchpad_saved = content
        self._scratchpad_dirty = False
        self.scratch_status.setText("Saved")
        self.scratch_status.setToolTip("")
        return True

    def _clear_cards(self) -> None:
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.note_cards.clear()
        self.highlight_legend = None
        self.empty_state = None

    def _highlight_legend_text(self) -> str:
        legend_text = " · ".join(
            f"{color.title()} {self.highlight_meanings.get(color, DEFAULT_HIGHLIGHT_MEANINGS[color])}"
            for color in ("yellow", "blue", "green", "red", "orange", "purple")
        )
        return f"Highlights · {legend_text}"

    def _annotation_row(self, row: Any, kind: str) -> dict[str, Any]:
        resolver = getattr(self.bridge, "annotation_row", None)
        return resolver(row, kind=kind) if callable(resolver) else _mapping(row)

    def reload_cards(self, focus_note_id: int | None = None) -> bool:
        if (
            self._scratchpad_dirty
            or any(card.is_dirty for card in self.note_cards.values())
        ) and not self.flush_pending_saves():
            return False
        self._clear_cards()
        try:
            notes = [
                self._annotation_row(row, "note")
                for row in self.note_repository.list_for_paper(self.paper_id)
                if str(row["kind"]) != "scratchpad"
                and str(_mapping(row).get("document_version") or "en")
                == self.bridge.document_version
            ]
            highlights = [
                self._annotation_row(row, "highlight")
                for row in self.highlight_repository.list_for_paper(self.paper_id)
                if str(_mapping(row).get("document_version") or "en")
                == self.bridge.document_version
            ]
        except (sqlite3.Error, OSError) as error:
            label = QLabel(f"Could not load notes: {error}")
            label.setObjectName("sidebarStatus")
            label.setWordWrap(True)
            self.list_layout.insertWidget(0, label)
            return False

        if not notes and not highlights:
            self.empty_state = QLabel(
                "No note cards or highlights yet.\n"
                "Create a note or select text in the PDF to begin."
            )
            self.empty_state.setObjectName("sidebarStatus")
            self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.empty_state.setWordWrap(True)
            self.list_layout.insertWidget(0, self.empty_state)

        for row in notes:
            card = NoteCard(self.paper_id, row, self.note_repository)
            card.navigation_requested.connect(self.navigation_requested)
            card.deleted.connect(self._note_deleted)
            self.note_cards[card.note_id] = card
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)

        if highlights:
            self.highlight_legend = QLabel(self._highlight_legend_text())
            self.highlight_legend.setObjectName("highlightLegend")
            self.highlight_legend.setWordWrap(True)
            self.list_layout.insertWidget(
                self.list_layout.count() - 1, self.highlight_legend
            )
        for row in highlights:
            card = HighlightCard(row)
            card.navigation_requested.connect(self.navigation_requested)
            card.color_requested.connect(self._change_highlight_color)
            card.delete_requested.connect(self._delete_highlight)
            card.note_requested.connect(self._open_highlight_note)
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)

        if focus_note_id is not None and focus_note_id in self.note_cards:
            card = self.note_cards[focus_note_id]
            QTimer.singleShot(
                0,
                card,
                lambda: self._reveal_note_card(card),
            )
        return True

    def _reveal_note_card(self, card: NoteCard) -> None:
        if self._active_note_id in self.note_cards:
            previous = self.note_cards[self._active_note_id]
            previous.setProperty("activeNote", False)
            previous.style().unpolish(previous)
            previous.style().polish(previous)
        self._active_note_id = card.note_id
        card.setProperty("activeNote", True)
        card.style().unpolish(card)
        card.style().polish(card)
        QTimer.singleShot(
            0,
            card,
            lambda: self._center_note_card(card),
        )

    def _center_note_card(self, card: NoteCard) -> None:
        if self.note_cards.get(card.note_id) is not card:
            return
        self.list_layout.activate()
        viewport_height = self.scroll.viewport().height()
        if viewport_height <= 0:
            return

        card_top = card.mapTo(self.list_widget, QPoint(0, 0)).y()
        card_height = max(card.height(), card.sizeHint().height())
        margin = 16
        if card_height >= viewport_height - (2 * margin):
            target = card_top - margin
        else:
            target = card_top - ((viewport_height - card_height) // 2)

        scrollbar = self.scroll.verticalScrollBar()
        target = max(scrollbar.minimum(), min(scrollbar.maximum(), target))
        scrollbar.setValue(target)
        card.focus_editor()
        # Focusing a child editor may request minimal visibility from Qt's
        # scroll area; restore the deliberate centered position afterwards.
        scrollbar.setValue(target)

    def create_manual_note(self) -> None:
        try:
            note_id = self.note_repository.create(
                self.paper_id,
                title="New note",
                kind="manual",
                document_version=self.bridge.document_version,
            )
        except (sqlite3.Error, OSError, ValueError) as error:
            self.scratch_status.setText("Could not create note")
            self.scratch_status.setToolTip(str(error))
            return
        self.reload_cards(note_id)

    def _publish_document_notes(self, *, include_highlights: bool = False) -> None:
        publish_notes = getattr(self.bridge, "publish_notes", None)
        if callable(publish_notes):
            publish_notes()
        if include_highlights:
            publish_highlights = getattr(self.bridge, "publish_highlights", None)
            if callable(publish_highlights):
                publish_highlights()

    def _note_deleted(self, _note_id: int) -> None:
        # highlights.note_id uses ON DELETE SET NULL, so republish both maps.
        self._publish_document_notes(include_highlights=True)
        self.reload_cards()

    def begin_selection_note(self, payload: Mapping[str, Any]) -> bool:
        self.discard_selection_draft(notify=False)
        card = SelectionDraftCard(
            self.paper_id,
            payload,
            self.note_repository,
            self.bridge.document_version,
        )
        card.saved.connect(self._selection_draft_saved)
        card.discarded.connect(self._selection_draft_discarded)
        self.draft_card = card
        self.outer_layout.insertWidget(self.outer_layout.indexOf(self.scroll), card)
        QTimer.singleShot(0, card, card.focus_editor)
        return True

    def _selection_draft_saved(self, note_id: int, request_id: str) -> None:
        card = self.draft_card
        highlight_id = card.payload.get("highlightId") if card is not None else None
        self.draft_card = None
        if card is not None:
            self.outer_layout.removeWidget(card)
            card.deleteLater()
        if highlight_id is not None:
            try:
                self.bridge.attach_note(int(highlight_id), int(note_id))
            except (sqlite3.Error, OSError, TypeError, ValueError) as error:
                self.scratch_status.setText("Note saved, but could not link highlight")
                self.scratch_status.setToolTip(str(error))
        self._publish_document_notes()
        self.reload_cards(int(note_id))
        self.selection_note_saved.emit(int(note_id), request_id)

    def _selection_draft_discarded(self, request_id: str) -> None:
        self.discard_selection_draft(notify=False)
        self.transient_closed.emit(request_id)

    def discard_selection_draft(
        self, request_id: str = "", *, notify: bool = False
    ) -> bool:
        card = self.draft_card
        if card is None or (request_id and card.request_id != request_id):
            return False
        actual_request = card.request_id
        self.draft_card = None
        self.outer_layout.removeWidget(card)
        card.deleteLater()
        if notify:
            self.transient_closed.emit(actual_request)
        return True

    def save_translation_note(
        self,
        original: str,
        translation: str,
        location: object,
    ) -> None:
        location_value = location if isinstance(location, dict) else {}
        segments = location_value.get("segments", [])
        page = segments[0].get("page") if segments else None
        try:
            note_id = self.note_repository.create(
                self.paper_id,
                content=translation,
                title="Translation",
                kind="translation",
                source_text=original,
                page_number=page,
                location_data=json.dumps(location_value, ensure_ascii=True),
                document_version=self.bridge.document_version,
            )
        except (sqlite3.Error, OSError, ValueError) as error:
            self.translation_card.set_error(f"Could not save note: {error}")
            return
        self.translation_card.status.setText("Saved as note")
        self._publish_document_notes()
        self.reload_cards(note_id)
        self.translation_card.dismiss()
        self.translation_note_saved.emit(int(note_id))

    def _change_highlight_color(self, highlight_id: int, color: str) -> None:
        try:
            self.bridge.update_highlight_color(highlight_id, color)
        except (sqlite3.Error, OSError, ValueError) as error:
            self.scratch_status.setText("Could not update highlight")
            self.scratch_status.setToolTip(str(error))
            return
        self.reload_cards()

    def _delete_highlight(self, highlight_id: int) -> None:
        try:
            self.bridge.delete_highlight(highlight_id)
        except (sqlite3.Error, OSError, ValueError) as error:
            self.scratch_status.setText("Could not delete highlight")
            self.scratch_status.setToolTip(str(error))
            return
        self._publish_document_notes()
        self.reload_cards()

    def _open_highlight_note(self, highlight_id: int) -> None:
        row = _mapping(self.highlight_repository.get(highlight_id))
        if not row or int(row.get("paper_id", -1)) != self.paper_id:
            return
        note_id = row.get("note_id")
        if note_id is None:
            location = _location_from_row(row) or {}
            self.begin_selection_note(
                {
                    "requestId": f"highlight-{highlight_id}",
                    "selectedText": str(row.get("selected_text") or ""),
                    "location": location,
                    "highlightId": int(highlight_id),
                }
            )
            return
        self.reload_cards(int(note_id))

    def open_note(self, note_id: int) -> bool:
        row = _mapping(self.note_repository.get(int(note_id)))
        if not row or int(row.get("paper_id", -1)) != self.paper_id:
            return False
        if str(row.get("document_version") or "en") != self.bridge.document_version:
            return False
        return self.reload_cards(int(note_id))

    def flush_pending_saves(self) -> bool:
        ok = self.save_scratchpad()
        if self.draft_card is not None:
            if self.draft_card.is_dirty:
                ok = self.draft_card.save() and ok
            else:
                self.discard_selection_draft(notify=False)
        for card in tuple(self.note_cards.values()):
            ok = card.save() and ok
        return ok

    def cancel_transient(self, request_id: str) -> None:
        draft = self.draft_card
        if draft is not None and (not request_id or draft.request_id == request_id):
            if draft.is_dirty:
                draft.save()
            else:
                self.discard_selection_draft(request_id, notify=False)
            return
        if self.translation_card.request_id == request_id:
            self.translation_card.dismiss()

    def refresh_for_document(self) -> None:
        self.reload_cards()

    def set_highlight_meanings(self, meanings: Mapping[str, Any]) -> None:
        self.highlight_meanings = {
            color: str(meanings.get(color) or default)
            for color, default in DEFAULT_HIGHLIGHT_MEANINGS.items()
        }
        if self.highlight_legend is not None:
            self.highlight_legend.setText(self._highlight_legend_text())


class SummaryPanel(QWidget):
    citation_requested = Signal(object)
    LABELS = {
        "problem": "Problem",
        "contribution": "Contribution",
        "method": "Method",
        "dataset": "Dataset",
        "baseline": "Baseline",
        "results": "Results",
        "limitations": "Limitations",
        "unclear_points": "Unclear points",
        "ideas": "Ideas",
    }
    ICONS = {
        "problem": "target",
        "contribution": "sparkles",
        "method": "workflow",
        "dataset": "database",
        "baseline": "layers",
        "results": "trending-up",
        "limitations": "triangle-alert",
        "unclear_points": "circle-help",
        "ideas": "lightbulb",
    }

    def __init__(
        self,
        paper_id: int,
        parent=None,
        *,
        repository: Any = SummaryRepository,
    ) -> None:
        super().__init__(parent)
        self.paper_id = paper_id
        self.repository = repository
        self.editors: dict[str, AutoExpandingMarkdownEdit] = {}
        self._last_saved: dict[str, str] = {field: "" for field in SUMMARY_FIELDS}
        self._dirty = False
        self._loading = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setProperty("modernScroll", True)
        scroll.setObjectName("sidebarScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(9)
        self.status = QLabel("Saved")
        self.status.setObjectName("sidebarStatus")
        for field in SUMMARY_FIELDS:
            heading = QHBoxLayout()
            heading.setContentsMargins(0, 0, 0, 0)
            heading.setSpacing(6)
            icon = QLabel()
            icon.setFixedSize(16, 16)
            icon.setPixmap(icon_pixmap(self.ICONS[field], 15, color="#5D6570"))
            icon.setAccessibleName(f"{self.LABELS[field]} section")
            label = QLabel(self.LABELS[field])
            label.setObjectName("summaryLabel")
            heading.addWidget(icon)
            heading.addWidget(label)
            heading.addStretch()
            editor = AutoExpandingMarkdownEdit(
                minimum_height=66,
                maximum_height=320,
            )
            editor.setObjectName("summaryEditor")
            editor.setProperty("modernScroll", True)
            editor.textChanged.connect(self._changed)
            editor.citation_requested.connect(self.citation_requested)
            self.editors[field] = editor
            layout.addLayout(heading)
            layout.addWidget(editor)
        layout.addWidget(self.status)
        layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(750)
        self._timer.timeout.connect(self.save)
        self._load()
        self._loading = False

    def _load(self) -> None:
        try:
            row = _mapping(self.repository.get_for_paper(self.paper_id))
        except (sqlite3.Error, OSError) as error:
            self.status.setText("Summary unavailable")
            self.status.setToolTip(str(error))
            for editor in self.editors.values():
                editor.setEnabled(False)
            return
        for field, editor in self.editors.items():
            value = str(row.get(field) or "")
            self._last_saved[field] = value
            editor.setPlainText(value)
        self._load_citations()

    def _load_citations(self) -> None:
        getter = getattr(self.repository, "get_citations_for_paper", None)
        try:
            citations = getter(self.paper_id) if callable(getter) else {}
        except (sqlite3.Error, OSError, ValueError):
            citations = {}
        numbering = citation_number_map(
            tuple(
                citation
                for field in SUMMARY_FIELDS
                for citation in citations.get(field, [])
            )
        )
        for field, editor in self.editors.items():
            editor.set_citations(citations.get(field, []), numbering)

    def reload(self) -> None:
        self._timer.stop()
        self._loading = True
        self._dirty = False
        self._load()
        self._loading = False
        self.status.setText("Saved")
        self.status.setToolTip("")

    def _values(self) -> dict[str, str]:
        return {field: editor.toPlainText() for field, editor in self.editors.items()}

    def _changed(self) -> None:
        if self._loading:
            return
        self._dirty = self._values() != self._last_saved
        if self._dirty:
            self.status.setText("Saving…")
            self._timer.setInterval(750)
            self._timer.start()

    def save(self) -> bool:
        self._timer.stop()
        if not self._dirty:
            return True
        values = self._values()
        try:
            self.repository.save_for_paper(self.paper_id, **values)
        except (sqlite3.Error, OSError, ValueError) as error:
            self.status.setText("Could not save summary - retrying…")
            self.status.setToolTip(str(error))
            if self._dirty:
                self._timer.setInterval(2000)
                self._timer.start()
            return False
        self._last_saved = values
        self._dirty = False
        self._load_citations()
        self.status.setText("Saved")
        self.status.setToolTip("")
        return True


class InfoPanel(QWidget):
    paper_updated = Signal(int)

    def __init__(
        self,
        paper: Mapping[str, Any],
        parent=None,
        *,
        project_id: int | None = None,
        project_repository: Any = ProjectRepository,
        paper_repository: Any = PaperRepository,
        tag_repository: Any = TagRepository,
        collection_repository: Any = CollectionRepository,
    ) -> None:
        super().__init__(parent)
        self.paper = _mapping(paper)
        self.paper_id = int(self.paper["id"])
        self.paper_repository = paper_repository
        self.project_id = int(project_id) if project_id is not None else None
        self.project_repository = project_repository
        self.tag_repository = tag_repository
        self.collection_repository = collection_repository

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("sidebarScroll")
        scroll.setProperty("modernScroll", True)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(7)

        self.title = self._field(layout, "Title", str(self.paper.get("title") or ""))
        self.authors = self._field(layout, "Authors", str(self.paper.get("authors") or ""))
        self.year = self._field(layout, "Year", str(self.paper.get("year") or ""))
        self.doi = self._field(layout, "DOI", str(self.paper.get("doi") or ""))
        self.tags = self._field(layout, "Tags", "")

        layout.addWidget(QLabel("Collections"))
        self.collections = QListWidget()
        self.collections.setObjectName("collectionsList")
        self.collections.setMaximumHeight(150)
        layout.addWidget(self.collections)

        layout.addWidget(QLabel("Status"))
        self.status_combo = QComboBox()
        populate_reading_status_combo(
            self.status_combo,
            str(self.paper.get("status") or "Unread"),
        )
        layout.addWidget(self.status_combo)
        self.important = QCheckBox("Important")
        self.important.setChecked(bool(self.paper.get("is_important")))
        layout.addWidget(self.important)

        self.message = QLabel("")
        self.message.setObjectName("sidebarStatus")
        self.message.setWordWrap(True)
        save_button = QPushButton("Save paper info")
        save_button.setObjectName("sidebarPrimaryButton")
        set_widget_icon(
            save_button,
            "save",
            size=15,
            color="#FFFFFF",
            active_color="#FFFFFF",
        )
        save_button.clicked.connect(self.save)
        layout.addWidget(self.message)
        layout.addWidget(save_button)
        layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        self._load_relations()
        self._baseline_state = self._form_state()

    @staticmethod
    def _field(layout: QVBoxLayout, label: str, value: str) -> QLineEdit:
        layout.addWidget(QLabel(label))
        editor = QLineEdit(value)
        editor.setObjectName("infoEditor")
        layout.addWidget(editor)
        return editor

    def _load_relations(self) -> None:
        self.collections.clear()
        try:
            current_tags = self.tag_repository.get_for_paper(self.paper_id)
            self.tags.setText(", ".join(str(row["name"]) for row in current_tags))
            selected_ids = {
                int(row["id"])
                for row in self.collection_repository.get_for_paper(
                    self.paper_id, self.project_id
                )
            }
            for row in self.collection_repository.list_all(self.project_id):
                item = QListWidgetItem(str(row["name"]))
                item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if int(row["id"]) in selected_ids
                    else Qt.CheckState.Unchecked
                )
                self.collections.addItem(item)
        except (sqlite3.Error, OSError, ValueError) as error:
            self.message.setText(f"Could not load tags or collections: {error}")

    def _form_state(self) -> dict[str, Any]:
        return {
            "title": self.title.text(),
            "authors": self.authors.text(),
            "year": self.year.text(),
            "doi": self.doi.text(),
            "tags": self.tags.text(),
            "collection_ids": tuple(
                int(self.collections.item(index).data(Qt.ItemDataRole.UserRole))
                for index in range(self.collections.count())
                if self.collections.item(index).checkState() == Qt.CheckState.Checked
            ),
            "status": self.status_combo.currentText(),
            "important": self.important.isChecked(),
        }

    @property
    def is_dirty(self) -> bool:
        return self._form_state() != self._baseline_state

    def reload_from_paper(
        self,
        paper: Mapping[str, Any],
        *,
        preserve_dirty: bool = True,
    ) -> bool:
        paper_map = _mapping(paper)
        if int(paper_map.get("id", -1)) != self.paper_id:
            raise ValueError("Cannot replace Reader info with another paper.")
        if preserve_dirty and self.is_dirty:
            self.paper = paper_map
            self.message.setText(
                "Paper info changed in the library; unsaved Reader edits were kept."
            )
            return False

        self.paper = paper_map
        self.title.setText(str(paper_map.get("title") or ""))
        self.authors.setText(str(paper_map.get("authors") or ""))
        self.year.setText(
            "" if paper_map.get("year") is None else str(paper_map.get("year"))
        )
        self.doi.setText(str(paper_map.get("doi") or ""))
        status = str(paper_map.get("status") or "Unread")
        status_index = self.status_combo.findText(status)
        self.status_combo.setCurrentIndex(max(0, status_index))
        self.important.setChecked(bool(paper_map.get("is_important")))
        self.message.clear()
        self._load_relations()
        self._baseline_state = self._form_state()
        return True

    def save(self) -> bool:
        title = self.title.text().strip()
        if not title:
            self.message.setText("Title cannot be empty")
            return False
        year_text = self.year.text().strip()
        try:
            year = int(year_text) if year_text else None
        except ValueError:
            self.message.setText("Year must be a number")
            return False
        tags = [value.strip() for value in self.tags.text().split(",") if value.strip()]
        collection_ids = [
            int(self.collections.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.collections.count())
            if self.collections.item(index).checkState() == Qt.CheckState.Checked
        ]
        try:
            update_details = getattr(self.paper_repository, "update_details", None)
            if callable(update_details):
                changed = update_details(
                    self.paper_id,
                    title=title,
                    authors=self.authors.text().strip() or None,
                    year=year,
                    doi=self.doi.text().strip() or None,
                    status=self.status_combo.currentText(),
                    is_important=self.important.isChecked(),
                    tags=tags,
                    collection_ids=collection_ids,
                    project_id=self.project_id,
                )
                if not changed:
                    raise ValueError("The paper no longer exists.")
            else:  # Compatibility for lightweight injected test repositories.
                self.paper_repository.update_metadata(
                    self.paper_id,
                    title=title,
                    authors=self.authors.text().strip() or None,
                    year=year,
                    doi=self.doi.text().strip() or None,
                )
                self.paper_repository.set_status(
                    self.paper_id, self.status_combo.currentText()
                )
                self.paper_repository.set_important(
                    self.paper_id, self.important.isChecked()
                )
                self.tag_repository.replace_for_paper(self.paper_id, tags)
                self.collection_repository.replace_for_paper(
                    self.paper_id,
                    collection_ids,
                    project_id=self.project_id,
                )
            if self.project_id is not None:
                self.project_repository.set_status(
                    self.project_id,
                    self.paper_id,
                    self.status_combo.currentText(),
                )
        except (AttributeError, sqlite3.Error, OSError, ValueError) as error:
            self.message.setText(f"Could not save paper info: {error}")
            return False
        self.message.setText("Saved")
        self.paper.update(
            title=title,
            authors=self.authors.text().strip() or None,
            year=year,
            doi=self.doi.text().strip() or None,
            status=self.status_combo.currentText(),
            is_important=int(self.important.isChecked()),
        )
        self._baseline_state = self._form_state()
        self.paper_updated.emit(self.paper_id)
        return True


class ReaderSidebar(QFrame):
    close_requested = Signal()
    paper_updated = Signal(int)
    navigation_requested = Signal(object)
    translation_closed = Signal()
    transient_closed = Signal(str)
    selection_note_saved = Signal(int, str)
    translation_note_saved = Signal(int)
    ai_send_requested = Signal(int, str, str, str, str, object, object, object, bool)
    ai_stop_requested = Signal(str)
    ai_summarize_requested = Signal(str, str)
    ai_summarize_cancel_requested = Signal()
    citation_requested = Signal(object)
    ai_group_conversation_requested = Signal(int)
    ai_solo_conversation_requested = Signal(object)
    ai_group_paper_requested = Signal(int, int)
    ai_context_paper_requested = Signal(int)
    ai_conversation_deleted = Signal(int)
    project_search_paper_requested = Signal(int)

    SECTIONS = {"notes": 0, "summary": 1, "info": 2, "ai": 3, "search": 4}

    def __init__(
        self,
        paper: Mapping[str, Any],
        bridge: PdfReaderBridge,
        parent=None,
        *,
        project_id: int | None = None,
        note_repository: Any = NoteRepository,
        highlight_repository: Any = HighlightRepository,
        summary_repository: Any = SummaryRepository,
        paper_repository: Any = PaperRepository,
        tag_repository: Any = TagRepository,
        collection_repository: Any = CollectionRepository,
        settings_repository: Any = SettingsRepository,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("readerSidebar")
        self.setMinimumWidth(260)
        self.setMaximumWidth(480)
        paper_map = _mapping(paper)
        paper_id = int(paper_map["id"])
        self.settings_repository = settings_repository
        highlight_meanings = self._highlight_meanings()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(10)
        header = QHBoxLayout()
        self.header_layout = header
        self.heading_icon = QLabel()
        self.heading_icon.setPixmap(icon_pixmap("sticky-note", 18))
        self.heading = QLabel("Notes")
        self.heading.setObjectName("readerSidebarTitle")
        self.ai_new_chat_button = QPushButton()
        self.ai_new_chat_button.setObjectName("sidebarIconButton")
        set_widget_icon(
            self.ai_new_chat_button,
            "plus",
            size=15,
            tooltip="New chat",
        )
        self.ai_history_button = QPushButton()
        self.ai_history_button.setObjectName("sidebarIconButton")
        set_widget_icon(
            self.ai_history_button,
            "history",
            size=15,
            tooltip="Chat history",
        )
        self.ai_new_chat_button.hide()
        self.ai_history_button.hide()
        self.search_new_button = QPushButton()
        self.search_new_button.setObjectName("sidebarIconButton")
        set_widget_icon(
            self.search_new_button,
            "plus",
            size=15,
            tooltip="New AI Search",
        )
        self.search_new_button.clicked.connect(self._new_project_search)
        self.search_history_button = QPushButton()
        self.search_history_button.setObjectName("sidebarIconButton")
        set_widget_icon(
            self.search_history_button,
            "history",
            size=15,
            tooltip="AI Search history",
        )
        self.search_history_button.clicked.connect(self._show_project_search_history)
        self.search_new_button.hide()
        self.search_history_button.hide()
        close_button = QPushButton()
        close_button.setObjectName("sidebarIconButton")
        set_widget_icon(
            close_button,
            "x",
            size=15,
            tooltip="Close sidebar",
        )
        close_button.clicked.connect(self.close_requested)
        header.addWidget(self.heading_icon)
        header.addWidget(self.heading)
        header.addStretch()
        header.addWidget(self.ai_new_chat_button)
        header.addWidget(self.ai_history_button)
        header.addWidget(self.search_new_button)
        header.addWidget(self.search_history_button)
        header.addWidget(close_button)
        layout.addLayout(header)

        self.stack = QStackedWidget()
        self.notes = NotesPanel(
            paper_id,
            bridge,
            note_repository=note_repository,
            highlight_repository=highlight_repository,
            highlight_meanings=highlight_meanings,
        )
        self.summary = SummaryPanel(
            paper_id, repository=summary_repository
        )
        self.info = InfoPanel(
            paper_map,
            project_id=project_id,
            paper_repository=paper_repository,
            tag_repository=tag_repository,
            collection_repository=collection_repository,
        )
        self._summary_repository = summary_repository
        self.solo_ai = AIChatPanel(
            paper_id,
            project_id=project_id,
            repository=AIRepository,
            settings_repository=settings_repository,
            summary_repository=summary_repository,
        )
        self.ai = self.solo_ai
        self.search_host = QWidget()
        self.search_host.setObjectName("readerProjectSearchHost")
        self.search_layout = QVBoxLayout(self.search_host)
        self.search_layout.setContentsMargins(0, 0, 0, 0)
        self.search_layout.setSpacing(0)
        self.project_search_panel: ReaderSearchPanel | None = None
        self._project_search_controller: ProjectSearchConversationController | None = None
        self._ai_signal_connections: dict[AIChatPanel, list[tuple[Any, Any]]] = {}
        self.ai_header_provider = ProviderCombo(self)
        self.ai_header_provider.setObjectName("aiHeaderProvider")
        self.ai_header_provider.setFixedSize(116, 28)
        self.ai_header_provider.setIconSize(QSize(19, 19))
        for label, provider in PROVIDERS:
            self.ai_header_provider.addItem(
                provider_logo_icon(provider), label, provider
            )
        self.ai_header_provider.setMaxVisibleItems(6)
        self.ai_header_provider.view().setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.ai_header_provider.view().setIconSize(QSize(18, 18))
        self.ai_header_provider.view().setMinimumWidth(155)
        self.ai_header_provider.view().setMaximumHeight(6 * 30)
        self.ai_header_provider.hide()
        self._header_provider_panel: AIChatPanel | None = None
        self._header_provider_slot: Any | None = None
        self.ai_header_provider.currentIndexChanged.connect(
            self._header_provider_changed
        )
        header.insertWidget(0, self.ai_header_provider)
        self.search_header_provider = ProviderCombo(self)
        self.search_header_provider.setObjectName("aiHeaderProvider")
        self.search_header_provider.setFixedSize(116, 28)
        self.search_header_provider.setIconSize(QSize(19, 19))
        self.search_header_provider.setMaxVisibleItems(6)
        self.search_header_provider.view().setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.search_header_provider.view().setIconSize(QSize(18, 18))
        self.search_header_provider.view().setMinimumWidth(155)
        self.search_header_provider.view().setMaximumHeight(6 * 30)
        self.search_header_provider.hide()
        self.search_header_provider.currentIndexChanged.connect(
            self._search_header_provider_changed
        )
        header.insertWidget(1, self.search_header_provider)
        self.notes.navigation_requested.connect(self.navigation_requested)
        self.notes.translation_card.closed.connect(self.translation_closed.emit)
        self.notes.transient_closed.connect(self.transient_closed)
        self.notes.selection_note_saved.connect(self.selection_note_saved)
        self.notes.translation_note_saved.connect(self.translation_note_saved)
        self.info.paper_updated.connect(self.paper_updated)
        self._connect_ai_panel(self.ai)
        self._bind_header_provider(self.ai)
        self.summary.citation_requested.connect(self.citation_requested)
        self.ai_history_button.clicked.connect(
            lambda: self.ai.show_history_popup(self.ai_history_button)
        )
        self.ai_new_chat_button.clicked.connect(lambda: self.ai.new_chat())
        self.stack.addWidget(self.notes)
        self.stack.addWidget(self.summary)
        self.stack.addWidget(self.info)
        self.stack.addWidget(self.ai)
        self.stack.addWidget(self.search_host)
        layout.addWidget(self.stack, 1)
        self.show_section("notes")

    def set_project_search_controller(
        self, controller: ProjectSearchConversationController
    ) -> None:
        if (
            self.project_search_panel is not None
            and self.project_search_panel.controller is controller
        ):
            self._sync_search_header_provider()
            return
        previous_controller = self._project_search_controller
        if previous_controller is not None:
            try:
                previous_controller.engine_changed.disconnect(
                    self._sync_search_header_provider
                )
            except (RuntimeError, TypeError):
                pass
        if self.project_search_panel is not None:
            self.search_layout.removeWidget(self.project_search_panel)
            self.project_search_panel.deleteLater()
        self._project_search_controller = controller
        self.project_search_panel = ReaderSearchPanel(
            controller=controller,
            parent=self.search_host,
            presentation_mode="reader",
        )
        self.project_search_panel.paper_requested.connect(
            self.project_search_paper_requested
        )
        self.search_layout.addWidget(self.project_search_panel)
        controller.engine_changed.connect(self._sync_search_header_provider)
        self._sync_search_header_provider()

    def _sync_search_header_provider(self) -> None:
        controller = self._project_search_controller
        if controller is None:
            return
        blocker = QSignalBlocker(self.search_header_provider)
        self.search_header_provider.clear()
        for label, provider in controller.provider_options:
            self.search_header_provider.addItem(
                provider_logo_icon(provider), label, provider
            )
        self.search_header_provider.setCurrentIndex(
            max(0, self.search_header_provider.findData(controller.provider))
        )
        del blocker

    def _search_header_provider_changed(self, _index: int) -> None:
        controller = self._project_search_controller
        if controller is not None:
            controller.set_engine(
                str(self.search_header_provider.currentData() or "gemini")
            )

    def _new_project_search(self) -> None:
        if self.project_search_panel is not None:
            self.project_search_panel.new_search()

    def _show_project_search_history(self) -> None:
        if self.project_search_panel is not None:
            self.project_search_panel.history_popup.show_below(
                self.search_history_button
            )

    def _connect_ai_panel(self, panel: AIChatPanel) -> None:
        if panel in self._ai_signal_connections:
            return
        connections = [
            (panel.send_requested, lambda *args: self.ai_send_requested.emit(*args)),
            (panel.stop_requested, lambda *args: self.ai_stop_requested.emit(*args)),
            (
                panel.summarize_requested,
                lambda *args: self.ai_summarize_requested.emit(*args),
            ),
            (
                panel.summarize_cancel_requested,
                lambda *args: self.ai_summarize_cancel_requested.emit(*args),
            ),
            (panel.citation_requested, lambda value: self.citation_requested.emit(value)),
            (
                panel.group_conversation_requested,
                lambda value: self.ai_group_conversation_requested.emit(value),
            ),
            (
                panel.solo_conversation_requested,
                lambda value: self.ai_solo_conversation_requested.emit(value),
            ),
            (
                panel.group_paper_requested,
                lambda conversation_id, paper_id: self.ai_group_paper_requested.emit(
                    conversation_id, paper_id
                ),
            ),
            (
                panel.context_paper_requested,
                lambda value: self.ai_context_paper_requested.emit(value),
            ),
            (
                panel.conversation_deleted,
                lambda value: self.ai_conversation_deleted.emit(value),
            ),
        ]
        for signal, slot in connections:
            signal.connect(slot)
        self._ai_signal_connections[panel] = connections

    def _disconnect_ai_panel(self, panel: AIChatPanel) -> None:
        for signal, slot in self._ai_signal_connections.pop(panel, []):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _bind_header_provider(self, panel: AIChatPanel) -> None:
        """Mirror provider state without moving panel-owned Qt widgets."""
        previous = self._header_provider_panel
        slot = self._header_provider_slot
        if previous is not None and slot is not None:
            try:
                previous.provider.currentIndexChanged.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._header_provider_panel = panel

        def sync_from_panel(_index: int, *, source: AIChatPanel = panel) -> None:
            if source is self._header_provider_panel:
                self._sync_header_provider_from_panel()

        self._header_provider_slot = sync_from_panel
        panel.provider.currentIndexChanged.connect(sync_from_panel)
        self._sync_header_provider_from_panel()

    def _sync_header_provider_from_panel(self) -> None:
        panel = self._header_provider_panel
        if panel is None:
            return
        provider = panel.current_provider()
        index = self.ai_header_provider.findData(provider)
        blocker = QSignalBlocker(self.ai_header_provider)
        for item_index in range(panel.provider.count()):
            target = self.ai_header_provider.findData(
                panel.provider.itemData(item_index)
            )
            if target >= 0:
                self.ai_header_provider.setItemText(
                    target, panel.provider.itemText(item_index)
                )
        self.ai_header_provider.setCurrentIndex(max(0, index))
        self.ai_header_provider.setToolTip(panel.provider.toolTip())
        del blocker

    def _header_provider_changed(self, _index: int) -> None:
        panel = self._header_provider_panel
        if panel is None:
            return
        provider = self.ai_header_provider.currentData()
        index = panel.provider.findData(provider)
        if index >= 0 and index != panel.provider.currentIndex():
            panel.provider.setCurrentIndex(index)
        self.ai_header_provider.setToolTip(panel.provider.toolTip())

    def sync_ai_header_provider(self) -> None:
        """Refresh the header proxy after settings change under a signal blocker."""
        self._sync_header_provider_from_panel()

    def use_ai_panel(self, panel: AIChatPanel) -> None:
        """Swap only the AI page; Notes/Summary/Info remain paper-local."""
        if panel is self.ai:
            return
        ai_was_current = self.stack.currentWidget() is self.ai
        previous = self.ai
        self._disconnect_ai_panel(previous)
        self.stack.removeWidget(previous)

        self.ai = panel
        self.stack.insertWidget(self.SECTIONS["ai"], panel)
        self._connect_ai_panel(panel)
        self._bind_header_provider(panel)
        if ai_was_current:
            self.stack.setCurrentWidget(panel)

    def restore_solo_ai(self) -> None:
        self.use_ai_panel(self.solo_ai)

    def show_section(self, section: str) -> None:
        if section not in self.SECTIONS:
            raise ValueError(f"Unknown Reader sidebar section: {section}")
        self.stack.setCurrentIndex(self.SECTIONS[section])
        self.heading.setText("AI Search" if section == "search" else section.title())
        self.heading_icon.setPixmap(
            icon_pixmap(
                {
                    "notes": "sticky-note",
                    "summary": "notebook-text",
                    "info": "info",
                    "ai": "sparkles",
                    "search": "search",
                }[section],
                18,
            )
        )
        is_ai = section == "ai"
        is_search = section == "search"
        self.heading_icon.setVisible(not (is_ai or is_search))
        self.heading.setVisible(not (is_ai or is_search))
        self.ai_header_provider.setVisible(is_ai)
        self.ai_new_chat_button.setVisible(is_ai)
        self.ai_history_button.setVisible(is_ai)
        self.search_header_provider.setVisible(is_search)
        self.search_new_button.setVisible(is_search)
        self.search_history_button.setVisible(is_search)
        if section == "notes":
            self.notes.set_highlight_meanings(self._highlight_meanings())

    def _highlight_meanings(self) -> dict[str, str]:
        try:
            value = self.settings_repository.get_json(
                "highlight_meanings", DEFAULT_HIGHLIGHT_MEANINGS
            )
        except (sqlite3.Error, OSError, ValueError, json.JSONDecodeError):
            value = DEFAULT_HIGHLIGHT_MEANINGS
        if not isinstance(value, dict):
            value = DEFAULT_HIGHLIGHT_MEANINGS
        return {
            color: str(value.get(color) or default)
            for color, default in DEFAULT_HIGHLIGHT_MEANINGS.items()
        }

    def begin_selection_note(self, payload: Mapping[str, Any]) -> bool:
        self.show_section("notes")
        return self.notes.begin_selection_note(payload)

    def begin_translation(
        self, text: str, location: dict[str, Any], request_id: str = ""
    ) -> None:
        self.show_section("notes")
        self.notes.translation_card.begin(text, location, request_id)

    def set_translation_result(self, text: str) -> None:
        self.notes.translation_card.set_result(text)

    def set_translation_error(self, message: str) -> None:
        self.notes.translation_card.set_error(message)

    def refresh_highlights(self, *_args: Any) -> None:
        self.notes.reload_cards()

    def cancel_transient(self, request_id: str) -> None:
        self.notes.cancel_transient(request_id)

    def refresh_for_document(self) -> None:
        self.notes.refresh_for_document()

    def open_note(self, note_id: int) -> bool:
        self.show_section("notes")
        return self.notes.open_note(note_id)

    def attach_ai_selection(self, payload: Mapping[str, Any]) -> None:
        self.show_section("ai")
        self.ai.attach_selection(payload)

    def new_note(self) -> None:
        self.show_section("notes")
        self.notes.create_manual_note()

    def refresh_paper(self, paper: Mapping[str, Any]) -> bool:
        return self.info.reload_from_paper(paper, preserve_dirty=True)

    def flush_pending_saves(self) -> bool:
        notes_ok = self.notes.flush_pending_saves()
        summary_ok = self.summary.save()
        info_ok = not self.info.is_dirty or self.info.save()
        return notes_ok and summary_ok and info_ok
