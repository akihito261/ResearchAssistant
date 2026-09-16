from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QFile,
    QIODevice,
    QPoint,
    QPropertyAnimation,
    QSignalBlocker,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QCursor,
    QDesktopServices,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QButtonGroup,
    QDialog,
    QFileDialog,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QMessageBox,
    QMenu,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWebEngineCore import (
    QWebEngineLoadingInfo,
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineSettings,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel

from app.bridge.pdf_reader_bridge import PdfReaderBridge
from app.database.paper_repository import PaperRepository
from app.database.document_version_repository import DocumentVersionRepository
from app.database.settings_repository import SettingsRepository
from app.database.summary_repository import SummaryRepository
from app.pdf.pdfjs_scheme import (
    SCHEME_HOST,
    SCHEME_NAME,
    SCHEME_NAME_TEXT,
    PdfJsSchemeHandler,
    build_pdfjs_viewer_url,
    is_pdfjs_scheme_registered,
    missing_pdfjs_assets,
)
from app.services.background_worker import FunctionWorker
from app.services.ai_chat_service import AIChatService, AIRequestControl
from app.services.library_path_service import resolve_paper_path
from app.services.vietnamese_pdf_service import VietnamesePdfService
from app.translation import MyMemoryTranslationProvider, TranslationProvider
from app.ui.icons import set_widget_icon
from app.ui.reader_sidebar import ReaderSidebar
from app.ui.quick_paper_switcher import (
    DRAWER_CLOSE_DURATION_MS,
    DRAWER_LEFT_MARGIN,
    DRAWER_OPEN_DURATION_MS,
    DRAWER_VERTICAL_MARGIN,
    EDGE_HIT_WIDTH,
    EdgeActivationZone,
    QuickPaperSwitcher,
    quick_drawer_width,
)
from app.ui.reader_workspace_tabs import ReaderWorkspaceTabs
from app.ui.ai_summary_preview_dialog import AISummaryPreviewDialog
from app.ui.citation_widgets import citation_value
from app.ui.ui_styles import MODERN_SCROLLBAR_QSS, apply_ui_palette
from app.runtime_paths import resource_path


LOGGER = logging.getLogger(__name__)
READER_INTEGRATION_PATH = resource_path(
    "resources", "pdfjs", "web", "research_assistant.js"
)


_PDFJS_STATE_HOOK = r"""
(() => {
  try {
    const preferences = JSON.parse(localStorage.getItem("pdfjs.preferences")) ?? {};
    preferences.enableWebGPU = false;
    localStorage.setItem("pdfjs.preferences", JSON.stringify(preferences));
  } catch {
    // PDF.js falls back safely when storage is unavailable.
  }

  const state = window.__researchAssistantPdfState = {
    state: "initializing",
    message: "",
    pages: 0,
    connected: false,
  };

  const connect = () => {
    const application = window.PDFViewerApplication;
    if (!application?.eventBus) {
      window.setTimeout(connect, 50);
      return;
    }
    if (state.connected) {
      return;
    }

    state.connected = true;
    state.state = "loading";

    application.eventBus.on("documentloaded", () => {
      state.state = "ready";
      state.pages = application.pdfDocument?.numPages ?? 0;
    });

    application.eventBus.on("pagesinit", () => {
      if (application.pdfDocument) {
        state.state = "ready";
        state.pages = application.pdfDocument.numPages ?? 0;
      }
    });

    application.eventBus.on("documenterror", event => {
      state.state = "error";
      state.message = event?.reason || event?.message || "PDF.js could not open this document.";
    });

    if (application.pdfDocument) {
      state.state = "ready";
      state.pages = application.pdfDocument.numPages ?? 0;
    }
  };

  connect();
})();
"""

_PDFJS_STATE_QUERY = r"""
(() => {
  const application = window.PDFViewerApplication;
  if (application?.pdfDocument) {
    return JSON.stringify({
      state: "ready",
      message: "",
      pages: application.pdfDocument.numPages ?? 0,
    });
  }
  return JSON.stringify(window.__researchAssistantPdfState ?? {
    state: "initializing",
    message: "",
    pages: 0,
  });
})();
"""


@lru_cache(maxsize=1)
def _reader_injection_source() -> str:
    """Load Qt's channel client and our integration into one ordered script."""
    channel_file = QFile(":/qtwebchannel/qwebchannel.js")
    if not channel_file.open(QIODevice.OpenModeFlag.ReadOnly):
        raise FileNotFoundError("Qt WebChannel JavaScript runtime is unavailable.")
    try:
        channel_source = bytes(channel_file.readAll()).decode("utf-8")
    finally:
        channel_file.close()

    try:
        integration_source = READER_INTEGRATION_PATH.read_text(encoding="utf-8")
    except OSError as error:
        raise FileNotFoundError(
            f"PDF Reader integration is missing:\n{READER_INTEGRATION_PATH}"
        ) from error
    return "\n".join((channel_source, _PDFJS_STATE_HOOK, integration_source))


def _is_trusted_reader_main_url(url: QUrl) -> bool:
    """Keep the privileged WebChannel page on the bundled Reader origin."""
    if url.scheme() == SCHEME_NAME_TEXT and url.host() == SCHEME_HOST:
        return True
    return url.scheme() == "about" and url.path() == "blank"


def _is_safe_external_reader_link(url: QUrl) -> bool:
    return url.isValid() and url.scheme().lower() in {"http", "https", "mailto"}


class PdfJsPage(QWebEnginePage):
    """Keep PDF.js console diagnostics in the application log."""

    def acceptNavigationRequest(
        self,
        url: QUrl,
        navigation_type: QWebEnginePage.NavigationType,
        is_main_frame: bool,
    ) -> bool:
        if is_main_frame and not _is_trusted_reader_main_url(url):
            if (
                navigation_type
                == QWebEnginePage.NavigationType.NavigationTypeLinkClicked
                and _is_safe_external_reader_link(url)
            ):
                QDesktopServices.openUrl(url)
            LOGGER.warning(
                "Blocked PDF Reader main-frame navigation outside the trusted origin: %s",
                url.toString(),
            )
            return False
        return super().acceptNavigationRequest(
            url,
            navigation_type,
            is_main_frame,
        )

    def javaScriptConsoleMessage(
        self,
        level: QWebEnginePage.JavaScriptConsoleMessageLevel,
        message: str,
        line_number: int,
        source_id: str,
    ) -> None:
        context = f"{source_id}:{line_number}" if source_id else f"line {line_number}"
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            if message.startswith("Failed to create WebGPU Context Provider"):
                LOGGER.debug("PDF.js: %s (%s)", message, context)
            else:
                LOGGER.error("PDF.js: %s (%s)", message, context)
        elif level == QWebEnginePage.JavaScriptConsoleMessageLevel.WarningMessageLevel:
            LOGGER.debug("PDF.js warning: %s (%s)", message, context)
        else:
            LOGGER.debug("PDF.js: %s (%s)", message, context)


class PdfReaderWindow(QMainWindow):
    document_ready = Signal(int)
    document_load_failed = Signal(str)
    paper_updated = Signal(int)
    global_search_requested = Signal()
    add_paper_requested = Signal()
    settings_requested = Signal()
    library_requested = Signal()
    paper_switch_requested = Signal(int)
    workspace_paper_activated = Signal(int)
    workspace_paper_close_requested = Signal(int)
    ai_comparison_add_requested = Signal(int)
    ai_comparison_remove_requested = Signal(int)
    workspace_paper_order_changed = Signal(object)
    ai_group_conversation_requested = Signal(int)
    ai_solo_conversation_requested = Signal(object)
    ai_group_paper_requested = Signal(int, int)
    ai_context_paper_requested = Signal(int)
    ai_conversation_deleted = Signal(int)
    project_search_requested = Signal()
    project_search_paper_requested = Signal(int)
    citation_navigation_requested = Signal(int, object)
    about_to_close = Signal(int)
    reading_interaction = Signal(int, str)
    ai_engaged = Signal(int, str, str)

    def __init__(
        self,
        paper: Any,
        parent=None,
        *,
        project_id: int | None = None,
        translation_provider: TranslationProvider | None = None,
        settings_repository: Any = SettingsRepository,
        document_version_repository: Any = DocumentVersionRepository,
        vietnamese_pdf_service: Any = None,
        ai_chat_service: AIChatService | None = None,
        paper_catalog_provider: Callable[[], tuple[list[Any], set[int]]] | None = None,
        embedded: bool = False,
    ) -> None:
        super().__init__(parent)

        self._embedded = bool(embedded)
        if self._embedded:
            self.setWindowFlags(Qt.WindowType.Widget)

        self.paper = paper
        self.paper_id = int(paper["id"])
        self.project_id = int(project_id) if project_id is not None else None
        self.reader_bridge = PdfReaderBridge(self.paper_id, self)
        self.translation_provider = translation_provider or MyMemoryTranslationProvider()
        self.settings_repository = settings_repository
        self.document_version_repository = document_version_repository
        self.vietnamese_pdf_service = vietnamese_pdf_service or VietnamesePdfService()
        self.ai_chat_service = ai_chat_service or AIChatService()
        self.paper_catalog_provider = paper_catalog_provider
        self.current_document_version = "en"
        self._vi_import_worker: FunctionWorker | None = None
        self._translation_generation = 0
        self._active_translation_request_id = ""
        self._translation_workers: set[FunctionWorker] = set()
        self._ai_worker: FunctionWorker | None = None
        self._ai_cancel_event: AIRequestControl | None = None
        self._ai_active_request: tuple[int, str, str] | None = None
        self._ai_active_panel: Any | None = None
        self._ai_request_generation = 0
        self._ai_summary_worker: FunctionWorker | None = None
        self._ai_summary_cancel: AIRequestControl | None = None
        self._ai_summary_generation = 0
        self._ai_summary_active_key: tuple[int, str, str, str] | None = None
        self._ai_summary_preview_cache: (
            tuple[tuple[int, str, str, str], object] | None
        ) = None
        self._ai_summary_timeout_timer = QTimer(self)
        self._ai_summary_timeout_timer.setSingleShot(True)
        self._ai_summary_timeout_timer.setInterval(120_000)
        self._ai_summary_timeout_timer.timeout.connect(self._ai_summary_timed_out)
        self._closing = False
        self.pdf_path: Path | None = None
        self._last_load_error = ""
        self._load_generation = 0
        self._document_check_attempt = 0
        self._document_ready_emitted = False
        self._pending_check_generation = 0
        self._pending_context_page: int | None = None
        self._pending_context_note_id: int | None = None
        self._pending_source_evidence = ""
        self._pending_source_page: int | None = None
        self._navigation_history: list[dict[str, object]] = []
        self._navigation_history_limit = 25
        self._restoring_navigation_history = False
        self._pending_navigation_restore: dict[str, object] | None = None
        self._sidebar_preferred_width = 420
        self._sidebar_shell_horizontal_margin = 17
        self._layout_refresh_pending = False
        self._workspace_papers: list[dict[str, object]] = []

        self.document_version_repository.ensure_original(self.paper_id)

        self._document_check_timer = QTimer(self)
        self._document_check_timer.setSingleShot(True)
        self._document_check_timer.timeout.connect(self._query_document_state)

        self.setWindowTitle(paper["title"] or "Research Assistant")
        self.resize(1500, 950)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self.setup_ui()
        self._setup_shortcuts()
        self.apply_style()
        self.open_pdf()

    def setup_ui(self) -> None:
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        top_bar.setFixedHeight(38)

        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 3, 12, 3)
        top_layout.setSpacing(4)

        self.back_button = QPushButton("Library")
        self.back_button.setObjectName("backButton")
        self.back_button.setFixedHeight(32)
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        set_widget_icon(self.back_button, "arrow-left", size=16)
        self.back_button.clicked.connect(self.library_requested.emit)

        top_layout.addWidget(self.back_button)

        self.position_back_button = QPushButton("Back")
        self.position_back_button.setObjectName("positionBackButton")
        self.position_back_button.setFixedHeight(32)
        self.position_back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        set_widget_icon(
            self.position_back_button,
            "arrow-left",
            size=16,
            tooltip="Back to previous position\nAlt+Left",
        )
        self.position_back_button.clicked.connect(self._back_to_previous_position)
        self.position_back_button.hide()
        top_layout.addWidget(self.position_back_button)

        self.workspace_tabs = ReaderWorkspaceTabs()
        self.workspace_tabs.paper_activated.connect(
            self.workspace_paper_activated
        )
        self.workspace_tabs.paper_close_requested.connect(
            self.workspace_paper_close_requested
        )
        self.workspace_tabs.add_to_ai_requested.connect(
            self.ai_comparison_add_requested
        )
        self.workspace_tabs.remove_from_ai_requested.connect(
            self.ai_comparison_remove_requested
        )
        self.workspace_tabs.paper_order_changed.connect(
            self.workspace_paper_order_changed
        )
        top_layout.addWidget(self.workspace_tabs, 1)

        self.version_buttons: dict[str, QPushButton] = {}
        self.version_button_group = QButtonGroup(self)
        self.version_button_group.setExclusive(True)
        version_switch = QFrame()
        version_switch.setObjectName("versionSwitch")
        version_switch.setFixedHeight(32)
        version_layout = QHBoxLayout(version_switch)
        version_layout.setContentsMargins(1, 1, 1, 1)
        version_layout.setSpacing(0)
        for version, label in (("en", "EN"), ("vi", "VI")):
            button = QPushButton(label)
            button.setObjectName("versionButton")
            button.setFixedHeight(28)
            button.setCheckable(True)
            button.setChecked(version == "en")
            button.clicked.connect(
                lambda checked, value=version: checked and self._switch_document_version(value)
            )
            version_layout.addWidget(button)
            self.version_button_group.addButton(button)
            self.version_buttons[version] = button
        vi_button = self.version_buttons["vi"]
        vi_button.setToolTip("Vietnamese PDF · right-click to replace or remove")
        vi_button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        vi_button.customContextMenuRequested.connect(self._show_vi_context_menu)
        top_layout.addWidget(version_switch)
        top_layout.addSpacing(4)

        self.sidebar_buttons: dict[str, QPushButton] = {}
        for section, label in (
            ("notes", "Notes"),
            ("summary", "Summary"),
            ("info", "Info"),
            ("ai", "AI"),
            ("search", "AI Search"),
        ):
            button = QPushButton(label)
            button.setObjectName("sidebarToggleButton")
            button.setProperty("section", section)
            button.setFixedHeight(32)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            set_widget_icon(
                button,
                {
                    "notes": "sticky-note",
                    "summary": "notebook-text",
                    "info": "info",
                    "ai": "sparkles",
                    "search": "search",
                }[section],
                size=16,
                tooltip=f"Open {label}",
                checked_color=(
                    "#756CE8" if section in {"ai", "search"} else "#4F7DF3"
                ),
            )
            if section == "search":
                button.clicked.connect(self._project_search_toggled)
            else:
                button.clicked.connect(
                    lambda checked, name=section: self._toggle_sidebar(name, checked)
                )
            self.sidebar_buttons[section] = button
            top_layout.addWidget(button)
        self.notes_button = self.sidebar_buttons["notes"]
        self.summary_button = self.sidebar_buttons["summary"]
        self.info_button = self.sidebar_buttons["info"]
        self.ai_button = self.sidebar_buttons["ai"]
        self.search_button = self.sidebar_buttons["search"]

        self.settings_button = QPushButton()
        self.settings_button.setObjectName("sidebarToggleButton")
        self.settings_button.setProperty("section", "utility")
        self.settings_button.setFixedSize(32, 32)
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        set_widget_icon(
            self.settings_button,
            "settings",
            size=16,
            tooltip="Settings",
        )
        self.settings_button.clicked.connect(self.settings_requested)
        top_layout.addWidget(self.settings_button)

        self.pdf_tools_button = QToolButton()
        self.pdf_tools_button.setObjectName("readerOverflowButton")
        self.pdf_tools_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.pdf_tools_button.setFixedSize(32, 32)
        set_widget_icon(
            self.pdf_tools_button,
            "more-horizontal",
            size=16,
            tooltip="PDF tools",
        )
        pdf_tools_menu = QMenu(self.pdf_tools_button)
        for label, command in (
            ("Find in PDF\tCtrl+F", "openFind"),
            ("Zoom in\tCtrl++", "zoomIn"),
            ("Zoom out\tCtrl+-", "zoomOut"),
        ):
            action = pdf_tools_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, value=command: self._run_pdfjs_command(value)
            )
        pdf_tools_menu.addSeparator()
        for label, command in (
            ("Automatic zoom", "zoomAutomatic"),
            ("Fit width", "fitWidth"),
            ("Fit page", "fitPage"),
        ):
            action = pdf_tools_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, value=command: self._run_pdfjs_command(value)
            )
        self.pdf_tools_button.setMenu(pdf_tools_menu)
        top_layout.addWidget(self.pdf_tools_button)

        main_layout.addWidget(top_bar)

        self.body_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.body_splitter.setObjectName("readerSplitter")
        self.body_splitter.setChildrenCollapsible(False)

        self.content_stack = QStackedWidget()
        self.content_stack.setMinimumSize(0, 0)
        self.content_stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.loading_widget = self._create_loading_widget()
        self.web_view = self._create_web_view()
        self.web_view.setMinimumSize(0, 0)
        self.web_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.error_widget = self._create_error_widget()

        self.content_stack.addWidget(self.loading_widget)
        self.content_stack.addWidget(self.web_view)
        self.content_stack.addWidget(self.error_widget)

        self.reader_sidebar = ReaderSidebar(
            self.paper,
            self.reader_bridge,
            project_id=self.project_id,
            settings_repository=self.settings_repository,
        )
        self.reader_sidebar.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self.reader_sidebar.close_requested.connect(self._close_sidebar)
        self.reader_sidebar.navigation_requested.connect(self._navigate_to_location)
        self.reader_sidebar.paper_updated.connect(self._on_paper_updated)
        self.reader_sidebar.translation_closed.connect(
            self._cancel_active_translation
        )
        self.reader_sidebar.transient_closed.connect(
            self._cancel_transient_from_sidebar
        )
        self.reader_sidebar.selection_note_saved.connect(
            self._on_selection_note_saved
        )
        self.reader_sidebar.translation_note_saved.connect(
            self._on_translation_note_saved
        )
        self.reader_sidebar.ai_send_requested.connect(self._on_ai_send_requested)
        self.reader_sidebar.ai_stop_requested.connect(self._stop_ai_request)
        self.reader_sidebar.ai_summarize_requested.connect(
            self._on_ai_summarize_requested
        )
        self.reader_sidebar.ai_summarize_cancel_requested.connect(
            self._cancel_ai_summary
        )
        self.reader_sidebar.citation_requested.connect(
            self._navigate_to_english_citation
        )
        self.reader_sidebar.ai_group_conversation_requested.connect(
            self.ai_group_conversation_requested
        )
        self.reader_sidebar.ai_solo_conversation_requested.connect(
            self.ai_solo_conversation_requested
        )
        self.reader_sidebar.ai_group_paper_requested.connect(
            self.ai_group_paper_requested
        )
        self.reader_sidebar.ai_context_paper_requested.connect(
            self.ai_context_paper_requested
        )
        self.reader_sidebar.ai_conversation_deleted.connect(
            self.ai_conversation_deleted
        )
        self.reader_sidebar.project_search_paper_requested.connect(
            self.project_search_paper_requested
        )
        self.reader_bridge.selectionNoteRequested.connect(
            self._on_selection_note_requested
        )
        self.reader_bridge.translationRequested.connect(
            self._on_translation_requested
        )
        self.reader_bridge.copyRequested.connect(self._on_copy_requested)
        self.reader_bridge.noteOpenRequested.connect(self._on_note_open_requested)
        self.reader_bridge.askAIRequested.connect(self._on_ask_ai_requested)
        self.reader_bridge.transientSelectionCancelled.connect(
            self._on_web_transient_cancelled
        )
        self.reader_bridge.highlightUpserted.connect(
            self.reader_sidebar.refresh_highlights
        )
        self.reader_bridge.highlightUpserted.connect(
            lambda _payload: self.reading_interaction.emit(
                self.paper_id, "annotation"
            )
        )
        self.reader_bridge.highlightDeleted.connect(
            self.reader_sidebar.refresh_highlights
        )
        self.reader_bridge.clientConnected.connect(self._apply_pending_context_page)
        self.reader_bridge.readingInteraction.connect(
            lambda kind: self.reading_interaction.emit(self.paper_id, kind)
        )

        self.sidebar_shell = QWidget()
        self.sidebar_shell.setObjectName("readerSidebarShell")
        self.sidebar_shell.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        sidebar_shell_chrome = self._sidebar_shell_horizontal_margin + 2
        self.sidebar_shell.setMinimumWidth(
            self.reader_sidebar.minimumWidth() + sidebar_shell_chrome
        )
        self.sidebar_shell.setMaximumWidth(
            self.reader_sidebar.maximumWidth() + sidebar_shell_chrome
        )
        sidebar_shell_layout = QVBoxLayout(self.sidebar_shell)
        sidebar_shell_layout.setContentsMargins(8, 9, 9, 9)
        sidebar_shell_layout.setSpacing(0)

        self.sidebar_surface = QFrame()
        self.sidebar_surface.setObjectName("readerSidebarSurface")
        self.sidebar_surface.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        sidebar_surface_layout = QVBoxLayout(self.sidebar_surface)
        sidebar_surface_layout.setContentsMargins(1, 1, 1, 1)
        sidebar_surface_layout.setSpacing(0)
        sidebar_surface_layout.addWidget(self.reader_sidebar)
        sidebar_shell_layout.addWidget(self.sidebar_surface)

        sidebar_shadow = QGraphicsDropShadowEffect(self.sidebar_surface)
        sidebar_shadow.setBlurRadius(18)
        sidebar_shadow.setOffset(-2, 1)
        sidebar_shadow.setColor(QColor(38, 53, 70, 28))
        self.sidebar_surface.setGraphicsEffect(sidebar_shadow)
        self._sidebar_shadow = sidebar_shadow
        self.sidebar_shell.hide()

        self.body_splitter.addWidget(self.content_stack)
        self.body_splitter.addWidget(self.sidebar_shell)
        self.body_splitter.setStretchFactor(0, 1)
        self.body_splitter.setStretchFactor(1, 0)
        main_layout.addWidget(self.body_splitter, 1)
        self._setup_quick_paper_switcher(central_widget)

    def _setup_quick_paper_switcher(self, parent: QWidget) -> None:
        self._quick_switch_open_timer = QTimer(self)
        self._quick_switch_open_timer.setSingleShot(True)
        self._quick_switch_open_timer.setInterval(280)
        self._quick_switch_open_timer.timeout.connect(
            self._show_quick_paper_switcher
        )
        self._quick_switch_close_timer = QTimer(self)
        self._quick_switch_close_timer.setSingleShot(True)
        self._quick_switch_close_timer.setInterval(240)
        self._quick_switch_close_timer.timeout.connect(
            self._close_quick_paper_switcher_if_outside
        )

        self.quick_paper_hotspot = EdgeActivationZone(parent)
        self.quick_paper_hotspot.pointer_entered.connect(
            self._quick_paper_hotspot_entered
        )
        self.quick_paper_hotspot.pointer_left.connect(
            self._quick_paper_hotspot_left
        )

        self.quick_paper_switcher = QuickPaperSwitcher(parent)
        self.quick_paper_switcher.hide()
        self.quick_paper_switcher.pointer_entered.connect(
            self._quick_paper_drawer_entered
        )
        self.quick_paper_switcher.pointer_left.connect(
            self._schedule_quick_paper_close
        )
        self.quick_paper_switcher.close_requested.connect(
            self._hide_quick_paper_switcher
        )
        self.quick_paper_switcher.paper_selected.connect(
            self._quick_paper_selected
        )
        self._quick_switch_target_open = False
        self._quick_switch_open_position = QPoint()
        self._quick_switch_closed_position = QPoint()
        self._quick_switch_animation = QPropertyAnimation(
            self.quick_paper_switcher,
            b"pos",
            self,
        )
        self._quick_switch_animation.finished.connect(
            self._quick_paper_animation_finished
        )
        self._position_quick_paper_switcher()

    def _position_quick_paper_switcher(self) -> None:
        if not hasattr(self, "quick_paper_hotspot"):
            return
        was_running = (
            self._quick_switch_animation.state()
            == QAbstractAnimation.State.Running
        )
        current_position = self.quick_paper_switcher.pos()
        animation_duration = self._quick_switch_animation.duration()
        animation_time = self._quick_switch_animation.currentTime()
        if was_running:
            self._quick_switch_animation.stop()

        body = self.body_splitter.geometry()
        body_width = max(0, body.width())
        body_height = max(0, body.height())
        drawer_width = quick_drawer_width(body_width)
        self.quick_paper_hotspot.setGeometry(
            body.x(), body.y(), EDGE_HIT_WIDTH, body_height
        )
        drawer_height = max(0, body_height - (2 * DRAWER_VERTICAL_MARGIN))
        self.quick_paper_switcher.resize(drawer_width, drawer_height)
        self._quick_switch_open_position = QPoint(
            body.x() + DRAWER_LEFT_MARGIN,
            body.y() + DRAWER_VERTICAL_MARGIN,
        )
        self._quick_switch_closed_position = QPoint(
            body.x() - drawer_width - 24,
            body.y() + DRAWER_VERTICAL_MARGIN,
        )

        if was_running:
            bounded_x = max(
                self._quick_switch_closed_position.x(),
                min(self._quick_switch_open_position.x(), current_position.x()),
            )
            self.quick_paper_switcher.move(
                bounded_x,
                self._quick_switch_open_position.y(),
            )
            progress = (
                min(1.0, animation_time / animation_duration)
                if animation_duration > 0
                else 1.0
            )
            base_duration = (
                DRAWER_OPEN_DURATION_MS
                if self._quick_switch_target_open
                else DRAWER_CLOSE_DURATION_MS
            )
            self._animate_quick_paper_switcher(
                self._quick_switch_target_open,
                duration=max(45, int(base_duration * (1.0 - progress))),
            )
        elif self.quick_paper_switcher.isVisible() and self._quick_switch_target_open:
            self.quick_paper_switcher.move(self._quick_switch_open_position)
        else:
            self.quick_paper_switcher.move(self._quick_switch_closed_position)

        if self.quick_paper_switcher.isVisible():
            self.quick_paper_switcher.raise_()
            self.quick_paper_hotspot.raise_()
        else:
            self.quick_paper_hotspot.raise_()

    def _animate_quick_paper_switcher(
        self,
        opening: bool,
        *,
        duration: int | None = None,
    ) -> None:
        self._quick_switch_animation.stop()
        self._quick_switch_target_open = opening
        target = (
            self._quick_switch_open_position
            if opening
            else self._quick_switch_closed_position
        )
        if self.quick_paper_switcher.pos() == target:
            self._quick_paper_animation_finished()
            return
        self._quick_switch_animation.setStartValue(self.quick_paper_switcher.pos())
        self._quick_switch_animation.setEndValue(target)
        self._quick_switch_animation.setDuration(
            duration
            if duration is not None
            else (
                DRAWER_OPEN_DURATION_MS if opening else DRAWER_CLOSE_DURATION_MS
            )
        )
        self._quick_switch_animation.setEasingCurve(
            QEasingCurve.Type.OutCubic
            if opening
            else QEasingCurve.Type.InOutCubic
        )
        self._quick_switch_animation.start()

    def _quick_paper_animation_finished(self) -> None:
        if self._quick_switch_target_open:
            self.quick_paper_switcher.move(self._quick_switch_open_position)
            self.quick_paper_switcher.raise_()
            self.quick_paper_hotspot.raise_()
            return
        self.quick_paper_switcher.hide()
        self.quick_paper_hotspot.set_active(False)
        self.quick_paper_hotspot.raise_()
        if hasattr(self, "quick_switcher_escape_shortcut"):
            self.quick_switcher_escape_shortcut.setEnabled(False)
        if hasattr(self, "sidebar_escape_shortcut"):
            self.sidebar_escape_shortcut.setEnabled(True)

    def _quick_paper_hotspot_entered(self) -> None:
        self._quick_switch_close_timer.stop()
        if self.quick_paper_switcher.isVisible():
            if not self._quick_switch_target_open:
                self._animate_quick_paper_switcher(True)
            return
        if QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            return
        self.quick_paper_hotspot.set_active(True)
        self._quick_switch_open_timer.start()

    def _quick_paper_hotspot_left(self) -> None:
        self._quick_switch_open_timer.stop()
        if self.quick_paper_switcher.isVisible():
            self._schedule_quick_paper_close()
        else:
            self.quick_paper_hotspot.set_active(False)

    def _quick_paper_drawer_entered(self) -> None:
        self._quick_switch_close_timer.stop()
        self.quick_paper_hotspot.set_active(True)
        if (
            self.quick_paper_switcher.isVisible()
            and not self._quick_switch_target_open
        ):
            self._animate_quick_paper_switcher(True)

    def _schedule_quick_paper_close(self) -> None:
        if self.quick_paper_switcher.isVisible():
            self._quick_switch_close_timer.start()

    def _quick_paper_catalog(self) -> tuple[list[Any], set[int]]:
        if self.paper_catalog_provider is None:
            return PaperRepository.list_papers(sort_by="updated"), {self.paper_id}
        papers, open_ids = self.paper_catalog_provider()
        return list(papers), {int(value) for value in open_ids}

    @staticmethod
    def _global_pointer_inside(widget: QWidget) -> bool:
        if not widget.isVisible():
            return False
        local_position = widget.mapFromGlobal(QCursor.pos())
        return widget.rect().contains(local_position)

    def _close_quick_paper_switcher_if_outside(self) -> None:
        if self._global_pointer_inside(
            self.quick_paper_switcher
        ) or self._global_pointer_inside(self.quick_paper_hotspot):
            return
        self._hide_quick_paper_switcher()

    def _show_quick_paper_switcher(self) -> None:
        if self._closing or QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            self.quick_paper_hotspot.set_active(False)
            return
        if not self._global_pointer_inside(self.quick_paper_hotspot):
            self.quick_paper_hotspot.set_active(False)
            return
        try:
            papers, open_ids = self._quick_paper_catalog()
        except (sqlite3.Error, RuntimeError, ValueError, TypeError):
            LOGGER.exception("Could not load papers for the quick switcher")
            papers, open_ids = [self.paper], {self.paper_id}
        self._quick_switch_close_timer.stop()
        self.quick_paper_switcher.set_catalog(
            papers,
            open_ids,
            self.paper_id,
        )
        self._position_quick_paper_switcher()
        self.quick_paper_switcher.show()
        self.quick_paper_switcher.raise_()
        self.quick_paper_hotspot.raise_()
        self.quick_paper_hotspot.set_active(True)
        self.quick_switcher_escape_shortcut.setEnabled(True)
        self.sidebar_escape_shortcut.setEnabled(False)
        self._animate_quick_paper_switcher(True)

    def _hide_quick_paper_switcher(self) -> None:
        self._quick_switch_open_timer.stop()
        self._quick_switch_close_timer.stop()
        self.quick_paper_hotspot.set_active(False)
        if not self.quick_paper_switcher.isVisible():
            self._quick_paper_animation_finished()
            return
        if (
            not self._quick_switch_target_open
            and self._quick_switch_animation.state()
            == QAbstractAnimation.State.Running
        ):
            return
        self._animate_quick_paper_switcher(False)

    def _quick_paper_selected(self, paper_id: int) -> None:
        self._hide_quick_paper_switcher()
        if int(paper_id) != self.paper_id:
            self.paper_switch_requested.emit(int(paper_id))

    def set_workspace_papers(
        self,
        papers: list[dict[str, object]],
        active_paper_id: int,
        ai_aliases: Mapping[int, str] | None = None,
    ) -> None:
        """Refresh session tabs without reloading this paper's Reader page."""
        self._workspace_papers = [dict(paper) for paper in papers]
        self.workspace_tabs.set_papers(
            self._workspace_papers,
            active_paper_id,
            ai_aliases or {},
        )

    def _toggle_sidebar(self, section: str, visible: bool) -> None:
        if not visible and self.sidebar_shell.isVisible():
            self._close_sidebar()
            return
        if not visible:
            return
        for name, button in self.sidebar_buttons.items():
            blocker = QSignalBlocker(button)
            button.setChecked(name == section)
            del blocker
        self.reader_sidebar.show_section(section)
        self.reader_sidebar.show()
        self.sidebar_shell.show()
        self._schedule_sidebar_layout_refresh()

    def _project_search_toggled(self, visible: bool) -> None:
        if visible:
            self.project_search_requested.emit()
        self._toggle_sidebar("search", visible)

    def _schedule_sidebar_layout_refresh(self) -> None:
        if self._layout_refresh_pending:
            return
        self._layout_refresh_pending = True
        QTimer.singleShot(0, self, self.refresh_reader_layout)

    def refresh_reader_layout(self) -> None:
        """Fit Reader and sidebar to the current outer splitter geometry."""
        self._layout_refresh_pending = False
        central_layout = self.centralWidget().layout() if self.centralWidget() else None
        if central_layout is not None:
            central_layout.activate()
        total_width = max(0, self.body_splitter.contentsRect().width())
        if total_width <= 0:
            return
        if not self.sidebar_shell.isVisible():
            self.body_splitter.setSizes([total_width, 0])
            QTimer.singleShot(0, self, self._notify_pdfjs_resize)
            return

        available_width = max(0, total_width - self.body_splitter.handleWidth())
        sidebar_minimum = self.sidebar_shell.minimumWidth()
        sidebar_maximum = self.sidebar_shell.maximumWidth()
        max_for_sidebar = max(sidebar_minimum, available_width - 360)
        sidebar_width = max(
            sidebar_minimum,
            min(
                self._sidebar_preferred_width
                + self._sidebar_shell_horizontal_margin
                + 2,
                sidebar_maximum,
                max_for_sidebar,
            ),
        )
        self.body_splitter.setSizes(
            [max(1, available_width - sidebar_width), sidebar_width]
        )
        QTimer.singleShot(0, self, self._notify_pdfjs_resize)

    def _refresh_sidebar_layout(self) -> None:
        """Compatibility alias for focused smoke checks."""
        self.refresh_reader_layout()

    def _notify_pdfjs_resize(self) -> None:
        if self._closing or not self.web_view.isVisible():
            return
        if self.web_view.width() > self.content_stack.contentsRect().width():
            LOGGER.warning(
                "Reader web view exceeded its content area "
                "(web=%s, content=%s)",
                self.web_view.width(),
                self.content_stack.contentsRect().width(),
            )
        self.web_view.page().runJavaScript(
            "window.dispatchEvent(new Event('resize'));"
        )

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._schedule_sidebar_layout_refresh()
        QTimer.singleShot(0, self, self._position_quick_paper_switcher)

    def changeEvent(self, event: Any) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            # The native window receives its final maximize/restore geometry
            # after this event, so size the splitter on the next event-loop turn.
            self._schedule_sidebar_layout_refresh()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._position_quick_paper_switcher()
        self._schedule_sidebar_layout_refresh()

    def _show_sidebar(self, section: str) -> None:
        self._toggle_sidebar(section, True)

    def workspace_sidebar_state(self) -> tuple[bool, str]:
        """Return explicit sidebar state even while this stacked page is hidden."""
        index = self.reader_sidebar.stack.currentIndex()
        section = next(
            (
                name
                for name, value in ReaderSidebar.SECTIONS.items()
                if value == index
            ),
            "notes",
        )
        return not self.sidebar_shell.isHidden(), section

    def apply_workspace_sidebar_state(self, visible: bool, section: str) -> None:
        """Apply workspace-owned sidebar visibility without replacing its content."""
        if section not in ReaderSidebar.SECTIONS:
            section = "notes"
        if visible:
            self._show_sidebar(section)
        elif not self.sidebar_shell.isHidden():
            self._close_sidebar()

    def open_context(
        self,
        section: str | None = None,
        page_number: int | None = None,
        *,
        document_version: str | None = None,
        note_id: int | None = None,
    ) -> None:
        """Reveal a Reader section and/or navigate to a one-based PDF page."""
        if section is not None and section not in ReaderSidebar.SECTIONS:
            raise ValueError(f"Unknown Reader sidebar section: {section}")
        if page_number is not None and (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number < 1
        ):
            raise ValueError("Reader page_number must be a positive integer.")
        if document_version is not None and document_version not in {"en", "vi"}:
            raise ValueError("Reader document_version must be 'en' or 'vi'.")
        if note_id is not None and (
            isinstance(note_id, bool) or not isinstance(note_id, int) or note_id < 1
        ):
            raise ValueError("Reader note_id must be a positive integer.")

        if not self._embedded:
            self.show()
            if self.isMinimized():
                self.showNormal()
            self.raise_()
            self.activateWindow()
        if section is not None:
            self._show_sidebar(section)

        def navigate() -> None:
            if page_number is not None:
                self._pending_context_page = page_number
            if note_id is not None:
                self._pending_context_note_id = note_id
            if (
                document_version is not None
                and document_version != self.current_document_version
            ):
                self._switch_document_version(document_version)
                return
            self._apply_pending_context_page()

        if page_number is not None:
            self._record_position_then(navigate)
        else:
            navigate()

    def refresh_paper(self, paper: Any) -> bool:
        """Refresh library metadata without discarding unsaved Info edits."""
        if int(paper["id"]) != self.paper_id:
            raise ValueError("Cannot refresh a Reader window with another paper.")
        self.paper = paper
        title = str(paper["title"] or "")
        self.setWindowTitle(title or "Research Assistant")
        return self.reader_sidebar.refresh_paper(paper)

    def _run_pdfjs_command(self, command: str) -> None:
        allowed = {
            "openFind",
            "zoomIn",
            "zoomOut",
            "zoomAutomatic",
            "fitWidth",
            "fitPage",
        }
        if command not in allowed or self._closing:
            return
        self.web_view.page().runJavaScript(
            f"window.ResearchAssistantReader?.{command}?.();"
        )

    def _reader_position(self, raw: object, version: str) -> dict[str, object] | None:
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                return None
        else:
            value = raw if isinstance(raw, Mapping) else None
        if not isinstance(value, Mapping) or value.get("version") != 1:
            return None
        page = value.get("page")
        scroll_top = value.get("scrollTop")
        page_offset = value.get("pageOffset")
        page_offset_ratio = value.get("pageOffsetRatio", 0)
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            return None
        numbers: list[float] = []
        for item in (scroll_top, page_offset, page_offset_ratio):
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                return None
            number = float(item)
            if not math.isfinite(number) or abs(number) > 10_000_000:
                return None
            numbers.append(number)
        return {
            "version": 1,
            "documentVersion": version,
            "page": page,
            "scrollTop": numbers[0],
            "pageOffset": numbers[1],
            "pageOffsetRatio": numbers[2],
        }

    @staticmethod
    def _same_reader_position(
        first: Mapping[str, object], second: Mapping[str, object]
    ) -> bool:
        return (
            first.get("documentVersion") == second.get("documentVersion")
            and first.get("page") == second.get("page")
            and abs(
                float(first.get("pageOffset", 0))
                - float(second.get("pageOffset", 0))
            )
            <= 32.0
        )

    def _push_navigation_position(self, position: dict[str, object]) -> None:
        if self._navigation_history and self._same_reader_position(
            self._navigation_history[-1], position
        ):
            return
        self._navigation_history.append(position)
        if len(self._navigation_history) > self._navigation_history_limit:
            del self._navigation_history[:-self._navigation_history_limit]
        self._update_position_back_button()

    def _update_position_back_button(self) -> None:
        self.position_back_button.setVisible(bool(self._navigation_history))

    def _discard_navigation_history_for_version(self, version: str) -> None:
        self._navigation_history = [
            position
            for position in self._navigation_history
            if position.get("documentVersion") != version
        ]
        self._update_position_back_button()

    def _record_position_then(self, callback: Callable[[], None]) -> None:
        if (
            self._restoring_navigation_history
            or self._closing
            or not self._document_ready_emitted
            or not self.reader_bridge.fingerprint
        ):
            callback()
            return
        generation = self._load_generation
        version = self.current_document_version

        def captured(raw: object) -> None:
            if self._closing:
                return
            if generation == self._load_generation and version == self.current_document_version:
                position = self._reader_position(raw, version)
                if position is not None:
                    self._push_navigation_position(position)
            callback()

        self.web_view.page().runJavaScript(
            "window.ResearchAssistantReader?.getReaderPosition?.() ?? '';",
            captured,
        )

    def _path_for_document_version(self, version: str) -> Path:
        if version == "en":
            path = resolve_paper_path(str(self.paper["file_path"] or ""))
        elif version == "vi":
            record = self.document_version_repository.get(self.paper_id, "vi")
            if record is None:
                raise FileNotFoundError("The previous Vietnamese PDF is no longer available.")
            path = resolve_paper_path(str(record["file_path"] or ""))
        else:
            raise ValueError("Unsupported Reader document version.")
        if not path.is_file() or path.suffix.lower() != ".pdf":
            raise FileNotFoundError("The previous PDF position is no longer available.")
        return path

    def _restore_reader_position(self, position: Mapping[str, object]) -> None:
        if self._closing or not self._document_ready_emitted:
            return
        page = min(int(position["page"]), max(1, self.reader_bridge.page_count))
        payload = json.dumps(
            {
                "version": 1,
                "page": page,
                "scrollTop": float(position.get("scrollTop", 0)),
                "pageOffset": float(position.get("pageOffset", 0)),
                "pageOffsetRatio": float(position.get("pageOffsetRatio", 0)),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )

        def restored(_result: object) -> None:
            self._restoring_navigation_history = False
            self._update_position_back_button()

        self.web_view.page().runJavaScript(
            "window.ResearchAssistantReader?.clearSourceHighlight?.();"
            f"window.ResearchAssistantReader?.restoreReaderPosition?.({payload});",
            restored,
        )

    def _back_to_previous_position(self) -> None:
        if self._restoring_navigation_history or not self._navigation_history:
            return
        position = self._navigation_history.pop()
        self._update_position_back_button()
        version = str(position.get("documentVersion") or "")
        self._restoring_navigation_history = True
        self._pending_context_page = None
        self._pending_context_note_id = None
        self._pending_source_page = None
        self._pending_source_evidence = ""
        self.web_view.page().runJavaScript(
            "window.ResearchAssistantReader?.clearSourceHighlight?.();"
        )
        if version == self.current_document_version:
            self._restore_reader_position(position)
            return
        if not self.reader_sidebar.flush_pending_saves():
            self._navigation_history.append(position)
            self._restoring_navigation_history = False
            self._update_position_back_button()
            return
        try:
            path = self._path_for_document_version(version)
        except (OSError, ValueError) as error:
            LOGGER.warning("Could not restore previous Reader position: %s", error)
            self._restoring_navigation_history = False
            self._update_position_back_button()
            return
        self._pending_navigation_restore = position
        self._cancel_all_transient()
        self._activate_document_version(version, path)

    def _apply_pending_context_page(self, *_args: Any) -> None:
        page_number = self._pending_context_page
        note_id = self._pending_context_note_id
        if (page_number is None and note_id is None) or not self._document_ready_emitted:
            return
        if not self.reader_bridge.fingerprint or self.reader_bridge.page_count < 1:
            return
        if note_id is not None:
            self.reader_sidebar.open_note(note_id)
            self._pending_context_note_id = None
        if page_number is None:
            return
        self._pending_context_page = None
        if page_number > self.reader_bridge.page_count:
            LOGGER.warning(
                "Could not navigate Reader context to page %s; document has %s pages.",
                page_number,
                self.reader_bridge.page_count,
            )
            return
        self.reader_bridge.navigate_to(
            {
                "version": 1,
                "fingerprint": self.reader_bridge.fingerprint,
                "segments": [{"page": page_number}],
            }
        )
        if self._pending_source_page == page_number and self._pending_source_evidence:
            evidence = self._pending_source_evidence
            self._pending_source_page = None
            self._pending_source_evidence = ""
            QTimer.singleShot(
                120,
                lambda value=page_number, text=evidence: self._show_source_evidence(
                    value, text
                ),
            )

    def _close_sidebar(self) -> None:
        draft = self.reader_sidebar.notes.draft_card
        if draft is not None and draft.is_dirty and not draft.save():
            return
        self._cancel_all_transient()
        self.sidebar_shell.hide()
        self._schedule_sidebar_layout_refresh()
        for button in self.sidebar_buttons.values():
            blocker = QSignalBlocker(button)
            button.setChecked(False)
            del blocker
        self.web_view.setFocus(Qt.FocusReason.OtherFocusReason)

    def _setup_shortcuts(self) -> None:
        self.new_note_shortcut = QShortcut(QKeySequence("Ctrl+N"), self)
        self.new_note_shortcut.activated.connect(self._new_note)
        self.save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        self.save_shortcut.activated.connect(self._save_reader_data)
        self.global_search_shortcut = QShortcut(
            QKeySequence("Ctrl+Shift+F"), self
        )
        self.global_search_shortcut.activated.connect(
            self.global_search_requested.emit
        )
        self.add_paper_shortcut = QShortcut(QKeySequence("Ctrl+O"), self)
        self.add_paper_shortcut.activated.connect(self.add_paper_requested.emit)
        self.position_back_shortcut = QShortcut(QKeySequence("Alt+Left"), self)
        self.position_back_shortcut.activated.connect(
            self._back_to_previous_position
        )
        self.sidebar_escape_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_Escape), self.reader_sidebar
        )
        self.sidebar_escape_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self.sidebar_escape_shortcut.activated.connect(self._close_sidebar)
        self.quick_switcher_escape_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_Escape), self
        )
        self.quick_switcher_escape_shortcut.setContext(
            Qt.ShortcutContext.WindowShortcut
        )
        self.quick_switcher_escape_shortcut.activated.connect(
            self._hide_quick_paper_switcher
        )
        self.quick_switcher_escape_shortcut.setEnabled(False)

    def _new_note(self) -> None:
        self._show_sidebar("notes")
        self.reader_sidebar.new_note()

    def _save_reader_data(self) -> None:
        if self.reader_sidebar.stack.currentWidget() is self.reader_sidebar.info:
            self.reader_sidebar.info.save()
        self.reader_sidebar.flush_pending_saves()

    def _create_loading_widget(self) -> QWidget:
        widget = QWidget()
        widget.setObjectName("readerState")
        layout = QVBoxLayout(widget)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.loading_label = QLabel("Opening PDF…")
        self.loading_label.setObjectName("stateTitle")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.loading_progress = QProgressBar()
        self.loading_progress.setObjectName("loadingProgress")
        self.loading_progress.setRange(0, 100)
        self.loading_progress.setValue(0)
        self.loading_progress.setTextVisible(False)
        self.loading_progress.setFixedWidth(320)

        layout.addWidget(self.loading_label)
        layout.addSpacing(12)
        layout.addWidget(self.loading_progress, alignment=Qt.AlignmentFlag.AlignCenter)
        return widget

    def _create_error_widget(self) -> QWidget:
        widget = QWidget()
        widget.setObjectName("readerState")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("Could not open this PDF")
        title.setObjectName("stateTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.error_detail = QLabel()
        self.error_detail.setObjectName("errorDetail")
        self.error_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.error_detail.setWordWrap(True)
        self.error_detail.setMaximumWidth(720)

        retry_button = QPushButton("Try again")
        set_widget_icon(
            retry_button,
            "refresh-cw",
            size=16,
            color="#FFFFFF",
            active_color="#FFFFFF",
        )
        retry_button.setObjectName("retryButton")
        retry_button.setCursor(Qt.CursorShape.PointingHandCursor)
        retry_button.clicked.connect(self.open_pdf)

        layout.addWidget(title)
        layout.addSpacing(8)
        layout.addWidget(self.error_detail)
        layout.addSpacing(16)
        layout.addWidget(retry_button, alignment=Qt.AlignmentFlag.AlignCenter)
        return widget

    def _create_web_view(self) -> QWebEngineView:
        if not is_pdfjs_scheme_registered():
            raise RuntimeError(
                "The PDF.js URL scheme was not registered before QApplication. "
                "Start the application through main.py."
            )

        self.web_profile = QWebEngineProfile(self)
        self.scheme_handler = PdfJsSchemeHandler(self.web_profile)
        self.web_profile.installUrlSchemeHandler(SCHEME_NAME, self.scheme_handler)

        view = QWebEngineView()
        page = PdfJsPage(self.web_profile, view)
        view.setPage(page)

        self.web_channel = QWebChannel(page)
        self.web_channel.registerObject("readerBridge", self.reader_bridge)
        page.setWebChannel(
            self.web_channel,
            QWebEngineScript.ScriptWorldId.MainWorld.value,
        )

        settings = view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard,
            True,
        )
        settings.setAttribute(QWebEngineSettings.WebAttribute.PdfViewerEnabled, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
            False,
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
            False,
        )

        integration_script = QWebEngineScript()
        integration_script.setName("research-assistant-pdf-integration")
        integration_script.setInjectionPoint(
            QWebEngineScript.InjectionPoint.DocumentCreation
        )
        integration_script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        integration_script.setRunsOnSubFrames(False)
        integration_script.setSourceCode(_reader_injection_source())
        page.scripts().insert(integration_script)

        view.loadStarted.connect(self._on_load_started)
        view.loadProgress.connect(self._on_load_progress)
        view.loadFinished.connect(self._on_load_finished)
        page.loadingChanged.connect(self._on_loading_changed)
        page.renderProcessTerminated.connect(self._on_render_process_terminated)
        return view

    def open_pdf(self, pdf_path_override: Path | None = None) -> None:
        self._document_check_timer.stop()
        self._load_generation += 1
        self._document_check_attempt = 0
        self._document_ready_emitted = False
        self._last_load_error = ""
        self._show_loading()

        try:
            pdf_path = pdf_path_override or self._validated_pdf_path()
            missing_assets = missing_pdfjs_assets()
            if missing_assets:
                missing = "\n".join(str(path) for path in missing_assets)
                raise FileNotFoundError(f"Bundled PDF.js assets are missing:\n{missing}")
        except (FileNotFoundError, OSError, ValueError) as error:
            self._show_error(str(error))
            self.document_load_failed.emit(str(error))
            return

        self.pdf_path = pdf_path
        self.scheme_handler.set_document_path(pdf_path)
        LOGGER.info("Opening PDF with bundled PDF.js: %s", pdf_path)
        self.web_view.load(
            build_pdfjs_viewer_url(
                f"{self.current_document_version}-{self._load_generation}"
            )
        )

    def _validated_pdf_path(self) -> Path:
        if self.current_document_version == "vi":
            version = self.document_version_repository.get(self.paper_id, "vi")
            if version is None:
                raise FileNotFoundError("The Vietnamese PDF has not been created yet.")
            file_path = version["file_path"]
        else:
            file_path = self.paper["file_path"]
        if not file_path:
            raise ValueError("This paper does not have a PDF path in the library database.")

        path = resolve_paper_path(file_path)

        if not path.is_file():
            raise FileNotFoundError(f"The PDF file no longer exists:\n{path}")
        if path.suffix.lower() != ".pdf":
            raise ValueError(f"The selected library file is not a PDF:\n{path}")

        with path.open("rb") as pdf_file:
            if b"%PDF-" not in pdf_file.read(1024):
                raise ValueError(f"The file does not contain a valid PDF header:\n{path}")

        return path

    def _set_version_buttons(self, version: str, *, enabled: bool = True) -> None:
        for name, button in self.version_buttons.items():
            blocker = QSignalBlocker(button)
            button.setChecked(name == version)
            button.setEnabled(enabled)
            del blocker

    def _switch_document_version(self, version: str) -> None:
        if version == self.current_document_version:
            self._set_version_buttons(version)
            return
        if version not in {"en", "vi"}:
            return
        if not self.reader_sidebar.flush_pending_saves():
            self._set_version_buttons(self.current_document_version)
            return
        self._cancel_all_transient()
        if version == "en":
            self._activate_document_version("en", resolve_paper_path(self.paper["file_path"]))
            return

        record = self.document_version_repository.get(self.paper_id, "vi")
        if record is not None:
            path = resolve_paper_path(str(record["file_path"]))
            if path.is_file() and path.suffix.lower() == ".pdf":
                self._activate_document_version("vi", path)
                return
        self._choose_vietnamese_pdf()

    def _choose_vietnamese_pdf(self, *, replace: bool = False) -> None:
        caption = "Replace Vietnamese PDF" if replace else "Add Vietnamese PDF"
        file_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            caption,
            str(resolve_paper_path(self.paper["file_path"]).parent),
            "PDF files (*.pdf)",
        )
        if not file_path:
            self._set_version_buttons(self.current_document_version)
            return
        self._start_vietnamese_pdf_import(Path(file_path))

    def _start_vietnamese_pdf_import(self, source_path: Path) -> None:
        if not self.reader_sidebar.flush_pending_saves():
            self._set_version_buttons(self.current_document_version)
            return
        self._cancel_all_transient()
        self._set_version_buttons(self.current_document_version, enabled=False)
        self.loading_label.setText("Adding Vietnamese PDF…")
        self._show_loading()
        worker = FunctionWorker(
            self.vietnamese_pdf_service.import_pdf,
            self.paper_id,
            source_path,
            self.paper["file_hash"],
        )
        self._vi_import_worker = worker
        worker.signals.result.connect(self._vietnamese_pdf_ready)
        worker.signals.error.connect(self._vietnamese_pdf_failed)
        worker.signals.finished.connect(self._vietnamese_pdf_import_finished)
        QThreadPool.globalInstance().start(worker)

    def _vietnamese_pdf_ready(self, result: Any) -> None:
        if self._closing:
            return
        path = Path(result.path)
        self._discard_navigation_history_for_version("vi")
        self._activate_document_version("vi", path)
        if getattr(result, "warning", ""):
            QMessageBox.warning(self, "Vietnamese PDF added", str(result.warning))

    def _vietnamese_pdf_failed(self, error: Any) -> None:
        if self._closing:
            return
        self._set_version_buttons(self.current_document_version)
        self.loading_label.setText("Opening PDF…")
        if self.pdf_path is not None:
            self.content_stack.setCurrentWidget(self.web_view)
        QMessageBox.warning(
            self,
            "Vietnamese PDF unavailable",
            str(error) or "The Vietnamese PDF could not be added.",
        )

    def _vietnamese_pdf_import_finished(self) -> None:
        self._vi_import_worker = None
        if not self._closing:
            self._set_version_buttons(self.current_document_version)

    def _show_vi_context_menu(self, position: Any) -> None:
        menu = QMenu(self)
        record = self.document_version_repository.get(self.paper_id, "vi")
        add_or_replace = menu.addAction(
            "Replace Vietnamese PDF…" if record is not None else "Add Vietnamese PDF…"
        )
        add_or_replace.triggered.connect(
            lambda: self._choose_vietnamese_pdf(replace=record is not None)
        )
        if record is not None:
            remove = menu.addAction("Remove Vietnamese PDF")
            remove.triggered.connect(self._remove_vietnamese_pdf)
        menu.exec(self.version_buttons["vi"].mapToGlobal(position))

    def _remove_vietnamese_pdf(self) -> None:
        if QMessageBox.question(
            self,
            "Remove Vietnamese PDF",
            "Remove the Vietnamese PDF version? Its Notes and Highlights will be kept, "
            "but locations tied to this file will be cleared.",
        ) != QMessageBox.StandardButton.Yes:
            return
        if not self.reader_sidebar.flush_pending_saves():
            return
        if self.current_document_version == "vi":
            self._activate_document_version(
                "en", resolve_paper_path(self.paper["file_path"])
            )
        try:
            result = self.vietnamese_pdf_service.remove_pdf(self.paper_id)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Could not remove Vietnamese PDF", str(error))
            return
        self._discard_navigation_history_for_version("vi")
        if result is not None and result.warning:
            QMessageBox.warning(self, "Vietnamese PDF removed", result.warning)

    def _activate_document_version(self, version: str, path: Path) -> None:
        self.current_document_version = version
        self.reader_bridge.set_document_version(version)
        self.reader_sidebar.refresh_for_document()
        self._set_version_buttons(version)
        self.loading_label.setText("Opening PDF…")
        self.open_pdf(Path(path))

    def _show_loading(self) -> None:
        self.loading_progress.setValue(0)
        self.content_stack.setCurrentWidget(self.loading_widget)

    def _show_error(self, message: str) -> None:
        self._document_check_timer.stop()
        self._pending_navigation_restore = None
        self._restoring_navigation_history = False
        self._update_position_back_button()
        self.error_detail.setText(message)
        self.content_stack.setCurrentWidget(self.error_widget)

    def _on_load_started(self) -> None:
        self._last_load_error = ""
        self._show_loading()
        LOGGER.debug("PDF.js viewer load started: %s", self.web_view.url().toString())

    def _on_load_progress(self, progress: int) -> None:
        self.loading_progress.setValue(progress)

    def _on_loading_changed(self, info: QWebEngineLoadingInfo) -> None:
        if info.status() != QWebEngineLoadingInfo.LoadStatus.LoadFailedStatus:
            return

        error_text = info.errorString().strip()
        if not error_text:
            error_text = f"WebEngine error {info.errorCode()} ({info.errorDomain().name})"
        self._last_load_error = error_text
        LOGGER.error(
            "PDF.js viewer navigation failed for %s: %s",
            info.url().toString(),
            error_text,
        )

    def _on_load_finished(self, succeeded: bool) -> None:
        if not succeeded:
            message = self._last_load_error or "The local PDF.js viewer could not be loaded."
            self._show_error(message)
            self.document_load_failed.emit(message)
            return

        self.content_stack.setCurrentWidget(self.web_view)
        self.web_view.setFocus(Qt.FocusReason.OtherFocusReason)
        self._schedule_document_check(self._load_generation)

    def _schedule_document_check(self, generation: int) -> None:
        self._pending_check_generation = generation
        self._document_check_timer.start(250)

    def _query_document_state(self) -> None:
        generation = self._pending_check_generation
        if generation != self._load_generation:
            return

        self.web_view.page().runJavaScript(
            _PDFJS_STATE_QUERY,
            lambda result: self._handle_document_state(generation, result),
        )

    def _handle_document_state(self, generation: int, result: Any) -> None:
        if generation != self._load_generation:
            return

        if isinstance(result, str):
            try:
                state = json.loads(result)
            except json.JSONDecodeError:
                state = {}
        else:
            state = result if isinstance(result, dict) else {}
        status = str(state.get("state", "initializing"))

        if status == "ready":
            pages = int(state.get("pages") or 0)
            if not self._document_ready_emitted:
                self._document_ready_emitted = True
                self.document_ready.emit(pages)
                LOGGER.info("PDF.js document ready: %s pages", pages)
            if self._pending_navigation_restore is not None:
                position = self._pending_navigation_restore
                self._pending_navigation_restore = None
                self._restore_reader_position(position)
                return
            self._apply_pending_context_page()
            return

        if status == "error":
            message = str(state.get("message") or "PDF.js could not open this document.")
            self._show_error(message)
            self.document_load_failed.emit(message)
            return

        self._document_check_attempt += 1
        if self._document_check_attempt >= 120:
            message = "PDF.js started, but the document did not become ready within 30 seconds."
            self._show_error(message)
            self.document_load_failed.emit(message)
            return

        self._schedule_document_check(generation)

    def _on_render_process_terminated(
        self,
        status: QWebEnginePage.RenderProcessTerminationStatus,
        exit_code: int,
    ) -> None:
        if status == QWebEnginePage.RenderProcessTerminationStatus.NormalTerminationStatus:
            return

        message = f"The PDF rendering process stopped unexpectedly ({status.name}, code {exit_code})."
        LOGGER.error(message)
        self._show_error(message)
        self.document_load_failed.emit(message)

    @staticmethod
    def _decoded_bridge_payload(raw_payload: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw_payload)
        except (TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _on_selection_note_requested(self, raw_payload: str) -> None:
        payload = self._decoded_bridge_payload(raw_payload)
        self._show_sidebar("notes")
        started = self.reader_sidebar.begin_selection_note(payload)
        self.reader_bridge.report_action_result(
            "createSelectionNote",
            str(payload.get("requestId") or ""),
            ok=started,
            error="" if started else "Could not open the selection note draft.",
            draft=started,
        )

    def _on_selection_note_saved(self, _note_id: int, _request_id: str) -> None:
        self.reading_interaction.emit(self.paper_id, "annotation")
        self._clear_web_transient()

    def _on_translation_requested(self, raw_payload: str) -> None:
        payload = self._decoded_bridge_payload(raw_payload)
        text = str(payload.get("selectedText") or "")
        location = payload.get("location")
        if not text or not isinstance(location, dict):
            return

        if self._active_translation_request_id:
            self._cancel_active_translation()
        self._active_translation_request_id = str(payload.get("requestId") or "")

        self._show_sidebar("notes")
        self.reader_sidebar.begin_translation(
            text, location, self._active_translation_request_id
        )
        self._translation_generation += 1
        generation = self._translation_generation
        worker = FunctionWorker(
            self.translation_provider.translate,
            text,
            source_language="en",
            target_language=self._translation_target_language(),
        )
        self._translation_workers.add(worker)
        worker.signals.result.connect(
            lambda result, current=generation, request=payload: self._translation_succeeded(
                current, request, result
            )
        )
        worker.signals.error.connect(
            lambda error, current=generation, request=payload: self._translation_failed(
                current, request, error
            )
        )
        worker.signals.finished.connect(
            lambda current_worker=worker: self._translation_workers.discard(
                current_worker
            )
        )
        QThreadPool.globalInstance().start(worker)

    def _translation_target_language(self) -> str:
        try:
            value = self.settings_repository.get("translation_target", "vi")
        except (sqlite3.Error, OSError, ValueError):
            return "vi"
        if not isinstance(value, str) or not value.strip():
            return "vi"
        return value.strip()

    def _translation_succeeded(
        self,
        generation: int,
        payload: Mapping[str, Any],
        result: Any,
    ) -> None:
        if self._closing or generation != self._translation_generation:
            return
        translated_text = str(result or "").strip()
        if not translated_text:
            self._translation_failed(
                generation,
                payload,
                ValueError("The translation service returned no text."),
            )
            return
        self.reader_sidebar.set_translation_result(translated_text)
        self.reader_bridge.report_action_result(
            "translateSelection",
            str(payload.get("requestId") or ""),
            ok=True,
        )

    def _translation_failed(
        self,
        generation: int,
        payload: Mapping[str, Any],
        error: Any,
    ) -> None:
        if self._closing or generation != self._translation_generation:
            return
        message = str(error) or "Translation failed."
        self.reader_sidebar.set_translation_error(message)
        self.reader_bridge.report_action_result(
            "translateSelection",
            str(payload.get("requestId") or ""),
            ok=False,
            error=message,
        )

    def _cancel_active_translation(self) -> None:
        request_id = self._active_translation_request_id
        if not request_id:
            self._clear_web_transient()
            return
        self._translation_generation += 1
        self._active_translation_request_id = ""
        self.reader_sidebar.cancel_transient(request_id)
        self.reader_bridge.report_action_result(
            "translateSelection",
            request_id,
            ok=True,
            canceled=True,
        )
        self._clear_web_transient()

    def _clear_web_transient(self) -> None:
        if self._closing:
            return
        self.web_view.page().runJavaScript(
            "window.ResearchAssistantReader?.cancelTransientSelection?.();"
        )

    def _cancel_transient_from_sidebar(self, request_id: str) -> None:
        if request_id == self._active_translation_request_id:
            self._cancel_active_translation()
        else:
            self._clear_web_transient()

    def _on_web_transient_cancelled(self, request_id: str) -> None:
        if request_id == self._active_translation_request_id:
            self._translation_generation += 1
            self._active_translation_request_id = ""
        self.reader_sidebar.cancel_transient(request_id)

    def _on_translation_note_saved(self, _note_id: int) -> None:
        self._active_translation_request_id = ""
        self._translation_generation += 1
        self._clear_web_transient()

    def _cancel_all_transient(self) -> None:
        request_id = self._active_translation_request_id
        if request_id:
            self._translation_generation += 1
            self._active_translation_request_id = ""
            self.reader_sidebar.cancel_transient(request_id)
        self.reader_sidebar.notes.discard_selection_draft(notify=False)
        self._clear_web_transient()

    def _on_note_open_requested(self, note_id: int) -> None:
        self._show_sidebar("notes")
        self.reader_sidebar.open_note(int(note_id))

    def _on_copy_requested(self, raw_payload: str) -> None:
        payload = self._decoded_bridge_payload(raw_payload)
        text = str(payload.get("selectedText") or "")
        if text:
            from PySide6.QtGui import QGuiApplication

            QGuiApplication.clipboard().setText(text)
        self.reader_bridge.report_action_result(
            "copySelection",
            str(payload.get("requestId") or ""),
            ok=bool(text),
            error="" if text else "There is no selected text to copy.",
        )

    def _on_ask_ai_requested(self, raw_payload: str) -> None:
        payload = self._decoded_bridge_payload(raw_payload)
        if not payload.get("selectedText"):
            return
        self.reading_interaction.emit(self.paper_id, "selection")
        self._show_sidebar("ai")
        self.reader_sidebar.attach_ai_selection(payload)

    def _on_ai_send_requested(
        self,
        conversation_id: int,
        provider: str,
        model: str,
        question: str,
        selected_text: str,
        selected_page: object,
        response_language: object,
        request_context: object,
        retrying: bool,
    ) -> None:
        if self._ai_worker is not None:
            return
        if not retrying:
            ai_panel = self.reader_sidebar.ai
            engaged_ids = (
                [int(row["id"]) for row in ai_panel._workspace_papers]
                if bool(getattr(ai_panel, "_group_mode", False))
                else [int(ai_panel.paper_id)]
            )
            for engaged_paper_id in dict.fromkeys(engaged_ids):
                self.ai_engaged.emit(engaged_paper_id, provider, model)
        try:
            page = int(selected_page) if selected_page is not None else None
        except (TypeError, ValueError):
            page = None
        self._ai_request_generation += 1
        generation = self._ai_request_generation
        cancel_event = AIRequestControl()
        self._ai_cancel_event = cancel_event
        self._ai_active_request = (conversation_id, provider, model)
        ai_panel = self.reader_sidebar.ai
        self._ai_active_panel = ai_panel
        conversation_paper_id = int(ai_panel.paper_id)
        conversation_paper = (
            self.paper
            if conversation_paper_id == self.paper_id
            else PaperRepository.get_paper_by_id(conversation_paper_id)
        )
        if conversation_paper is None:
            ai_panel.finish_request(
                error="The comparison conversation paper is no longer available.",
                conversation_id=conversation_id,
                retryable=True,
            )
            self._ai_cancel_event = None
            self._ai_active_request = None
            self._ai_active_panel = None
            return
        retry_context = (
            dict(request_context) if isinstance(request_context, dict) else {}
        )
        if retrying:
            try:
                partial_message_id = int(
                    retry_context.get("partial_message_id") or 0
                )
            except (TypeError, ValueError):
                partial_message_id = 0
            cancel_event.partial_message_id = partial_message_id or None
        workspace_context = None
        if bool(retry_context.get("use_workspace_context")):
            workspace_context = [
                {
                    "id": int(paper["id"]),
                    "title": str(paper.get("title") or "Untitled paper"),
                    "pdf_path": resolve_paper_path(str(paper.get("file_path") or "")),
                    "file_hash": str(paper.get("file_hash") or ""),
                    "alias_index": paper.get("alias_index"),
                }
                for paper in retry_context.get("workspace_papers", [])
                if isinstance(paper, dict)
            ]
        worker = FunctionWorker(
            self.ai_chat_service.send_message,
            paper_id=conversation_paper_id,
            conversation_id=conversation_id,
            provider=provider,
            model=model,
            pdf_path=resolve_paper_path(conversation_paper["file_path"]),
            file_hash=str(conversation_paper["file_hash"] or ""),
            question=question,
            selected_text=selected_text or None,
            selected_page=page,
            response_language=str(response_language or "").strip() or None,
            workspace_papers=workspace_context,
            append_user_message=not retrying,
            cancel_event=cancel_event,
        )
        self._ai_worker = worker
        worker.kwargs["on_chunk"] = worker.signals.progress.emit
        worker.signals.progress.connect(
            lambda chunk, value=generation: self._ai_stream_chunk(value, chunk)
        )
        worker.signals.result.connect(
            lambda result, value=generation: self._ai_request_succeeded(
                value, result
            )
        )
        worker.signals.error.connect(
            lambda error, value=generation: self._ai_request_failed(value, error)
        )
        worker.signals.finished.connect(
            lambda value=generation: self._ai_request_finished(value)
        )
        QThreadPool.globalInstance().start(worker)

    def _ai_stream_chunk(self, generation: int, chunk: object) -> None:
        if generation != self._ai_request_generation or self._closing:
            return
        if self._ai_active_request is None:
            return
        conversation_id, provider, model = self._ai_active_request
        panel = self._ai_active_panel or self.reader_sidebar.ai
        panel.append_stream_chunk(
            conversation_id,
            provider,
            model,
            str(chunk or ""),
        )

    def _stop_ai_request(self, partial_text: str) -> None:
        if self._ai_cancel_event is not None:
            self._ai_cancel_event.set()
            if partial_text and self._ai_active_request is not None:
                conversation_id, provider, model = self._ai_active_request
                try:
                    persist = getattr(
                        self.ai_chat_service,
                        "persist_partial_response",
                        None,
                    )
                    if callable(persist):
                        persist(
                            self._ai_cancel_event,
                            conversation_id=conversation_id,
                            provider=provider,
                            model=model,
                            content=partial_text,
                        )
                except (sqlite3.Error, OSError, ValueError):
                    LOGGER.exception("Could not save the interrupted AI response")
        self._ai_request_generation += 1
        self._ai_cancel_event = None
        self._ai_active_request = None
        self._ai_active_panel = None
        self._ai_worker = None

    def _ai_request_succeeded(self, generation: int, result: object) -> None:
        if generation != self._ai_request_generation or self._closing:
            if not bool(getattr(result, "cancelled", False)):
                discard = getattr(self.ai_chat_service, "discard_response", None)
                if callable(discard):
                    discard(result)
            return
        panel = self._ai_active_panel or self.reader_sidebar.ai
        self._ai_worker = None
        self._ai_cancel_event = None
        self._ai_active_request = None
        self._ai_active_panel = None
        if bool(getattr(result, "cancelled", False)):
            panel.cancel_request()
            return
        panel.finish_request(result)

    def _ai_request_failed(self, generation: int, error: object) -> None:
        if generation != self._ai_request_generation or self._closing:
            return
        active_request = self._ai_active_request
        panel = self._ai_active_panel or self.reader_sidebar.ai
        partial_text = panel.streaming_text()
        partial_message_id = (
            self._ai_cancel_event.partial_message_id
            if self._ai_cancel_event is not None
            else None
        )
        if (
            partial_text
            and self._ai_cancel_event is not None
            and self._ai_active_request is not None
        ):
            conversation_id, provider, model = self._ai_active_request
            try:
                persist = getattr(
                    self.ai_chat_service,
                    "persist_partial_response",
                    None,
                )
                if callable(persist):
                    partial_message_id = persist(
                        self._ai_cancel_event,
                        conversation_id=conversation_id,
                        provider=provider,
                        model=model,
                        content=partial_text,
                    )
            except (sqlite3.Error, OSError, ValueError):
                LOGGER.exception("Could not save the partial AI response")
        self._ai_worker = None
        self._ai_cancel_event = None
        self._ai_active_request = None
        self._ai_active_panel = None
        panel.finish_request(
            error=str(error) or "The AI request failed.",
            conversation_id=(active_request[0] if active_request else None),
            retryable=True,
            partial_message_id=partial_message_id,
        )

    def _ai_request_finished(self, generation: int) -> None:
        if generation == self._ai_request_generation:
            self._ai_worker = None
            self._ai_cancel_event = None
            self._ai_active_request = None
            self._ai_active_panel = None

    def _navigate_to_english_citation(self, citation: object) -> None:
        try:
            if not bool(citation_value(citation, "verified", False)):
                return
            cited_paper_id = int(
                citation_value(citation, "paper_id", self.paper_id)
                or self.paper_id
            )
            if cited_paper_id != self.paper_id:
                self.citation_navigation_requested.emit(cited_paper_id, citation)
                return
            page = int(citation_value(citation, "resolved_page"))
            evidence = str(citation_value(citation, "evidence", "") or "").strip()
            self._pending_source_page = page
            self._pending_source_evidence = evidence
            self.open_context(
                page_number=page,
                document_version="en",
            )
        except (TypeError, ValueError) as error:
            LOGGER.warning("Could not navigate to AI citation: %s", error)

    def _show_source_evidence(self, page: int, evidence: str) -> None:
        if self._closing or self.current_document_version != "en":
            return
        payload = json.dumps(
            {"page": int(page), "evidence": evidence},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        self.web_view.page().runJavaScript(
            f"window.ResearchAssistantReader?.showSourceEvidence?.({payload});"
        )

    def _on_ai_summarize_requested(self, provider: str, model: str) -> None:
        if self._ai_summary_worker is not None:
            return
        if self._ai_worker is not None:
            self.reader_sidebar.ai.set_summary_busy(
                False, "Another AI operation is already running."
            )
            return
        output_language = self._translation_target_language()
        cache_key = (self.paper_id, provider, model, output_language)
        if (
            self._ai_summary_preview_cache is not None
            and self._ai_summary_preview_cache[0] == cache_key
        ):
            self._present_ai_summary_preview(
                self._ai_summary_preview_cache[1], cache_key
            )
            return
        self._ai_summary_generation += 1
        generation = self._ai_summary_generation
        cancel_event = AIRequestControl()
        self._ai_summary_cancel = cancel_event
        self._ai_summary_active_key = cache_key
        worker = FunctionWorker(
            self.ai_chat_service.generate_summary,
            paper_id=self.paper_id,
            provider=provider,
            model=model,
            pdf_path=resolve_paper_path(self.paper["file_path"]),
            file_hash=str(self.paper["file_hash"] or ""),
            output_language=output_language,
            cancel_event=cancel_event,
        )
        self._ai_summary_worker = worker
        self._ai_summary_timeout_timer.start()
        worker.signals.result.connect(
            lambda result, value=generation: self._ai_summary_succeeded(
                value, result
            )
        )
        worker.signals.error.connect(
            lambda error, value=generation: self._ai_summary_failed(value, error)
        )
        worker.signals.finished.connect(
            lambda value=generation: self._ai_summary_finished(value)
        )
        QThreadPool.globalInstance().start(worker)

    def _cancel_ai_summary(self) -> None:
        self._ai_summary_timeout_timer.stop()
        if self._ai_summary_cancel is not None:
            self._ai_summary_cancel.set()
        self._ai_summary_generation += 1
        self._ai_summary_cancel = None
        self._ai_summary_active_key = None
        self._ai_summary_worker = None
        self.reader_sidebar.ai.set_summary_busy(False)

    def _ai_summary_timed_out(self) -> None:
        if self._ai_summary_worker is None:
            return
        if self._ai_summary_cancel is not None:
            self._ai_summary_cancel.set()
        self._ai_summary_generation += 1
        self._ai_summary_cancel = None
        self._ai_summary_active_key = None
        self._ai_summary_worker = None
        self.reader_sidebar.ai.set_summary_busy(
            False,
            "The Summary request timed out. Try again when the provider is available.",
        )

    def _ai_summary_succeeded(self, generation: int, result: object) -> None:
        if generation != self._ai_summary_generation or self._closing:
            return
        self._ai_summary_timeout_timer.stop()
        self._ai_summary_worker = None
        self._ai_summary_cancel = None
        self.reader_sidebar.ai.set_summary_busy(False)
        cache_key = self._ai_summary_active_key
        self._ai_summary_active_key = None
        if cache_key is None:
            return
        self._ai_summary_preview_cache = (cache_key, result)
        self._present_ai_summary_preview(result, cache_key)

    def _present_ai_summary_preview(
        self,
        result: object,
        cache_key: tuple[int, str, str, str],
    ) -> None:
        existing = SummaryRepository.get_for_paper(self.paper_id)
        dialog = AISummaryPreviewDialog(result, existing, self)
        dialog.citation_requested.connect(self._navigate_to_english_citation)
        outcome = dialog.exec()
        if outcome == AISummaryPreviewDialog.REGENERATE_RESULT:
            self._ai_summary_preview_cache = None
            _paper_id, provider, model, _language = cache_key
            QTimer.singleShot(
                0,
                self,
                lambda: self._on_ai_summarize_requested(provider, model),
            )
            return
        if outcome != QDialog.DialogCode.Accepted:
            return
        try:
            SummaryRepository.save_with_citations(
                self.paper_id,
                dialog.values(),
                dialog.field_results(),
            )
        except (sqlite3.Error, OSError, ValueError) as error:
            QMessageBox.warning(self, "Could not apply AI Summary", str(error))
            return
        self._ai_summary_preview_cache = None
        self.reader_sidebar.summary.reload()
        self._show_sidebar("summary")

    def _ai_summary_failed(self, generation: int, error: object) -> None:
        if generation != self._ai_summary_generation or self._closing:
            return
        self._ai_summary_timeout_timer.stop()
        self._ai_summary_worker = None
        self._ai_summary_cancel = None
        self._ai_summary_active_key = None
        self.reader_sidebar.ai.set_summary_busy(
            False,
            str(error) or "Could not summarize the paper.",
        )

    def _ai_summary_finished(self, generation: int) -> None:
        if generation == self._ai_summary_generation:
            self._ai_summary_timeout_timer.stop()
            self._ai_summary_worker = None
            self._ai_summary_cancel = None

    def refresh_ai_settings(self) -> None:
        self.reader_sidebar.ai.refresh_settings(preserve_selection=True)
        self.reader_sidebar.sync_ai_header_provider()

    def _navigate_to_location(self, location: object) -> None:
        if not isinstance(location, Mapping):
            return

        def navigate() -> None:
            try:
                self.reader_bridge.navigate_to(location)
            except ValueError as error:
                LOGGER.warning("Could not navigate to PDF annotation: %s", error)

        self._record_position_then(navigate)

    def _on_paper_updated(self, paper_id: int) -> None:
        paper = PaperRepository.get_paper_by_id(paper_id)
        if paper is not None:
            self.refresh_paper(paper)
        self.paper_updated.emit(paper_id)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.reader_sidebar.flush_pending_saves():
            if self.reader_sidebar.info.is_dirty:
                self._show_sidebar("info")
            elif self.reader_sidebar.summary._dirty:
                self._show_sidebar("summary")
            else:
                self._show_sidebar("notes")
            event.ignore()
            return

        self._quick_switch_open_timer.stop()
        self._quick_switch_close_timer.stop()
        self._quick_switch_animation.stop()
        self._quick_switch_target_open = False
        self.quick_paper_switcher.hide()
        self.quick_switcher_escape_shortcut.setEnabled(False)

        if self._ai_cancel_event is not None:
            panel = self._ai_active_panel or self.reader_sidebar.ai
            self._stop_ai_request(panel.cancel_request())
        if self._ai_summary_cancel is not None:
            self._cancel_ai_summary()
        self._closing = True
        self._translation_generation += 1
        self._load_generation += 1
        self._document_check_timer.stop()
        self.web_view.stop()
        self.about_to_close.emit(self.paper_id)
        super().closeEvent(event)

    def apply_style(self) -> None:
        self.setStyleSheet(
            apply_ui_palette("""
            QMainWindow {
                background-color: @APP_BG@;
            }

            #topBar {
                background-color: @SURFACE@;
                border-bottom: 1px solid @BORDER_SOFT@;
            }

            #readerWorkspaceTabs {
                border: none;
                background: transparent;
            }
            #readerWorkspaceTabs::pane { border: none; }
            #readerWorkspaceTabs::tab {
                min-width: 56px;
                max-width: 170px;
                min-height: 27px;
                margin: 1px 2px;
                padding: 0 4px 0 8px;
                border: 1px solid transparent;
                border-radius: 8px;
                background: transparent;
                color: @TEXT_MUTED@;
                font-size: 11px;
            }
            #readerWorkspaceTabs::tab:hover {
                background: @SURFACE_SUBTLE@;
                color: @TEXT@;
            }
            #readerWorkspaceTabs::tab:selected {
                border-color: #D5E1F8;
                background: @PRIMARY_SOFT@;
                color: @PRIMARY_TEXT@;
                font-weight: 600;
            }
            #readerWorkspaceTabs QToolButton {
                border: none;
                border-radius: 6px;
                background: transparent;
                padding: 0;
            }
            #readerWorkspaceTabs QToolButton:hover {
                background: @SURFACE_SUBTLE@;
            }
            #workspaceTabClose {
                border: none;
                border-radius: 5px;
                background: transparent;
                padding: 0;
            }
            #workspaceTabClose:hover { background: #DCE7F7; }
            #workspaceTabControls { background: transparent; }
            #workspaceTabAlias {
                min-width: 22px;
                max-width: 28px;
                min-height: 16px;
                max-height: 18px;
                border: 1px solid #CDDDF5;
                border-radius: 7px;
                background: #EAF2FF;
                color: #3F65A2;
                font-size: 9px;
                font-weight: 700;
                padding: 0 2px;
            }

            #backButton, #positionBackButton {
                border: none;
                background-color: transparent;
                color: #44484F;
                padding: 0 9px;
                border-radius: 6px;
                font-size: 13px;
            }

            #backButton:hover, #positionBackButton:hover {
                background-color: @SURFACE_SUBTLE@;
            }

            #readerOverflowButton {
                border: none;
                border-radius: 6px;
                background-color: transparent;
                color: #44484F;
                padding: 0;
            }

            #readerOverflowButton:hover,
            #readerOverflowButton:pressed {
                background-color: #F0F1F3;
            }

            #sidebarToggleButton {
                min-height: 30px;
                padding: 0 10px;
                border: none;
                border-radius: 6px;
                background-color: transparent;
                color: #44484F;
                font-size: 13px;
                font-weight: 500;
            }

            #versionSwitch {
                border: 1px solid @BORDER@;
                border-radius: 6px;
                background: #F4F5F7;
            }

            #versionButton {
                min-width: 31px;
                min-height: 26px;
                border: none;
                border-radius: 4px;
                background: transparent;
                color: #6B717A;
                font-size: 11px;
                font-weight: 650;
            }

            #versionButton:checked {
                background: white;
                color: @PRIMARY_TEXT@;
                border: 1px solid #CAD8F8;
            }

            #versionButton:disabled {
                color: #A4A9B0;
            }

            #sidebarToggleButton:hover {
                background-color: @SURFACE_SUBTLE@;
            }

            #sidebarToggleButton:checked {
                background-color: @PRIMARY_SOFT@;
                color: @PRIMARY_TEXT@;
                font-weight: 600;
            }

            #sidebarToggleButton[section="ai"]:checked,
            #sidebarToggleButton[section="search"]:checked {
                background-color: @AI_SOFT@;
                color: @AI@;
            }

            #sidebarToggleButton[section="utility"] {
                min-width: 32px;
                max-width: 32px;
                padding: 0;
            }

            #readerSplitter::handle {
                width: 1px;
                background-color: transparent;
            }

            #notesSidebar {
                border: none;
                background-color: white;
            }

            #notesTitle {
                color: #292C31;
                font-size: 15px;
                font-weight: 600;
            }

            #notesEditor {
                border: 1px solid #E1E4E8;
                border-radius: 8px;
                padding: 10px;
                background-color: #FAFAFB;
                color: #292C31;
                selection-background-color: #C9D8F0;
                font-size: 13px;
            }

            #notesEditor:focus {
                border-color: #AEB4BC;
                background-color: white;
            }

            #notesStatus {
                color: #7A8089;
                font-size: 11px;
            }

            #notesDiscardButton {
                border: none;
                background-color: transparent;
                color: #A33A3A;
                padding: 3px 0;
                font-size: 11px;
                font-weight: 600;
            }

            #notesDiscardButton:hover {
                color: #7E2525;
                text-decoration: underline;
            }

            #readerSidebarShell {
                background: transparent;
            }

            #readerSidebarSurface {
                background: #FBFCFE;
                border: 1px solid @BORDER@;
                border-radius: 16px;
            }

            #readerSidebar {
                border: none;
                background: transparent;
            }

            #readerSidebarTitle {
                color: #292C31;
                font-size: 15px;
                font-weight: 650;
            }

            #aiHistory, #aiHistory > QWidget > QWidget {
                border: none;
                background: transparent;
            }
            #aiContextStrip {
                min-height: 24px;
                max-height: 28px;
                border: 1px solid #E1E7EF;
                border-radius: 8px;
                background: #F7F9FC;
            }
            #aiContextLabel {
                color: #657181;
                font-size: 9.5px;
                font-weight: 600;
            }
            #aiContextChip {
                min-height: 18px;
                max-height: 18px;
                border: 1px solid #D5E1F6;
                border-radius: 8px;
                background: #EDF3FE;
            }
            #aiContextChipText {
                min-width: 20px;
                border: none;
                background: transparent;
                color: #41639E;
                font-size: 9px;
                font-weight: 650;
                padding: 0 1px;
            }
            #aiContextChipText:hover { color: #294E88; }
            #aiContextChipClose {
                border: none;
                border-radius: 5px;
                background: transparent;
                padding: 0;
            }
            #aiContextChipClose:hover { background: #DCE7F8; }
            #aiContextAction {
                border: none;
                border-radius: 5px;
                background: transparent;
                color: #4E6F9E;
                padding: 2px 4px;
                font-size: 9px;
            }
            #aiContextAction:hover { background: #E8EEF7; }

            #aiHeaderProvider {
                border: none;
                border-radius: 6px;
                background: transparent;
                padding: 0 18px 0 2px;
            }
            #aiHeaderProvider:hover { background: @AI_SOFT@; }
            #aiHeaderProvider::drop-down {
                width: 13px;
                border: none;
                background: transparent;
            }
            #aiHeaderProvider QAbstractItemView {
                min-width: 150px;
                border: 1px solid #D8DDE3;
                border-radius: 7px;
                background: white;
                color: #343A42;
                padding: 4px;
                outline: none;
            }
            #aiHeaderProvider QAbstractItemView::item:selected {
                background: @AI_SOFT@;
                color: #5149B8;
            }
            #aiModelCombo {
                min-height: 26px;
                max-height: 26px;
                border: none;
                border-radius: 6px;
                background: transparent;
                color: #3C424A;
                padding: 0 19px 0 4px;
                font-size: 10px;
            }
            #aiModelCombo:hover { background: #F2F4F6; }
            #aiModelCombo QLineEdit {
                border: none;
                background: transparent;
                color: #3C424A;
                padding: 0;
                font-size: 10px;
            }
            #aiModelCombo::drop-down {
                width: 15px;
                border: none;
                background: transparent;
            }
            #aiModelCombo QAbstractItemView {
                border: 1px solid #D8DDE3;
                border-radius: 7px;
                background: white;
                color: #343A42;
                padding: 4px;
                outline: none;
            }
            #aiSummarizeButton {
                min-height: 24px;
                max-height: 24px;
                border: none;
                border-radius: 6px;
                background: @SURFACE_SUBTLE@;
                color: #46515E;
                padding: 0 7px;
                font-size: 10px;
                font-weight: 600;
            }
            #aiSummarizeButton:hover { background: @AI_SOFT@; color: #5149B8; }
            #aiSummarizeButton:disabled { color: #9AA1AA; background: #F2F3F5; }
            #inlineCitationButton {
                border: none;
                border-radius: 4px;
                background: transparent;
                padding: 0;
            }
            #inlineCitationButton:hover { background: #E8EEF4; }
            QMenu#citationSourcesPopup {
                border: 1px solid #D8DDE3;
                border-radius: 7px;
                background: white;
                color: #343A42;
                padding: 4px;
            }
            QMenu#citationSourcesPopup::item {
                border-radius: 5px;
                padding: 5px 12px;
            }
            QMenu#citationSourcesPopup::item:selected { background: #EDF2F6; }

            #aiUserBubble {
                border: 1px solid #DCE6F4;
                border-radius: 11px;
                background: @PRIMARY_SOFT@;
            }

            #aiMessageText { color: #292C31; font-size: 12px; }
            #aiAssistantText {
                color: #292C31;
                font-size: 12.5px;
                border: none;
                background: transparent;
            }
            #aiProviderName {
                color: #343B46;
                font-size: 10.5px;
                font-weight: 650;
            }
            #aiProviderModel {
                color: #8A929E;
                font-size: 9.5px;
                font-weight: 500;
            }
            #aiMessageQuote {
                color: #56616D; font-size: 11px; font-style: italic;
                border-left: 2px solid #91A8C0; padding-left: 7px;
            }
            #aiEmptyState, #aiStatus {
                color: #737B85; font-size: 10px;
            }
            #aiPaperStartCard, #aiPaperBriefCard {
                border: 1px solid #E5E4F3;
                border-radius: 11px;
                background: #FCFCFE;
            }
            #aiCompareStartGroup {
                border-top: 1px solid #E4E9F0;
                background: transparent;
            }
            #aiCitationPaperMap {
                color: #707A87;
                font-size: 9.5px;
                padding-top: 2px;
            }
            #aiPaperStartTitle {
                color: #2F343A;
                font-size: 13px;
                font-weight: 650;
            }
            #aiPaperStartSubtitle {
                color: #747C86;
                font-size: 10.5px;
            }
            #aiSuggestionButton {
                min-height: 34px;
                border: 1px solid #DEE3E8;
                border-radius: 8px;
                background: white;
                text-align: left;
            }
            #aiSuggestionButton:hover,
            #aiSuggestionButton:focus {
                border-color: #C9C5F0;
                background: @AI_SOFT@;
            }
            #aiSuggestionText {
                color: #3F4852;
                font-size: 10.5px;
                background: transparent;
            }
            #aiPaperBriefButton, #aiBriefBackButton {
                min-height: 27px;
                border: none;
                border-radius: 6px;
                background: @AI_SOFT@;
                color: #554DB5;
                padding: 0 8px;
                font-size: 10px;
                font-weight: 600;
            }
            #aiPaperBriefButton:hover, #aiBriefBackButton:hover {
                background: #E6E3FA;
            }
            #aiBriefHeading {
                color: #4D5965;
                font-size: 10px;
                font-weight: 650;
                margin-top: 3px;
            }
            #aiBriefText {
                color: #30363D;
                font-size: 11px;
                background: transparent;
            }
            #aiStatus[error="true"] { color: #9A4545; }
            #aiAttachment {
                border: 1px solid #D5DEE8; border-radius: 7px; background: #F2F6FA;
            }
            #aiAttachmentText { color: #4F5E6D; font-size: 10px; }
            #aiThinkingText { color: @AI@; font-size: 11px; font-style: italic; }
            #aiInlineError { color: #A14A4A; font-size: 10px; }
            #aiInlineRetry {
                min-height: 22px; padding: 1px 8px;
                border: 1px solid #C9D7E8; border-radius: 6px;
                color: #315F91; background: #F4F8FC; font-size: 10px;
            }
            #aiInlineRetry:hover { background: #E8F1FA; }
            #aiIconButton {
                min-width: 25px; max-width: 25px; min-height: 25px;
                border: none; border-radius: 5px; background: transparent;
            }
            #aiIconButton:hover { background: #E3E9EF; }
            #aiComposerShell {
                border: 1px solid @BORDER@;
                border-radius: 10px;
                background: white;
            }
            #aiComposer {
                border: none;
                background: transparent;
                color: #292C31;
                padding: 3px 4px;
                font-size: 12px;
            }
            #aiComposerPlaceholder {
                color: #8A919A;
                background: transparent;
                font-size: 12px;
            }
            #aiSendButton {
                border: none;
                border-radius: 15px;
                background: @PRIMARY@;
            }
            #aiSendButton:hover { background: @PRIMARY_HOVER@; }
            #aiSendButton:disabled { background: #D7DBE0; }
            #aiHistoryPopup {
                border: 1px solid #D8DDE3;
                border-radius: 9px;
                background: white;
            }
            #aiHistoryTitle {
                color: #555E69;
                font-size: 10px;
                font-weight: 650;
                padding: 2px 5px;
            }
            #aiHistoryPopupScroll,
            #aiHistoryPopupScroll > QWidget > QWidget {
                border: none;
                background: transparent;
            }
            #aiHistoryRow {
                border: none;
                border-radius: 6px;
                background: transparent;
            }
            #aiHistoryRow[active="true"] { background: #EDF2F7; }
            #aiHistoryRow:hover { background: #F3F5F7; }
            #aiHistoryChatButton {
                min-height: 28px;
                border: none;
                background: transparent;
                color: #3D444D;
                text-align: left;
                padding: 0 5px;
                font-size: 11px;
            }
            #aiHistoryGroupKind, #aiHistorySoloKind {
                border-radius: 5px;
                padding: 1px 4px;
                font-size: 8.5px;
                font-weight: 650;
            }
            #aiHistoryGroupKind {
                background: #EAF2FF;
                color: #41639E;
            }
            #aiHistorySoloKind {
                background: #F0F2F4;
                color: #717983;
            }
            #aiHistoryMemberChip {
                min-width: 24px;
                max-width: 30px;
                min-height: 17px;
                max-height: 17px;
                border: 1px solid #D5E1F6;
                border-radius: 7px;
                background: #F0F5FD;
                color: #42659E;
                font-size: 8.5px;
                font-weight: 650;
                padding: 0 2px;
            }
            #aiHistoryMemberChip:hover { background: #E1ECFB; }
            #aiHistoryDeleteButton {
                min-width: 25px; max-width: 25px; min-height: 25px;
                border: none; border-radius: 5px; background: transparent;
            }
            #aiHistoryDeleteButton:hover { background: #F3E5E5; }
            #aiHistoryNewButton {
                min-height: 29px;
                border-top: 1px solid #E6E9ED;
                border-right: none;
                border-bottom: none;
                border-left: none;
                background: transparent;
                color: #3D444D;
                text-align: left;
                padding: 3px 6px 0 6px;
                font-size: 11px;
                font-weight: 600;
            }
            #aiHistoryNewButton:hover { background: #F3F5F7; }
            #aiHistoryEmpty { color: #858B94; font-size: 10px; padding: 14px; }
            #aiHistory QScrollBar:vertical,
            #aiHistoryPopupScroll QScrollBar:vertical,
            #aiModelCombo QScrollBar:vertical,
            #aiHeaderProvider QScrollBar:vertical {
                width: 8px;
                margin: 2px 1px;
                border: none;
                background: transparent;
            }
            #aiHistory QScrollBar::handle:vertical,
            #aiHistoryPopupScroll QScrollBar::handle:vertical,
            #aiModelCombo QScrollBar::handle:vertical,
            #aiHeaderProvider QScrollBar::handle:vertical {
                min-height: 28px;
                border: none;
                border-radius: 3px;
                background: #C8CDD3;
            }
            #aiHistory QScrollBar::handle:vertical:hover,
            #aiHistoryPopupScroll QScrollBar::handle:vertical:hover,
            #aiModelCombo QScrollBar::handle:vertical:hover,
            #aiHeaderProvider QScrollBar::handle:vertical:hover {
                background: #AEB5BD;
            }
            #aiHistory QScrollBar::add-line:vertical,
            #aiHistory QScrollBar::sub-line:vertical,
            #aiHistoryPopupScroll QScrollBar::add-line:vertical,
            #aiHistoryPopupScroll QScrollBar::sub-line:vertical,
            #aiModelCombo QScrollBar::add-line:vertical,
            #aiModelCombo QScrollBar::sub-line:vertical,
            #aiHeaderProvider QScrollBar::add-line:vertical,
            #aiHeaderProvider QScrollBar::sub-line:vertical {
                height: 0;
                border: none;
                background: transparent;
            }
            #aiHistory QScrollBar::add-page:vertical,
            #aiHistory QScrollBar::sub-page:vertical,
            #aiHistoryPopupScroll QScrollBar::add-page:vertical,
            #aiHistoryPopupScroll QScrollBar::sub-page:vertical,
            #aiModelCombo QScrollBar::add-page:vertical,
            #aiModelCombo QScrollBar::sub-page:vertical,
            #aiHeaderProvider QScrollBar::add-page:vertical,
            #aiHeaderProvider QScrollBar::sub-page:vertical {
                background: transparent;
            }
            #aiCompactButton {
                min-height: 27px;
                border: none;
                border-radius: 6px;
                background: transparent;
                color: #626A74;
                padding: 0 7px;
                font-size: 10px;
            }
            #aiCompactButton:hover { background: #F0F2F4; }

            #sidebarSectionTitle,
            #summaryLabel {
                color: #3B3F45;
                font-size: 12px;
                font-weight: 650;
            }

            #sidebarStatus,
            #highlightLegend {
                color: #777D86;
                font-size: 10px;
            }

            #sidebarScroll,
            #sidebarScroll > QWidget > QWidget {
                border: none;
                background: transparent;
            }

            #scratchpadEditor,
            #noteContentEdit,
            #summaryEditor,
            #translationOriginal,
            #translationResult {
                border: 1px solid #E1E4E8;
                border-radius: 7px;
                padding: 7px;
                background: #FAFAFB;
                color: #292C31;
                selection-background-color: #C9D8F0;
                font-size: 12px;
            }

            #scratchpadEditor:focus,
            #noteContentEdit:focus,
            #summaryEditor:focus,
            #infoEditor:focus {
                border-color: #AEB4BC;
                background: white;
            }

            #noteCard,
            #highlightCard,
            #translationCard {
                border: 1px solid #E4E6E9;
                border-radius: 8px;
                background: #FAFAFB;
            }

            #noteCard[activeNote="true"] {
                border-color: #98A9BD;
                background: #F3F7FB;
            }

            #noteCard[draftNote="true"] {
                border-style: dashed;
                border-color: #9AA8B8;
                background: #F6F8FA;
            }

            #noteTitleEdit,
            #infoEditor,
            #collectionsList,
            #highlightColor {
                min-height: 28px;
                border: 1px solid #E1E4E8;
                border-radius: 6px;
                padding: 2px 7px;
                background: white;
                color: #292C31;
                font-size: 12px;
            }

            #highlightQuote {
                color: #454A52;
                font-size: 11px;
                font-style: italic;
            }

            #sidebarSmallButton,
            #noteSourceButton,
            #sidebarDangerButton,
            #sidebarIconButton {
                min-height: 24px;
                border: none;
                border-radius: 5px;
                padding: 2px 6px;
                background: transparent;
                color: #555B64;
                font-size: 10px;
            }

            #sidebarSmallButton:hover,
            #noteSourceButton:hover,
            #sidebarIconButton:hover {
                background: @SURFACE_SUBTLE@;
            }

            #noteSourceButton {
                text-align: left;
                color: #4E6688;
            }

            #sidebarDangerButton {
                min-width: 26px;
                max-width: 26px;
                color: #9A4545;
            }

            #sidebarDangerButton:hover {
                background: #F8EAEA;
            }

            #sidebarIconButton {
                min-width: 26px;
                max-width: 26px;
            }

            #sidebarPrimaryButton {
                min-height: 29px;
                border: none;
                border-radius: 6px;
                padding: 0 10px;
                background: @PRIMARY@;
                color: white;
                font-size: 11px;
                font-weight: 600;
            }

            #sidebarPrimaryButton:hover {
                background: @PRIMARY_HOVER@;
            }

            #readerState {
                background-color: #F4F5F7;
            }

            #stateTitle {
                color: #292C31;
                font-size: 16px;
                font-weight: 600;
            }

            #errorDetail {
                color: #6B7078;
                font-size: 13px;
            }

            #loadingProgress {
                min-height: 4px;
                max-height: 4px;
                border: none;
                border-radius: 2px;
                background-color: #E1E4E8;
            }

            #loadingProgress::chunk {
                border-radius: 2px;
                background-color: @PRIMARY@;
            }

            #retryButton {
                min-height: 36px;
                padding: 0 18px;
                border: none;
                border-radius: 7px;
                background-color: @PRIMARY@;
                color: white;
                font-size: 13px;
                font-weight: 600;
            }

            #retryButton:hover {
                background-color: @PRIMARY_HOVER@;
            }
            """ + MODERN_SCROLLBAR_QSS)
        )
