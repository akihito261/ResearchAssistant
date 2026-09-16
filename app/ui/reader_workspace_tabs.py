from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QTabBar,
    QWidget,
)

from app.ui.icons import app_icon, set_widget_icon


def _paper_value(paper: Any, key: str, default: object = "") -> object:
    try:
        value = paper[key]
    except (KeyError, TypeError, IndexError):
        value = paper.get(key, default) if isinstance(paper, Mapping) else default
    return default if value is None else value


class ReaderWorkspaceTabs(QTabBar):
    """Compact session-only paper tabs for the Reader toolbar."""

    paper_activated = Signal(int)
    paper_close_requested = Signal(int)
    add_to_ai_requested = Signal(int)
    remove_from_ai_requested = Signal(int)
    paper_order_changed = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("readerWorkspaceTabs")
        self.setDocumentMode(True)
        self.setDrawBase(False)
        self.setExpanding(False)
        self.setMovable(True)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(120)
        self.setFixedHeight(32)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_tab_context_menu)
        self.currentChanged.connect(self._current_changed)
        self.tabMoved.connect(self._tab_moved)
        self._ai_aliases: dict[int, str] = {}

    def set_papers(
        self,
        papers: Iterable[Any],
        active_paper_id: int,
        ai_aliases: Mapping[int, str] | None = None,
    ) -> None:
        prepared: list[tuple[int, str]] = []
        for paper in papers:
            paper_id = int(_paper_value(paper, "id", 0))
            if paper_id < 1:
                continue
            title = str(_paper_value(paper, "title", "Untitled paper")).strip()
            prepared.append((paper_id, title or "Untitled paper"))
        self._ai_aliases = {
            int(paper_id): str(alias)
            for paper_id, alias in (ai_aliases or {}).items()
        }
        current = [
            (int(self.tabData(index)), self.tabText(index))
            for index in range(self.count())
        ]
        if current == prepared:
            self._refresh_tab_controls(prepared)
            active_index = next(
                (
                    index
                    for index, (paper_id, _title) in enumerate(prepared)
                    if paper_id == int(active_paper_id)
                ),
                -1,
            )
            if active_index >= 0 and active_index != self.currentIndex():
                blocker = self.blockSignals(True)
                self.setCurrentIndex(active_index)
                self.blockSignals(blocker)
            self.setVisible(self.count() > 0)
            return
        self.blockSignals(True)
        try:
            while self.count():
                index = self.count() - 1
                controls = self.tabButton(index, QTabBar.ButtonPosition.RightSide)
                legacy_alias = self.tabButton(index, QTabBar.ButtonPosition.LeftSide)
                self.removeTab(index)
                if controls is not None:
                    controls.deleteLater()
                if legacy_alias is not None:
                    legacy_alias.deleteLater()
            active_index = -1
            for paper_id, title in prepared:
                index = self.addTab(title)
                self.setTabData(index, paper_id)
                self.setTabToolTip(index, title)
                self.setTabButton(
                    index,
                    QTabBar.ButtonPosition.RightSide,
                    self._create_tab_controls(paper_id, title),
                )
                if paper_id == int(active_paper_id):
                    active_index = index
            if active_index >= 0:
                self.setCurrentIndex(active_index)
        finally:
            self.blockSignals(False)
        self.setVisible(self.count() > 0)

    def _create_tab_controls(self, paper_id: int, title: str) -> QWidget:
        controls = QWidget(self)
        controls.setObjectName("workspaceTabControls")
        controls.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        layout = QHBoxLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        badge = QLabel(controls)
        badge.setObjectName("workspaceTabAlias")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(28, 18)
        layout.addWidget(badge)
        close = QPushButton(controls)
        close.setObjectName("workspaceTabClose")
        close.setFixedSize(18, 18)
        set_widget_icon(close, "x", size=11, tooltip=f"Close {title}")
        close.clicked.connect(
            lambda _checked=False, value=int(paper_id): (
                self.paper_close_requested.emit(value)
            )
        )
        layout.addWidget(close)
        self._update_tab_controls(controls, paper_id, title)
        return controls

    def _update_tab_controls(
        self, controls: QWidget, paper_id: int, title: str
    ) -> None:
        badge = controls.findChild(QLabel, "workspaceTabAlias")
        if badge is None:
            return
        alias = self._ai_aliases.get(int(paper_id), "")
        badge.setText(alias)
        badge.setToolTip(title)
        badge.setVisible(bool(alias))
        controls.setFixedSize(48 if alias else 18, 18)

    def _refresh_tab_controls(self, papers: list[tuple[int, str]]) -> None:
        for index, (paper_id, title) in enumerate(papers):
            controls = self.tabButton(index, QTabBar.ButtonPosition.RightSide)
            if controls is None or controls.objectName() != "workspaceTabControls":
                controls = self._create_tab_controls(paper_id, title)
                self.setTabButton(
                    index, QTabBar.ButtonPosition.RightSide, controls
                )
            self._update_tab_controls(controls, paper_id, title)

    def _tab_moved(self, _from_index: int, _to_index: int) -> None:
        order: list[int] = []
        for index in range(self.count()):
            try:
                order.append(int(self.tabData(index)))
            except (TypeError, ValueError):
                return
        self.paper_order_changed.emit(order)

    def _current_changed(self, index: int) -> None:
        if index < 0:
            return
        try:
            paper_id = int(self.tabData(index))
        except (TypeError, ValueError):
            return
        if paper_id > 0:
            self.paper_activated.emit(paper_id)

    def _show_tab_context_menu(self, position: Any) -> None:
        index = self.tabAt(position)
        if index < 0:
            return
        try:
            paper_id = int(self.tabData(index))
        except (TypeError, ValueError):
            return
        menu = QMenu(self)
        if paper_id in self._ai_aliases:
            action = menu.addAction(
                app_icon("x"), "Remove from AI comparison"
            )
            selected = menu.exec(self.mapToGlobal(position))
            if selected is action:
                self.remove_from_ai_requested.emit(paper_id)
        else:
            action = menu.addAction(
                app_icon("sparkles"), "Add to AI comparison"
            )
            selected = menu.exec(self.mapToGlobal(position))
            if selected is action:
                self.add_to_ai_requested.emit(paper_id)
