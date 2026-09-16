from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.icons import app_icon, set_widget_icon
from app.ui.ui_styles import MODERN_SCROLLBAR_QSS, apply_ui_palette


PAPER_ROLE = int(Qt.ItemDataRole.UserRole)
KIND_ROLE = PAPER_ROLE + 1
CURRENT_ROLE = PAPER_ROLE + 2
OPEN_ROLE = PAPER_ROLE + 3
DRAWER_OPEN_DURATION_MS = 210
DRAWER_CLOSE_DURATION_MS = 175
DRAWER_WIDTH = 304
DRAWER_LEFT_MARGIN = 10
DRAWER_VERTICAL_MARGIN = 11
EDGE_HIT_WIDTH = 10
EDGE_INDICATOR_WIDTH = 3
EDGE_INDICATOR_HEIGHT = 64


def quick_drawer_width(body_width: int) -> int:
    return min(DRAWER_WIDTH, int(max(0, body_width) * 0.40))


def _paper_value(paper: Any, key: str, default: object = "") -> object:
    try:
        value = paper[key]
    except (KeyError, TypeError, IndexError):
        if isinstance(paper, Mapping):
            value = paper.get(key, default)
        else:
            value = default
    return default if value is None else value


def _normalized_paper(paper: Any) -> dict[str, object]:
    return {
        "id": int(_paper_value(paper, "id", 0)),
        "title": str(_paper_value(paper, "title", "Untitled paper")).strip()
        or "Untitled paper",
        "authors": str(_paper_value(paper, "authors", "")).strip(),
        "year": _paper_value(paper, "year", ""),
        "status": str(_paper_value(paper, "status", "Unread")).strip()
        or "Unread",
    }


class EdgeActivationZone(QFrame):
    pointer_entered = Signal()
    pointer_left = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("quickPaperHotspot")
        self.setMouseTracking(True)
        self.indicator = QFrame(self)
        self.indicator.setObjectName("quickPaperEdgeIndicator")
        self.indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.set_active(False)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        indicator_height = min(EDGE_INDICATOR_HEIGHT, max(0, self.height() - 20))
        self.indicator.setGeometry(
            1,
            max(0, (self.height() - indicator_height) // 2),
            EDGE_INDICATOR_WIDTH,
            indicator_height,
        )

    def enterEvent(self, event: Any) -> None:
        super().enterEvent(event)
        self.setProperty("active", True)
        self.style().unpolish(self)
        self.style().polish(self)
        self.pointer_entered.emit()

    def leaveEvent(self, event: Any) -> None:
        super().leaveEvent(event)
        self.pointer_left.emit()

    def set_active(self, active: bool) -> None:
        self.setProperty("active", active)
        color = "#7F9CC8" if active else "#BCC9D9"
        self.indicator.setStyleSheet(
            f"background: {color}; border: none; border-radius: 2px;"
        )


class PaperItemDelegate(QStyledItemDelegate):
    def sizeHint(self, option: QStyleOptionViewItem, index: Any) -> QSize:
        kind = index.data(KIND_ROLE)
        if kind == "section":
            return QSize(100, 28)
        if kind == "empty":
            return QSize(100, 44)
        return QSize(100, 70)

    @staticmethod
    def _title_lines(text: str, metrics: QFontMetrics, width: int) -> list[str]:
        words = text.split()
        if not words:
            return ["Untitled paper"]
        lines: list[str] = []
        current = ""
        while words and len(lines) < 2:
            word = words.pop(0)
            candidate = f"{current} {word}".strip()
            if current and metrics.horizontalAdvance(candidate) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current and len(lines) < 2:
            lines.append(current)
        if words:
            remainder = " ".join([lines[-1], *words])
            lines[-1] = metrics.elidedText(
                remainder, Qt.TextElideMode.ElideRight, width
            )
        elif lines:
            lines[-1] = metrics.elidedText(
                lines[-1], Qt.TextElideMode.ElideRight, width
            )
        return lines[:2]

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: Any,
    ) -> None:
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = option.rect.adjusted(4, 2, -4, -2)
            kind = index.data(KIND_ROLE)
            if kind == "section":
                font = QFont(option.font)
                font.setPointSizeF(max(8.0, font.pointSizeF() - 1.5))
                font.setWeight(QFont.Weight.DemiBold)
                painter.setFont(font)
                painter.setPen(QColor("#8A94A3"))
                painter.drawText(
                    rect.adjusted(8, 5, 0, 0),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    str(index.data(Qt.ItemDataRole.DisplayRole)).upper(),
                )
                return
            if kind == "empty":
                painter.setPen(QColor("#8A94A3"))
                painter.drawText(
                    rect.adjusted(8, 0, -8, 0),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    str(index.data(Qt.ItemDataRole.DisplayRole)),
                )
                return

            current = bool(index.data(CURRENT_ROLE))
            hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
            if current:
                painter.setBrush(QColor("#EEF4FF"))
            elif hovered:
                painter.setBrush(QColor("#F3F6FA"))
            else:
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 9, 9)

            paper = index.data(PAPER_ROLE) or {}
            is_open = bool(index.data(OPEN_ROLE))
            left = rect.left() + (28 if is_open else 11)
            if is_open:
                center_x = rect.left() + 14
                center_y = rect.top() + 18
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#4F7DF3" if current else "#A9B5C5"))
                painter.drawEllipse(center_x - 4, center_y - 4, 8, 8)

            title_font = QFont(option.font)
            title_font.setPointSizeF(max(9.0, title_font.pointSizeF()))
            title_font.setWeight(
                QFont.Weight.DemiBold if current else QFont.Weight.Medium
            )
            painter.setFont(title_font)
            painter.setPen(QColor("#243047" if current else "#303844"))
            metrics = QFontMetrics(title_font)
            available = max(40, rect.right() - left - 10)
            lines = self._title_lines(str(paper.get("title", "")), metrics, available)
            line_y = rect.top() + 8 + metrics.ascent()
            for line in lines:
                painter.drawText(left, line_y, line)
                line_y += metrics.height()

            year = str(paper.get("year") or "").strip()
            status = str(paper.get("status") or "").strip()
            metadata = " \u00b7 ".join(value for value in (year, status) if value)
            if current:
                metadata = "Current" + (f" \u00b7 {metadata}" if metadata else "")
            meta_font = QFont(option.font)
            meta_font.setPointSizeF(max(8.0, meta_font.pointSizeF() - 1.0))
            painter.setFont(meta_font)
            painter.setPen(QColor("#637083"))
            meta_metrics = QFontMetrics(meta_font)
            metadata = meta_metrics.elidedText(
                metadata or "Paper", Qt.TextElideMode.ElideRight, available
            )
            painter.drawText(left, rect.bottom() - 9, metadata)
        finally:
            painter.restore()


class QuickPaperSwitcher(QFrame):
    paper_selected = Signal(int)
    close_requested = Signal()
    pointer_entered = Signal()
    pointer_left = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("quickPaperDrawer")
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setOffset(2, 2)
        shadow.setColor(QColor(35, 50, 70, 28))
        self.setGraphicsEffect(shadow)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 13, 12, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setContentsMargins(2, 0, 0, 0)
        title = QLabel("Papers")
        title.setObjectName("quickPaperTitle")
        header.addWidget(title)
        header.addStretch(1)
        close_button = QPushButton()
        close_button.setObjectName("quickPaperClose")
        close_button.setFixedSize(28, 28)
        set_widget_icon(close_button, "x", size=14, tooltip="Close paper switcher")
        close_button.clicked.connect(self.close_requested.emit)
        header.addWidget(close_button)
        layout.addLayout(header)

        self.search = QLineEdit()
        self.search.setObjectName("quickPaperSearch")
        self.search.setPlaceholderText("Search papers...")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(
            app_icon("search"), QLineEdit.ActionPosition.LeadingPosition
        )
        self.search.textChanged.connect(self._render)
        layout.addWidget(self.search)

        self.paper_list = QListWidget()
        self.paper_list.setObjectName("quickPaperList")
        self.paper_list.setProperty("modernScroll", True)
        self.paper_list.setItemDelegate(PaperItemDelegate(self.paper_list))
        self.paper_list.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.paper_list.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.paper_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.paper_list.setMouseTracking(True)
        self.paper_list.itemClicked.connect(self._activate_item)
        layout.addWidget(self.paper_list, 1)

        self._papers: list[dict[str, object]] = []
        self._open_ids: set[int] = set()
        self._current_id = 0
        self.setStyleSheet(
            apply_ui_palette(
                """
                #quickPaperDrawer {
                    background: #FBFCFE;
                    border: 1px solid @BORDER@;
                    border-radius: 16px;
                }
                #quickPaperTitle {
                    color: @TEXT@;
                    font-size: 15px;
                    font-weight: 650;
                }
                #quickPaperClose {
                    border: none;
                    border-radius: 6px;
                    background: transparent;
                    padding: 0;
                }
                #quickPaperClose:hover { background: @SURFACE_SUBTLE@; }
                #quickPaperSearch {
                    min-height: 31px;
                    border: 1px solid @BORDER@;
                    border-radius: 9px;
                    background: @SURFACE@;
                    color: @TEXT@;
                    padding: 0 8px;
                    selection-background-color: @PRIMARY@;
                }
                #quickPaperSearch:focus { border-color: #9CB7F8; }
                #quickPaperList {
                    border: none;
                    background: transparent;
                    outline: none;
                }
                """
                + MODERN_SCROLLBAR_QSS
            )
        )
        for child in self.findChildren(QWidget):
            child.installEventFilter(self)

    def eventFilter(self, watched: Any, event: Any) -> bool:
        if event.type() == QEvent.Type.Enter:
            self.pointer_entered.emit()
        elif event.type() == QEvent.Type.Leave:
            self.pointer_left.emit()
        return super().eventFilter(watched, event)

    def enterEvent(self, event: Any) -> None:
        super().enterEvent(event)
        self.pointer_entered.emit()

    def leaveEvent(self, event: Any) -> None:
        super().leaveEvent(event)
        self.pointer_left.emit()

    def set_catalog(
        self,
        papers: Iterable[Any],
        open_ids: Iterable[int],
        current_id: int,
    ) -> None:
        self._papers = [
            normalized
            for paper in papers
            if (normalized := _normalized_paper(paper))["id"]
        ]
        self._open_ids = {int(value) for value in open_ids}
        self._current_id = int(current_id)
        self._open_ids.add(self._current_id)
        self.search.clear()
        self._render()

    def paper_ids(self) -> list[int]:
        result: list[int] = []
        for row in range(self.paper_list.count()):
            paper = self.paper_list.item(row).data(PAPER_ROLE)
            if paper:
                result.append(int(paper["id"]))
        return result

    def _matches(self, paper: Mapping[str, object], query: str) -> bool:
        haystack = " ".join(
            str(paper.get(key) or "") for key in ("title", "authors", "year")
        ).casefold()
        return all(token in haystack for token in query.casefold().split())

    def _add_section(self, label: str) -> None:
        item = QListWidgetItem(label)
        item.setData(KIND_ROLE, "section")
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.paper_list.addItem(item)

    def _add_empty(self, label: str) -> None:
        item = QListWidgetItem(label)
        item.setData(KIND_ROLE, "empty")
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.paper_list.addItem(item)

    def _add_paper(self, paper: dict[str, object]) -> None:
        paper_id = int(paper["id"])
        item = QListWidgetItem(str(paper["title"]))
        item.setData(KIND_ROLE, "paper")
        item.setData(PAPER_ROLE, paper)
        item.setData(CURRENT_ROLE, paper_id == self._current_id)
        item.setData(OPEN_ROLE, paper_id in self._open_ids)
        tooltip = str(paper["title"])
        if paper.get("authors"):
            tooltip += f"\n{paper['authors']}"
        item.setToolTip(tooltip)
        self.paper_list.addItem(item)

    def _render(self, *_args: object) -> None:
        self.paper_list.clear()
        query = self.search.text().strip()
        if query:
            self._add_section("Results")
            matches = [paper for paper in self._papers if self._matches(paper, query)]
            for paper in sorted(
                matches,
                key=lambda value: (
                    int(value["id"]) != self._current_id,
                    str(value["title"]).casefold(),
                ),
            ):
                self._add_paper(paper)
            if not matches:
                self._add_empty("No matching papers")
            return

        by_id = {int(paper["id"]): paper for paper in self._papers}
        open_papers = [
            by_id[paper_id] for paper_id in self._open_ids if paper_id in by_id
        ]
        open_papers.sort(
            key=lambda value: (
                int(value["id"]) != self._current_id,
                str(value["title"]).casefold(),
            )
        )
        self._add_section("Open")
        for paper in open_papers:
            self._add_paper(paper)

        library_papers = [
            paper for paper in self._papers if int(paper["id"]) not in self._open_ids
        ]
        library_papers.sort(key=lambda value: str(value["title"]).casefold())
        self._add_section("Library")
        if library_papers:
            for paper in library_papers:
                self._add_paper(paper)
        else:
            self._add_empty("No other papers in Library")

    def _activate_item(self, item: QListWidgetItem) -> None:
        paper = item.data(PAPER_ROLE)
        if paper:
            self.paper_selected.emit(int(paper["id"]))
