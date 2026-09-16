from __future__ import annotations

import html
import logging
from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from app.database.paper_repository import PaperRepository
from app.database.search_repository import SearchRepository
from app.ui.icons import app_icon, set_widget_icon


LOGGER = logging.getLogger(__name__)

SOURCE_LABELS = {
    "metadata": "Paper",
    "pdf": "PDF",
    "note": "Note",
    "highlight": "Highlight",
    "summary": "Summary",
}


class GlobalSearchDialog(QDialog):
    """Non-modal, debounced search over every indexed library source."""

    result_activated = Signal(object)
    DEBOUNCE_MS = 250
    RESULT_LIMIT = 100

    def __init__(
        self,
        parent=None,
        *,
        project_id: int | None = None,
        project_name: str = "",
        repository: Any = SearchRepository,
        paper_repository: Any = PaperRepository,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.paper_repository = paper_repository
        self.project_id = int(project_id) if project_id is not None else None
        self.project_name = str(project_name).strip()
        self.setWindowTitle("Search Current Project")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setModal(False)
        self.resize(760, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        title = QLabel("Search current project")
        title.setObjectName("dialogTitle")
        description = QLabel(
            "Find metadata, PDF text, notes, highlights, and summaries."
        )
        description.setObjectName("dialogDescription")
        layout.addWidget(title)
        layout.addWidget(description)

        self.search_input = QLineEdit()
        self.search_input.setObjectName("globalSearchInput")
        self.search_input.setPlaceholderText("Search all indexed content...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.addAction(
            app_icon("search"), QLineEdit.ActionPosition.LeadingPosition
        )
        layout.addWidget(self.search_input)

        self.results = QListWidget()
        self.results.setObjectName("globalSearchResults")
        self.results.setAlternatingRowColors(False)
        self.results.setIconSize(QSize(18, 18))
        self.results.itemDoubleClicked.connect(self._activate_item)
        layout.addWidget(self.results, 1)

        footer = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setObjectName("dialogStatus")
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        self.open_button = QPushButton("Open result")
        self.open_button.setObjectName("primaryButton")
        set_widget_icon(
            self.open_button,
            "external-link",
            size=16,
            color="#FFFFFF",
            active_color="#FFFFFF",
        )
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._activate_current)
        footer.addWidget(close_button)
        footer.addWidget(self.open_button)
        layout.addLayout(footer)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(self.DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._run_search)
        self.search_input.textChanged.connect(self._schedule_search)
        self.search_input.returnPressed.connect(self._activate_from_input)
        self.results.currentItemChanged.connect(self._current_item_changed)

        self._show_idle_status()
        self._apply_style()

    def set_query_and_focus(self, query: str = "") -> None:
        """Show the reusable window, update its query, and focus the input."""
        normalized_query = str(query)
        if normalized_query != self.search_input.text():
            self.search_input.setText(normalized_query)
        if normalized_query.strip():
            self._search_timer.start()
        else:
            self._search_timer.stop()
            self.results.clear()
            self._show_idle_status()

        self.show()
        self.raise_()
        self.activateWindow()
        self.search_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_input.selectAll()

    def present(self, query: str = "") -> None:
        """Compatibility alias for callers created before the public API name."""
        self.set_query_and_focus(query)

    def _schedule_search(self, _text: str = "") -> None:
        self._search_timer.start()

    def _run_search(self) -> None:
        query = self.search_input.text().strip()
        self.results.clear()
        self.open_button.setEnabled(False)
        if not query:
            self._show_idle_status()
            return

        try:
            rows = self.repository.search(
                query,
                project_id=self.project_id,
                limit=self.RESULT_LIMIT,
            )
        except Exception as error:  # UI boundary: keep repository failures recoverable.
            LOGGER.exception("Global library search failed")
            self.status_label.setText("Search unavailable. Please try again.")
            self.status_label.setToolTip(_safe_tooltip(str(error)))
            return

        paper_cache: dict[int, dict[str, Any]] = {}
        for row in rows:
            result = _mapping(row)
            paper_id = _as_int(result.get("paper_id"))
            paper = paper_cache.get(paper_id)
            if paper is None:
                paper = self._load_paper(paper_id)
                paper_cache[paper_id] = paper

            paper_title = str(
                paper.get("title") or result.get("title") or "Untitled paper"
            )
            result["paper_title"] = paper_title
            source_label = _source_label(result)
            snippet = _plain_snippet(result.get("snippet"))

            lines = (paper_title, source_label, snippet)
            item = QListWidgetItem("\n".join(line for line in lines if line))
            item.setIcon(
                app_icon(
                    {
                        "metadata": "file-text",
                        "pdf": "file-text",
                        "note": "sticky-note",
                        "highlight": "highlighter",
                        "summary": "notebook-text",
                    }.get(str(result.get("source_type") or "metadata"), "file-text")
                )
            )
            item.setData(Qt.ItemDataRole.UserRole, result)
            item.setToolTip(_result_tooltip(paper_title, source_label, snippet))
            item.setSizeHint(item.sizeHint() + QSize(0, 20))
            self.results.addItem(item)

        count = self.results.count()
        hint = self._indexing_hint()
        if count:
            message = f"{count} result{'s' if count != 1 else ''}"
            self.results.setCurrentRow(0)
        else:
            message = f'No results for "{query}".'
        self.status_label.setText(_join_status(message, hint))
        self.status_label.setToolTip("")

    def _load_paper(self, paper_id: int) -> dict[str, Any]:
        if paper_id < 1:
            return {}
        try:
            return _mapping(self.paper_repository.get_paper_by_id(paper_id))
        except Exception:
            LOGGER.exception("Could not load paper %s for a search result", paper_id)
            return {}

    def _indexing_hint(self) -> str:
        list_states = getattr(self.repository, "list_index_states", None)
        if not callable(list_states):
            return ""
        try:
            states = list_states(project_id=self.project_id)
        except Exception:
            LOGGER.debug("Could not read background indexing state", exc_info=True)
            return ""

        statuses = [str(_mapping(row).get("status") or "") for row in states]
        active_count = sum(status in {"pending", "indexing"} for status in statuses)
        if active_count:
            noun = "paper" if active_count == 1 else "papers"
            return f"Indexing {active_count} {noun} in the background."
        error_count = statuses.count("error")
        if error_count:
            noun = "paper" if error_count == 1 else "papers"
            return f"{error_count} {noun} could not be indexed."
        return ""

    def _show_idle_status(self) -> None:
        self.status_label.setText(
            _join_status("Type a query to search.", self._indexing_hint())
        )
        self.status_label.setToolTip("")

    def _activate_from_input(self) -> None:
        if self._search_timer.isActive():
            self._search_timer.stop()
            self._run_search()
        self._activate_current()

    def _activate_current(self) -> None:
        current = self.results.currentItem()
        if current is not None:
            self._activate_item(current)

    def _activate_item(self, item: QListWidgetItem, *_args: Any) -> None:
        result = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(result, dict):
            self.result_activated.emit(dict(result))
            self.hide()

    def _current_item_changed(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        self.open_button.setEnabled(current is not None)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QDialog { background: #F7F8FA; }
            #dialogTitle { font-size: 21px; font-weight: 700; color: #202328; }
            #dialogDescription, #dialogStatus { color: #737983; font-size: 12px; }
            #globalSearchInput {
                min-height: 42px; background: white; border: 1px solid #DDE1E6;
                border-radius: 8px; padding: 0 12px; font-size: 14px;
            }
            #globalSearchInput:focus { border-color: #8D949E; }
            #globalSearchResults {
                background: white; border: 1px solid #E1E4E8; border-radius: 9px;
                padding: 5px; outline: none; font-size: 13px;
            }
            #globalSearchResults::item { border-radius: 7px; padding: 9px; }
            #globalSearchResults::item:selected { background: #E8EDF4; color: #202328; }
            QPushButton {
                min-height: 34px; padding: 0 14px; border: 1px solid #DDE1E6;
                border-radius: 7px; background: white; color: #34383E;
            }
            #primaryButton { background: #25282D; color: white; border: none; }
            """
        )


def _mapping(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return dict(row)
    keys = getattr(row, "keys", None)
    return {key: row[key] for key in keys()} if callable(keys) else {}


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _source_label(result: Mapping[str, Any]) -> str:
    source_type = str(result.get("source_type") or "metadata")
    label = SOURCE_LABELS.get(source_type, source_type.title())
    page_number = _as_int(result.get("page_number"))
    if source_type == "pdf" and page_number:
        return f"{label} page {page_number}"
    if page_number and source_type in {"note", "highlight"}:
        return f"{label} - page {page_number}"
    return label


def _plain_snippet(value: Any) -> str:
    return " ".join(str(value or "").split())


def _safe_tooltip(value: str) -> str:
    return f"<qt>{html.escape(value, quote=True)}</qt>"


def _result_tooltip(title: str, source: str, snippet: str) -> str:
    safe_title = html.escape(title, quote=True)
    safe_source = html.escape(source, quote=True)
    safe_snippet = html.escape(snippet, quote=True)
    return f"<qt><b>{safe_title}</b><br>{safe_source}<br>{safe_snippet}</qt>"


def _join_status(message: str, hint: str) -> str:
    return f"{message} {hint}".strip()
