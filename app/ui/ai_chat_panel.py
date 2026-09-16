from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
import sqlite3
from typing import Any
import unicodedata

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QRect,
    QSignalBlocker,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QDesktopServices,
    QIcon,
    QInputMethodEvent,
    QKeyEvent,
    QPainter,
    QPixmap,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStyleOptionComboBox,
    QStylePainter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.database.ai_repository import AIRepository
from app.ai.custom_config import load_custom_api_config
from app.database.settings_repository import SettingsRepository
from app.database.summary_repository import SummaryRepository
from app.ui.icons import icon_pixmap, set_widget_icon
from app.ui.citation_widgets import (
    activate_source_group,
    insert_inline_citations,
)
from app.ui.markdown_math import set_markdown_math
from app.runtime_paths import resource_path


PROVIDER_LOGO_ROOT = resource_path("resources", "provider_logos")
PROVIDERS = (
    ("Gemini", "gemini"),
    ("OpenAI", "openai"),
    ("Claude", "claude"),
    ("DeepSeek", "deepseek"),
    ("Custom API", "custom"),
)
PROVIDER_LABELS = {provider: label for label, provider in PROVIDERS}
CHAT_FONT_MIN_PX = 11.0
CHAT_FONT_DEFAULT_PX = 12.5
CHAT_FONT_MAX_PX = 22.0
CHAT_FONT_STEP_PX = 1.0
CHAT_FONT_SETTING = "ai_chat_font_size"


def _chat_document_stylesheet(font_size: float) -> str:
    body = max(CHAT_FONT_MIN_PX, min(CHAT_FONT_MAX_PX, float(font_size)))
    return f"""
        p {{ font-size: {body:g}px; margin-top: 0; margin-bottom: 10px; }}
        li, td, th {{ font-size: {body:g}px; }}
        h1 {{ font-size: {body + 4.5:g}px; font-weight: 700; margin: 12px 0 7px 0; }}
        h2 {{ font-size: {body + 2.5:g}px; font-weight: 700; margin: 11px 0 6px 0; }}
        h3 {{ font-size: {body + 0.5:g}px; font-weight: 700; margin: 9px 0 5px 0; }}
        ul, ol {{ margin-top: 3px; margin-bottom: 9px; margin-left: 14px; }}
        li {{ margin-bottom: 4px; }}
        code {{ font-size: {max(10.0, body - 0.5):g}px; background-color: #F0F2F4; color: #353B43; }}
        pre {{ font-size: {max(10.0, body - 0.5):g}px; background-color: #F4F5F7; margin: 7px 0; padding: 7px; }}
        a {{ color: #356FA8; text-decoration: none; }}
    """
STARTER_COPY = {
    "en": {
        "title": "Start with this paper",
        "subtitle": "Ask a quick question or generate a short overview.",
        "questions": (
            "What problem does this paper solve?",
            "What is the main contribution?",
            "How does it differ from previous methods?",
            "What are the key results?",
        ),
        "brief": "Generate Paper Brief",
    },
    "vi": {
        "title": "Bắt đầu với bài báo này",
        "subtitle": "Đặt một câu hỏi nhanh hoặc xem tóm tắt ngắn về bài báo.",
        "questions": (
            "Bài báo này giải quyết vấn đề gì?",
            "Đóng góp chính của bài báo là gì?",
            "Phương pháp này khác gì so với các phương pháp trước?",
            "Các kết quả chính của bài báo là gì?",
        ),
        "brief": "Tạo tóm tắt nhanh",
    },
}
COMPARISON_COPY = {
    "en": {
        "title": "Compare these papers",
        "questions": (
            "Compare the methods used across these papers.",
            "Compare their datasets and experimental setups.",
            "Compare the main results reported by these papers.",
            "How do their strengths and weaknesses differ?",
        ),
    },
    "vi": {
        "title": "So sánh các bài báo",
        "questions": (
            "So sánh phương pháp của các bài báo.",
            "So sánh bộ dữ liệu và thiết lập thí nghiệm.",
            "So sánh các kết quả chính.",
            "Điểm mạnh và điểm yếu khác nhau như thế nào?",
        ),
    },
}
BRIEF_FIELDS = (
    ("problem", "Problem"),
    ("contribution", "Main contribution"),
    ("method", "Method"),
    ("results", "Key results"),
    ("dataset", "Dataset"),
)


def _is_meaningful_summary_value(value: object) -> bool:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return False
    normalized = unicodedata.normalize("NFKD", text.casefold())
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    placeholders = (
        "paper does not provide",
        "paper does not report",
        "not provided",
        "not specified",
        "not reported",
        "not available",
        "bai bao khong neu ro",
        "bai bao khong cung cap",
        "khong duoc de cap",
        "khong co thong tin",
        "chua ro",
    )
    return not any(placeholder in normalized for placeholder in placeholders)


class SuggestedQuestionButton(QPushButton):
    """Word-wrapping question chip that remains a normal accessible button."""

    def __init__(self, question: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("aiSuggestionButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        self._label = QLabel()
        self._label.setObjectName("aiSuggestionText")
        self._label.setWordWrap(True)
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        layout.addWidget(self._label)
        self.set_question(question)

    def set_question(self, question: str) -> None:
        self._question = question
        self._label.setText(question)
        self.setAccessibleName(question)

    def question(self) -> str:
        return self._question


class PaperStartWidget(QFrame):
    suggestion_requested = Signal(str, str)
    brief_requested = Signal()

    def __init__(self, output_language: str = "vi", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("aiPaperStartCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 11, 12, 11)
        layout.setSpacing(7)
        self.title = QLabel()
        self.title.setObjectName("aiPaperStartTitle")
        self.subtitle = QLabel()
        self.subtitle.setObjectName("aiPaperStartSubtitle")
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)
        layout.addSpacing(2)
        self.question_buttons: list[SuggestedQuestionButton] = []
        for _index in range(4):
            button = SuggestedQuestionButton("")
            button.clicked.connect(
                lambda _checked=False, source=button: self.suggestion_requested.emit(
                    source.question(), self.output_language
                )
            )
            self.question_buttons.append(button)
            layout.addWidget(button)
        self.brief_button = QPushButton()
        self.brief_button.setObjectName("aiPaperBriefButton")
        self.brief_button.setCursor(Qt.CursorShape.PointingHandCursor)
        set_widget_icon(self.brief_button, "notebook-text", size=14)
        self.brief_button.clicked.connect(self.brief_requested)
        layout.addWidget(self.brief_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.compare_group = QFrame()
        self.compare_group.setObjectName("aiCompareStartGroup")
        compare_layout = QVBoxLayout(self.compare_group)
        compare_layout.setContentsMargins(0, 7, 0, 0)
        compare_layout.setSpacing(7)
        self.compare_title = QLabel()
        self.compare_title.setObjectName("aiPaperStartTitle")
        compare_layout.addWidget(self.compare_title)
        self.compare_buttons: list[SuggestedQuestionButton] = []
        for _index in range(4):
            button = SuggestedQuestionButton("")
            button.clicked.connect(
                lambda _checked=False, source=button: self.suggestion_requested.emit(
                    source.question(), self.output_language
                )
            )
            self.compare_buttons.append(button)
            compare_layout.addWidget(button)
        self.compare_group.hide()
        layout.addWidget(self.compare_group)
        self.set_output_language(output_language)

    def set_output_language(self, output_language: str) -> None:
        self.output_language = str(output_language or "vi").strip() or "vi"
        copy = STARTER_COPY.get(self.output_language.casefold(), STARTER_COPY["en"])
        self.title.setText(str(copy["title"]))
        self.subtitle.setText(str(copy["subtitle"]))
        for button, question in zip(self.question_buttons, copy["questions"]):
            button.set_question(str(question))
        self.brief_button.setText(str(copy["brief"]))
        comparison = COMPARISON_COPY.get(
            self.output_language.casefold(), COMPARISON_COPY["en"]
        )
        self.compare_title.setText(str(comparison["title"]))
        for button, question in zip(
            self.compare_buttons, comparison["questions"]
        ):
            button.set_question(str(question))

    def set_workspace_paper_count(self, count: int) -> None:
        self.set_group_mode(int(count) >= 2)

    def set_group_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self.title.setVisible(not enabled)
        self.subtitle.setVisible(not enabled)
        for button in self.question_buttons:
            button.setVisible(not enabled)
        self.brief_button.setVisible(not enabled)
        self.compare_group.setVisible(enabled)


class PaperBriefWidget(QFrame):
    close_requested = Signal()

    def __init__(self, sections: list[tuple[str, str]], parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("aiPaperBriefCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 11, 12, 11)
        layout.setSpacing(6)
        header = QHBoxLayout()
        title = QLabel("Paper Brief")
        title.setObjectName("aiPaperStartTitle")
        back = QPushButton("Back")
        back.setObjectName("aiBriefBackButton")
        set_widget_icon(back, "arrow-left", size=13)
        back.clicked.connect(self.close_requested)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(back)
        layout.addLayout(header)
        for heading, content in sections:
            label = QLabel(heading)
            label.setObjectName("aiBriefHeading")
            value = QLabel(content)
            value.setObjectName("aiBriefText")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(label)
            layout.addWidget(value)
        ask = QPushButton("Ask about this paper")
        ask.setObjectName("aiPaperBriefButton")
        ask.clicked.connect(self.close_requested)
        layout.addWidget(ask, 0, Qt.AlignmentFlag.AlignLeft)


def _configure_dropdown(combo: QComboBox, *, visible_rows: int = 10) -> None:
    combo.setMaxVisibleItems(visible_rows)
    combo.view().setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    combo.view().setMaximumHeight(visible_rows * 30)


@lru_cache(maxsize=16)
def provider_logo_pixmap(provider: str, size: int = 19) -> QPixmap:
    path = PROVIDER_LOGO_ROOT / f"{provider}.png"
    pixmap = QPixmap(str(path))
    if not pixmap.isNull():
        return pixmap.scaled(
            QSize(size, size),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return icon_pixmap("sparkles", size)


def provider_logo_icon(provider: str) -> QIcon:
    return QIcon(provider_logo_pixmap(provider, 20))


def _chat_title(text: str, limit: int = 60) -> str:
    value = " ".join(text.split()).strip()
    if not value:
        return "New Chat"
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


class ChevronComboBox(QComboBox):
    """Compact combo with a consistently visible, click-through chevron."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.chevron = QLabel(self)
        self.chevron.setPixmap(icon_pixmap("chevron-down", 11))
        self.chevron.setFixedSize(11, 11)
        self.chevron.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.chevron.move(
            max(0, self.width() - self.chevron.width() - 5),
            max(0, (self.height() - self.chevron.height()) // 2),
        )
        self.chevron.raise_()

    def setLineEdit(self, edit: QLineEdit) -> None:
        previous = self.lineEdit()
        if previous is not None:
            previous.removeEventFilter(self)
        super().setLineEdit(edit)
        edit.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is self.lineEdit()
            and event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            # Editable combo boxes normally give the click to their line edit.
            # Open only after the gesture has completed so a synthesized
            # touchpad release cannot immediately close a popup opened on press.
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.showPopup()
            return True
        return super().eventFilter(watched, event)


class ProviderCombo(ChevronComboBox):
    """Provider logo and name; popup items retain the same visual identity."""

    def paintEvent(self, _event) -> None:
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        option.iconSize = QSize(19, 19)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)


class ElidingPushButton(QPushButton):
    """Single-line button whose text cannot force a popup row wider."""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _event) -> None:
        painter = QStylePainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = self.fontMetrics().elidedText(
            self.text(),
            Qt.TextElideMode.ElideRight,
            max(0, self.width() - 10),
        )
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)


class ElidingLineEdit(QLineEdit):
    """Show an ellipsis when idle while retaining normal manual editing."""

    def paintEvent(self, event) -> None:
        if self.hasFocus() or not self.text():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        rect = self.contentsRect().adjusted(1, 0, -2, 0)
        text = self.fontMetrics().elidedText(
            self.text(),
            Qt.TextElideMode.ElideRight,
            rect.width(),
        )
        painter.drawText(
            rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            text,
        )


class AIComposer(QPlainTextEdit):
    submit_requested = Signal()

    _MIN_HEIGHT = 36
    _MAX_HEIGHT = 116

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(self._MIN_HEIGHT)
        self.setMaximumHeight(self._MAX_HEIGHT)
        self.setFixedHeight(self._MIN_HEIGHT)
        self._preedit = ""
        self._placeholder = QLabel(self.viewport())
        self._placeholder.setObjectName("aiComposerPlaceholder")
        self._placeholder.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        super().setPlaceholderText("")
        self.textChanged.connect(self._resize_to_content)
        self.textChanged.connect(self._update_placeholder)

    def setPlaceholderText(self, text: str) -> None:
        self._placeholder.setText(text)
        super().setPlaceholderText("")
        self._position_placeholder()
        self._update_placeholder()

    def placeholderText(self) -> str:
        return self._placeholder.text()

    def inputMethodEvent(self, event: QInputMethodEvent) -> None:
        self._preedit = event.preeditString()
        super().inputMethodEvent(event)
        self._update_placeholder()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (
            event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}
            and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.submit_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self, self._resize_to_content)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_placeholder()
        QTimer.singleShot(0, self, self._resize_to_content)

    def _position_placeholder(self) -> None:
        self._placeholder.setGeometry(
            7,
            0,
            max(0, self.viewport().width() - 14),
            self.viewport().height(),
        )

    def _update_placeholder(self) -> None:
        self._placeholder.setVisible(
            not self.toPlainText() and not self._preedit
        )

    def _resize_to_content(self) -> None:
        document = self.document()
        layout = document.documentLayout()
        height = 0.0
        block = document.firstBlock()
        while block.isValid():
            height += layout.blockBoundingRect(block).height()
            block = block.next()
        desired = int(height + 14)
        widget_height = max(self._MIN_HEIGHT, min(self._MAX_HEIGHT, desired))
        if self.height() != widget_height:
            self.setFixedHeight(widget_height)
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if desired >= self._MAX_HEIGHT
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )


class MarkdownMessage(QTextBrowser):
    """Auto-height Qt Markdown view; the surrounding chat owns scrolling."""

    citation_requested = Signal(object)

    def __init__(
        self,
        markdown: str,
        parent=None,
        *,
        font_size: float = CHAT_FONT_DEFAULT_PX,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("aiAssistantText")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().setDocumentMargin(0)
        self._source_markdown = str(markdown or "")
        self._streaming_render = False
        self._chat_font_size = float(font_size)
        self._citation_values: list[object] = []
        self._citation_paper_labels: dict[int, str] = {}
        self._citation_groups: dict[str, list[object]] = {}
        self._apply_document_style()
        set_markdown_math(self.document(), self._source_markdown)
        self._layout_width = -1
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.timeout.connect(self._adjust_height)
        self.anchorClicked.connect(self._anchor_clicked)
        self.document().documentLayout().documentSizeChanged.connect(
            self._schedule_adjust_height
        )
        self._schedule_adjust_height()

    def update_markdown(self, markdown: str, *, streaming: bool = False) -> None:
        self._source_markdown = str(markdown or "")
        self._streaming_render = bool(streaming)
        self._citation_values = []
        self._citation_paper_labels = {}
        self._citation_groups.clear()
        set_markdown_math(
            self.document(),
            self._source_markdown,
            streaming=self._streaming_render,
        )
        self._schedule_adjust_height()

    def set_citations(
        self,
        citations: object,
        paper_labels: Mapping[int, str] | None = None,
    ) -> None:
        self._streaming_render = False
        self._citation_values = (
            list(citations) if isinstance(citations, (list, tuple)) else []
        )
        self._citation_paper_labels = dict(paper_labels or {})
        self._citation_groups = insert_inline_citations(
            self.document(),
            self._citation_values,
            paper_labels=self._citation_paper_labels,
        )
        self._schedule_adjust_height()

    def set_chat_font_size(self, font_size: float) -> None:
        value = max(CHAT_FONT_MIN_PX, min(CHAT_FONT_MAX_PX, float(font_size)))
        if abs(value - self._chat_font_size) < 0.01:
            return
        self._chat_font_size = value
        self._apply_document_style()
        set_markdown_math(
            self.document(),
            self._source_markdown,
            streaming=self._streaming_render,
        )
        self._citation_groups = insert_inline_citations(
            self.document(),
            self._citation_values,
            paper_labels=self._citation_paper_labels,
        )
        self._layout_width = -1
        self._schedule_adjust_height()

    def _apply_document_style(self) -> None:
        self.setStyleSheet(f"font-size: {self._chat_font_size:g}px;")
        self.document().setDefaultStyleSheet(
            _chat_document_stylesheet(self._chat_font_size)
        )

    def _anchor_clicked(self, url: QUrl) -> None:
        if url.scheme() != "ra-source":
            QDesktopServices.openUrl(url)
            return
        group = self._citation_groups.get(url.host())
        if group:
            activate_source_group(self, group, self.citation_requested.emit)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._schedule_adjust_height()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._schedule_adjust_height()

    def _schedule_adjust_height(self, *_args: object) -> None:
        # The timer is owned by this message, so pending work is cancelled when
        # a conversation reload deletes the row. Repeated document/resize
        # signals are also coalesced into one geometry update.
        if not self._height_timer.isActive():
            self._height_timer.start(0)

    def _adjust_height(self) -> None:
        width = max(40, self.viewport().width())
        if self._layout_width != width:
            self._layout_width = width
            self.document().setTextWidth(width)
        height = max(24, int(self.document().size().height()) + 4)
        if self.height() != height:
            self.setFixedHeight(height)


class ChatHistoryPopup(QFrame):
    """Compact non-modal list anchored below the Reader history button."""

    def __init__(self, panel: "AIChatPanel") -> None:
        super().__init__(panel, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.panel = panel
        self.setObjectName("aiHistoryPopup")
        self.setMinimumWidth(270)
        self.setMaximumWidth(340)
        self._preferred_height = 480

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        self.title = QLabel("Recent chats")
        self.title.setObjectName("aiHistoryTitle")
        layout.addWidget(self.title)

        self.scroll = QScrollArea()
        self.scroll.setProperty("modernScroll", True)
        self.scroll.setObjectName("aiHistoryPopupScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.content = QWidget()
        self.rows = QVBoxLayout(self.content)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(3)
        self.scroll.setWidget(self.content)
        layout.addWidget(self.scroll, 1)

        self.new_chat_button = QPushButton("New Chat")
        self.new_chat_button.setObjectName("aiHistoryNewButton")
        set_widget_icon(self.new_chat_button, "plus", size=14)
        self.new_chat_button.clicked.connect(self._new_chat)
        layout.addWidget(self.new_chat_button)

    def _clear_rows(self) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def refresh(self) -> None:
        self._clear_rows()
        conversations = self.panel.available_conversations(for_history=True)
        if not conversations:
            empty = QLabel("No saved chats yet")
            empty.setObjectName("aiHistoryEmpty")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.rows.addWidget(empty)
        for conversation in conversations:
            conversation_id = int(conversation["id"])
            row = QFrame()
            row.setObjectName("aiHistoryRow")
            row.setProperty(
                "active",
                conversation_id == self.panel.active_conversation_id,
            )
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(5, 2, 2, 2)
            row_layout.setSpacing(4)
            title = str(conversation.get("title") or "New Chat")
            details = QWidget()
            details_layout = QVBoxLayout(details)
            details_layout.setContentsMargins(0, 0, 0, 0)
            details_layout.setSpacing(2)
            title_row = QHBoxLayout()
            title_row.setContentsMargins(0, 0, 0, 0)
            title_row.setSpacing(4)
            is_group = conversation.get("conversation_type") == "group"
            kind = QLabel(
                "Group"
                if is_group
                else str(getattr(self.panel, "history_item_kind", "Solo"))
            )
            kind.setObjectName(
                "aiHistoryGroupKind" if is_group else "aiHistorySoloKind"
            )
            kind.setFixedHeight(18)
            kind.setAlignment(Qt.AlignmentFlag.AlignCenter)
            kind.setSizePolicy(
                QSizePolicy.Policy.Fixed,
                QSizePolicy.Policy.Fixed,
            )
            select_button = ElidingPushButton(title)
            select_button.setObjectName("aiHistoryChatButton")
            select_button.setToolTip(title)
            select_button.setFixedHeight(28)
            select_button.clicked.connect(
                lambda _checked=False, value=conversation_id: self._select_chat(value)
            )
            title_row.addWidget(kind, 0, Qt.AlignmentFlag.AlignVCenter)
            title_row.addWidget(select_button, 1)
            details_layout.addLayout(title_row)
            if is_group:
                member_row = QHBoxLayout()
                member_row.setContentsMargins(0, 0, 0, 0)
                member_row.setSpacing(3)
                for member in conversation.get(
                    "all_members", conversation.get("members", [])
                ):
                    paper_id = int(member["id"])
                    alias = f"P{int(member['alias_index'])}"
                    member_button = QPushButton(alias)
                    member_button.setObjectName("aiHistoryMemberChip")
                    member_button.setFixedHeight(17)
                    member_button.setSizePolicy(
                        QSizePolicy.Policy.Fixed,
                        QSizePolicy.Policy.Fixed,
                    )
                    member_button.setToolTip(
                        str(member.get("title") or "Untitled paper")
                    )
                    member_button.clicked.connect(
                        lambda _checked=False, chat=conversation_id, value=paper_id: (
                            self.panel.group_paper_requested.emit(chat, value)
                        )
                    )
                    member_row.addWidget(member_button)
                member_row.addStretch()
                details_layout.addLayout(member_row)
            delete_button = QPushButton()
            delete_button.setObjectName("aiHistoryDeleteButton")
            delete_button.setFixedSize(25, 25)
            set_widget_icon(
                delete_button,
                "trash",
                size=13,
                tooltip=f'Delete chat "{title}"',
            )
            delete_button.clicked.connect(
                lambda _checked=False, value=conversation_id, label=title: (
                    self._delete_chat(value, label)
                )
            )
            row_layout.addWidget(details, 1)
            row_layout.addWidget(
                delete_button,
                0,
                Qt.AlignmentFlag.AlignTop,
            )
            row.setFixedHeight(52 if is_group else 34)
            self.rows.addWidget(row)
        self.rows.addStretch()

    def show_below(self, anchor: QWidget) -> None:
        self.refresh()
        requested_width = max(270, min(330, self.panel.width() - 8))
        anchor_bottom_right = anchor.mapToGlobal(
            QPoint(anchor.width(), anchor.height() + 4)
        )
        screen = anchor.screen()
        available = screen.availableGeometry() if screen is not None else QRect()
        window = self.panel.window()
        window_bounds = QRect(
            window.mapToGlobal(QPoint(0, 0)),
            window.size(),
        )
        if available.isValid():
            bounded = available.intersected(window_bounds)
            if bounded.isValid():
                available = bounded
        else:
            available = window_bounds

        edge_margin = 8
        maximum_width = max(1, available.width() - edge_margin * 2)
        width = min(requested_width, maximum_width)
        desired_y = anchor_bottom_right.y()
        bottom = available.bottom() - edge_margin
        space_below = max(0, bottom - desired_y + 1)
        maximum_height = max(1, available.height() - edge_margin * 2)
        height = min(
            self._preferred_height,
            maximum_height,
            max(160, space_below),
        )

        left = available.left() + edge_margin
        right = available.right() - edge_margin
        x = max(left, min(anchor_bottom_right.x() - width, right - width + 1))
        y = max(
            available.top() + edge_margin,
            min(desired_y, bottom - height + 1),
        )
        self.setFixedSize(width, height)
        self.move(x, y)
        self.show()
        self.raise_()

    def _select_chat(self, conversation_id: int) -> None:
        self.hide()
        self.panel.select_chat(conversation_id)

    def _new_chat(self) -> None:
        self.hide()
        self.panel.new_chat()

    def _delete_chat(self, conversation_id: int, title: str) -> None:
        if self.panel.delete_chat(conversation_id, title):
            self.refresh()


class AIContextChip(QFrame):
    remove_requested = Signal(int)
    activated = Signal(int)

    def __init__(self, paper_id: int, label: str, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("aiContextChip")
        self.setToolTip(title)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 2, 0)
        layout.setSpacing(2)
        text = QPushButton(label)
        text.setObjectName("aiContextChipText")
        text.setCursor(Qt.CursorShape.PointingHandCursor)
        text.setToolTip(title)
        text.clicked.connect(
            lambda _checked=False, value=int(paper_id): self.activated.emit(value)
        )
        close = QPushButton()
        close.setObjectName("aiContextChipClose")
        close.setFixedSize(17, 17)
        set_widget_icon(close, "x", size=10, tooltip=f"Remove {title} from comparison")
        close.clicked.connect(
            lambda _checked=False, value=int(paper_id): self.remove_requested.emit(
                value
            )
        )
        layout.addWidget(text)
        layout.addWidget(close)


class AIChatPanel(QWidget):
    send_requested = Signal(int, str, str, str, str, object, object, object, bool)
    stop_requested = Signal(str)
    summarize_requested = Signal(str, str)
    summarize_cancel_requested = Signal()
    citation_requested = Signal(object)
    context_focus_requested = Signal()
    context_paper_remove_requested = Signal(int)
    context_paper_requested = Signal(int)
    group_conversation_requested = Signal(int)
    solo_conversation_requested = Signal(object)
    group_paper_requested = Signal(int, int)
    conversation_deleted = Signal(int)

    def __init__(
        self,
        paper_id: int,
        parent=None,
        *,
        project_id: int | None = None,
        repository: Any = AIRepository,
        settings_repository: Any = SettingsRepository,
        summary_repository: Any = SummaryRepository,
    ) -> None:
        super().__init__(parent)
        self.paper_id = int(paper_id)
        self.project_id = int(project_id) if project_id is not None else None
        self.repository = repository
        self.settings_repository = settings_repository
        self.summary_repository = summary_repository
        self._chat_font_size = self._load_chat_font_size()
        self._chat_zoom_wheel_remainder = 0
        self._chat_scroll_ratio = 0.0
        self._retry_payload: dict[str, Any] | None = None
        self.active_conversation_id: int | None = None
        self._draft_active = False
        self._selected_text = ""
        self._selected_page: int | None = None
        self._busy = False
        self._summary_busy = False
        self._thinking_row: QWidget | None = None
        self._streaming_message: MarkdownMessage | None = None
        self._streaming_markdown = ""
        self._stream_follow = True
        self._request_conversation_id: int | None = None
        self._request_provider = ""
        self._request_model = ""
        self._active_provider = "gemini"
        self._runtime_models: dict[str, str] = {}
        self._brief_open = False
        self._history_has_messages = False
        self._suggested_question_text: str | None = None
        self._suggested_response_language: str | None = None
        self._workspace_papers: list[dict[str, object]] = []
        self._paper_aliases: dict[int, str] = {}
        self._group_mode = False
        self._settings_refreshing = False
        self.history_popup = ChatHistoryPopup(self)
        self._stream_render_timer = QTimer(self)
        self._stream_render_timer.setSingleShot(True)
        self._stream_render_timer.setInterval(45)
        self._stream_render_timer.timeout.connect(self._flush_stream_render)
        self._font_scroll_restore_timer = QTimer(self)
        self._font_scroll_restore_timer.setSingleShot(True)
        self._font_scroll_restore_timer.setInterval(45)
        self._font_scroll_restore_timer.timeout.connect(
            self._restore_chat_scroll_position
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)

        self.context_strip = QFrame()
        self.context_strip.setObjectName("aiContextStrip")
        context_layout = QHBoxLayout(self.context_strip)
        context_layout.setContentsMargins(7, 3, 5, 3)
        context_layout.setSpacing(4)
        self.context_label = QLabel("Context: This paper")
        self.context_label.setObjectName("aiContextLabel")
        context_layout.addWidget(self.context_label)
        self.context_chips = QWidget()
        self.context_chips_layout = QHBoxLayout(self.context_chips)
        self.context_chips_layout.setContentsMargins(0, 0, 0, 0)
        self.context_chips_layout.setSpacing(3)
        context_layout.addWidget(self.context_chips)
        context_layout.addStretch(1)
        self.focus_current_button = QPushButton("Focus current")
        self.focus_current_button.setObjectName("aiContextAction")
        self.focus_current_button.clicked.connect(
            self.context_focus_requested.emit
        )
        self.focus_current_button.hide()
        context_layout.addWidget(self.focus_current_button)
        layout.addWidget(self.context_strip)

        # Keep the state-bearing provider control owned by this panel for its
        # entire lifetime. ReaderSidebar mirrors it into a sidebar-owned header
        # control instead of reparenting this widget between disposable Readers.
        self.provider = ProviderCombo(self)
        self.provider.setObjectName("aiHeaderProvider")
        self.provider.setFixedSize(116, 28)
        self.provider.setIconSize(QSize(19, 19))
        for label, provider in PROVIDERS:
            self.provider.addItem(provider_logo_icon(provider), label, provider)
        _configure_dropdown(self.provider, visible_rows=6)
        self.provider.view().setIconSize(QSize(18, 18))
        self.provider.view().setMinimumWidth(155)
        self.provider.hide()
        self.model = ChevronComboBox()
        self.model.setObjectName("aiModelCombo")
        self.model.setEditable(True)
        self.model.setLineEdit(ElidingLineEdit())
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.setMinimumWidth(0)
        self.model.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        _configure_dropdown(self.model, visible_rows=10)

        self.history_scroll = QScrollArea()
        self.history_scroll.setObjectName("aiHistory")
        self.history_scroll.setProperty("modernScroll", True)
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.history_content = QWidget()
        self.history_layout = QVBoxLayout(self.history_content)
        self.history_layout.setContentsMargins(2, 5, 5, 5)
        self.history_layout.setSpacing(15)
        self.history_layout.addStretch()
        self.history_scroll.setWidget(self.history_content)
        self._register_chat_zoom_widget(self.history_scroll.viewport())
        self._register_chat_zoom_widget(self.history_content)
        self.history_scroll.verticalScrollBar().valueChanged.connect(
            self._update_stream_follow
        )
        layout.addWidget(self.history_scroll, 1)
        self.empty_state = self._new_empty_state()
        self.history_layout.insertWidget(0, self.empty_state)

        self.composer_shell = QFrame()
        self.composer_shell.setObjectName("aiComposerShell")
        composer_layout = QVBoxLayout(self.composer_shell)
        composer_layout.setContentsMargins(8, 7, 7, 7)
        composer_layout.setSpacing(5)

        self.attachment = QFrame()
        self.attachment.setObjectName("aiAttachment")
        attachment_layout = QHBoxLayout(self.attachment)
        attachment_layout.setContentsMargins(8, 6, 4, 6)
        self.attachment_label = QLabel()
        self.attachment_label.setWordWrap(True)
        self.attachment_label.setMaximumHeight(56)
        self.attachment_label.setObjectName("aiAttachmentText")
        remove_attachment = QPushButton()
        remove_attachment.setObjectName("aiIconButton")
        set_widget_icon(
            remove_attachment,
            "x",
            size=14,
            tooltip="Remove selected text",
        )
        remove_attachment.clicked.connect(self.clear_attachment)
        attachment_layout.addWidget(self.attachment_label, 1)
        attachment_layout.addWidget(remove_attachment)
        self.attachment.hide()
        composer_layout.addWidget(self.attachment)

        self.composer = AIComposer()
        self.composer.setObjectName("aiComposer")
        self.composer.setPlaceholderText("Ask about this paper...")
        self.composer.submit_requested.connect(self._submit)
        composer_layout.addWidget(self.composer)

        footer = QHBoxLayout()
        footer.setContentsMargins(1, 0, 0, 0)
        footer.setSpacing(5)
        footer.addWidget(self.model, 1)
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
        self.send_button.clicked.connect(self._send_or_stop)
        footer.addWidget(self.send_button)
        composer_layout.addLayout(footer)

        self.summarize_button = QPushButton("Summarize Paper")
        self.summarize_button.setObjectName("aiSummarizeButton")
        set_widget_icon(
            self.summarize_button,
            "notebook-text",
            size=14,
        )
        self.summarize_button.clicked.connect(self._summarize_or_cancel)
        footer.insertWidget(0, self.summarize_button)
        layout.addWidget(self.composer_shell)

        self.status = QLabel()
        self.status.setObjectName("aiStatus")
        self.status.setWordWrap(True)
        self.status.hide()
        layout.addWidget(self.status)

        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.model.currentTextChanged.connect(self._model_changed)
        self.composer.textChanged.connect(self._composer_text_changed)
        self.refresh_settings()
        self.load_initial_chat()
        self._update_send_enabled()

    def _load_chat_font_size(self) -> float:
        getter = getattr(self.settings_repository, "get", None)
        if not callable(getter):
            return CHAT_FONT_DEFAULT_PX
        try:
            value = float(getter(CHAT_FONT_SETTING, str(CHAT_FONT_DEFAULT_PX)))
        except (sqlite3.Error, OSError, TypeError, ValueError):
            return CHAT_FONT_DEFAULT_PX
        if not CHAT_FONT_MIN_PX <= value <= CHAT_FONT_MAX_PX:
            return CHAT_FONT_DEFAULT_PX
        return value

    def _register_chat_zoom_widget(self, widget: QWidget) -> None:
        targets = [widget, *widget.findChildren(QWidget)]
        for target in targets:
            target.setProperty("aiChatWheelTarget", True)
            target.installEventFilter(self)
            viewport = getattr(target, "viewport", None)
            if callable(viewport):
                child_viewport = viewport()
                if isinstance(child_viewport, QWidget):
                    child_viewport.setProperty("aiChatWheelTarget", True)
                    child_viewport.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if (
            event.type() == QEvent.Type.Wheel
            and bool(watched.property("aiChatWheelTarget"))
            and bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        ):
            angle = int(event.angleDelta().y())
            delta = angle if angle else int(event.pixelDelta().y()) * 4
            self._chat_zoom_wheel_remainder += delta
            steps = int(self._chat_zoom_wheel_remainder / 120)
            if steps:
                self._chat_zoom_wheel_remainder -= steps * 120
                self._change_chat_font_size(steps * CHAT_FONT_STEP_PX)
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def _change_chat_font_size(self, delta: float) -> None:
        value = max(
            CHAT_FONT_MIN_PX,
            min(CHAT_FONT_MAX_PX, self._chat_font_size + float(delta)),
        )
        if abs(value - self._chat_font_size) < 0.01:
            return
        bar = self.history_scroll.verticalScrollBar()
        self._chat_scroll_ratio = (
            bar.value() / bar.maximum() if bar.maximum() > 0 else 0.0
        )
        self._chat_font_size = value
        for message in self.history_content.findChildren(MarkdownMessage):
            message.set_chat_font_size(value)
        for label in self.history_content.findChildren(QLabel):
            if label.objectName() in {
                "aiMessageText",
                "aiMessageQuote",
                "aiCitationPaperMap",
            }:
                self._apply_chat_label_font(label)
        self._persist_setting(CHAT_FONT_SETTING, f"{value:g}")
        self._font_scroll_restore_timer.start()

    def _apply_chat_label_font(self, label: QLabel) -> None:
        label.setStyleSheet(f"font-size: {self._chat_font_size:g}px;")

    def _restore_chat_scroll_position(self) -> None:
        bar = self.history_scroll.verticalScrollBar()
        bar.setValue(round(self._chat_scroll_ratio * bar.maximum()))

    def _new_empty_state(self) -> QWidget:
        if self._brief_open:
            sections = self._paper_brief_sections()
            if sections:
                brief = PaperBriefWidget(sections)
                brief.close_requested.connect(self._close_paper_brief)
                return brief
            self._brief_open = False
        start = PaperStartWidget(self._configured_output_language())
        start.set_group_mode(
            self._group_mode and len(self._workspace_papers) >= 2
        )
        start.suggestion_requested.connect(self._use_suggested_question)
        start.brief_requested.connect(self._open_paper_brief)
        return start

    def set_workspace_papers(
        self,
        papers: list[dict[str, object]],
        aliases: Mapping[int, str] | None = None,
    ) -> None:
        updated = [dict(paper) for paper in papers]
        updated_aliases = {
            int(paper_id): str(alias)
            for paper_id, alias in (aliases or {}).items()
        }
        if updated == self._workspace_papers and updated_aliases == self._paper_aliases:
            return
        self._workspace_papers = updated
        self._paper_aliases = updated_aliases
        if isinstance(self.empty_state, PaperStartWidget):
            self.empty_state.set_group_mode(
                self._group_mode and len(self._workspace_papers) >= 2
            )
        self._refresh_context_strip()

    def workspace_papers(self) -> list[dict[str, object]]:
        return [dict(paper) for paper in self._workspace_papers]

    def set_group_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._group_mode:
            self.summarize_button.setVisible(not enabled)
            return
        self._group_mode = enabled
        self.summarize_button.setVisible(not self._group_mode)
        if isinstance(self.empty_state, PaperStartWidget):
            self.empty_state.set_group_mode(
                self._group_mode and len(self._workspace_papers) >= 2
            )
        self._refresh_context_strip()

    def _refresh_context_strip(self) -> None:
        while self.context_chips_layout.count():
            item = self.context_chips_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not self._group_mode or len(self._workspace_papers) < 2:
            self.context_label.setText("Context: This paper")
            self.context_chips.hide()
            self.focus_current_button.hide()
            return
        self.context_label.setText(
            f"Comparing {len(self._workspace_papers)} papers"
        )
        for index, paper in enumerate(self._workspace_papers, start=1):
            paper_id = int(paper.get("id", 0))
            title = str(paper.get("title") or "Untitled paper")
            chip = AIContextChip(
                paper_id,
                self._paper_aliases.get(paper_id, f"P{index}"),
                title,
            )
            chip.remove_requested.connect(
                self.context_paper_remove_requested.emit
            )
            chip.activated.connect(self.context_paper_requested.emit)
            self.context_chips_layout.addWidget(chip)
        self.context_chips.show()
        self.focus_current_button.show()

    def uses_workspace_context(self, question: str) -> bool:
        if self._group_mode and len(self._workspace_papers) >= 2:
            return True
        if len(self._workspace_papers) < 2:
            return False
        normalized = " ".join(str(question or "").casefold().split())
        comparison_action = any(
            term in normalized
            for term in ("compare", "comparison", "contrast", "connect", "so sánh", "đối chiếu")
        )
        workspace_target = any(
            term in normalized
            for term in (
                "open papers",
                "the papers",
                "these papers",
                "both papers",
                "two papers",
                "các bài báo",
                "giữa các bài",
                "hai bài",
            )
        )
        paper_labels = sum(
            token.startswith("p") and token[1:].strip(".,:;()[]").isdigit()
            for token in normalized.split()
        )
        return comparison_action and (workspace_target or paper_labels >= 2)

    def _paper_brief_sections(self) -> list[tuple[str, str]]:
        row = self.summary_repository.get_for_paper(self.paper_id)
        if row is None:
            return []
        values = dict(row)
        return [
            (label, str(values.get(field) or "").strip())
            for field, label in BRIEF_FIELDS
            if _is_meaningful_summary_value(values.get(field))
        ]

    def _configured_output_language(self) -> str:
        try:
            value = self.settings_repository.get("translation_target", "vi")
        except (sqlite3.Error, OSError, ValueError):
            return "vi"
        return str(value or "vi").strip() or "vi"

    def _refresh_starter_language(self) -> None:
        output_language = self._configured_output_language()
        if isinstance(self.empty_state, PaperStartWidget):
            self.empty_state.set_output_language(output_language)

    def _use_suggested_question(self, question: str, language: str) -> None:
        if not self.composer.toPlainText().strip():
            self.composer.setPlainText(question)
            self._suggested_question_text = question
            self._suggested_response_language = str(language or "").strip() or None
            cursor = self.composer.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self.composer.setTextCursor(cursor)
        self.composer.setFocus(Qt.FocusReason.OtherFocusReason)

    def _composer_text_changed(self) -> None:
        if (
            self._suggested_question_text is not None
            and self.composer.toPlainText().strip() != self._suggested_question_text
        ):
            self._suggested_question_text = None
            self._suggested_response_language = None
        self._update_send_enabled()

    def _confirm_generate_summary(self) -> bool:
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Generate Paper Brief")
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setText("No paper summary is available yet.")
        dialog.setInformativeText("Generate one now using the existing Summarize Paper flow?")
        generate = dialog.addButton(
            "Generate Summary", QMessageBox.ButtonRole.AcceptRole
        )
        dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.exec()
        return dialog.clickedButton() is generate

    def _open_paper_brief(self) -> None:
        try:
            sections = self._paper_brief_sections()
        except (sqlite3.Error, OSError, ValueError) as error:
            self.show_error(f"Could not load the saved Summary: {error}")
            return
        if sections:
            self._brief_open = True
            self.reload_history()
            return
        if self._confirm_generate_summary():
            self._summarize_or_cancel()

    def _close_paper_brief(self) -> None:
        self._brief_open = False
        self.reload_history()
        self.composer.setFocus(Qt.FocusReason.OtherFocusReason)

    def current_provider(self) -> str:
        return str(self.provider.currentData() or "gemini")

    def current_model(self) -> str:
        return self.model.currentText().strip()

    def refresh_settings(self, *, preserve_selection: bool = False) -> None:
        self._settings_refreshing = True
        self._refresh_starter_language()
        try:
            custom_name = str(
                load_custom_api_config(self.settings_repository)["name"]
                or "Custom API"
            ).strip()
            PROVIDER_LABELS["custom"] = custom_name
            custom_index = self.provider.findData("custom")
            if custom_index >= 0:
                self.provider.setItemText(custom_index, custom_name)
            if preserve_selection:
                provider = self.current_provider()
                self._runtime_models[provider] = self.current_model()
            else:
                provider = (
                    self.settings_repository.get("ai_provider", "gemini")
                    or "gemini"
                )
            blocker = QSignalBlocker(self.provider)
            index = self.provider.findData(provider)
            self.provider.setCurrentIndex(max(0, index))
            del blocker
            self._active_provider = self.current_provider()
            self.provider.setToolTip(
                f"Provider: {PROVIDER_LABELS.get(self._active_provider, self._active_provider)}"
            )
            self._load_provider_models(self._active_provider)
        finally:
            self._settings_refreshing = False

    def _provider_changed(self) -> None:
        previous_model = self.current_model()
        self._runtime_models[self._active_provider] = previous_model
        if previous_model:
            self._persist_setting(
                f"ai_model_{self._active_provider}", previous_model
            )
        self._active_provider = self.current_provider()
        self._persist_setting("ai_provider", self._active_provider)
        self.provider.setToolTip(
            f"Provider: {PROVIDER_LABELS.get(self._active_provider, self._active_provider)}"
        )
        self._load_provider_models(self._active_provider)

    def _load_provider_models(self, provider: str) -> None:
        try:
            raw = self.settings_repository.get_json(f"ai_models_{provider}", [])
        except (TypeError, ValueError):
            raw = []
        options = (
            list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
            if isinstance(raw, list)
            else []
        )
        saved = self.settings_repository.get(f"ai_model_{provider}", "") or ""
        preferred = self._runtime_models.get(provider, saved)
        if options and preferred not in options:
            preferred = options[0]
        blocker = QSignalBlocker(self.model)
        self.model.clear()
        self.model.addItems(options)
        self.model.setEditText(preferred)
        del blocker
        self.model.setToolTip(preferred)

    def _model_changed(self, model: str) -> None:
        value = str(model or "").strip()
        self.model.setToolTip(value)
        if not value:
            return
        provider = self.current_provider()
        self._runtime_models[provider] = value
        self._persist_setting(f"ai_model_{provider}", value)

    def _persist_setting(self, key: str, value: str) -> None:
        if self._settings_refreshing:
            return
        setter = getattr(self.settings_repository, "set", None)
        if not callable(setter):
            return
        try:
            setter(key, value)
        except (sqlite3.Error, OSError, ValueError):
            pass

    def load_initial_chat(self) -> None:
        conversations = [
            row
            for row in self._list_conversations()
            if row.get("conversation_type", "solo") == "solo"
        ]
        if conversations:
            self.active_conversation_id = int(conversations[0]["id"])
            self._draft_active = False
        else:
            self.active_conversation_id = None
            self._draft_active = True
        self.reload_history()

    def available_conversations(
        self, *, for_history: bool = False
    ) -> list[dict[str, Any]]:
        conversations = self._list_conversations()
        if self._group_mode:
            return [
                row
                for row in conversations
                if row.get("conversation_type") == "group"
            ]
        return conversations if for_history else [
            row
            for row in conversations
            if row.get("conversation_type", "solo") == "solo"
        ]

    def _list_conversations(self) -> list[dict[str, Any]]:
        try:
            return self.repository.list_conversations(
                self.paper_id, project_id=self.project_id
            )
        except TypeError:
            return self.repository.list_conversations(self.paper_id)

    def load_group_chat(self, conversation: Mapping[str, Any]) -> None:
        members = [dict(member) for member in conversation.get("members", [])]
        if len(members) < 2:
            raise ValueError("The comparison chat no longer has two papers.")
        self.paper_id = int(members[0]["id"])
        aliases = {
            int(member["id"]): f"P{int(member['alias_index'])}"
            for member in members
        }
        self.set_group_mode(True)
        self.set_workspace_papers(members, aliases)
        self.active_conversation_id = int(conversation["id"])
        self._draft_active = False
        self._brief_open = False
        self._retry_payload = None
        self.composer.clear()
        self.clear_attachment()
        self.reload_history()

    def show_history_popup(self, anchor: QWidget) -> None:
        self.history_popup.show_below(anchor)

    def select_chat(self, conversation_id: int) -> None:
        try:
            conversation = self.repository.get_conversation(
                conversation_id,
                self.paper_id,
                project_id=self.project_id,
            )
        except TypeError:
            conversation = self.repository.get_conversation(
                conversation_id, self.paper_id
            )
        if conversation is None:
            return
        if conversation.get("conversation_type") == "group":
            self.group_conversation_requested.emit(int(conversation["id"]))
            return
        self.active_conversation_id = int(conversation["id"])
        self._draft_active = False
        self._brief_open = False
        self._retry_payload = None
        self.composer.clear()
        self.clear_attachment()
        self.reload_history()
        self.solo_conversation_requested.emit(self.active_conversation_id)

    def new_chat(self, *, notify: bool = True) -> None:
        if self._group_mode and notify:
            self.solo_conversation_requested.emit(None)
            return
        self.active_conversation_id = None
        self._draft_active = True
        self._brief_open = False
        self._retry_payload = None
        self.composer.clear()
        self.clear_attachment()
        self.reload_history()
        self.composer.setFocus(Qt.FocusReason.OtherFocusReason)
        if notify:
            self.solo_conversation_requested.emit(None)

    def delete_chat(self, conversation_id: int, title: str) -> bool:
        if self._busy and conversation_id == self._request_conversation_id:
            self.show_error("Stop the active response before deleting this chat.")
            return False
        answer = QMessageBox.question(
            self,
            "Delete chat",
            f'Delete "{title}" and its complete local history?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self.repository.delete_conversation(
            conversation_id,
            self.paper_id,
        )
        self.conversation_deleted.emit(int(conversation_id))
        if self.active_conversation_id == conversation_id:
            conversations = self.available_conversations()
            self.active_conversation_id = (
                int(conversations[0]["id"]) if conversations else None
            )
            self._draft_active = not bool(conversations)
            self._brief_open = False
            self.composer.clear()
            self.clear_attachment()
            self.reload_history()
            self.solo_conversation_requested.emit(self.active_conversation_id)
        return True

    def _clear_history_widgets(self) -> None:
        while self.history_layout.count() > 1:
            item = self.history_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._thinking_row = None
        self._streaming_message = None

    def reload_history(self) -> None:
        self._clear_history_widgets()
        rows = (
            self.repository.list_messages(self.active_conversation_id)
            if self.active_conversation_id is not None
            else []
        )
        self._history_has_messages = bool(rows)
        if rows:
            self._brief_open = False
        self.empty_state = self._new_empty_state()
        self.history_layout.insertWidget(0, self.empty_state)
        self._update_empty_state_visibility()
        for row in rows:
            self._append_message(row)
        if self._busy and self.active_conversation_id == self._request_conversation_id:
            if self._streaming_markdown:
                self._streaming_message = self._append_assistant_message(
                    self._request_provider,
                    self._request_model,
                    "",
                )
                self._flush_stream_render()
            else:
                self._show_thinking()
        QTimer.singleShot(0, self, self._scroll_to_bottom)

    def _provider_logo_label(self, provider: str | None = None) -> QLabel:
        name = provider or self.current_provider()
        label = QLabel()
        label.setObjectName("aiProviderIcon")
        label.setPixmap(provider_logo_pixmap(name, 19))
        label.setFixedSize(24, 24)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label

    def _append_message(self, raw: Mapping[str, Any]) -> None:
        role = str(raw.get("role") or "assistant")
        self._history_has_messages = True
        self.empty_state.hide()
        row = QWidget()
        row.setObjectName("aiMessageRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(7)

        if role == "user":
            row_layout.addStretch(1)
            bubble = QFrame()
            bubble.setObjectName("aiUserBubble")
            bubble.setMaximumWidth(330)
            bubble.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
            bubble_layout = QVBoxLayout(bubble)
            bubble_layout.setContentsMargins(10, 7, 10, 7)
            bubble_layout.setSpacing(5)
            selected = str(raw.get("selected_text") or "").strip()
            if selected:
                quote = QLabel(selected)
                quote.setObjectName("aiMessageQuote")
                self._apply_chat_label_font(quote)
                quote.setWordWrap(True)
                page = raw.get("selected_page")
                quote.setToolTip(
                    f"Attached selection{f' · page {page}' if page else ''}"
                )
                bubble_layout.addWidget(quote)
            content = QLabel(str(raw.get("content") or ""))
            content.setObjectName("aiMessageText")
            self._apply_chat_label_font(content)
            content.setWordWrap(True)
            content.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            bubble_layout.addWidget(content)
            row_layout.addWidget(bubble, 0, Qt.AlignmentFlag.AlignRight)
        else:
            provider = str(raw.get("provider") or self.current_provider())
            model = str(raw.get("model") or "").strip()
            row.deleteLater()
            self._append_assistant_message(
                provider,
                model,
                str(raw.get("content") or ""),
                raw.get("citations"),
                metadata_valid=bool(raw.get("metadata_valid", True)),
            )
            return
        self.history_layout.insertWidget(self.history_layout.count() - 1, row)
        self._register_chat_zoom_widget(row)

    def _append_assistant_message(
        self,
        provider: str,
        model: str,
        content: str,
        citations: object = None,
        *,
        metadata_valid: bool = True,
    ) -> MarkdownMessage:
        row = QWidget()
        row.setObjectName("aiMessageRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        body = QWidget()
        body.setObjectName("aiAssistantBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(5)
        header_layout.addWidget(self._provider_logo_label(provider))
        provider_name = QLabel(PROVIDER_LABELS.get(provider, provider.title()))
        provider_name.setObjectName("aiProviderName")
        header_layout.addWidget(provider_name)
        if model:
            model_name = QLabel(model)
            model_name.setObjectName("aiProviderModel")
            model_name.setToolTip(model)
            header_layout.addWidget(model_name)
        header_layout.addStretch()
        body_layout.addLayout(header_layout)
        message = MarkdownMessage(content, font_size=self._chat_font_size)
        body_layout.addWidget(message)
        message._citation_layout = body_layout
        message.citation_requested.connect(self.citation_requested)
        self._render_citations(
            message,
            body_layout,
            citations,
            metadata_valid=metadata_valid,
        )
        row_layout.addWidget(body, 1)
        self.history_layout.insertWidget(self.history_layout.count() - 1, row)
        self._register_chat_zoom_widget(row)
        return message

    def _render_citations(
        self,
        message: MarkdownMessage,
        layout: QVBoxLayout,
        citations: object,
        *,
        metadata_valid: bool = True,
    ) -> None:
        values = list(citations) if isinstance(citations, (list, tuple)) else []
        paper_labels = dict(self._paper_aliases)
        message.set_citations(values, paper_labels)
        cited_ids: list[int] = []
        for citation in values:
            raw_id = (
                citation.get("paper_id", 0)
                if isinstance(citation, Mapping)
                else getattr(citation, "paper_id", 0)
            )
            try:
                paper_id = int(raw_id or 0)
            except (TypeError, ValueError):
                continue
            if paper_id in paper_labels and paper_id not in cited_ids:
                cited_ids.append(paper_id)
        if len(self._workspace_papers) >= 2 and cited_ids:
            titles = {
                int(paper.get("id", 0)): str(
                    paper.get("title") or "Untitled paper"
                )
                for paper in self._workspace_papers
            }
            legend = QLabel(
                "  ·  ".join(
                    f"{paper_labels[paper_id]}: "
                    f"{titles.get(paper_id, 'Untitled paper')}"
                    for paper_id in cited_ids
                )
            )
            legend.setObjectName("aiCitationPaperMap")
            legend.setWordWrap(True)
            self._apply_chat_label_font(legend)
            layout.addWidget(legend)
            self._register_chat_zoom_widget(legend)

    def _show_thinking(self) -> None:
        if self._thinking_row is not None:
            return
        if self.active_conversation_id != self._request_conversation_id:
            return
        self.empty_state.hide()
        row = QWidget()
        row.setObjectName("aiThinkingRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(7)
        row_layout.addWidget(
            self._provider_logo_label(self._request_provider),
            0,
            Qt.AlignmentFlag.AlignTop,
        )
        label = QLabel(
            f"{PROVIDER_LABELS.get(self._request_provider, 'AI')} · Thinking…"
        )
        label.setObjectName("aiThinkingText")
        row_layout.addWidget(label, 1)
        self.history_layout.insertWidget(self.history_layout.count() - 1, row)
        self._thinking_row = row
        QTimer.singleShot(0, self, self._scroll_to_bottom)

    def _remove_thinking(self) -> None:
        if self._thinking_row is None:
            return
        row = self._thinking_row
        self._thinking_row = None
        self.history_layout.removeWidget(row)
        row.deleteLater()

    def append_stream_chunk(
        self,
        conversation_id: int,
        provider: str,
        model: str,
        chunk: str,
    ) -> None:
        if (
            not self._busy
            or conversation_id != self._request_conversation_id
            or not chunk
        ):
            return
        self._streaming_markdown += chunk
        if self.active_conversation_id != conversation_id:
            return
        if self._streaming_message is None:
            self._stream_follow = self._is_near_history_bottom()
            self._remove_thinking()
            self._streaming_message = self._append_assistant_message(
                provider,
                model,
                "",
            )
        if not self._stream_render_timer.isActive():
            self._stream_render_timer.start()

    def _flush_stream_render(self, *, final: bool = False) -> None:
        if self._streaming_message is None:
            return
        follow = self._stream_follow or self._is_near_history_bottom()
        visible = self._streaming_markdown.split("<!--RA_RESULT", 1)[0].rstrip()
        self._streaming_message.update_markdown(visible, streaming=not final)
        if follow:
            QTimer.singleShot(0, self, self._scroll_to_bottom)

    def _is_near_history_bottom(self) -> bool:
        bar = self.history_scroll.verticalScrollBar()
        return bar.maximum() - bar.value() <= 72

    def _update_stream_follow(self, _value: int) -> None:
        if self._busy and self._streaming_message is not None:
            self._stream_follow = self._is_near_history_bottom()

    def streaming_text(self) -> str:
        return self._streaming_markdown.split("<!--RA_RESULT", 1)[0].rstrip()

    def _finalize_stream_widget(self, final_text: str | None = None) -> None:
        if self._stream_render_timer.isActive():
            self._stream_render_timer.stop()
        if final_text is not None and self._streaming_message is not None:
            self._streaming_markdown = final_text
        self._flush_stream_render(final=True)
        self._streaming_message = None
        self._streaming_markdown = ""
        self._stream_follow = True

    def _append_error(
        self,
        message: str,
        *,
        scroll: bool = True,
        retryable: bool = False,
    ) -> None:
        row = QWidget()
        row.setObjectName("aiErrorRow")
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(31, 0, 0, 0)
        row_layout.setSpacing(4)
        label = QLabel(message)
        label.setObjectName("aiInlineError")
        label.setWordWrap(True)
        row_layout.addWidget(label)
        if retryable and self._retry_payload is not None:
            retry = QPushButton("Retry")
            retry.setObjectName("aiInlineRetry")
            retry.setCursor(Qt.CursorShape.PointingHandCursor)
            set_widget_icon(retry, "refresh-cw", size=12)
            retry.clicked.connect(
                lambda _checked=False, failed_row=row: self._retry_failed_request(
                    failed_row
                )
            )
            row_layout.addWidget(retry, 0, Qt.AlignmentFlag.AlignLeft)
        self.history_layout.insertWidget(self.history_layout.count() - 1, row)
        if scroll:
            QTimer.singleShot(0, self, self._scroll_to_bottom)

    def _retry_failed_request(self, error_row: QWidget) -> None:
        payload = self._retry_payload
        if self._busy or payload is None:
            return
        conversation_id = int(payload["conversation_id"])
        if self.active_conversation_id != conversation_id:
            return
        provider = self.current_provider()
        model = self.current_model()
        if not model:
            self.show_error("Choose or enter a model in the AI sidebar.")
            return
        self.history_layout.removeWidget(error_row)
        error_row.deleteLater()
        failed_message = payload.get("partial_widget")
        if isinstance(failed_message, MarkdownMessage):
            message_row = failed_message.parentWidget()
            while message_row is not None and message_row.objectName() != "aiMessageRow":
                message_row = message_row.parentWidget()
            if message_row is not None:
                self.history_layout.removeWidget(message_row)
                message_row.deleteLater()
        payload["partial_widget"] = None
        self.status.hide()
        self.set_busy(
            True,
            conversation_id=conversation_id,
            provider=provider,
            model=model,
        )
        self.send_requested.emit(
            conversation_id,
            provider,
            model,
            str(payload["question"]),
            str(payload.get("selected_text") or ""),
            payload.get("selected_page"),
            payload.get("response_language"),
            dict(payload.get("request_context") or {}),
            True,
        )

    def attach_selection(self, payload: Mapping[str, Any]) -> None:
        self._selected_text = str(payload.get("selectedText") or "").strip()
        location = payload.get("location")
        self._selected_page = None
        if isinstance(location, Mapping):
            segments = location.get("segments")
            if isinstance(segments, list) and segments and isinstance(segments[0], Mapping):
                try:
                    self._selected_page = int(segments[0].get("page"))
                except (TypeError, ValueError):
                    pass
        preview = " ".join(self._selected_text.split())
        if len(preview) > 220:
            preview = preview[:217].rstrip() + "…"
        prefix = (
            f"Selected text · page {self._selected_page}\n"
            if self._selected_page
            else "Selected text\n"
        )
        self.attachment_label.setText(prefix + preview)
        self.attachment.setVisible(bool(self._selected_text))
        self.composer.setFocus(Qt.FocusReason.OtherFocusReason)

    def clear_attachment(self) -> None:
        self._selected_text = ""
        self._selected_page = None
        self.attachment.hide()

    def _send_or_stop(self) -> None:
        if self._busy:
            partial_text = self.cancel_request()
            self.stop_requested.emit(partial_text)
            return
        self._submit()

    def _summarize_or_cancel(self) -> None:
        if self._summary_busy:
            self.summarize_cancel_requested.emit()
            return
        model = self.current_model()
        if not model:
            self.show_error("Choose or enter a model in the AI sidebar.")
            return
        self.set_summary_busy(True)
        self.summarize_requested.emit(self.current_provider(), model)

    def set_summary_busy(self, busy: bool, error: str | None = None) -> None:
        self._summary_busy = busy
        self._update_empty_state_visibility()
        self.summarize_button.setText("Cancel Summary" if busy else "Summarize Paper")
        set_widget_icon(
            self.summarize_button,
            "square" if busy else "notebook-text",
            size=12 if busy else 14,
        )
        if busy:
            self.status.setProperty("error", False)
            self.status.setText("Summarizing the original English PDF…")
            self.status.show()
        elif error:
            self.show_error(error)
        else:
            self.status.hide()
        self._update_send_enabled()

    def _submit(self) -> None:
        if self._busy:
            return
        question = self.composer.toPlainText().strip()
        model = self.current_model()
        if not question:
            return
        if not model:
            self.show_error("Choose or enter a model in the AI sidebar.")
            return

        if self.active_conversation_id is None:
            if self._group_mode:
                paper_ids = [int(paper["id"]) for paper in self._workspace_papers]
                try:
                    conversation = self.repository.create_group_conversation(
                        paper_ids,
                        _chat_title(question),
                        project_id=self.project_id,
                    )
                except TypeError:
                    conversation = self.repository.create_group_conversation(
                        paper_ids, _chat_title(question)
                    )
            else:
                try:
                    conversation = self.repository.create_conversation(
                        self.paper_id,
                        _chat_title(question),
                        project_id=self.project_id,
                    )
                except TypeError:
                    conversation = self.repository.create_conversation(
                        self.paper_id, _chat_title(question)
                    )
            self.active_conversation_id = int(conversation["id"])
            self._draft_active = False
            if self._group_mode:
                self.group_conversation_requested.emit(
                    self.active_conversation_id
                )
            else:
                self.solo_conversation_requested.emit(
                    self.active_conversation_id
                )
        conversation_id = self.active_conversation_id
        provider = self.current_provider()
        selected_text = self._selected_text
        selected_page = self._selected_page
        response_language = (
            self._suggested_response_language
            if question == self._suggested_question_text
            else None
        )
        self.status.hide()
        self._append_message(
            {
                "role": "user",
                "content": question,
                "selected_text": selected_text,
                "selected_page": selected_page,
            }
        )
        self.composer.clear()
        self._suggested_question_text = None
        self._suggested_response_language = None
        self.clear_attachment()
        self.set_busy(
            True,
            conversation_id=conversation_id,
            provider=provider,
            model=model,
        )
        QTimer.singleShot(0, self, self._scroll_to_bottom)
        use_workspace_context = self.uses_workspace_context(question)
        request_context = {
            "use_workspace_context": use_workspace_context,
            "workspace_papers": (
                self.workspace_papers() if use_workspace_context else []
            ),
            "partial_message_id": None,
        }
        self._retry_payload = {
            "conversation_id": conversation_id,
            "question": question,
            "selected_text": selected_text,
            "selected_page": selected_page,
            "response_language": response_language,
            "request_context": request_context,
            "partial_widget": None,
        }
        self.send_requested.emit(
            conversation_id,
            provider,
            model,
            question,
            selected_text,
            selected_page,
            response_language,
            dict(request_context),
            False,
        )

    def _update_send_enabled(self) -> None:
        self.send_button.setEnabled(
            not self._summary_busy
            and (self._busy or bool(self.composer.toPlainText().strip()))
        )

    def _update_empty_state_visibility(self) -> None:
        if hasattr(self, "empty_state"):
            self.empty_state.setVisible(
                not self._history_has_messages
                and not self._busy
                and not self._summary_busy
            )

    def set_busy(
        self,
        busy: bool,
        _message: str = "",
        *,
        conversation_id: int | None = None,
        provider: str = "",
        model: str = "",
    ) -> None:
        self._busy = busy
        self.summarize_button.setEnabled(not busy)
        if busy:
            self._request_conversation_id = conversation_id
            self._request_provider = provider or self.current_provider()
            self._request_model = model or self.current_model()
            self._streaming_message = None
            self._streaming_markdown = ""
            self._stream_follow = True
            set_widget_icon(
                self.send_button,
                "square",
                size=13,
                tooltip="Stop generating",
                color="#FFFFFF",
                active_color="#FFFFFF",
            )
            self.send_button.setAccessibleName("Stop generating")
            self._show_thinking()
        else:
            set_widget_icon(
                self.send_button,
                "arrow-up",
                size=15,
                tooltip="Send",
                color="#FFFFFF",
                active_color="#FFFFFF",
            )
            self.send_button.setAccessibleName("Send")
            self._remove_thinking()
            self._request_conversation_id = None
            self._request_provider = ""
            self._request_model = ""
        self._update_empty_state_visibility()
        self._update_send_enabled()

    def cancel_request(self) -> str:
        if not self._busy:
            return ""
        partial_text = self.streaming_text()
        self._finalize_stream_widget()
        self.set_busy(False)
        self._retry_payload = None
        return partial_text

    def show_error(self, message: str) -> None:
        self.status.setText(message)
        self.status.setProperty("error", True)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.status.show()

    def finish_request(
        self,
        result: object | None = None,
        *,
        error: str | None = None,
        conversation_id: int | None = None,
        retryable: bool = False,
        partial_message_id: int | None = None,
    ) -> None:
        result_conversation_id = conversation_id
        if result is not None:
            try:
                result_conversation_id = int(getattr(result, "conversation_id"))
            except (TypeError, ValueError):
                result_conversation_id = self._request_conversation_id
        if (
            result_conversation_id is not None
            and self.active_conversation_id != result_conversation_id
        ):
            if self._stream_render_timer.isActive():
                self._stream_render_timer.stop()
            self._streaming_message = None
            self._streaming_markdown = ""
            self.set_busy(False)
            return
        follow = (
            self._streaming_message is None
            or self._stream_follow
            or self._is_near_history_bottom()
        )
        reply = str(getattr(result, "reply", "") or "")
        message = self._streaming_message
        if reply:
            if self._streaming_message is None:
                self._streaming_message = self._append_assistant_message(
                    str(getattr(result, "provider", "") or self.current_provider()),
                    str(getattr(result, "model", "") or self.current_model()),
                    "",
                )
                message = self._streaming_message
            self._finalize_stream_widget(reply)
        else:
            self._finalize_stream_widget()
        self.set_busy(False)
        if result is not None and message is not None:
            self._render_citations(
                message,
                message._citation_layout,
                getattr(result, "citations", ()),
                metadata_valid=bool(getattr(result, "metadata_valid", True)),
            )
        if error:
            if retryable and self._retry_payload is not None:
                request_context = self._retry_payload.get("request_context")
                if isinstance(request_context, dict):
                    request_context["partial_message_id"] = partial_message_id
                self._retry_payload["partial_widget"] = message
            self._append_error(
                error,
                scroll=follow,
                retryable=retryable,
            )
            if not retryable:
                self._retry_payload = None
            return
        self._retry_payload = None
        if follow:
            QTimer.singleShot(0, self, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        bar = self.history_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
