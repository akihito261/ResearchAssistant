from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMainWindow, QStackedWidget


class ReaderWorkspaceWindow(QMainWindow):
    """The single native Reader window; paper Readers live as persistent pages."""

    close_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Research Assistant")
        self.resize(1500, 950)
        self.reader_stack = QStackedWidget()
        self.reader_stack.setObjectName("readerWorkspaceStack")
        self.setCentralWidget(self.reader_stack)
        self._readers: dict[int, Any] = {}

    def add_reader(self, paper_id: int, reader: Any) -> None:
        paper_id = int(paper_id)
        if paper_id in self._readers:
            return
        self._readers[paper_id] = reader
        self.reader_stack.addWidget(reader)

    def remove_reader(self, paper_id: int) -> None:
        reader = self._readers.pop(int(paper_id), None)
        if reader is not None:
            self.reader_stack.removeWidget(reader)

    def set_active(self, paper_id: int) -> Any | None:
        reader = self._readers.get(int(paper_id))
        if reader is None:
            return None
        self.reader_stack.setCurrentWidget(reader)
        title = str(reader.paper["title"] or "Research Assistant")
        self.setWindowTitle(title)
        return reader

    def reader(self, paper_id: int) -> Any | None:
        return self._readers.get(int(paper_id))

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.close_requested.emit()
