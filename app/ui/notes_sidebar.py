from __future__ import annotations

import logging
import sqlite3

from PySide6.QtCore import QSignalBlocker, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from app.database.note_repository import NoteRepository


LOGGER = logging.getLogger(__name__)


class NotesSidebar(QFrame):
    """A compact, auto-saving scratchpad for one paper."""

    save_failed = Signal(str)

    def __init__(
        self,
        paper_id: int,
        parent=None,
        *,
        repository: type[NoteRepository] = NoteRepository,
        autosave_delay_ms: int = 700,
        retry_delay_ms: int = 1500,
    ) -> None:
        super().__init__(parent)

        self.paper_id = paper_id
        self.repository = repository
        self._last_saved_content = ""
        self._dirty = False
        self._available = True
        self._autosave_delay_ms = max(0, autosave_delay_ms)
        self._retry_delay_ms = max(100, retry_delay_ms)
        self._retry_attempt = 0

        self.setObjectName("notesSidebar")
        self.setMinimumWidth(300)
        self.setMaximumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = QLabel("Notes")
        title.setObjectName("notesTitle")

        self.editor = QPlainTextEdit()
        self.editor.setObjectName("notesEditor")
        self.editor.setPlaceholderText(
            "Write observations, questions, or ideas about this paper…"
        )
        self.editor.setTabChangesFocus(False)

        self.status_label = QLabel("Saved")
        self.status_label.setObjectName("notesStatus")

        self.discard_button = QPushButton("Discard unsaved changes")
        self.discard_button.setObjectName("notesDiscardButton")
        self.discard_button.setToolTip(
            "Restore the last saved note and discard the current edits"
        )
        self.discard_button.clicked.connect(self.discard_unsaved_changes)
        self.discard_button.hide()

        status_layout = QHBoxLayout()
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(8)
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        status_layout.addWidget(self.discard_button)

        layout.addWidget(title)
        layout.addWidget(self.editor, 1)
        layout.addLayout(status_layout)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_note)

        self._load_note()
        self.editor.textChanged.connect(self._on_text_changed)

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def focus_editor(self) -> None:
        if self.editor.isEnabled():
            self.editor.setFocus()

    def flush_pending_save(self) -> bool:
        self._save_timer.stop()
        return self._save_note(schedule_retry=False)

    def retry_pending_save(self) -> None:
        if self._available and self._dirty and not self._save_timer.isActive():
            self._schedule_retry()

    def discard_unsaved_changes(self) -> None:
        if not self._dirty:
            return

        self._save_timer.stop()
        blocker = QSignalBlocker(self.editor)
        self.editor.setPlainText(self._last_saved_content)
        del blocker

        self._dirty = False
        self._retry_attempt = 0
        self.status_label.setText("Unsaved changes discarded")
        self.status_label.setToolTip("")
        self.discard_button.hide()

    def _load_note(self) -> None:
        try:
            content = self.repository.get_for_paper(self.paper_id)
        except (sqlite3.Error, OSError) as error:
            LOGGER.exception("Could not load note for paper %s", self.paper_id)
            self._available = False
            self.editor.setEnabled(False)
            self.status_label.setText("Could not load this note")
            self.status_label.setToolTip(str(error))
            return

        self._last_saved_content = content
        self.editor.setPlainText(content)

    def _on_text_changed(self) -> None:
        if not self._available:
            return

        self._dirty = self.editor.toPlainText() != self._last_saved_content
        if not self._dirty:
            self._save_timer.stop()
            self._retry_attempt = 0
            self.status_label.setText("Saved")
            self.status_label.setToolTip("")
            self.discard_button.hide()
            return

        self._retry_attempt = 0
        self.status_label.setText("Saving…")
        self.discard_button.hide()
        self._save_timer.start(self._autosave_delay_ms)

    def _save_note(self, *, schedule_retry: bool = True) -> bool:
        if not self._available or not self._dirty:
            return True

        content = self.editor.toPlainText()
        try:
            self.repository.save_for_paper(self.paper_id, content)
        except (sqlite3.Error, OSError) as error:
            LOGGER.exception("Could not save note for paper %s", self.paper_id)
            if schedule_retry:
                self.status_label.setText("Could not save — retrying…")
                self._schedule_retry()
            else:
                self.status_label.setText("Could not save — close cancelled")
            self.status_label.setToolTip(str(error))
            self.discard_button.show()
            self.save_failed.emit(str(error))
            return False

        self._last_saved_content = content
        self._dirty = False
        self._retry_attempt = 0
        self.status_label.setText("Saved")
        self.status_label.setToolTip("")
        self.discard_button.hide()
        return True

    def _schedule_retry(self) -> None:
        delay = min(
            self._retry_delay_ms * (2 ** min(self._retry_attempt, 4)),
            30_000,
        )
        self._retry_attempt += 1
        self._save_timer.start(delay)
