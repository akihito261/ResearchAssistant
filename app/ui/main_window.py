from __future__ import annotations

import logging
import sqlite3
from time import monotonic
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRect, QSignalBlocker, Qt, QThreadPool, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QCloseEvent,
    QDesktopServices,
    QIcon,
    QKeySequence,
    QPainter,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QInputDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyleOption,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import app.database.database as database_module
from app.database.ai_repository import AIRepository
from app.database.collection_repository import CollectionRepository
from app.database.highlight_repository import HighlightRepository
from app.database.note_repository import NoteRepository
from app.database.paper_repository import PAPER_SORTS, PaperRepository
from app.database.project_repository import ProjectRepository
from app.database.research_summary_repository import ResearchSummaryRepository
from app.database.settings_repository import SettingsRepository
from app.database.summary_repository import SummaryRepository
from app.database.tag_repository import TagRepository
from app.logging_config import configure_logging
from app.services.background_worker import FunctionWorker
from app.services.ai_chat_service import AIChatService
from app.services.backup_service import BackupResult, create_backup
from app.services.export_service import (
    export_bibtex,
    export_library_csv,
    export_notes_markdown,
    export_summaries_markdown,
)
from app.services.library_path_service import MANAGED_PAPERS_ROOT, resolve_paper_path
from app.services.paper_import_service import (
    DuplicateCandidate,
    PaperImportMetadata,
    PaperImportService,
)
from app.services.paper_library_service import DeleteOutcome, PaperLibraryService
from app.services.pdf_service import PdfInspectionResult, calculate_sha256
from app.services.search_index_service import IndexingOutcome, SearchIndexService
from app.translation.base import TranslationProvider
from app.translation.mymemory_provider import MyMemoryTranslationProvider
from app.ui.global_search_dialog import GlobalSearchDialog
from app.ui.ai_chat_panel import AIChatPanel
from app.ui.icons import (
    DANGER_ICON_COLOR,
    IMPORTANT_ICON_COLOR,
    READING_STATUS_STYLES,
    app_icon,
    reading_status_icon,
    set_action_icon,
    set_widget_icon,
)
from app.ui.pdf_reader_window import PdfReaderWindow
from app.ui.project_ai_search_dialog import (
    ProjectAISearchDialog,
    ProjectSearchConversationController,
)
from app.ui.reader_workspace_window import ReaderWorkspaceWindow
from app.ui.review_paper_dialog import ReviewPaperDialog
from app.ui.settings_dialog import SettingsDialog
from app.ui.summary_preview_dialog import SummaryPreviewDialog
from app.ui.taxonomy_dialog import CollectionSelectionDialog, TaxonomyManagerDialog
from app.ui.ui_styles import apply_ui_palette


LOGGER = logging.getLogger(__name__)

TABLE_COLUMNS = ("Paper", "Authors", "Year", "Tags", "Collections", "Status")
HEADER_SORTS = {0: "title", 1: "author", 2: "year"}
SORT_LABELS = {
    "date_added": "Date added",
    "updated": "Recently updated",
    "title": "Title",
    "author": "Author",
    "year": "Year",
}
READING_SECONDS_THRESHOLD = 120
READING_INTERACTION_THRESHOLD = 3
READING_RECENT_INTERACTION_SECONDS = 35
READING_TICK_SECONDS = 5


class _ReadingStatusDelegate(QStyledItemDelegate):
    """Draw a compact semantic status chip while preserving row selection."""

    def paint(self, painter: QPainter, option, index) -> None:
        status = str(index.data(Qt.ItemDataRole.DisplayRole) or "Unread")
        visual = READING_STATUS_STYLES.get(
            status, READING_STATUS_STYLES["Unread"]
        )
        base = QStyleOptionViewItem(option)
        self.initStyleOption(base, index)
        base.text = ""
        base.icon = QIcon()
        style = option.widget.style() if option.widget is not None else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem,
            base,
            painter,
            option.widget,
        )

        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            text_width = option.fontMetrics.horizontalAdvance(status)
            chip_width = min(option.rect.width() - 12, text_width + 45)
            chip_height = min(24, option.rect.height() - 8)
            chip = QRect(
                option.rect.left() + 6,
                option.rect.center().y() - chip_height // 2,
                max(1, chip_width),
                max(1, chip_height),
            )
            painter.setPen(QColor(str(visual["border"])))
            painter.setBrush(QColor(str(visual["tint"])))
            painter.drawRoundedRect(chip, 7, 7)

            icon_rect = QRect(chip.left() + 8, chip.center().y() - 8, 16, 16)
            reading_status_icon(status).paint(painter, icon_rect)
            text_rect = chip.adjusted(30, 0, -8, 0)
            painter.setPen(QColor(str(visual["text"])))
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                status,
            )
        finally:
            painter.restore()


class _ElidedLabel(QLabel):
    """Single-line label that keeps long research metadata inside its column."""

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        option = QStyleOption()
        option.initFrom(self)
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_Widget, option, painter, self
        )
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        text = self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideRight, max(0, self.width())
        )
        painter.drawText(
            self.contentsRect(),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            text,
        )


class _PaperCell(QWidget):
    """Compact research-entry presentation over the unchanged table row model."""

    def __init__(self, paper: Mapping[str, Any], parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("paperTableCell")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 6, 8, 6)
        layout.setSpacing(3)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(5)
        if bool(paper.get("is_important")):
            star = QLabel()
            star.setPixmap(
                app_icon(
                    "star-filled",
                    color=IMPORTANT_ICON_COLOR,
                    active_color=IMPORTANT_ICON_COLOR,
                    selected_color=IMPORTANT_ICON_COLOR,
                ).pixmap(14, 14)
            )
            star.setFixedSize(15, 15)
            star.setToolTip("Important")
            title_row.addWidget(star)
        title = _ElidedLabel(str(paper.get("title") or "Untitled paper"))
        title.setObjectName("paperTitleText")
        title.setToolTip(title.text())
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        title_row.addWidget(title, 1)
        layout.addLayout(title_row)

        authors = " ".join(str(paper.get("authors") or "").split())
        metadata = _ElidedLabel(authors or "Author information unavailable")
        metadata.setObjectName("paperMetadataText")
        metadata.setToolTip(authors)
        metadata.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(metadata)

        chips: list[tuple[str, str]] = []
        for kind, raw in (
            ("tag", paper.get("tags")),
            ("collection", paper.get("collections")),
        ):
            chips.extend(
                (kind, value.strip())
                for value in str(raw or "").split(",")
                if value.strip()
            )
        if chips:
            chip_row = QHBoxLayout()
            chip_row.setContentsMargins(0, 0, 0, 0)
            chip_row.setSpacing(4)
            visible = chips[:3]
            for kind, text in visible:
                chip = _ElidedLabel(text)
                chip.setObjectName(
                    "paperTagChip" if kind == "tag" else "paperCollectionChip"
                )
                chip.setToolTip(text)
                chip.setMinimumWidth(0)
                chip.setMaximumWidth(118)
                chip_row.addWidget(chip)
            if len(chips) > len(visible):
                more = QLabel(f"+{len(chips) - len(visible)}")
                more.setObjectName("paperMoreChip")
                more.setToolTip(", ".join(text for _kind, text in chips[len(visible) :]))
                chip_row.addWidget(more)
            chip_row.addStretch()
            layout.addLayout(chip_row)


@dataclass(frozen=True, slots=True)
class _ImportInspectionOutcome:
    inspection: PdfInspectionResult | None = None
    exact_duplicate: dict[str, Any] | None = None
    potential_duplicates: tuple[DuplicateCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class _ImportCommitOutcome:
    paper_id: int


class _ConfiguredTranslationProvider(TranslationProvider):
    """Read the current target language for every background request."""

    def __init__(self, settings_repository: Any = SettingsRepository) -> None:
        self.provider = MyMemoryTranslationProvider()
        self.settings_repository = settings_repository

    def translate(
        self,
        text: str,
        *,
        source_language: str = "en",
        target_language: str = "vi",
    ) -> str:
        try:
            configured_target = self.settings_repository.get(
                "translation_target",
                target_language,
            )
        except sqlite3.Error:
            LOGGER.exception("Could not read the configured translation language")
            configured_target = target_language
        return self.provider.translate(
            text,
            source_language=source_language,
            target_language=configured_target or target_language,
        )


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        paper_repository: type[PaperRepository] = PaperRepository,
        collection_repository: type[CollectionRepository] = CollectionRepository,
        tag_repository: type[TagRepository] = TagRepository,
        summary_repository: type[SummaryRepository] = SummaryRepository,
        settings_repository: type[SettingsRepository] = SettingsRepository,
        project_repository: type[ProjectRepository] = ProjectRepository,
        import_service: PaperImportService | None = None,
        library_service: PaperLibraryService | None = None,
        index_service: SearchIndexService | None = None,
        start_background_tasks: bool = True,
    ) -> None:
        super().__init__()
        try:
            configure_logging()
        except OSError:
            LOGGER.exception("File logging could not be initialized")

        self.paper_repository = paper_repository
        self.collection_repository = collection_repository
        self.tag_repository = tag_repository
        self.summary_repository = summary_repository
        self.settings_repository = settings_repository
        self.project_repository = project_repository
        self.index_service = index_service or SearchIndexService()
        self.import_service = import_service or PaperImportService(
            repository=paper_repository,
            index_service=self.index_service,
        )
        self.library_service = library_service or PaperLibraryService(
            repository=paper_repository
        )

        self._pdf_readers: dict[int, PdfReaderWindow] = {}
        self._workspace_paper_ids: list[int] = []
        self._active_paper_id: int | None = None
        self._workspace_sidebar_visible = False
        self._workspace_sidebar_section = "notes"
        self._workspace_transitioning = False
        self._app_closing = False
        self._reader_workspace = ReaderWorkspaceWindow()
        self._reader_workspace.close_requested.connect(
            self._close_reader_workspace
        )
        self._ai_panel_parking = QWidget(self._reader_workspace)
        self._ai_panel_parking.hide()
        self._ai_group_paper_ids: list[int] = []
        self._ai_group_panel: AIChatPanel | None = None
        self._ai_group_conversation_id: int | None = None
        self._ai_group_aliases: dict[int, str] = {}
        self._ai_group_members: list[dict[str, Any]] = []
        self._active_ai_conversation_id: int | None = None
        self._active_ai_conversation_type = "solo"
        self._workers: set[FunctionWorker] = set()
        self._global_search_dialog: GlobalSearchDialog | None = None
        self._project_ai_search_dialog: ProjectAISearchDialog | None = None
        self._project_search_controller: ProjectSearchConversationController | None = None
        self._import_in_progress = False
        self._rows: dict[int, dict[str, Any]] = {}
        self._status_filter: str | None = None
        self._important_filter: bool | None = None
        self._year_filter: int | None = None
        self._collection_filter: int | None = None
        self._tag_filter: int | None = None
        self._sort_by = "date_added"
        self._sort_descending = True
        self._scope_title = "Library"
        self._projects: list[dict[str, Any]] = []
        self.current_project_id = self._initial_project_id()
        self._pending_workspace_active_id: int | None = None
        self._last_reading_interaction: dict[int, float] = {}
        self._research_summary_workers: dict[int, FunctionWorker] = {}

        self.setWindowTitle("Research Assistant")
        self.resize(1460, 880)
        self._setup_actions()
        self._setup_ui()
        self._reload_taxonomy()
        self.load_papers()
        self._apply_style()

        self._load_project_workspace()
        self._reading_timer = QTimer(self)
        self._reading_timer.setInterval(READING_TICK_SECONDS * 1000)
        self._reading_timer.timeout.connect(self._record_active_reading_tick)
        self._reading_timer.start()

        if start_background_tasks:
            QTimer.singleShot(0, self._start_background_index)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _initial_project_id(self) -> int:
        projects = self.project_repository.list_all()
        if not projects:
            project = self.project_repository.create("Default Project")
            projects = [project]
        configured = self.settings_repository.get("current_project_id", "") or ""
        try:
            selected = int(configured)
        except (TypeError, ValueError):
            selected = int(projects[0]["id"])
        valid = {int(project["id"]) for project in projects}
        if selected not in valid:
            selected = int(projects[0]["id"])
        return selected

    def _reload_projects(self) -> None:
        self._projects = self.project_repository.list_all()
        blocker = QSignalBlocker(self.project_selector)
        self.project_selector.clear()
        for project in self._projects:
            self.project_selector.addItem(
                str(project["name"]), int(project["id"])
            )
            self.project_selector.setItemData(
                self.project_selector.count() - 1,
                str(project["name"]),
                Qt.ItemDataRole.ToolTipRole,
            )
        index = self.project_selector.findData(self.current_project_id)
        self.project_selector.setCurrentIndex(max(0, index))
        del blocker

    def _create_project(self) -> None:
        name, accepted = QInputDialog.getText(
            self, "Create Project", "Project name:"
        )
        if not accepted:
            return
        try:
            project = self.project_repository.create(name)
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error("Could not create the project", error)
            return
        self._reload_projects()
        self._switch_project(int(project["id"]))

    def _show_project_menu(self) -> None:
        menu = QMenu(self)
        rename = menu.addAction("Rename project…", self._rename_current_project)
        set_action_icon(rename, "pencil")
        delete = menu.addAction("Delete project…", self._delete_current_project)
        set_action_icon(delete, "trash", color=DANGER_ICON_COLOR)
        delete.setEnabled(len(self._projects) > 1)
        menu.exec(
            self.project_manage_button.mapToGlobal(
                self.project_manage_button.rect().bottomLeft()
            )
        )

    def _rename_current_project(self) -> None:
        current = self.project_repository.get(self.current_project_id)
        if current is None:
            return
        name, accepted = QInputDialog.getText(
            self,
            "Rename Project",
            "Project name:",
            text=str(current["name"]),
        )
        if not accepted:
            return
        try:
            self.project_repository.rename(self.current_project_id, name)
            self._reload_projects()
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error("Could not rename the project", error)

    def _delete_current_project(self) -> None:
        current = self.project_repository.get(self.current_project_id)
        if current is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete Project",
            f'Delete project "{current["name"]}"?\n\n'
            "Papers and their global notes, highlights, and summaries are kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        next_id = next(
            int(project["id"])
            for project in self._projects
            if int(project["id"]) != self.current_project_id
        )
        self._save_workspace_state()
        try:
            self.project_repository.delete(self.current_project_id)
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error("Could not delete the project", error)
            return
        self._reload_projects()
        self._switch_project(next_id, save_current=False)

    def _project_selection_changed(self, _index: int) -> None:
        value = self.project_selector.currentData()
        if value is not None:
            self._switch_project(int(value))

    def _switch_project(self, project_id: int, *, save_current: bool = True) -> None:
        project_id = int(project_id)
        if project_id == self.current_project_id:
            return
        if self.project_repository.get(project_id) is None:
            self._reload_projects()
            return
        if save_current:
            self._save_workspace_state()
        search_page_active = (
            self._project_ai_search_dialog is not None
            and hasattr(self, "app_pages")
            and self.app_pages.currentWidget()
            is self._project_ai_search_dialog
        )
        if self._global_search_dialog is not None:
            self._global_search_dialog.close()
            self._global_search_dialog.deleteLater()
            self._global_search_dialog = None
        self._workspace_transitioning = True
        try:
            self._stop_group_ai()
            for reader in tuple(self._pdf_readers.values()):
                if not reader.close():
                    self._reload_projects()
                    return
            self._pdf_readers.clear()
            self._workspace_paper_ids.clear()
            self._active_paper_id = None
            self._dissolve_ai_group()
        finally:
            self._workspace_transitioning = False
        self.current_project_id = project_id
        self.settings_repository.set("current_project_id", str(project_id))
        self._clear_filters()
        self._load_project_workspace()
        self._reload_projects()
        self._reload_taxonomy()
        self.load_papers()
        if self._project_search_controller is not None:
            project = self.project_repository.get(self.current_project_id) or {}
            self._project_search_controller.set_project(
                self.current_project_id,
                str(project.get("name") or ""),
            )
            if search_page_active:
                self.project_ai_search_nav_button.setChecked(True)
                self.app_pages.setCurrentWidget(
                    self._project_ai_search_dialog
                )

    def _save_workspace_state(self) -> None:
        if self._workspace_transitioning:
            return
        self.project_repository.save_workspace(
            self.current_project_id,
            self._workspace_paper_ids,
            self._active_paper_id,
        )

    def _load_project_workspace(self) -> None:
        papers, active = self.project_repository.load_workspace(
            self.current_project_id
        )
        self._workspace_paper_ids = list(papers)
        self._pending_workspace_active_id = active

    def _restore_project_workspace_readers(self) -> None:
        for paper_id in tuple(self._workspace_paper_ids):
            if paper_id not in self._pdf_readers:
                self._create_workspace_reader(paper_id)

    def _setup_actions(self) -> None:
        self.add_action = QAction("Add Paper…", self)
        set_action_icon(self.add_action, "file-plus")
        self.add_action.setShortcut(QKeySequence("Ctrl+O"))
        self.add_action.triggered.connect(self.add_paper)

        self.focus_search_action = QAction("Search Library", self)
        set_action_icon(self.focus_search_action, "search")
        self.focus_search_action.setShortcut(QKeySequence("Ctrl+F"))
        self.focus_search_action.triggered.connect(self._focus_search)

        self.global_search_action = QAction("Search Current Project…", self)
        set_action_icon(self.global_search_action, "search")
        self.global_search_action.setShortcut(QKeySequence("Ctrl+Shift+F"))
        self.global_search_action.triggered.connect(self.open_global_search)

        self.project_ai_search_action = QAction("AI Search…", self)
        set_action_icon(self.project_ai_search_action, "sparkles")
        self.project_ai_search_action.triggered.connect(self.open_project_ai_search)

        self.open_action = QAction("Open Selected Paper", self)
        set_action_icon(self.open_action, "file-text")
        self.open_action.setShortcut(QKeySequence(Qt.Key.Key_Return))
        self.open_action.triggered.connect(self._shortcut_open)

        self.edit_action = QAction("Edit Selected Paper", self)
        set_action_icon(self.edit_action, "pencil")
        self.edit_action.setShortcut(QKeySequence("Ctrl+E"))
        self.edit_action.triggered.connect(self.edit_selected_paper)

        self.delete_action = QAction("Delete Selected Paper", self)
        set_action_icon(
            self.delete_action,
            "trash",
            color=DANGER_ICON_COLOR,
            active_color="#7E2525",
        )
        self.delete_action.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        self.delete_action.triggered.connect(self._shortcut_delete)

        self.settings_action = QAction("Settings…", self)
        set_action_icon(self.settings_action, "settings")
        self.settings_action.setShortcut(QKeySequence("Ctrl+,"))
        self.settings_action.triggered.connect(self.open_settings)

        self.backup_action = QAction("Create Backup…", self)
        set_action_icon(self.backup_action, "archive")
        self.backup_action.triggered.connect(self.create_backup)

        self.export_actions: dict[str, QAction] = {}
        for kind, label in (
            ("notes", "Notes as Markdown…"),
            ("summaries", "Summaries as Markdown…"),
            ("csv", "Library as CSV…"),
            ("bibtex", "Library as BibTeX…"),
        ):
            action = QAction(label, self)
            set_action_icon(
                action,
                {
                    "notes": "sticky-note",
                    "summaries": "notebook-text",
                    "csv": "download",
                    "bibtex": "file-text",
                }[kind],
            )
            action.triggered.connect(
                lambda _checked=False, export_kind=kind: self.export_library(
                    export_kind
                )
            )
            self.export_actions[kind] = action

        for action in (
            self.add_action,
            self.focus_search_action,
            self.global_search_action,
            self.project_ai_search_action,
            self.open_action,
            self.edit_action,
            self.delete_action,
            self.settings_action,
        ):
            self.addAction(action)

    def _setup_ui(self) -> None:
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(238)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(18, 20, 18, 16)
        sidebar_layout.setSpacing(6)

        brand = QVBoxLayout()
        brand.setContentsMargins(1, 0, 0, 0)
        brand.setSpacing(0)
        logo = QLabel("Research")
        logo.setObjectName("logo")
        subtitle = QLabel("Assistant")
        subtitle.setObjectName("logoSubtitle")
        brand.addWidget(logo)
        brand.addWidget(subtitle)
        sidebar_layout.addLayout(brand)
        sidebar_layout.addSpacing(12)

        project_label = QLabel("CURRENT PROJECT")
        project_label.setObjectName("sectionLabel")
        sidebar_layout.addWidget(project_label)
        self.project_selector = QComboBox()
        self.project_selector.setObjectName("projectSelector")
        self.project_selector.setFixedHeight(36)
        self.project_selector.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.project_selector.setMinimumContentsLength(8)
        self.project_selector.setToolTip("Current project")
        sidebar_layout.addWidget(self.project_selector)
        project_actions = QHBoxLayout()
        project_actions.setContentsMargins(0, 0, 0, 0)
        project_actions.setSpacing(3)
        self.project_add_button = QPushButton("New Project")
        self.project_add_button.setObjectName("sidebarLink")
        set_widget_icon(self.project_add_button, "plus", size=13)
        self.project_add_button.clicked.connect(self._create_project)
        self.project_manage_button = QPushButton("Manage")
        self.project_manage_button.setObjectName("sidebarLink")
        set_widget_icon(self.project_manage_button, "settings", size=13)
        self.project_manage_button.clicked.connect(self._show_project_menu)
        project_actions.addWidget(self.project_add_button)
        project_actions.addStretch()
        project_actions.addWidget(self.project_manage_button)
        sidebar_layout.addLayout(project_actions)
        sidebar_layout.addSpacing(10)

        self.scope_button_group = QButtonGroup(self)
        self.scope_button_group.setExclusive(True)
        self.library_button = self._scope_button("Library", "library")
        self.inbox_button = self._scope_button("Inbox", "inbox")
        self.reading_button = self._scope_button("Reading", "reading")
        self.important_button = self._scope_button("Important", "important")
        for button in (
            self.library_button,
            self.inbox_button,
            self.reading_button,
            self.important_button,
        ):
            sidebar_layout.addWidget(button)
        self.library_button.setChecked(True)
        self._last_library_scope_button = self.library_button

        self.project_ai_search_nav_button = QPushButton("AI Search")
        self.project_ai_search_nav_button.setObjectName("sidebarButton")
        self.project_ai_search_nav_button.setCheckable(True)
        set_widget_icon(
            self.project_ai_search_nav_button, "sparkles", size=17
        )
        self.project_ai_search_nav_button.clicked.connect(
            self.open_project_ai_search
        )
        self.scope_button_group.addButton(self.project_ai_search_nav_button)
        sidebar_layout.addWidget(self.project_ai_search_nav_button)

        sidebar_layout.addSpacing(14)
        collection_header = QHBoxLayout()
        collections_label = QLabel("COLLECTIONS")
        collections_label.setObjectName("sectionLabel")
        manage_taxonomy_button = QPushButton("Manage")
        manage_taxonomy_button.setObjectName("sidebarLink")
        set_widget_icon(manage_taxonomy_button, "pencil", size=14)
        manage_taxonomy_button.clicked.connect(self.open_taxonomy_manager)
        collection_header.addWidget(collections_label)
        collection_header.addStretch()
        collection_header.addWidget(manage_taxonomy_button)
        sidebar_layout.addLayout(collection_header)

        collection_scroll = QScrollArea()
        collection_scroll.setObjectName("collectionScroll")
        collection_scroll.setWidgetResizable(True)
        collection_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.collection_container = QWidget()
        self.collection_layout = QVBoxLayout(self.collection_container)
        self.collection_layout.setContentsMargins(0, 0, 0, 0)
        self.collection_layout.setSpacing(5)
        self.collection_layout.addStretch()
        collection_scroll.setWidget(self.collection_container)
        sidebar_layout.addWidget(collection_scroll, 1)

        settings_button = QPushButton("Settings")
        settings_button.setObjectName("sidebarButton")
        set_widget_icon(settings_button, "settings", size=17)
        settings_button.clicked.connect(self.settings_action.trigger)
        sidebar_layout.addWidget(settings_button)

        content = QWidget()
        content.setObjectName("content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(30, 20, 30, 24)
        content_layout.setSpacing(14)

        header = QHBoxLayout()
        title_container = QVBoxLayout()
        self.page_title = QLabel("Library")
        self.page_title.setObjectName("pageTitle")
        self.description_label = QLabel("0 papers")
        self.description_label.setObjectName("description")
        title_container.addWidget(self.page_title)
        title_container.addWidget(self.description_label)
        header.addLayout(title_container)
        header.addStretch()
        self.add_button = QPushButton("Add Paper")
        self.add_button.setObjectName("primaryButton")
        self.add_button.setFixedHeight(38)
        set_widget_icon(
            self.add_button,
            "file-plus",
            size=18,
            color="#FFFFFF",
            active_color="#FFFFFF",
        )
        self.add_button.clicked.connect(self.add_action.trigger)
        self.project_ai_search_button = QPushButton("AI Search")
        self.project_ai_search_button.setObjectName("secondaryButton")
        self.project_ai_search_button.setFixedHeight(38)
        set_widget_icon(self.project_ai_search_button, "sparkles", size=17)
        self.project_ai_search_button.clicked.connect(
            self.project_ai_search_action.trigger
        )
        header.addWidget(self.project_ai_search_button)
        header.addWidget(self.add_button)
        self.library_tools_button = QToolButton()
        self.library_tools_button.setObjectName("headerMenuButton")
        self.library_tools_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.library_tools_button.setFixedSize(38, 38)
        set_widget_icon(
            self.library_tools_button,
            "more-horizontal",
            size=17,
            tooltip="Backup and export",
        )
        self.library_tools_menu = QMenu(self.library_tools_button)
        self.library_tools_menu.addAction(self.backup_action)
        self.library_export_menu = self.library_tools_menu.addMenu(
            app_icon("download"), "Export"
        )
        self.library_export_menu.addActions(list(self.export_actions.values()))
        self.library_tools_button.setMenu(self.library_tools_menu)
        header.addWidget(self.library_tools_button)
        content_layout.addLayout(header)

        self._reload_projects()
        self.project_selector.currentIndexChanged.connect(
            self._project_selection_changed
        )

        controls = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setObjectName("searchBox")
        self.search_box.setPlaceholderText(
            "Search title, author, year, DOI, tag, or collection…"
        )
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setFixedHeight(38)
        self.search_box.addAction(
            app_icon("search"), QLineEdit.ActionPosition.LeadingPosition
        )
        self.filter_button = QPushButton("Filter")
        self.filter_button.setObjectName("secondaryButton")
        self.filter_button.setFixedHeight(38)
        set_widget_icon(self.filter_button, "filter", size=16)
        self.filter_button.clicked.connect(self._show_filter_menu)
        self.sort_button = QToolButton()
        self.sort_button.setObjectName("secondaryButton")
        self.sort_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.sort_button.setFixedHeight(38)
        set_widget_icon(self.sort_button, "sort-descending", size=16)
        self.sort_menu = QMenu(self.sort_button)
        self.sort_button.setMenu(self.sort_menu)
        self._rebuild_sort_menu()
        controls.addWidget(self.search_box, 1)
        controls.addWidget(self.filter_button)
        controls.addWidget(self.sort_button)
        content_layout.addLayout(controls)

        self.paper_table = QTableWidget()
        self.paper_table.setObjectName("paperTable")
        self.paper_table.setColumnCount(len(TABLE_COLUMNS))
        self.paper_table.setHorizontalHeaderLabels(TABLE_COLUMNS)
        self.paper_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.paper_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.paper_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.paper_table.setShowGrid(False)
        self.paper_table.setItemDelegateForColumn(
            5, _ReadingStatusDelegate(self.paper_table)
        )
        self.paper_table.verticalHeader().setVisible(False)
        self.paper_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.paper_table.customContextMenuRequested.connect(self._show_context_menu)
        self.paper_table.cellDoubleClicked.connect(self.open_selected_paper)
        self.paper_table.horizontalHeader().sectionClicked.connect(
            self._header_sort_clicked
        )
        table_header = self.paper_table.horizontalHeader()
        table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(TABLE_COLUMNS)):
            table_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for column in (1, 3, 4):
            self.paper_table.setColumnHidden(column, True)
        table_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.paper_table.setColumnWidth(5, 132)
        content_layout.addWidget(self.paper_table, 1)

        bottom = QHBoxLayout()
        self.result_count = QLabel("")
        self.result_count.setObjectName("resultCount")
        bottom.addWidget(self.result_count)
        bottom.addStretch()
        global_search_button = QPushButton("Search current project  Ctrl+Shift+F")
        global_search_button.setObjectName("sidebarLink")
        set_widget_icon(global_search_button, "search", size=14)
        global_search_button.clicked.connect(self.global_search_action.trigger)
        bottom.addWidget(global_search_button)
        content_layout.addLayout(bottom)

        self.library_page = content
        self.app_pages = QStackedWidget()
        self.app_pages.setObjectName("appPages")
        self.app_pages.addWidget(self.library_page)
        main_layout.addWidget(sidebar)
        main_layout.addWidget(self.app_pages, 1)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(220)
        self._search_timer.timeout.connect(self.load_papers)
        self.search_box.textChanged.connect(
            lambda _text: self._search_timer.start()
        )
        self.statusBar().showMessage("Ready")

    def _scope_button(self, label: str, scope: str) -> QPushButton:
        button = QPushButton(label)
        button.setObjectName("sidebarButton")
        set_widget_icon(
            button,
            {
                "library": "library",
                "inbox": "inbox",
                "reading": "book-open",
                "important": "star",
            }.get(scope, "folder"),
            size=17,
            checked_color=(
                IMPORTANT_ICON_COLOR if scope == "important" else "#4F7DF3"
            ),
        )
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda _checked=False, value=scope: self._set_scope(value))
        self.scope_button_group.addButton(button)
        return button

    # ------------------------------------------------------------------
    # Library query/filter/sort
    # ------------------------------------------------------------------
    def load_papers(self, *, preserve_paper_id: int | None = None) -> None:
        if preserve_paper_id is None:
            preserve_paper_id = self._selected_paper_id()
        try:
            papers = self.paper_repository.list_papers(
                project_id=self.current_project_id,
                search=self.search_box.text(),
                status=self._status_filter,
                important=self._important_filter,
                year=self._year_filter,
                collection_id=self._collection_filter,
                tag_id=self._tag_filter,
                sort_by=self._sort_by,
                descending=self._sort_descending,
            )
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error("Could not load the paper library", error)
            return

        self.paper_table.setRowCount(len(papers))
        self._rows.clear()
        selected_row = -1
        for row_index, raw_paper in enumerate(papers):
            paper = _mapping(raw_paper)
            if paper.get("project_status"):
                paper["status"] = str(paper["project_status"])
            paper_id = int(paper["id"])
            self._rows[paper_id] = paper
            title = str(paper.get("title") or "")
            values = (
                title,
                str(paper.get("authors") or ""),
                str(paper.get("year") or ""),
                str(paper.get("tags") or ""),
                str(paper.get("collections") or ""),
                str(paper.get("status") or "Unread"),
            )
            for column, value in enumerate(values):
                # Column 0 is presented by _PaperCell. Keep a backing item for
                # selection/UserRole without letting Qt paint the title again.
                item = QTableWidgetItem("" if column == 0 else value)
                item.setData(Qt.ItemDataRole.UserRole, paper_id)
                if column == 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column == 0:
                    tooltip = str(paper.get("doi") or paper.get("file_path") or "")
                    item.setToolTip(tooltip)
                elif column == 5:
                    item.setIcon(
                        reading_status_icon(str(paper.get("status") or "Unread"))
                    )
                self.paper_table.setItem(row_index, column, item)
            self.paper_table.setCellWidget(row_index, 0, _PaperCell(paper))
            has_taxonomy = bool(
                str(paper.get("tags") or "").strip()
                or str(paper.get("collections") or "").strip()
            )
            self.paper_table.setRowHeight(row_index, 76 if has_taxonomy else 58)
            if paper_id == preserve_paper_id:
                selected_row = row_index

        if selected_row >= 0:
            self.paper_table.selectRow(selected_row)
            self.paper_table.scrollToItem(self.paper_table.item(selected_row, 0))
        self.result_count.setText(
            f"{len(papers)} paper{'s' if len(papers) != 1 else ''}"
        )
        reading_count = sum(
            str(_mapping(paper).get("status") or "Unread") == "Reading"
            for paper in papers
        )
        count_label = f"{len(papers)} paper{'s' if len(papers) != 1 else ''}"
        self.description_label.setText(
            f"{count_label} \u00b7 {reading_count} reading"
            if reading_count
            else count_label
        )
        self._update_filter_button()

    def _set_scope(self, scope: str, collection_id: int | None = None) -> None:
        self._show_library_page(preserve_scope=True)
        self._status_filter = None
        self._important_filter = None
        self._year_filter = None
        self._collection_filter = None
        self._tag_filter = None
        titles = {
            "library": "Library",
            "inbox": "Inbox",
            "reading": "Reading",
            "important": "Important",
        }
        self._scope_title = titles.get(scope, "Collection")
        if scope == "inbox":
            self._status_filter = "Unread"
        elif scope == "reading":
            self._status_filter = "Reading"
        elif scope == "important":
            self._important_filter = True
        elif scope == "collection":
            self._collection_filter = collection_id
        scope_buttons = {
            "library": self.library_button,
            "inbox": self.inbox_button,
            "reading": self.reading_button,
            "important": self.important_button,
        }
        if scope in scope_buttons:
            self._last_library_scope_button = scope_buttons[scope]
        elif scope == "collection":
            for button in self.scope_button_group.buttons():
                if button.property("collectionId") == collection_id:
                    self._last_library_scope_button = button
                    break
        self.page_title.setText(self._scope_title)
        self.load_papers()

    def _set_filter(self, name: str, value: Any) -> None:
        setattr(self, f"_{name}_filter", value)
        self._scope_title = "Filtered Library"
        self.page_title.setText(self._scope_title)
        blocker = QSignalBlocker(self.library_button)
        self.library_button.setChecked(True)
        self._last_library_scope_button = self.library_button
        del blocker
        self.load_papers()

    def _clear_filters(self) -> None:
        self.library_button.setChecked(True)
        self._set_scope("library")

    def _show_filter_menu(self) -> None:
        menu = QMenu(self)
        status_menu = menu.addMenu(app_icon("circle"), "Status")
        self._add_filter_actions(
            status_menu,
            "status",
            (("All", None), ("Unread", "Unread"), ("Reading", "Reading"), ("Completed", "Completed")),
            self._status_filter,
        )
        important_menu = menu.addMenu(app_icon("star"), "Importance")
        self._add_filter_actions(
            important_menu,
            "important",
            (("All", None), ("Important", True), ("Not important", False)),
            self._important_filter,
        )

        try:
            all_papers = self.paper_repository.list_papers(
                project_id=self.current_project_id
            )
            collections = self.collection_repository.list_all(
                self.current_project_id
            )
            tags = self.tag_repository.list_all()
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error("Could not build filters", error)
            return

        years = sorted(
            {int(row["year"]) for row in all_papers if row["year"] is not None},
            reverse=True,
        )
        year_menu = menu.addMenu(app_icon("file-text"), "Year")
        self._add_filter_actions(
            year_menu,
            "year",
            (("All", None), *((str(year), year) for year in years)),
            self._year_filter,
        )
        collection_menu = menu.addMenu(app_icon("folder"), "Collection")
        self._add_filter_actions(
            collection_menu,
            "collection",
            (("All", None), *((str(row["name"]), int(row["id"])) for row in collections)),
            self._collection_filter,
        )
        tag_menu = menu.addMenu(app_icon("tag"), "Tag")
        self._add_filter_actions(
            tag_menu,
            "tag",
            (("All", None), *((str(row["name"]), int(row["id"])) for row in tags)),
            self._tag_filter,
        )
        menu.addSeparator()
        clear_action = menu.addAction("Clear all filters", self._clear_filters)
        set_action_icon(clear_action, "x")
        menu.exec(
            self.filter_button.mapToGlobal(self.filter_button.rect().bottomLeft())
        )

    def _add_filter_actions(
        self,
        menu: QMenu,
        name: str,
        choices,
        current: Any,
    ) -> None:
        group = QActionGroup(menu)
        group.setExclusive(True)
        for label, value in choices:
            action = menu.addAction(label)
            if name == "status" and value in {"Unread", "Reading", "Completed"}:
                set_action_icon(
                    action,
                    {
                        "Unread": "circle",
                        "Reading": "book-open",
                        "Completed": "circle-check",
                    }[value],
                )
            elif name == "important" and value is True:
                set_action_icon(
                    action,
                    "star-filled",
                    color=IMPORTANT_ICON_COLOR,
                    active_color=IMPORTANT_ICON_COLOR,
                )
            action.setCheckable(True)
            action.setChecked(value == current)
            action.triggered.connect(
                lambda _checked=False, field=name, selected=value: self._set_filter(
                    field, selected
                )
            )
            group.addAction(action)

    def _update_filter_button(self) -> None:
        count = sum(
            value is not None
            for value in (
                self._status_filter,
                self._important_filter,
                self._year_filter,
                self._collection_filter,
                self._tag_filter,
            )
        )
        self.filter_button.setText(f"Filter ({count})" if count else "Filter")

    def _header_sort_clicked(self, column: int) -> None:
        sort_key = HEADER_SORTS.get(column)
        if sort_key is None:
            return
        if self._sort_by == sort_key:
            self._sort_descending = not self._sort_descending
        else:
            self._sort_by = sort_key
            self._sort_descending = False
        self._rebuild_sort_menu()
        self.load_papers()

    def _set_sort(self, sort_by: str) -> None:
        if sort_by not in PAPER_SORTS:
            return
        self._sort_by = sort_by
        self._rebuild_sort_menu()
        self.load_papers()

    def _toggle_sort_direction(self) -> None:
        self._sort_descending = not self._sort_descending
        self._rebuild_sort_menu()
        self.load_papers()

    def _rebuild_sort_menu(self) -> None:
        self.sort_menu.clear()
        group = QActionGroup(self.sort_menu)
        group.setExclusive(True)
        for key, label in SORT_LABELS.items():
            action = self.sort_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(key == self._sort_by)
            action.triggered.connect(
                lambda _checked=False, selected=key: self._set_sort(selected)
            )
            group.addAction(action)
        self.sort_menu.addSeparator()
        direction = self.sort_menu.addAction(
            "Descending" if self._sort_descending else "Ascending"
        )
        direction_icon = (
            "sort-descending" if self._sort_descending else "sort-ascending"
        )
        set_action_icon(direction, direction_icon)
        direction.triggered.connect(self._toggle_sort_direction)
        self.sort_button.setText(SORT_LABELS[self._sort_by])
        set_widget_icon(self.sort_button, direction_icon, size=16)

    # ------------------------------------------------------------------
    # Dynamic taxonomy and context actions
    # ------------------------------------------------------------------
    def _reload_taxonomy(self) -> None:
        active_collection_id = self._collection_filter
        while self.collection_layout.count() > 1:
            item = self.collection_layout.takeAt(0)
            if item.widget() is not None:
                self.scope_button_group.removeButton(item.widget())
                item.widget().deleteLater()
        try:
            collections = self.collection_repository.list_all(
                self.current_project_id
            )
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error("Could not load collections", error)
            return
        active_collection_found = False
        for collection in collections:
            collection_id = int(collection["id"])
            collection_name = str(collection["name"])
            button = QPushButton(str(collection["name"]))
            button.setObjectName("collectionButton")
            button.setProperty("collectionId", collection_id)
            set_widget_icon(
                button,
                "folder",
                size=16,
                color="#718096",
                active_color="#4F7DF3",
                checked_color="#4F7DF3",
            )
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, value=collection_id, title=collection_name: self._activate_collection(
                    value, title
                )
            )
            self.scope_button_group.addButton(button)
            self.collection_layout.insertWidget(self.collection_layout.count() - 1, button)
            if collection_id == active_collection_id:
                button.setChecked(True)
                self._last_library_scope_button = button
                active_collection_found = True
                self._scope_title = collection_name
                self.page_title.setText(collection_name)
        if active_collection_id is not None and not active_collection_found:
            self._collection_filter = None
            self._scope_title = "Library"
            self.page_title.setText(self._scope_title)
            self.library_button.setChecked(True)
            self._last_library_scope_button = self.library_button

    def _activate_collection(self, collection_id: int, title: str) -> None:
        self._set_scope("collection", collection_id)
        self._scope_title = title
        self.page_title.setText(title)

    def open_taxonomy_manager(self) -> None:
        dialog = TaxonomyManagerDialog(
            self,
            project_id=self.current_project_id,
            collection_repository=self.collection_repository,
            tag_repository=self.tag_repository,
        )
        dialog.taxonomy_changed.connect(self._taxonomy_changed)
        dialog.exec()

    def _taxonomy_changed(self) -> None:
        self._reload_taxonomy()
        self.load_papers()

    def _show_context_menu(self, position) -> None:
        item = self.paper_table.itemAt(position)
        if item is None:
            return
        self.paper_table.selectRow(item.row())
        paper = self._selected_paper()
        if paper is None:
            return
        paper_id = int(paper["id"])

        menu = QMenu(self)
        open_action = menu.addAction(
            "Open", lambda: self.open_paper_by_id(paper_id)
        )
        set_action_icon(open_action, "file-text")
        menu.addSeparator()
        edit_action = menu.addAction(
            "Edit paper info…", lambda: self.edit_paper(paper_id)
        )
        set_action_icon(edit_action, "pencil")
        tag_action = menu.addAction(
            "Edit tags…", lambda: self.edit_tags(paper_id)
        )
        set_action_icon(tag_action, "tag")
        collections_action = menu.addAction(
            "Change collections…", lambda: self.change_collections(paper_id)
        )
        set_action_icon(collections_action, "folder")
        status_menu = menu.addMenu(app_icon("circle"), "Status")
        for status in ("Unread", "Reading", "Completed"):
            action = status_menu.addAction(status)
            set_action_icon(
                action,
                {
                    "Unread": "circle",
                    "Reading": "book-open",
                    "Completed": "circle-check",
                }[status],
            )
            action.setCheckable(True)
            action.setChecked(str(paper.get("status") or "Unread") == status)
            action.triggered.connect(
                lambda _checked=False, value=status: self.change_status(
                    paper_id, value
                )
            )
        important_action = menu.addAction("Important")
        set_action_icon(
            important_action,
            "star-filled" if bool(paper.get("is_important")) else "star",
            color=(
                IMPORTANT_ICON_COLOR
                if bool(paper.get("is_important"))
                else "#5D6570"
            ),
            active_color=(
                IMPORTANT_ICON_COLOR
                if bool(paper.get("is_important"))
                else "#252A31"
            ),
            checked_color=(
                IMPORTANT_ICON_COLOR
                if bool(paper.get("is_important"))
                else "#252A31"
            ),
        )
        important_action.setCheckable(True)
        important_action.setChecked(bool(paper.get("is_important")))
        important_action.triggered.connect(
            lambda checked: self.change_important(paper_id, checked)
        )
        menu.addSeparator()
        summary_action = menu.addAction(
            "Show summary…", lambda: self.show_summary(paper_id)
        )
        set_action_icon(summary_action, "notebook-text")
        location_action = menu.addAction(
            "Open file location", lambda: self.open_file_location(paper_id)
        )
        set_action_icon(location_action, "external-link")
        menu.addSeparator()
        delete = menu.addAction("Delete…", lambda: self.delete_paper(paper_id))
        delete.setObjectName("dangerAction")
        set_action_icon(
            delete,
            "trash",
            color=DANGER_ICON_COLOR,
            active_color="#7E2525",
        )
        menu.exec(self.paper_table.viewport().mapToGlobal(position))

    def edit_selected_paper(self) -> None:
        paper_id = self._selected_paper_id()
        if paper_id is not None:
            self.edit_paper(paper_id)

    def edit_paper(self, paper_id: int) -> None:
        paper = self._paper_by_id(
            paper_id,
            "Could not load the paper information",
        )
        if paper is None:
            return
        try:
            tags = [str(row["name"]) for row in self.tag_repository.get_for_paper(paper_id)]
            selected_collections = [
                int(row["id"])
                for row in self.collection_repository.get_for_paper(
                    paper_id, self.current_project_id
                )
            ]
            collections = self.collection_repository.list_all(
                self.current_project_id
            )
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error("Could not load paper information", error)
            return
        metadata = _mapping(paper)
        metadata["tags"] = tags
        metadata["collection_ids"] = selected_collections
        dialog = ReviewPaperDialog(
            metadata,
            collections,
            self,
            dialog_title="Edit Paper Information",
            save_label="Save Changes",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        data = dialog.get_data()
        try:
            update_details = getattr(self.paper_repository, "update_details", None)
            if callable(update_details):
                changed = update_details(
                    paper_id,
                    title=data["title"],
                    authors=data["authors"] or None,
                    year=data["year"],
                    doi=data["doi"],
                    status=data["status"],
                    is_important=data["is_important"],
                    tags=data["tags"],
                    collection_ids=data["collection_ids"],
                    project_id=self.current_project_id,
                )
                if not changed:
                    raise ValueError("The paper no longer exists.")
            else:  # Compatibility for lightweight injected test repositories.
                self.paper_repository.update_metadata(
                    paper_id,
                    title=data["title"],
                    authors=data["authors"] or None,
                    year=data["year"],
                    doi=data["doi"],
                )
                self.paper_repository.replace_relationships(
                    paper_id,
                    tags=data["tags"],
                    collection_ids=data["collection_ids"],
                    project_id=self.current_project_id,
                )
                self.paper_repository.set_status(paper_id, data["status"])
                self.paper_repository.set_important(
                    paper_id, data["is_important"]
                )
            self.project_repository.set_status(
                self.current_project_id, paper_id, data["status"]
            )
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error("Could not update the paper", error)
            return
        self._paper_changed(paper_id)

    def edit_tags(self, paper_id: int) -> None:
        try:
            current = ", ".join(
                str(row["name"]) for row in self.tag_repository.get_for_paper(paper_id)
            )
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error("Could not load paper tags", error)
            return
        from PySide6.QtWidgets import QInputDialog

        text, accepted = QInputDialog.getText(
            self,
            "Edit Tags",
            "Comma-separated tags:",
            text=current,
        )
        if not accepted:
            return
        tags = [value.strip() for value in text.split(",") if value.strip()]
        try:
            self.paper_repository.replace_tags(paper_id, tags)
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error("Could not update paper tags", error)
            return
        self._paper_changed(paper_id)

    def change_collections(self, paper_id: int) -> None:
        try:
            collections = self.collection_repository.list_all(
                self.current_project_id
            )
            selected_ids = [
                int(row["id"])
                for row in self.collection_repository.get_for_paper(
                    paper_id, self.current_project_id
                )
            ]
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error("Could not load paper collections", error)
            return
        dialog = CollectionSelectionDialog(collections, selected_ids, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.paper_repository.replace_collections(
                paper_id,
                dialog.selected_ids(),
                project_id=self.current_project_id,
            )
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error("Could not update paper collections", error)
            return
        self._paper_changed(paper_id)

    def change_status(self, paper_id: int, status: str) -> None:
        try:
            self.project_repository.set_status(
                self.current_project_id, paper_id, status
            )
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error("Could not update paper status", error)
            return
        self._paper_changed(paper_id)

    def change_important(self, paper_id: int, important: bool) -> None:
        try:
            self.paper_repository.set_important(paper_id, important)
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error("Could not update importance", error)
            return
        self._paper_changed(paper_id)

    def show_summary(self, paper_id: int) -> None:
        paper = self._paper_by_id(paper_id, "Could not load the paper summary")
        if paper is None:
            return
        try:
            summary = self.summary_repository.get_for_paper(paper_id)
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error("Could not load the paper summary", error)
            return
        SummaryPreviewDialog(_mapping(paper), _mapping(summary) if summary else None, self).exec()

    def open_file_location(self, paper_id: int) -> None:
        paper = self._paper_by_id(paper_id, "Could not locate the PDF")
        if paper is None:
            return
        try:
            path = resolve_paper_path(paper["file_path"])
        except (OSError, ValueError) as error:
            self._report_error("Could not resolve the PDF path", error)
            return
        if not path.is_file():
            QMessageBox.warning(self, "Missing PDF", f"The PDF no longer exists:\n{path}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent))):
            QMessageBox.warning(self, "Open Folder", f"Could not open:\n{path.parent}")

    def delete_paper(self, paper_id: int) -> None:
        paper = self._paper_by_id(paper_id, "Could not load the paper")
        if paper is None:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Delete Paper")
        box.setText(f'Remove "{paper["title"]}" from the Library?')
        box.setInformativeText(
            "You can keep the PDF file or move the managed local copy to Trash."
        )
        keep_button = box.addButton(
            "Remove from Library Only",
            QMessageBox.ButtonRole.AcceptRole,
        )
        delete_file_button = box.addButton(
            "Remove and Trash PDF",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in {keep_button, delete_file_button}:
            return

        reader = self._pdf_readers.get(paper_id)
        if reader is not None and not reader.close():
            QMessageBox.warning(
                self,
                "Unsaved Reader Data",
                "The Reader could not close because some data was not saved. "
                "Resolve it before deleting this paper.",
            )
            self._activate_workspace_paper(paper_id)
            return
        self._pdf_readers.pop(paper_id, None)

        try:
            outcome = self.library_service.delete_paper(
                paper_id,
                delete_local_file=clicked is delete_file_button,
            )
        except (sqlite3.Error, OSError, RuntimeError, ValueError) as error:
            self._report_error("Could not delete the paper", error)
            return
        self._handle_delete_outcome(outcome)

    def _handle_delete_outcome(self, outcome: DeleteOutcome) -> None:
        self._reload_taxonomy()
        self.load_papers()
        if outcome.warning:
            QMessageBox.warning(self, "Paper Removed", outcome.warning)
        elif outcome.removed_from_library:
            detail = "PDF moved to Trash" if outcome.file_deleted else "PDF file kept"
            self.statusBar().showMessage(f"Paper removed · {detail}", 6000)

    # ------------------------------------------------------------------
    # Reader and global search
    # ------------------------------------------------------------------
    def open_selected_paper(self, row: int, _column: int = 0) -> None:
        item = self.paper_table.item(row, 0)
        if item is not None:
            paper_id = item.data(Qt.ItemDataRole.UserRole)
            if paper_id is not None:
                self.open_paper_by_id(int(paper_id))

    def _quick_paper_catalog(self) -> tuple[list[Any], set[int]]:
        papers = self.paper_repository.list_papers(
            project_id=self.current_project_id,
            sort_by="updated",
            descending=True,
        )
        return papers, set(self._workspace_paper_ids)

    def _reading_context_active(self, paper_id: int) -> bool:
        return (
            int(self._active_paper_id or 0) == int(paper_id)
            and self._reader_workspace.isVisible()
            and QApplication.activeWindow() is self._reader_workspace
            and QApplication.applicationState()
            == Qt.ApplicationState.ApplicationActive
        )

    def _on_reading_interaction(self, paper_id: int, _kind: str) -> None:
        paper_id = int(paper_id)
        if not self._reading_context_active(paper_id):
            return
        self._last_reading_interaction[paper_id] = monotonic()
        outcome = self.project_repository.record_activity(
            self.current_project_id,
            paper_id,
            interactions=1,
            reading_seconds_threshold=READING_SECONDS_THRESHOLD,
            interaction_threshold=READING_INTERACTION_THRESHOLD,
        )
        if outcome and outcome.get("transitioned_to_reading"):
            self._paper_changed(paper_id)
            self._trigger_research_summary(paper_id)

    def _record_active_reading_tick(self) -> None:
        paper_id = int(self._active_paper_id or 0)
        if not paper_id or not self._reading_context_active(paper_id):
            return
        last = self._last_reading_interaction.get(paper_id)
        if last is None or monotonic() - last > READING_RECENT_INTERACTION_SECONDS:
            return
        outcome = self.project_repository.record_activity(
            self.current_project_id,
            paper_id,
            seconds=READING_TICK_SECONDS,
            reading_seconds_threshold=READING_SECONDS_THRESHOLD,
            interaction_threshold=READING_INTERACTION_THRESHOLD,
        )
        if outcome and outcome.get("transitioned_to_reading"):
            self._paper_changed(paper_id)
            self._trigger_research_summary(paper_id)

    def _on_ai_engaged(self, paper_id: int, provider: str, model: str) -> None:
        paper_id = int(paper_id)
        if not self.project_repository.contains_paper(
            self.current_project_id, paper_id
        ):
            return
        transitioned = self.project_repository.mark_ai_engagement(
            self.current_project_id, paper_id
        )
        if transitioned:
            self._paper_changed(paper_id)
        self._trigger_research_summary(paper_id, provider=provider, model=model)

    def _trigger_research_summary(
        self,
        paper_id: int,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        if int(paper_id) in self._research_summary_workers:
            return
        paper = self.paper_repository.get_paper_by_id(int(paper_id))
        if paper is None:
            return
        file_hash = str(paper["file_hash"] or "")
        if not file_hash:
            return
        selected_provider = str(
            provider
            or self.settings_repository.get("ai_provider", "gemini")
            or "gemini"
        )
        selected_model = str(
            model
            or self.settings_repository.get(
                f"ai_model_{selected_provider}", ""
            )
            or ""
        )
        if not selected_model:
            LOGGER.info(
                "Research Profile generation deferred for paper_id=%s: no model selected",
                paper_id,
            )
            return
        ResearchSummaryRepository.mark_stale_if_source_changed(
            paper_id, file_hash
        )
        if not ResearchSummaryRepository.claim_generation(paper_id, file_hash):
            return

        def generate() -> dict[str, Any]:
            return AIChatService(
                settings_repository=self.settings_repository
            ).generate_research_summary(
                paper_id=int(paper_id),
                provider=selected_provider,
                model=selected_model,
                pdf_path=resolve_paper_path(str(paper["file_path"])),
                file_hash=file_hash,
            )

        worker = FunctionWorker(generate)
        self._research_summary_workers[int(paper_id)] = worker

        def succeeded(value: object) -> None:
            if isinstance(value, Mapping):
                ResearchSummaryRepository.save(
                    paper_id,
                    value,
                    source_hash=file_hash,
                    provider=selected_provider,
                    model=selected_model,
                )

        def failed(error: object) -> None:
            ResearchSummaryRepository.fail(paper_id, str(error))
            LOGGER.error(
                "Research Profile generation failed for paper_id=%s: %s",
                paper_id,
                error,
            )

        worker.signals.result.connect(succeeded)
        worker.signals.error.connect(failed)
        worker.signals.finished.connect(
            lambda key=int(paper_id): self._research_summary_workers.pop(key, None)
        )
        QThreadPool.globalInstance().start(worker)

    def _workspace_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for paper_id in self._workspace_paper_ids:
            if not self.project_repository.contains_paper(
                self.current_project_id, paper_id
            ):
                continue
            paper = self.paper_repository.get_paper_by_id(paper_id)
            if paper is not None:
                value = dict(paper)
                value["status"] = self.project_repository.status(
                    self.current_project_id, paper_id
                ) or value.get("status", "Unread")
                rows.append(value)
        return rows

    def _sync_workspace_readers(self) -> None:
        rows = self._workspace_rows()
        rows_by_id = {int(row["id"]): row for row in rows}
        active_id = int(self._active_paper_id or 0)
        active_aliases = (
            {
                paper_id: alias
                for paper_id, alias in self._ai_group_aliases.items()
                if paper_id in self._ai_group_paper_ids
            }
            if self._active_ai_conversation_type == "group"
            and self._active_ai_conversation_id
            == self._ai_group_conversation_id
            else {}
        )
        for paper_id in tuple(self._workspace_paper_ids):
            reader = self._pdf_readers.get(paper_id)
            if reader is not None:
                reader.set_workspace_papers(
                    rows,
                    active_id,
                    active_aliases,
                )
                paper_row = rows_by_id.get(paper_id)
                reader.reader_sidebar.solo_ai.set_group_mode(False)
                reader.reader_sidebar.solo_ai.set_workspace_papers(
                    [paper_row] if paper_row is not None else []
                )
        if (
            self._active_ai_conversation_type == "group"
            and self._ai_group_panel is not None
        ):
            group_rows = [dict(member) for member in self._ai_group_members]
            self._ai_group_panel.set_group_mode(True)
            self._ai_group_panel.set_workspace_papers(
                group_rows, self._ai_group_aliases
            )
        self._apply_active_ai_mode()

    def _activate_workspace_paper(self, paper_id: int) -> PdfReaderWindow | None:
        reader = self._pdf_readers.get(int(paper_id))
        if reader is None and int(paper_id) in self._workspace_paper_ids:
            reader = self._create_workspace_reader(int(paper_id))
        if reader is None or int(paper_id) not in self._workspace_paper_ids:
            return None
        if self._active_paper_id != int(paper_id):
            self._remember_workspace_sidebar_state()
        self._active_paper_id = int(paper_id)
        self._pending_workspace_active_id = int(paper_id)
        if self._active_ai_conversation_type == "solo":
            self._active_ai_conversation_id = (
                reader.reader_sidebar.solo_ai.active_conversation_id
            )
        self._reader_workspace.set_active(paper_id)
        self._save_workspace_state()
        self.hide()
        if not self._reader_workspace.isVisible():
            self._reader_workspace.showMaximized()
            self._reader_workspace.raise_()
            self._reader_workspace.activateWindow()
        self._sync_workspace_readers()
        reader.apply_workspace_sidebar_state(
            self._workspace_sidebar_visible,
            self._workspace_sidebar_section,
        )
        return reader

    def _remember_workspace_sidebar_state(self) -> None:
        if self._active_paper_id is None:
            return
        reader = self._pdf_readers.get(int(self._active_paper_id))
        if reader is None:
            return
        visible, section = reader.workspace_sidebar_state()
        self._workspace_sidebar_visible = bool(visible)
        self._workspace_sidebar_section = section

    def _create_workspace_reader(self, paper_id: int) -> PdfReaderWindow | None:
        existing = self._pdf_readers.get(int(paper_id))
        if existing is not None:
            return existing
        paper = self._paper_by_id(paper_id, "Could not open the PDF Reader")
        if paper is None:
            return None
        paper_value = dict(paper)
        paper_value["status"] = self.project_repository.status(
            self.current_project_id, paper_id
        ) or paper_value.get("status", "Unread")
        try:
            reader = PdfReaderWindow(
                paper_value,
                self._reader_workspace.reader_stack,
                project_id=self.current_project_id,
                translation_provider=_ConfiguredTranslationProvider(
                    self.settings_repository
                ),
                settings_repository=self.settings_repository,
                paper_catalog_provider=self._quick_paper_catalog,
                embedded=True,
            )
        except (sqlite3.Error, OSError, RuntimeError, ValueError) as error:
            self._report_error("Could not open the PDF Reader", error)
            return None
        self._pdf_readers[paper_id] = reader
        self._reader_workspace.add_reader(paper_id, reader)
        reader.reader_sidebar.set_project_search_controller(
            self._ensure_project_search_controller()
        )
        reader.paper_updated.connect(self._on_reader_paper_updated)
        reader.global_search_requested.connect(self.open_global_search)
        reader.add_paper_requested.connect(self.add_paper)
        reader.settings_requested.connect(self.open_settings)
        reader.project_search_requested.connect(
            lambda owner=reader: self._open_project_search_in_reader(owner)
        )
        reader.project_search_paper_requested.connect(
            self._open_project_search_reference
        )
        reader.library_requested.connect(self._show_library_from_workspace)
        reader.paper_switch_requested.connect(self.open_paper_by_id)
        reader.workspace_paper_activated.connect(self._activate_workspace_paper)
        reader.workspace_paper_close_requested.connect(self._close_workspace_paper)
        reader.workspace_paper_order_changed.connect(
            self._reorder_workspace_papers
        )
        reader.ai_comparison_add_requested.connect(self._add_to_ai_comparison)
        reader.ai_comparison_remove_requested.connect(
            self._remove_from_ai_comparison
        )
        reader.ai_group_conversation_requested.connect(
            self._restore_ai_group_conversation
        )
        reader.ai_solo_conversation_requested.connect(
            lambda conversation_id, owner=paper_id: (
                self._activate_solo_conversation(owner, conversation_id)
            )
        )
        reader.ai_group_paper_requested.connect(
            self._open_group_history_paper
        )
        reader.ai_context_paper_requested.connect(self.open_paper_by_id)
        reader.ai_conversation_deleted.connect(
            self._ai_conversation_deleted
        )
        reader.citation_navigation_requested.connect(self._open_workspace_citation)
        reader.reading_interaction.connect(self._on_reading_interaction)
        reader.ai_engaged.connect(self._on_ai_engaged)
        reader.about_to_close.connect(self._reader_about_to_close)
        reader.destroyed.connect(
            lambda _object=None, key=paper_id, token=id(reader): (
                self._reader_destroyed(key, token)
            )
        )
        return reader

    def open_paper_by_id(self, paper_id: int) -> PdfReaderWindow | None:
        paper_id = int(paper_id)
        if not self.project_repository.contains_paper(
            self.current_project_id, paper_id
        ):
            QMessageBox.warning(
                self,
                "Paper Outside Project",
                "This paper is not part of the current project.",
            )
            return None
        if paper_id in self._workspace_paper_ids:
            self._restore_project_workspace_readers()
            return self._activate_workspace_paper(paper_id)

        reader = self._create_workspace_reader(paper_id)
        if reader is None:
            return None
        if paper_id not in self._workspace_paper_ids:
            self._workspace_paper_ids.append(paper_id)
        self.project_repository.record_opened(self.current_project_id, paper_id)
        self._save_workspace_state()
        return self._activate_workspace_paper(paper_id)

    def _close_workspace_paper(self, paper_id: int) -> None:
        reader = self._pdf_readers.get(int(paper_id))
        if reader is not None:
            reader.close()

    def _reorder_workspace_papers(self, raw_order: object) -> None:
        try:
            order = [int(value) for value in raw_order]
        except (TypeError, ValueError):
            return
        if len(order) != len(set(order)):
            return
        if set(order) != set(self._workspace_paper_ids):
            return
        if order == self._workspace_paper_ids:
            return
        self._workspace_paper_ids = order
        self._save_workspace_state()
        self._sync_workspace_readers()

    def _ensure_ai_group_panel(
        self, conversation: Mapping[str, Any] | None = None
    ) -> None:
        conversation_id = self._ai_group_conversation_id
        if (
            self._ai_group_panel is not None
            or conversation_id is None
            or self._active_ai_conversation_type != "group"
            or self._active_ai_conversation_id != conversation_id
            or len(self._ai_group_paper_ids) < 2
        ):
            return
        conversation = (
            dict(conversation)
            if conversation is not None
            else AIRepository.get_conversation(
                conversation_id, project_id=self.current_project_id
            )
        )
        if conversation is None:
            self._dissolve_ai_group()
            return
        owner_id = int(self._ai_group_paper_ids[0])
        self._ai_group_panel = AIChatPanel(
            owner_id,
            project_id=self.current_project_id,
            repository=AIRepository,
            settings_repository=self.settings_repository,
            summary_repository=self.summary_repository,
        )
        self._ai_group_panel.load_group_chat(conversation)
        self._ai_group_panel.context_focus_requested.connect(
            self._focus_current_ai_paper
        )
        self._ai_group_panel.context_paper_remove_requested.connect(
            self._remove_from_ai_comparison
        )
        self._ai_group_panel.stop_requested.connect(
            self._stop_shared_group_request
        )

    @staticmethod
    def _persisted_group_state(
        conversation: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[int], dict[int, str]] | None:
        members = [dict(member) for member in conversation.get("members", [])]
        if len(members) < 2:
            return None
        paper_ids: list[int] = []
        aliases: dict[int, str] = {}
        seen_aliases: set[int] = set()
        for member in members:
            paper_id = int(member["id"])
            alias_index = int(member["alias_index"])
            if paper_id in aliases or alias_index < 1 or alias_index in seen_aliases:
                return None
            paper_ids.append(paper_id)
            aliases[paper_id] = f"P{alias_index}"
            seen_aliases.add(alias_index)
        return members, paper_ids, aliases

    def _activate_ai_conversation(self, conversation_id: int) -> bool:
        """Hydrate and commit one persisted conversation before repainting UI."""
        conversation = AIRepository.get_conversation(
            int(conversation_id), project_id=self.current_project_id
        )
        if conversation is None or conversation.get("conversation_type") != "group":
            return False
        persisted = self._persisted_group_state(conversation)
        if persisted is None:
            return False
        members, paper_ids, aliases = persisted
        restored_id = int(conversation["id"])
        reuse_panel = (
            self._ai_group_panel is not None
            and self._ai_group_panel.active_conversation_id
            == restored_id
        )
        self._reader_workspace.setUpdatesEnabled(False)
        try:
            if not reuse_panel:
                self._stop_group_ai()
                self._detach_ai_group_panel()
                if self._ai_group_panel is not None:
                    self._ai_group_panel.deleteLater()
                self._ai_group_panel = None

            # Clear every previous in-memory mapping before committing the one
            # persisted snapshot. The active tab is deliberately not consulted.
            self._ai_group_conversation_id = None
            self._ai_group_paper_ids = []
            self._ai_group_aliases = {}
            self._ai_group_members = []
            self._active_ai_conversation_id = restored_id
            self._active_ai_conversation_type = "group"
            self._ai_group_conversation_id = restored_id
            self._ai_group_paper_ids = list(paper_ids)
            self._ai_group_aliases = dict(aliases)
            self._ai_group_members = [dict(member) for member in members]

            if reuse_panel and self._ai_group_panel is not None:
                self._ai_group_panel.load_group_chat(conversation)
            else:
                self._ensure_ai_group_panel(conversation)
            self._sync_workspace_readers()
        finally:
            self._reader_workspace.setUpdatesEnabled(True)
            self._reader_workspace.update()
        return True

    def _restore_ai_group_conversation(self, conversation_id: int) -> None:
        self._activate_ai_conversation(int(conversation_id))

    def _detach_ai_group_panel(self) -> None:
        panel = self._ai_group_panel
        if panel is None:
            return
        for reader in tuple(self._pdf_readers.values()):
            if reader.reader_sidebar.ai is panel:
                reader.reader_sidebar.restore_solo_ai()
        panel.hide()
        panel.setParent(self._ai_panel_parking)

    def _apply_active_ai_mode(self) -> None:
        active_id = self._active_paper_id
        if active_id is None:
            return
        reader = self._pdf_readers.get(active_id)
        if reader is None:
            return
        use_group = (
            self._active_ai_conversation_type == "group"
            and self._active_ai_conversation_id
            == self._ai_group_conversation_id
            and self._ai_group_panel is not None
            and len(self._ai_group_paper_ids) >= 2
        )
        if use_group and reader.reader_sidebar.ai is self._ai_group_panel:
            return
        self._detach_ai_group_panel()
        if use_group and self._ai_group_panel is not None:
            reader.reader_sidebar.use_ai_panel(self._ai_group_panel)
        else:
            reader.reader_sidebar.restore_solo_ai()

    def _add_to_ai_comparison(self, paper_id: int) -> None:
        paper_id = int(paper_id)
        if paper_id not in self._workspace_paper_ids:
            return
        active_group = (
            self._active_ai_conversation_type == "group"
            and self._active_ai_conversation_id
            == self._ai_group_conversation_id
            and self._ai_group_conversation_id is not None
        )
        if not active_group:
            active_id = self._active_paper_id
            if active_id is None or active_id == paper_id:
                return
            members = [int(active_id), paper_id]
            conversation = AIRepository.create_group_conversation(
                members, project_id=self.current_project_id
            )
            self._restore_ai_group_conversation(int(conversation["id"]))
            return
        elif paper_id not in self._ai_group_paper_ids:
            if self._ai_group_conversation_id is None:
                return
            AIRepository.add_group_member(
                self._ai_group_conversation_id, paper_id
            )
            conversation = AIRepository.get_conversation(
                self._ai_group_conversation_id,
                project_id=self.current_project_id,
            )
            if conversation is not None:
                self._restore_ai_group_conversation(int(conversation["id"]))
            return
        self._ensure_ai_group_panel()
        self._sync_workspace_readers()

    def _stop_group_ai(self) -> None:
        panel = self._ai_group_panel
        if panel is None or not panel._busy:
            return
        for reader in tuple(self._pdf_readers.values()):
            if reader._ai_active_panel is panel:
                reader._stop_ai_request(panel.cancel_request())
                return
        for reader in tuple(self._pdf_readers.values()):
            if reader.reader_sidebar.ai is panel:
                reader._stop_ai_request(panel.cancel_request())
                return

    def _stop_shared_group_request(self, partial_text: str) -> None:
        panel = self._ai_group_panel
        if panel is None:
            return
        for reader in tuple(self._pdf_readers.values()):
            if reader._ai_active_panel is panel:
                reader._stop_ai_request(partial_text)
                return

    def _activate_solo_conversation(
        self, paper_id: int, conversation_id: object
    ) -> None:
        target_id = int(self._active_paper_id or paper_id)
        reader = self._pdf_readers.get(target_id)
        self._stop_group_ai()
        self._detach_ai_group_panel()
        if self._ai_group_panel is not None:
            self._ai_group_panel.deleteLater()
        self._ai_group_panel = None
        self._ai_group_conversation_id = None
        self._ai_group_paper_ids = []
        self._ai_group_aliases = {}
        self._ai_group_members = []
        self._active_ai_conversation_type = "solo"
        try:
            selected_id = int(conversation_id) if conversation_id is not None else None
        except (TypeError, ValueError):
            selected_id = None
        self._active_ai_conversation_id = selected_id
        if reader is not None:
            solo_panel = reader.reader_sidebar.solo_ai
            if selected_id is None and solo_panel.active_conversation_id is not None:
                solo_panel.new_chat(notify=False)
        self._sync_workspace_readers()

    def _open_group_history_paper(
        self, conversation_id: int, paper_id: int
    ) -> None:
        if self._activate_ai_conversation(int(conversation_id)):
            self.open_paper_by_id(int(paper_id))

    def _dissolve_ai_group(self) -> None:
        self._stop_group_ai()
        self._detach_ai_group_panel()
        if self._ai_group_panel is not None:
            self._ai_group_panel.deleteLater()
        self._ai_group_panel = None
        self._ai_group_conversation_id = None
        self._ai_group_paper_ids = []
        self._ai_group_aliases = {}
        self._ai_group_members = []
        self._active_ai_conversation_type = "solo"
        active_reader = self._pdf_readers.get(int(self._active_paper_id or 0))
        self._active_ai_conversation_id = (
            active_reader.reader_sidebar.solo_ai.active_conversation_id
            if active_reader is not None
            else None
        )

    def _remove_from_ai_comparison(self, paper_id: int) -> None:
        paper_id = int(paper_id)
        if paper_id not in self._ai_group_paper_ids:
            return
        if len(self._ai_group_paper_ids) <= 2:
            self._dissolve_ai_group()
            self._sync_workspace_readers()
            return
        if self._ai_group_conversation_id is None:
            return
        AIRepository.remove_group_member(
            self._ai_group_conversation_id, paper_id
        )
        conversation = AIRepository.get_conversation(
            self._ai_group_conversation_id,
            project_id=self.current_project_id,
        )
        if conversation is not None:
            self._restore_ai_group_conversation(int(conversation["id"]))

    def _ai_conversation_deleted(self, conversation_id: int) -> None:
        if int(conversation_id) == int(self._ai_group_conversation_id or 0):
            self._dissolve_ai_group()
            self._sync_workspace_readers()

    def _focus_current_ai_paper(self) -> None:
        if self._active_paper_id is None:
            return
        reader = self._pdf_readers.get(int(self._active_paper_id))
        if reader is None:
            return
        self._activate_solo_conversation(
            int(self._active_paper_id),
            reader.reader_sidebar.solo_ai.active_conversation_id,
        )

    def _reader_about_to_close(self, paper_id: int) -> None:
        paper_id = int(paper_id)
        was_active = self._active_paper_id == paper_id
        if was_active:
            self._remember_workspace_sidebar_state()
        reader = self._pdf_readers.get(paper_id)
        if (
            reader is not None
            and self._ai_group_panel is not None
            and reader.reader_sidebar.ai is self._ai_group_panel
        ):
            # Park the shared panel (and unbind this Reader's header proxy)
            # while the closing Reader still has a valid QObject tree.
            self._detach_ai_group_panel()
        try:
            index = self._workspace_paper_ids.index(paper_id)
        except ValueError:
            index = 0
        if paper_id in self._workspace_paper_ids:
            self._workspace_paper_ids.remove(paper_id)
        self._pdf_readers.pop(paper_id, None)
        self._reader_workspace.remove_reader(paper_id)
        if was_active:
            self._active_paper_id = None
        if self._workspace_transitioning or self._app_closing:
            return
        self._save_workspace_state()
        if was_active and self._workspace_paper_ids:
            next_index = min(index, len(self._workspace_paper_ids) - 1)
            self._activate_workspace_paper(self._workspace_paper_ids[next_index])
        elif not self._workspace_paper_ids:
            self._detach_ai_group_panel()
            self._show_library_from_workspace()
        else:
            self._sync_workspace_readers()

    def _reader_destroyed(self, paper_id: int, reader_token: int | None = None) -> None:
        current = self._pdf_readers.get(int(paper_id))
        if current is not None and (
            reader_token is None or id(current) == int(reader_token)
        ):
            self._pdf_readers.pop(int(paper_id), None)
        if (
            not self._workspace_paper_ids
            and not self._workspace_transitioning
            and not self._app_closing
        ):
            self._show_library_from_workspace()

    def _show_library_from_workspace(self) -> None:
        if self._app_closing:
            return
        self._remember_workspace_sidebar_state()
        if (
            self._workspace_sidebar_visible
            and self._workspace_sidebar_section == "search"
        ):
            self.open_project_ai_search()
        elif hasattr(self, "app_pages"):
            self.app_pages.setCurrentWidget(self.library_page)
        self._reader_workspace.hide()
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def _open_workspace_citation(self, paper_id: int, citation: object) -> None:
        reader = self.open_paper_by_id(int(paper_id))
        if reader is not None:
            reader._navigate_to_english_citation(citation)

    def _close_reader_workspace(self) -> None:
        self._workspace_transitioning = True
        try:
            for paper_id in tuple(self._workspace_paper_ids):
                reader = self._pdf_readers.get(paper_id)
                if reader is not None and not reader.close():
                    self._activate_workspace_paper(paper_id)
                    return
        finally:
            self._workspace_transitioning = False
        self._show_library_from_workspace()

    def open_global_search(self) -> None:
        if self._global_search_dialog is None:
            project = self.project_repository.get(self.current_project_id) or {}
            self._global_search_dialog = GlobalSearchDialog(
                self,
                project_id=self.current_project_id,
                project_name=str(project.get("name") or ""),
            )
            self._global_search_dialog.result_activated.connect(
                self._open_global_search_result
            )
        self._global_search_dialog.present(self.search_box.text().strip())

    def open_project_ai_search(self) -> None:
        panel = self._ensure_project_search_panel()
        if self.app_pages.indexOf(panel) < 0:
            self.app_pages.addWidget(panel)
        self.project_ai_search_nav_button.setChecked(True)
        self.app_pages.setCurrentWidget(panel)

    def _ensure_project_search_controller(
        self,
    ) -> ProjectSearchConversationController:
        project = self.project_repository.get(self.current_project_id) or {}
        if self._project_search_controller is None:
            self._project_search_controller = ProjectSearchConversationController(
                self.current_project_id,
                str(project.get("name") or ""),
                self,
                settings_repository=self.settings_repository,
            )
        else:
            self._project_search_controller.set_project(
                self.current_project_id, str(project.get("name") or "")
            )
        return self._project_search_controller

    def _ensure_project_search_panel(self) -> ProjectAISearchDialog | None:
        project = self.project_repository.get(self.current_project_id)
        if project is None:
            return None
        if self._project_ai_search_dialog is None:
            self._project_ai_search_dialog = ProjectAISearchDialog(
                controller=self._ensure_project_search_controller(),
                parent=self.app_pages,
                presentation_mode="library",
            )
            self._project_ai_search_dialog.paper_requested.connect(
                self._open_project_search_reference
            )
            self._project_ai_search_dialog.library_requested.connect(
                self._show_library_page
            )
        return self._project_ai_search_dialog

    def _open_project_search_in_reader(
        self, reader: PdfReaderWindow
    ) -> None:
        self._workspace_sidebar_visible = True
        self._workspace_sidebar_section = "search"
        reader.reader_sidebar.set_project_search_controller(
            self._ensure_project_search_controller()
        )

    def _open_project_search_reference(self, paper_id: int) -> None:
        self._workspace_sidebar_visible = True
        self._workspace_sidebar_section = "search"
        reader = self.open_paper_by_id(int(paper_id))
        if reader is not None:
            reader.apply_workspace_sidebar_state(True, "search")

    def _show_library_page(self, *, preserve_scope: bool = False) -> None:
        if not hasattr(self, "app_pages"):
            return
        self.app_pages.setCurrentWidget(self.library_page)
        if not preserve_scope and hasattr(self, "library_button"):
            getattr(
                self, "_last_library_scope_button", self.library_button
            ).setChecked(True)

    def _open_global_search_result(self, raw_result: object) -> None:
        if not isinstance(raw_result, Mapping):
            return
        try:
            paper_id = int(raw_result["paper_id"])
        except (KeyError, TypeError, ValueError):
            return
        reader = self.open_paper_by_id(paper_id)
        if reader is None:
            return
        source_type = str(raw_result.get("source_type") or "metadata")
        section = {
            "metadata": "info",
            "note": "notes",
            "highlight": "notes",
            "summary": "summary",
        }.get(source_type)
        page_number = raw_result.get("page_number")
        try:
            page = int(page_number) if page_number else None
        except (TypeError, ValueError):
            page = None
        if page is not None and page < 1:
            page = None
        source_id = raw_result.get("source_id")
        annotation_version = None
        note_id = None
        try:
            identifier = int(source_id) if source_id is not None else None
        except (TypeError, ValueError):
            identifier = None
        if source_type == "note" and identifier is not None:
            row = NoteRepository.get(identifier)
            if row is not None:
                annotation_version = str(row["document_version"] or "en")
                note_id = identifier
        elif source_type == "highlight" and identifier is not None:
            row = HighlightRepository.get(identifier)
            if row is not None:
                annotation_version = str(row["document_version"] or "en")
        try:
            context = {"section": section, "page_number": page}
            if annotation_version is not None:
                context["document_version"] = annotation_version
            if note_id is not None:
                context["note_id"] = note_id
            reader.open_context(**context)
        except ValueError as error:
            self._report_error("Could not open the search result", error)

    def _paper_changed(self, paper_id: int) -> None:
        if self.project_repository.status(
            self.current_project_id, paper_id
        ) == "Reading":
            self._trigger_research_summary(paper_id)
        reader = self._pdf_readers.get(paper_id)
        if reader is not None:
            paper = self._paper_by_id(paper_id, "Could not refresh the paper")
            if paper is not None:
                refresh_paper = getattr(reader, "refresh_paper", None)
                if callable(refresh_paper):
                    refresh_paper(paper)
                else:
                    reader.paper = paper
                    reader.setWindowTitle(
                        str(paper["title"] or "Research Assistant")
                    )
        self._sync_workspace_readers()
        self._reload_taxonomy()
        self.load_papers(preserve_paper_id=paper_id)

    def _on_reader_paper_updated(self, paper_id: int) -> None:
        if self.project_repository.status(
            self.current_project_id, paper_id
        ) == "Reading":
            self._trigger_research_summary(paper_id)
        self._sync_workspace_readers()
        self._reload_taxonomy()
        self.load_papers(preserve_paper_id=paper_id)

    # ------------------------------------------------------------------
    # Asynchronous import/index/backup/export
    # ------------------------------------------------------------------
    def add_paper(self) -> None:
        if self._import_in_progress:
            self.statusBar().showMessage("A paper import is already running", 4000)
            return
        source_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Add Research Paper",
            "",
            "PDF Files (*.pdf)",
        )
        if not source_path:
            return
        self._import_in_progress = True
        self.add_button.setEnabled(False)
        self.add_action.setEnabled(False)
        self.statusBar().showMessage("Checking and inspecting PDF…")
        self._run_worker(
            lambda: _inspect_import_candidate(self.import_service, source_path),
            on_result=self._import_inspection_finished,
            on_error=lambda error: self._import_failed("Could not inspect the PDF", error),
        )

    def _import_inspection_finished(self, raw_outcome: object) -> None:
        if not isinstance(raw_outcome, _ImportInspectionOutcome):
            self._import_failed("Could not inspect the PDF", RuntimeError("Unexpected import result"))
            return
        if raw_outcome.exact_duplicate is not None:
            duplicate = raw_outcome.exact_duplicate
            title = str(duplicate.get("title") or "Untitled paper")
            QMessageBox.information(
                self,
                "Paper Already Exists",
                f'This exact PDF is already in the Library as "{title}".',
            )
            paper_id = duplicate.get("id")
            self._finish_import()
            if paper_id is not None:
                self.project_repository.add_paper(
                    self.current_project_id,
                    int(paper_id),
                    status=str(duplicate.get("status") or "Unread"),
                )
                self.load_papers(preserve_paper_id=int(paper_id))
            return
        inspection = raw_outcome.inspection
        if inspection is None:
            self._import_failed("Could not inspect the PDF", RuntimeError("No PDF inspection was returned"))
            return
        if raw_outcome.potential_duplicates:
            details = "\n".join(
                f"• {candidate.title} — {candidate.reason}"
                for candidate in raw_outcome.potential_duplicates
            )
            answer = QMessageBox.warning(
                self,
                "Possible Duplicate",
                "A related paper may already exist:\n\n"
                f"{details}\n\nImport this PDF anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._finish_import()
                return
        try:
            collections = self.collection_repository.list_all(
                self.current_project_id
            )
        except (sqlite3.Error, OSError, ValueError) as error:
            self._import_failed("Could not load collections", error)
            return
        metadata = {
            "title": inspection.title,
            "authors": inspection.authors,
            "year": inspection.year,
            "doi": inspection.doi,
            "status": "Unread",
            "is_important": False,
        }
        dialog = ReviewPaperDialog(metadata, collections, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._finish_import()
            return
        reviewed = dialog.get_data()
        import_metadata = PaperImportMetadata(
            title=reviewed["title"],
            authors=reviewed["authors"],
            year=reviewed["year"],
            doi=reviewed["doi"],
        )
        self.statusBar().showMessage("Copying and indexing paper…")
        self._run_worker(
            lambda: _commit_import(
                self.import_service,
                inspection,
                import_metadata,
                tags=reviewed["tags"],
                collection_ids=reviewed["collection_ids"],
                status=reviewed["status"],
                is_important=reviewed["is_important"],
                project_id=self.current_project_id,
            ),
            on_result=self._import_commit_finished,
            on_error=lambda error: self._import_failed("Could not import the PDF", error),
        )

    def _import_commit_finished(self, raw_outcome: object) -> None:
        if not isinstance(raw_outcome, _ImportCommitOutcome):
            self._import_failed("Could not import the PDF", RuntimeError("Unexpected commit result"))
            return
        self._finish_import()
        paper = self.paper_repository.get_paper_by_id(raw_outcome.paper_id)
        self.project_repository.add_paper(
            self.current_project_id,
            raw_outcome.paper_id,
            status=str(paper["status"] if paper is not None else "Unread"),
        )
        self._reload_taxonomy()
        self.load_papers(preserve_paper_id=raw_outcome.paper_id)
        # Indexing is best-effort inside PaperImportService and is retried on
        # the next startup, so do not claim that it necessarily succeeded.
        self.statusBar().showMessage("Paper imported", 7000)

    def _import_failed(self, title: str, error: object) -> None:
        self._finish_import()
        exception = error if isinstance(error, BaseException) else RuntimeError(str(error))
        self._report_error(title, exception)

    def _finish_import(self) -> None:
        self._import_in_progress = False
        self.add_button.setEnabled(True)
        self.add_action.setEnabled(True)

    def _start_background_index(self) -> None:
        self.statusBar().showMessage("Checking search index…")
        self._run_worker(
            self.index_service.index_missing_papers,
            on_result=self._indexing_finished,
            on_error=lambda error: self._background_error("Background indexing failed", error),
        )

    def _indexing_finished(self, raw_outcomes: object) -> None:
        outcomes = raw_outcomes if isinstance(raw_outcomes, list) else []
        errors = sum(
            isinstance(outcome, IndexingOutcome) and outcome.status == "error"
            for outcome in outcomes
        )
        if errors:
            self.statusBar().showMessage(
                f"Search index updated with {errors} unavailable PDF(s)",
                8000,
            )
        else:
            self.statusBar().showMessage("Search index ready", 4000)

    def create_backup(self) -> None:
        try:
            backup_path = self.settings_repository.get("backup_path", "") or ""
        except sqlite3.Error as error:
            self._report_error("Could not read backup settings", error)
            return
        if not backup_path:
            backup_path = QFileDialog.getExistingDirectory(
                self,
                "Choose Backup Folder",
                str(Path.home()),
            )
            if not backup_path:
                return
            try:
                self.settings_repository.set("backup_path", backup_path)
            except sqlite3.Error as error:
                self._report_error("Could not save the backup folder", error)
                return
        self.backup_action.setEnabled(False)
        self.statusBar().showMessage("Creating backup…")
        self._run_worker(
            create_backup,
            database_module.DATABASE_PATH,
            MANAGED_PAPERS_ROOT,
            backup_path,
            on_result=self._backup_finished,
            on_error=lambda error: self._service_action_failed(
                self.backup_action, "Could not create the backup", error
            ),
        )

    def _backup_finished(self, raw_result: object) -> None:
        self.backup_action.setEnabled(True)
        if isinstance(raw_result, BackupResult):
            self.statusBar().showMessage(
                f"Backup created: {raw_result.archive_path}",
                10000,
            )
            LOGGER.info("Backup created: %s", raw_result.archive_path)

    def export_library(self, export_kind: str) -> None:
        configurations: dict[str, tuple[str, str, str, Callable[..., Path]]] = {
            "notes": ("Export Notes", "notes.md", "Markdown (*.md)", export_notes_markdown),
            "summaries": (
                "Export Summaries",
                "summaries.md",
                "Markdown (*.md)",
                export_summaries_markdown,
            ),
            "csv": ("Export Library", "library.csv", "CSV (*.csv)", export_library_csv),
            "bibtex": ("Export BibTeX", "library.bib", "BibTeX (*.bib)", export_bibtex),
        }
        configuration = configurations.get(export_kind)
        if configuration is None:
            return
        title, default_name, file_filter, function = configuration
        output_path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            title,
            str(Path.home() / default_name),
            file_filter,
        )
        if not output_path:
            return
        action = self.export_actions[export_kind]
        action.setEnabled(False)
        self.statusBar().showMessage(f"{title}…")
        self._run_worker(
            function,
            database_module.DATABASE_PATH,
            output_path,
            on_result=lambda result, current_action=action: self._export_finished(
                current_action, result
            ),
            on_error=lambda error, current_action=action: self._service_action_failed(
                current_action, f"{title} failed", error
            ),
        )

    def _export_finished(self, action: QAction, raw_path: object) -> None:
        action.setEnabled(True)
        self.statusBar().showMessage(f"Export created: {raw_path}", 10000)
        LOGGER.info("Export created: %s", raw_path)

    def _service_action_failed(
        self,
        action: QAction,
        title: str,
        error: object,
    ) -> None:
        action.setEnabled(True)
        exception = error if isinstance(error, BaseException) else RuntimeError(str(error))
        self._report_error(title, exception)

    def _run_worker(
        self,
        function: Callable[..., Any],
        *args: Any,
        on_result: Callable[[object], None] | None = None,
        on_error: Callable[[object], None] | None = None,
    ) -> FunctionWorker:
        worker = FunctionWorker(function, *args)
        self._workers.add(worker)
        if on_result is not None:
            worker.signals.result.connect(on_result)
        if on_error is not None:
            worker.signals.error.connect(on_error)
        else:
            worker.signals.error.connect(
                lambda error: self._background_error("Background task failed", error)
            )
        worker.signals.finished.connect(
            lambda current_worker=worker: self._workers.discard(current_worker)
        )
        QThreadPool.globalInstance().start(worker)
        return worker

    # ------------------------------------------------------------------
    # Settings, shortcuts, lifecycle, diagnostics
    # ------------------------------------------------------------------
    def open_settings(self) -> None:
        dialog = SettingsDialog(
            self,
            repository=self.settings_repository,
            library_path=MANAGED_PAPERS_ROOT,
        )
        dialog.settings_saved.connect(self._refresh_reader_ai_settings)
        dialog.exec()

    def _refresh_reader_ai_settings(self) -> None:
        for reader in tuple(self._pdf_readers.values()):
            refresh = getattr(reader, "refresh_ai_settings", None)
            if callable(refresh):
                refresh()
        if self._project_search_controller is not None:
            self._project_search_controller.refresh_settings()

    def _focus_search(self) -> None:
        self.search_box.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_box.selectAll()

    def _shortcut_open(self) -> None:
        if isinstance(QApplication.focusWidget(), QLineEdit):
            return
        paper_id = self._selected_paper_id()
        if paper_id is not None:
            self.open_paper_by_id(paper_id)

    def _shortcut_delete(self) -> None:
        if isinstance(QApplication.focusWidget(), QLineEdit):
            return
        paper_id = self._selected_paper_id()
        if paper_id is not None:
            self.delete_paper(paper_id)

    def _selected_paper_id(self) -> int | None:
        row = self.paper_table.currentRow() if hasattr(self, "paper_table") else -1
        if row < 0:
            return None
        item = self.paper_table.item(row, 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return int(value) if value is not None else None

    def _selected_paper(self) -> dict[str, Any] | None:
        paper_id = self._selected_paper_id()
        return self._rows.get(paper_id) if paper_id is not None else None

    def _paper_by_id(self, paper_id: int, error_title: str) -> Any | None:
        try:
            paper = self.paper_repository.get_paper_by_id(paper_id)
        except (sqlite3.Error, OSError, RuntimeError, ValueError) as error:
            self._report_error(error_title, error)
            return None
        if paper is None:
            self._missing_paper()
            return None
        value = dict(paper)
        project_status = self.project_repository.status(
            self.current_project_id, paper_id
        )
        if project_status:
            value["status"] = project_status
        return value

    def _missing_paper(self) -> None:
        QMessageBox.warning(
            self,
            "Paper Not Found",
            "This paper no longer exists in the Library.",
        )
        self.load_papers()

    def _background_error(self, title: str, error: object) -> None:
        exception = error if isinstance(error, BaseException) else RuntimeError(str(error))
        LOGGER.error(
            title,
            exc_info=(type(exception), exception, exception.__traceback__),
        )
        self.statusBar().showMessage(f"{title}: {exception}", 9000)

    def _report_error(self, title: str, error: BaseException) -> None:
        LOGGER.error(title, exc_info=(type(error), error, error.__traceback__))
        self.statusBar().showMessage(f"{title}: {error}", 9000)
        QMessageBox.critical(self, title, f"{title}.\n\n{error}")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._import_in_progress:
            QMessageBox.information(
                self,
                "Import in Progress",
                "Wait for the current paper import to finish before closing.",
            )
            event.ignore()
            return
        self._app_closing = True
        for reader in tuple(self._pdf_readers.values()):
            if not reader.close():
                self._app_closing = False
                self._activate_workspace_paper(reader.paper_id)
                event.ignore()
                return
        self._reader_workspace.hide()
        super().closeEvent(event)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            apply_ui_palette(
            """
            QMainWindow { background: @APP_BG@; }
            #sidebar { background: @SURFACE@; border-right: 1px solid @BORDER_SOFT@; }
            #logo { font-size: 21px; font-weight: 700; color: @TEXT@; }
            #logoSubtitle { font-size: 12px; color: @TEXT_MUTED@; }
            #sectionLabel { font-size: 10px; font-weight: 700; color: #969CA5; }
            #projectSelector {
                background: @PRIMARY_SOFT@; border: 1px solid #D5E1F4;
                border-radius: 9px; padding: 0 9px; color: @PRIMARY_TEXT@;
                font-weight: 600;
            }
            #sidebarButton, #collectionButton {
                min-height: 36px; border: none; border-radius: 7px; background: transparent;
                color: #586474; text-align: left; padding: 0 10px; font-size: 13px;
            }
            #sidebarButton:hover, #collectionButton:hover { background: @SURFACE_SUBTLE@; }
            #sidebarButton:checked, #collectionButton:checked {
                background: @PRIMARY_SOFT@; color: @PRIMARY_TEXT@; font-weight: 600;
            }
            #sidebarLink {
                border: none; background: transparent; color: #666D77;
                padding: 4px; font-size: 11px;
            }
            #sidebarLink:hover { color: #202328; }
            #collectionScroll { background: transparent; }
            #collectionScroll > QWidget > QWidget { background: transparent; }
            #content { background: @APP_BG@; }
            #pageTitle { font-size: 24px; font-weight: 700; color: @TEXT@; }
            #description, #resultCount { color: @TEXT_MUTED@; font-size: 12px; }
            #primaryButton {
                min-width: 116px; border: none; border-radius: 8px;
                background: @PRIMARY@; color: white; padding: 0 15px; font-weight: 600;
            }
            #primaryButton:hover { background: @PRIMARY_HOVER@; }
            #headerMenuButton {
                border: none; border-radius: 8px; background: transparent; padding: 0;
            }
            #headerMenuButton:hover, #headerMenuButton:pressed {
                background: @SURFACE_SUBTLE@;
            }
            #headerMenuButton::menu-indicator { image: none; width: 0; }
            #secondaryButton {
                min-width: 88px; border: 1px solid @BORDER@; border-radius: 8px;
                background: @SURFACE@; color: #4A5563; padding: 0 12px;
            }
            #secondaryButton:hover { background: @SURFACE_SUBTLE@; border-color: #CCD5E0; }
            #searchBox {
                background: @SURFACE@; border: 1px solid @BORDER@; border-radius: 8px;
                padding: 0 12px; color: #272B30; font-size: 13px;
            }
            #searchBox:focus { border: 2px solid @PRIMARY@; padding: 0 11px; }
            #paperTable {
                background: @SURFACE@; border: 1px solid @BORDER_SOFT@; border-radius: 10px;
                selection-background-color: @PRIMARY_SOFT@; selection-color: @TEXT@;
                outline: none; font-size: 12px;
            }
            #paperTable::item { border-bottom: 1px solid #EFF2F6; padding: 6px 8px; }
            #paperTable::item:hover { background: #F7F9FC; }
            #paperTable::item:selected { background: @PRIMARY_SOFT@; color: @TEXT@; }
            #paperTableCell { background: transparent; }
            #paperTitleText { color: @TEXT@; font-size: 13px; font-weight: 600; }
            #paperMetadataText { color: @TEXT_MUTED@; font-size: 11px; }
            #paperTagChip, #paperCollectionChip, #paperMoreChip {
                min-height: 16px; max-height: 16px; border: none; border-radius: 5px;
                background: #EEF2F7; color: #58687A; padding: 0 6px; font-size: 9px;
            }
            #paperCollectionChip { background: #F1F3F7; color: #667386; }
            #paperMoreChip { background: transparent; color: #7A8593; padding: 0 2px; }
            QHeaderView::section {
                background: #F8FAFC; color: #687382; border: none;
                border-bottom: 1px solid @BORDER_SOFT@; padding: 9px 8px; font-weight: 600;
            }
            QStatusBar { background: @SURFACE@; border-top: 1px solid @BORDER_SOFT@; color: #666D77; }
            QMenu { background: @SURFACE@; border: 1px solid @BORDER@; padding: 5px; }
            QMenu::item { padding: 7px 24px 7px 10px; border-radius: 5px; }
            QMenu::item:selected { background: @PRIMARY_SOFT@; color: @PRIMARY_TEXT@; }
            """
            )
        )


def _mapping(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return dict(row)
    keys = getattr(row, "keys", None)
    return {key: row[key] for key in keys()} if callable(keys) else {}


def _inspect_import_candidate(
    service: PaperImportService,
    source_path: str,
) -> _ImportInspectionOutcome:
    file_hash = calculate_sha256(source_path)
    duplicate = service.exact_duplicate(file_hash)
    if duplicate is not None:
        return _ImportInspectionOutcome(exact_duplicate=_mapping(duplicate))
    inspection = service.inspect(source_path)
    metadata = PaperImportMetadata(
        title=inspection.title,
        authors=inspection.authors,
        year=inspection.year,
        doi=inspection.doi,
    )
    potential = tuple(service.potential_duplicates(metadata))
    return _ImportInspectionOutcome(
        inspection=inspection,
        potential_duplicates=potential,
    )


def _commit_import(
    service: PaperImportService,
    inspection: PdfInspectionResult,
    metadata: PaperImportMetadata,
    *,
    tags: list[str],
    collection_ids: list[int],
    status: str,
    is_important: bool,
    project_id: int | None = None,
) -> _ImportCommitOutcome:
    paper_id = service.commit(
        inspection,
        metadata,
        tags=tags,
        collection_ids=collection_ids,
        status=status,
        is_important=is_important,
        project_id=project_id,
    )
    return _ImportCommitOutcome(paper_id)
