from __future__ import annotations

import re
from typing import Any, Mapping

from PySide6.QtCore import QObject, QSignalBlocker, QThreadPool, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QTextCursor, QTextDocument, QTextImageFormat
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.ai.base import (
    AIAuthenticationError,
    AIProviderUnavailableError,
    AIRateLimitError,
)
from app.ai.custom_config import load_custom_api_config
from app.database.ai_search_repository import AISearchRepository
from app.database.settings_repository import SettingsRepository
from app.services.background_worker import FunctionWorker
from app.services.project_ai_search_service import (
    ProjectAISearchService,
    ProjectSearchResponseError,
    ProjectSearchResult,
)
from app.ui.ai_chat_panel import (
    AIComposer,
    ChatHistoryPopup,
    ChevronComboBox,
    ElidingLineEdit,
    MarkdownMessage,
    PROVIDER_LABELS,
    PROVIDERS,
    ProviderCombo,
    provider_logo_icon,
    provider_logo_pixmap,
)
from app.ui.citation_widgets import _citation_badge
from app.ui.icons import set_widget_icon
from app.ui.ui_styles import MODERN_SCROLLBAR_QSS, apply_ui_palette


def inline_reference_markdown(
    answer: str,
    references: object,
) -> tuple[str, dict[int, int]]:
    """Render canonical paper references as direct, clickable inline links."""
    markdown = str(answer or "")
    if not isinstance(references, (list, tuple)):
        return markdown, {}

    paper_numbers: dict[int, int] = {}
    old_to_canonical: dict[int, int] = {}
    titles: dict[int, str] = {}
    for raw in references:
        if not isinstance(raw, Mapping):
            continue
        try:
            old_number = int(raw["ref"])
            paper_id = int(raw["paper_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if old_number < 1 or paper_id < 1:
            continue
        canonical = paper_numbers.setdefault(paper_id, len(paper_numbers) + 1)
        old_to_canonical[old_number] = canonical
        titles.setdefault(paper_id, " ".join(str(raw.get("title") or "").split()))

    markdown = re.sub(
        r"\[(\d+)\]",
        lambda match: (
            f"[{old_to_canonical[int(match.group(1))]}]"
            if int(match.group(1)) in old_to_canonical
            else match.group(0)
        ),
        markdown,
    )
    markdown = re.sub(r"(\[\d+\])(?:\s*[,;]\s*\1)+", r"\1", markdown)
    reference_map = {
        number: paper_id for paper_id, number in paper_numbers.items()
    }
    for paper_id in reference_map.values():
        title = titles.get(paper_id, "").strip()
        if title:
            markdown = re.sub(
                rf"(?<!\*){re.escape(title)}(?!\*)",
                lambda match: f"**{match.group(0)}**",
                markdown,
                count=1,
                flags=re.I,
            )
    for number, paper_id in reference_map.items():
        target = f"ra-paper://paper/{paper_id}"
        marker = re.compile(rf"(?<![\w\[])\[{number}\](?!\()")
        if marker.search(markdown):
            markdown = marker.sub(rf"[[{number}]]({target})", markdown)
            continue
        title = titles.get(paper_id, "").strip()
        if title:
            position = markdown.casefold().find(title.casefold())
            if position >= 0:
                end = position + len(title)
                markdown = markdown[:end] + f" [[{number}]]({target})" + markdown[end:]
    return markdown, reference_map


class ProjectSearchMessage(MarkdownMessage):
    paper_requested = Signal(int)

    def __init__(self, markdown: str, references: object = None, parent=None) -> None:
        rendered, self.reference_map = inline_reference_markdown(markdown, references)
        super().__init__(rendered, parent)
        self.setObjectName("aiAssistantText")
        self._render_paper_badges()

    def _render_paper_badges(self) -> None:
        """Replace Search links with the same compact badge used by Paper AI."""
        document = self.document()
        replacements: list[tuple[int, int, str, str]] = []
        block = document.begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid():
                    char_format = fragment.charFormat()
                    href = char_format.anchorHref()
                    if href.startswith("ra-paper://paper/"):
                        replacements.append(
                            (
                                fragment.position(),
                                fragment.length(),
                                fragment.text(),
                                href,
                            )
                        )
                iterator += 1
            block = block.next()
        for position, length, label, href in reversed(replacements):
            resource = QUrl(f"ra-paper-badge://{position}")
            pixmap, width, height = _citation_badge(label)
            document.addResource(QTextDocument.ResourceType.ImageResource, resource, pixmap)
            image = QTextImageFormat()
            image.setName(resource.toString())
            image.setWidth(width)
            image.setHeight(height)
            image.setAnchor(True)
            image.setAnchorHref(href)
            image.setToolTip("Open paper")
            cursor = QTextCursor(document)
            cursor.setPosition(position)
            cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertImage(image)
        self._schedule_adjust_height()

    def _anchor_clicked(self, url: QUrl) -> None:
        if url.scheme() != "ra-paper":
            super()._anchor_clicked(url)
            return
        try:
            paper_id = int(url.path().strip("/"))
        except (TypeError, ValueError):
            return
        if paper_id in self.reference_map.values():
            self.paper_requested.emit(paper_id)


class ProjectSearchConversationController(QObject):
    """Project Search state and requests, independent of all presentation widgets."""

    state_changed = Signal()
    draft_changed = Signal(str)
    engine_changed = Signal()

    def __init__(
        self,
        project_id: int,
        project_name: str,
        parent=None,
        *,
        repository: Any = AISearchRepository,
        settings_repository: Any = SettingsRepository,
        service: ProjectAISearchService | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.settings_repository = settings_repository
        self.service = service or ProjectAISearchService()
        self.project_id = int(project_id)
        self.project_name = str(project_name)
        self.conversation_id: int | None = None
        self.messages: list[dict[str, Any]] = []
        self.draft = ""
        self.provider = str(
            settings_repository.get("ai_provider", "gemini") or "gemini"
        )
        self.model = ""
        self.provider_options: list[tuple[str, str]] = []
        self.model_options: list[str] = []
        self.busy = False
        self.status = ""
        self.error_message = ""
        self.retry_available = False
        self._worker: FunctionWorker | None = None
        self._retry_payload: dict[str, Any] | None = None
        self._project_states: dict[int, tuple[int | None, str]] = {}
        self.refresh_settings(emit=False)
        self._select_latest_conversation()
        self._reload_messages()

    def refresh_settings(self, *, emit: bool = True) -> None:
        custom_name = str(
            load_custom_api_config(self.settings_repository)["name"] or "Custom API"
        )
        self.provider_options = [
            (custom_name if value == "custom" else label, value)
            for label, value in PROVIDERS
        ]
        if self.provider not in {value for _label, value in self.provider_options}:
            self.provider = "gemini"
        self._load_model_options()
        if emit:
            self.engine_changed.emit()

    def _load_model_options(self) -> None:
        raw = self.settings_repository.get_json(f"ai_models_{self.provider}", [])
        self.model_options = [str(item) for item in raw] if isinstance(raw, list) else []
        self.model = str(
            self.settings_repository.get(f"ai_model_{self.provider}", "") or ""
        )

    def set_engine(self, provider: str, model: str | None = None) -> None:
        provider = str(provider or "gemini")
        changed_provider = provider != self.provider
        self.provider = provider
        if changed_provider:
            self._load_model_options()
        if model is not None:
            self.model = str(model).strip()
        self.engine_changed.emit()

    def set_model(self, model: str) -> None:
        value = str(model).strip()
        if value != self.model:
            self.model = value
            self.engine_changed.emit()

    def set_draft(self, text: str) -> None:
        value = str(text)
        if value != self.draft:
            self.draft = value
            self.draft_changed.emit(value)

    def set_project(self, project_id: int, project_name: str) -> None:
        project_id = int(project_id)
        if project_id == self.project_id:
            self.project_name = str(project_name)
            self.state_changed.emit()
            return
        self._project_states[self.project_id] = (self.conversation_id, self.draft)
        self.project_id = project_id
        self.project_name = str(project_name)
        saved = self._project_states.get(project_id)
        self.conversation_id = saved[0] if saved is not None else None
        self.draft = saved[1] if saved is not None else ""
        self.error_message = ""
        self.retry_available = False
        self._retry_payload = None
        if saved is None:
            self._select_latest_conversation()
        self._reload_messages()
        self.draft_changed.emit(self.draft)
        self.state_changed.emit()

    def _select_latest_conversation(self) -> None:
        rows = self.repository.list_conversations(self.project_id)
        self.conversation_id = int(rows[0]["id"]) if rows else None

    def _reload_messages(self) -> None:
        self.messages = (
            self.repository.list_messages(self.conversation_id)
            if self.conversation_id is not None
            else []
        )

    def available_conversations(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.repository.list_conversations(self.project_id)]

    def select_conversation(self, conversation_id: int) -> None:
        if self.repository.get_conversation(self.project_id, conversation_id) is None:
            return
        self.conversation_id = int(conversation_id)
        self.draft = ""
        self.error_message = ""
        self.retry_available = False
        self._retry_payload = None
        self._reload_messages()
        self.draft_changed.emit("")
        self.state_changed.emit()

    def new_search(self) -> None:
        self.conversation_id = None
        self.messages = []
        self.draft = ""
        self.error_message = ""
        self.retry_available = False
        self._retry_payload = None
        self.draft_changed.emit("")
        self.state_changed.emit()

    def delete_conversation(self, conversation_id: int) -> bool:
        if self.busy:
            return False
        deleted = self.repository.delete_conversation(self.project_id, conversation_id)
        if deleted and self.conversation_id == int(conversation_id):
            self._select_latest_conversation()
            self._reload_messages()
            self.state_changed.emit()
        return bool(deleted)

    @staticmethod
    def _friendly_error(error: object) -> str:
        if isinstance(error, ProjectSearchResponseError):
            return "The AI response could not be read. Please retry the search."
        if isinstance(error, AIRateLimitError) or "429" in str(error):
            return "The AI provider is rate-limited. Please wait, then retry."
        if isinstance(error, AIAuthenticationError):
            return "The selected provider credentials are not valid."
        if isinstance(error, AIProviderUnavailableError) or re.search(
            r"\b5\d\d\b", str(error)
        ):
            return "The AI provider is temporarily unavailable. Please retry."
        return str(error) or "AI Search failed."

    def send(self) -> None:
        if self.busy:
            return
        question = self.draft.strip()
        if not question:
            return
        if not self.model:
            self.error_message = "Choose or enter a model before searching."
            self.retry_available = False
            self.state_changed.emit()
            return
        if self.conversation_id is None:
            self.conversation_id = self.repository.create_conversation(self.project_id)
        self.repository.append_message(self.conversation_id, "user", question)
        self._reload_messages()
        self.draft = ""
        self._retry_payload = {
            "project_id": self.project_id,
            "conversation_id": self.conversation_id,
            "question": question,
        }
        self.draft_changed.emit("")
        self._start_request()

    def retry(self) -> None:
        if self.busy or not self.retry_available or self._retry_payload is None:
            return
        if int(self._retry_payload["project_id"]) != self.project_id:
            return
        self._start_request()

    def _start_request(self) -> None:
        payload = self._retry_payload
        if payload is None:
            return
        self.busy = True
        self.status = "Searching Research Profiles…"
        self.error_message = ""
        self.retry_available = False
        self.state_changed.emit()
        request_project_id = int(payload["project_id"])
        worker = FunctionWorker(
            self.service.search,
            project_id=request_project_id,
            conversation_id=int(payload["conversation_id"]),
            question=str(payload["question"]),
            provider=self.provider,
            model=self.model,
            append_user_message=False,
        )
        self._worker = worker

        def succeeded(value: object) -> None:
            if not isinstance(value, ProjectSearchResult):
                return
            if self.project_id == request_project_id:
                self.conversation_id = value.conversation_id
                self._reload_messages()
                self._retry_payload = None

        def failed(error: object) -> None:
            if self.project_id == request_project_id:
                self.error_message = self._friendly_error(error)
                self.retry_available = True

        worker.signals.result.connect(succeeded)
        worker.signals.error.connect(failed)
        worker.signals.finished.connect(self._request_finished)
        QThreadPool.globalInstance().start(worker)

    def _request_finished(self) -> None:
        self._worker = None
        self.busy = False
        self.status = ""
        self.state_changed.emit()


class ProjectAISearchPanel(QWidget):
    """Stable Library or Reader view bound to a shared Search controller."""

    paper_requested = Signal(int)
    library_requested = Signal()
    history_item_kind = "AI Search"
    _LIBRARY_MAX_CONTENT_WIDTH = 1120
    _LIBRARY_MIN_MARGIN = 40

    def __init__(
        self,
        project_id: int | None = None,
        project_name: str = "",
        parent=None,
        *,
        controller: ProjectSearchConversationController | None = None,
        repository: Any = AISearchRepository,
        settings_repository: Any = SettingsRepository,
        service: ProjectAISearchService | None = None,
        presentation_mode: str = "library",
    ) -> None:
        super().__init__(parent)
        self.controller = controller or ProjectSearchConversationController(
            int(project_id or 1),
            project_name,
            self,
            repository=repository,
            settings_repository=settings_repository,
            service=service,
        )
        self.setObjectName("projectAISearchPage")
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._presentation_mode = presentation_mode
        self._message_rows: list[QWidget] = []
        self._syncing_engine = False
        self._setup_ui()
        self.controller.state_changed.connect(self._render_state)
        self.controller.draft_changed.connect(self._sync_draft)
        self.controller.engine_changed.connect(self._sync_engine)
        self._sync_engine()
        self._sync_draft(self.controller.draft)
        self._render_state()
        self.set_presentation_mode(presentation_mode)
        self._apply_style()

    @property
    def project_id(self) -> int:
        return self.controller.project_id

    @property
    def project_name(self) -> str:
        return self.controller.project_name

    @property
    def conversation_id(self) -> int | None:
        return self.controller.conversation_id

    @conversation_id.setter
    def conversation_id(self, value: int | None) -> None:
        self.controller.conversation_id = value

    def _setup_ui(self) -> None:
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(24, 18, 24, 20)
        self.outer.setSpacing(9)
        self.header_widget = QWidget()
        self.header_widget.setMinimumWidth(0)
        self.header_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        header = QHBoxLayout(self.header_widget)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.back_button = QPushButton("Library")
        self.back_button.setObjectName("searchAIBack")
        set_widget_icon(self.back_button, "arrow-left", size=15)
        self.back_button.clicked.connect(self.library_requested)
        self.heading_widget = QWidget()
        heading = QVBoxLayout(self.heading_widget)
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(0)
        title = QLabel("AI Search")
        title.setObjectName("searchAITitle")
        self.project_badge = QLabel()
        self.project_badge.setObjectName("searchAIProject")
        heading.addWidget(title)
        heading.addWidget(self.project_badge)
        self.history_button = QPushButton("History")
        self.history_button.setObjectName("searchAIHeaderButton")
        set_widget_icon(self.history_button, "history", size=14)
        self.new_button = QPushButton("New Search")
        self.new_button.setObjectName("searchAIHeaderButton")
        set_widget_icon(self.new_button, "plus", size=14)
        header.addWidget(self.back_button)
        header.addWidget(self.heading_widget)
        header.addStretch()
        header.addWidget(self.history_button)
        header.addWidget(self.new_button)
        self.outer.addWidget(self.header_widget)
        self.history_popup = ChatHistoryPopup(self)
        self.history_popup.title.setText("Recent searches")
        self.history_popup.new_chat_button.setText("New Search")
        self.history_button.clicked.connect(
            lambda: self.history_popup.show_below(self.history_button)
        )
        self.new_button.clicked.connect(self.new_chat)

        self.context_strip = QFrame()
        self.context_strip.setObjectName("aiContextStrip")
        self.context_strip.setMinimumWidth(0)
        context_layout = QHBoxLayout(self.context_strip)
        context_layout.setContentsMargins(8, 3, 8, 3)
        self.context_label = QLabel()
        self.context_label.setObjectName("aiContextLabel")
        context_layout.addWidget(self.context_label)
        context_layout.addStretch()
        self.outer.addWidget(self.context_strip)

        self.conversation_pages = QStackedWidget()
        self.conversation_pages.setObjectName("searchAIConversationPages")
        self.conversation_pages.setMinimumWidth(0)
        empty_page = QWidget()
        empty_page.setObjectName("searchAIEmptyPage")
        empty_page.setMinimumWidth(0)
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.setContentsMargins(20, 20, 20, 20)
        empty_layout.addStretch()
        empty_title = QLabel("Search your research")
        empty_title.setObjectName("searchAIEmptyTitle")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_title.setWordWrap(True)
        empty_text = QLabel(
            "Find papers you previously studied,\n"
            "compare approaches, datasets or results."
        )
        empty_text.setObjectName("searchAIEmptyText")
        empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_text.setWordWrap(True)
        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(empty_text)
        self.suggestions = QWidget()
        self.suggestions.setMinimumWidth(0)
        suggestions_layout = QVBoxLayout(self.suggestions)
        suggestions_layout.setContentsMargins(0, 8, 0, 0)
        suggestions_layout.setSpacing(6)
        for question in (
            "Which papers used ROI-based compression?",
            "Which papers used YOLO for ROI detection?",
            "What papers achieved the best bitrate savings?",
        ):
            button = QPushButton(question)
            button.setObjectName("searchAISuggestion")
            button.setMinimumWidth(0)
            button.clicked.connect(
                lambda _checked=False, value=question: self.controller.set_draft(value)
            )
            suggestions_layout.addWidget(button)
        empty_layout.addWidget(
            self.suggestions, 0, Qt.AlignmentFlag.AlignHCenter
        )
        empty_layout.addStretch()
        self.scroll = QScrollArea()
        self.scroll.setObjectName("searchAIConversation")
        self.scroll.setMinimumWidth(0)
        self.scroll.setProperty("modernScroll", True)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.content.setObjectName("searchAIMessageContent")
        self.content.setMinimumWidth(0)
        self.messages_layout = QVBoxLayout(self.content)
        self.messages_layout.setContentsMargins(8, 12, 8, 12)
        self.messages_layout.setSpacing(10)
        self.messages_layout.addStretch()
        self.scroll.setWidget(self.content)
        self.conversation_pages.addWidget(empty_page)
        self.conversation_pages.addWidget(self.scroll)
        self.conversation_pages.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.outer.addWidget(self.conversation_pages, 1)

        self.composer_shell = QFrame()
        self.composer_shell.setObjectName("aiComposerShell")
        self.composer_shell.setMinimumWidth(0)
        composer_layout = QVBoxLayout(self.composer_shell)
        composer_layout.setContentsMargins(9, 7, 7, 7)
        composer_layout.setSpacing(5)
        self.composer = AIComposer()
        self.composer.setObjectName("aiComposer")
        self.composer.setPlaceholderText("Ask across papers in this project...")
        self.composer.submit_requested.connect(self.send)
        self.composer.textChanged.connect(
            lambda: self.controller.set_draft(self.composer.toPlainText())
        )
        composer_layout.addWidget(self.composer)
        controls = QHBoxLayout()
        controls.setContentsMargins(1, 0, 0, 0)
        controls.setSpacing(5)
        self.provider = ProviderCombo()
        self.provider.setObjectName("aiHeaderProvider")
        self.provider.setMinimumWidth(112)
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.model = ChevronComboBox()
        self.model.setObjectName("aiModelCombo")
        self.model.setEditable(True)
        self.model.setLineEdit(ElidingLineEdit())
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.setMinimumWidth(130)
        self.model.currentTextChanged.connect(self._model_changed)
        self.send_button = QPushButton()
        self.send_button.setObjectName("aiSendButton")
        self.send_button.setFixedSize(30, 30)
        set_widget_icon(
            self.send_button,
            "arrow-up",
            size=15,
            tooltip="Send",
            color="#FFFFFF",
            active_color="#FFFFFF",
        )
        self.send_button.clicked.connect(self.send)
        controls.addWidget(self.provider)
        controls.addWidget(self.model, 1)
        controls.addWidget(self.send_button)
        composer_layout.addLayout(controls)
        self.composer_shell.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.outer.addWidget(self.composer_shell)
        self.status = QLabel()
        self.status.setObjectName("searchAIStatus")
        self.status.setWordWrap(True)
        self.status.hide()
        self.outer.addWidget(self.status)

    def set_presentation_mode(self, mode: str) -> None:
        if mode not in {"library", "reader"}:
            raise ValueError("Unknown Search presentation mode.")
        self._presentation_mode = mode
        reader = mode == "reader"
        self.back_button.setVisible(not reader)
        self.heading_widget.setVisible(not reader)
        self.outer.setContentsMargins(*(0, 0, 0, 0) if reader else (24, 18, 24, 20))
        self.header_widget.setVisible(not reader)
        self.context_strip.setVisible(reader)
        self.suggestions.setVisible(not reader)
        self.provider.setVisible(not reader)
        self.provider.setMinimumWidth(74 if reader else 112)
        self.model.setMinimumWidth(80 if reader else 130)
        self._update_responsive_layout()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_responsive_layout()

    def _update_responsive_layout(self) -> None:
        if self._presentation_mode == "reader":
            self.outer.setContentsMargins(0, 0, 0, 0)
            content_width = max(0, self.width())
        else:
            available = max(0, self.width())
            minimum_margin = self._LIBRARY_MIN_MARGIN if available >= 900 else 18
            content_width = min(
                self._LIBRARY_MAX_CONTENT_WIDTH,
                max(0, available - (minimum_margin * 2)),
            )
            horizontal_margin = max(
                minimum_margin, (available - content_width) // 2
            )
            self.outer.setContentsMargins(horizontal_margin, 18, horizontal_margin, 20)
            content_width = max(0, available - (horizontal_margin * 2))

        conversation_width = max(
            0,
            content_width
            - self.messages_layout.contentsMargins().left()
            - self.messages_layout.contentsMargins().right(),
        )
        user_ratio = 0.72 if self._presentation_mode == "library" else 0.82
        user_maximum = max(180, int(conversation_width * user_ratio))
        for bubble in self.content.findChildren(QFrame, "aiUserBubble"):
            bubble.setMaximumWidth(user_maximum)

    def _provider_changed(self, _index: int) -> None:
        if not self._syncing_engine:
            self.controller.set_engine(str(self.provider.currentData() or "gemini"))

    def _model_changed(self, text: str) -> None:
        if not self._syncing_engine:
            self.controller.set_model(text)

    def _sync_engine(self) -> None:
        self._syncing_engine = True
        try:
            self.provider.clear()
            for label, value in self.controller.provider_options:
                self.provider.addItem(provider_logo_icon(value), label, value)
            self.provider.setCurrentIndex(
                max(0, self.provider.findData(self.controller.provider))
            )
            self.model.clear()
            self.model.addItems(self.controller.model_options)
            self.model.setEditText(self.controller.model)
        finally:
            self._syncing_engine = False

    def _sync_draft(self, text: str) -> None:
        if self.composer.toPlainText() == text:
            return
        blocker = QSignalBlocker(self.composer)
        self.composer.setPlainText(text)
        del blocker
        self.composer._resize_to_content()

    def _clear_messages(self) -> None:
        while self.messages_layout.count() > 1:
            item = self.messages_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._message_rows.clear()

    def _render_state(self) -> None:
        self.project_badge.setText(f"Current project: {self.controller.project_name}")
        self.project_badge.setToolTip(self.controller.project_name)
        self.context_label.setText(f"Context: {self.controller.project_name}")
        self.context_label.setToolTip(self.controller.project_name)
        self._clear_messages()
        for row in self.controller.messages:
            self._append_message(
                str(row["role"]),
                str(row["content"]),
                row.get("references", []),
                provider=str(row.get("provider") or ""),
                model=str(row.get("model") or ""),
            )
        if self.controller.error_message:
            self._append_error(
                self.controller.error_message,
                retryable=self.controller.retry_available,
            )
        has_content = bool(self.controller.messages or self.controller.error_message)
        self.conversation_pages.setCurrentIndex(1 if has_content else 0)
        self.status.setText(self.controller.status)
        self.status.setVisible(bool(self.controller.status))
        self.send_button.setEnabled(not self.controller.busy)
        self._update_responsive_layout()
        self._scroll_to_bottom()

    def _append_message(
        self,
        role: str,
        content: str,
        references: object = None,
        *,
        provider: str = "",
        model: str = "",
    ) -> None:
        row = QWidget()
        row.setObjectName("aiMessageRow")
        row.setMinimumWidth(0)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(7)
        if role == "user":
            row_layout.addStretch(1)
            bubble = QFrame()
            bubble.setObjectName("aiUserBubble")
            bubble.setMinimumWidth(0)
            bubble.setMaximumWidth(680)
            bubble_layout = QVBoxLayout(bubble)
            bubble_layout.setContentsMargins(10, 7, 10, 7)
            message = QLabel(content)
            message.setObjectName("aiMessageText")
            message.setMinimumWidth(0)
            message.setWordWrap(True)
            message.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            bubble_layout.addWidget(message)
            row_layout.addWidget(bubble, 0, Qt.AlignmentFlag.AlignRight)
        else:
            provider = provider or self.controller.provider
            model = model or self.controller.model
            body = QWidget()
            body.setObjectName("aiAssistantBody")
            body.setMinimumWidth(0)
            body_layout = QVBoxLayout(body)
            body_layout.setContentsMargins(0, 0, 0, 0)
            body_layout.setSpacing(4)
            provider_header = QHBoxLayout()
            provider_header.setContentsMargins(0, 0, 0, 0)
            provider_header.setSpacing(5)
            logo = QLabel()
            logo.setObjectName("aiProviderIcon")
            logo.setPixmap(provider_logo_pixmap(provider, 19))
            logo.setFixedSize(24, 24)
            logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            provider_header.addWidget(logo)
            provider_name = QLabel(PROVIDER_LABELS.get(provider, provider.title()))
            provider_name.setObjectName("aiProviderName")
            provider_header.addWidget(provider_name)
            if model:
                model_name = QLabel(model)
                model_name.setObjectName("aiProviderModel")
                model_name.setToolTip(model)
                provider_header.addWidget(model_name)
            provider_header.addStretch()
            body_layout.addLayout(provider_header)
            message = ProjectSearchMessage(content, references)
            message.paper_requested.connect(self.paper_requested)
            body_layout.addWidget(message)
            row_layout.addWidget(body, 1)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, row)
        self._message_rows.append(row)

    def _append_error(self, text: str, *, retryable: bool) -> None:
        row = QWidget()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(12, 1, 12, 1)
        layout.setSpacing(4)
        label = QLabel(text)
        label.setObjectName("aiInlineError")
        label.setWordWrap(True)
        layout.addWidget(label)
        if retryable:
            retry = QPushButton("Retry")
            retry.setObjectName("aiInlineRetry")
            set_widget_icon(retry, "refresh-cw", size=12)
            retry.clicked.connect(self.controller.retry)
            layout.addWidget(retry, 0, Qt.AlignmentFlag.AlignLeft)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, row)
        self._message_rows.append(row)

    def _scroll_to_bottom(self) -> None:
        QTimer.singleShot(
            0,
            self,
            lambda: self.scroll.verticalScrollBar().setValue(
                self.scroll.verticalScrollBar().maximum()
            ),
        )

    def available_conversations(self, *, for_history: bool = False) -> list[dict[str, Any]]:
        del for_history
        return [
            {**row, "conversation_type": "solo"}
            for row in self.controller.available_conversations()
        ]

    @property
    def active_conversation_id(self) -> int | None:
        return self.controller.conversation_id

    def select_chat(self, conversation_id: int) -> None:
        self.controller.select_conversation(conversation_id)

    def new_chat(self, *, notify: bool = True) -> None:
        del notify
        self.controller.new_search()

    new_search = new_chat

    def delete_chat(self, conversation_id: int, title: str) -> bool:
        answer = QMessageBox.question(
            self,
            "Delete search",
            f'Delete "{title}" and its local Search history?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        return self.controller.delete_conversation(conversation_id)

    def send(self) -> None:
        self.controller.set_draft(self.composer.toPlainText())
        self.controller.set_engine(
            str(self.provider.currentData() or "gemini"),
            self.model.currentText(),
        )
        self.controller.send()

    def refresh_settings(self) -> None:
        self.controller.refresh_settings()

    def set_project(self, project_id: int, project_name: str) -> None:
        self.controller.set_project(project_id, project_name)

    def reload_history(self) -> None:
        self.controller._reload_messages()
        self.controller.state_changed.emit()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            apply_ui_palette(
                """
                #projectAISearchPage { background: @APP_BG@; }
                #searchAITitle { font-size: 22px; font-weight: 700; color: @TEXT@; }
                #searchAIProject { color: @TEXT_MUTED@; font-size: 11px; }
                #searchAIBack, #searchAIHeaderButton { min-height: 30px; border: none; border-radius: 7px; background: transparent; color: #536172; padding: 0 7px; }
                #searchAIBack:hover, #searchAIHeaderButton:hover { background: @SURFACE_SUBTLE@; }
                #searchAIConversationPages { background: transparent; border: none; }
                #searchAIConversation, #searchAIEmptyPage { background: @SURFACE@; border: 1px solid @BORDER_SOFT@; border-radius: 12px; }
                #searchAIMessageContent { background: @SURFACE@; }
                #aiUserBubble { background: @PRIMARY_SOFT@; border: 1px solid #D8E4F7; border-radius: 10px; }
                #aiMessageText { color: @TEXT@; font-size: 12px; }
                #aiAssistantText { background: transparent; color: @TEXT@; }
                #aiProviderName { color: #343B46; font-size: 10.5px; font-weight: 650; }
                #aiProviderModel { color: #8A929E; font-size: 9.5px; }
                #searchAIEmptyTitle { color: @TEXT@; font-size: 16px; font-weight: 650; }
                #searchAIEmptyText { color: @TEXT_MUTED@; font-size: 12px; }
                #searchAISuggestion { min-height: 30px; border: 1px solid #DEE3E8; border-radius: 8px; background: white; color: #485563; text-align: left; padding: 0 10px; }
                #searchAISuggestion:hover { border-color: #C9C5F0; background: @AI_SOFT@; }
                #aiContextStrip { min-height: 24px; max-height: 28px; border: 1px solid #E1E7EF; border-radius: 8px; background: #F7F9FC; }
                #aiContextLabel { color: #657181; font-size: 9.5px; font-weight: 600; }
                #aiComposerShell { background: @SURFACE@; border: 1px solid @BORDER@; border-radius: 10px; }
                #aiComposer { background: transparent; border: none; color: @TEXT@; padding: 3px 4px; font-size: 12px; }
                #aiComposerPlaceholder { color: #8A919A; background: transparent; font-size: 12px; }
                #aiHeaderProvider { min-height: 26px; max-height: 28px; border: none; border-radius: 6px; background: transparent; padding: 0 18px 0 2px; color: #3C424A; }
                #aiHeaderProvider:hover, #aiModelCombo:hover { background: @SURFACE_SUBTLE@; }
                #aiHeaderProvider::drop-down, #aiModelCombo::drop-down { width: 15px; border: none; background: transparent; }
                #aiHeaderProvider QAbstractItemView, #aiModelCombo QAbstractItemView { border: 1px solid #D8DDE3; border-radius: 7px; background: white; color: #343A42; padding: 4px; outline: none; }
                #aiHeaderProvider QAbstractItemView::item:selected, #aiModelCombo QAbstractItemView::item:selected { background: @AI_SOFT@; color: #5149B8; }
                #aiModelCombo { min-height: 26px; max-height: 26px; border: none; border-radius: 6px; background: transparent; color: #3C424A; padding: 0 19px 0 4px; font-size: 10px; }
                #aiModelCombo QLineEdit { border: none; background: transparent; color: #3C424A; padding: 0; font-size: 10px; }
                #aiSendButton { border: none; border-radius: 15px; background: @PRIMARY@; }
                #aiSendButton:hover { background: @PRIMARY_HOVER@; }
                #aiSendButton:disabled { background: #D7DBE0; }
                #searchAIStatus { color: @TEXT_MUTED@; font-size: 11px; }
                #aiInlineError { color: #A14A4A; font-size: 11px; }
                #aiInlineRetry { max-width: 76px; min-height: 25px; border: 1px solid #C8D5E4; border-radius: 7px; background: #F4F8FC; color: #355B7D; padding: 0 8px; }
                #aiInlineRetry:hover { background: #E8F1FA; }
                #aiHistoryPopup { background: white; border: 1px solid #D9DEE5; border-radius: 10px; }
                #aiHistoryPopupScroll, #aiHistoryPopupScroll > QWidget > QWidget { background: white; border: none; }
                #aiHistoryRow { background: transparent; border-radius: 7px; }
                #aiHistoryRow[active="true"], #aiHistoryRow:hover { background: #F1F4F8; }
                #aiHistoryChatButton, #aiHistoryDeleteButton, #aiHistoryNewButton { border: none; background: transparent; border-radius: 6px; color: @TEXT@; }
                #aiHistoryNewButton { min-height: 30px; background: #F5F7FA; }
                #aiHistorySoloKind { color: #657384; font-size: 9px; }
                """
            )
            + MODERN_SCROLLBAR_QSS
        )


LibrarySearchView = ProjectAISearchPanel
ReaderSearchPanel = ProjectAISearchPanel
ProjectAISearchDialog = ProjectAISearchPanel
