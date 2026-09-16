from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.database.collection_repository import CollectionRepository
from app.database.tag_repository import TagRepository
from app.ui.icons import DANGER_ICON_COLOR, app_icon, set_widget_icon


LOGGER = logging.getLogger(__name__)


class CollectionSelectionDialog(QDialog):
    def __init__(
        self,
        collections: Iterable[Mapping[str, Any]],
        selected_ids: Iterable[int],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Change Collections")
        self.setMinimumWidth(420)
        selected = {int(value) for value in selected_ids}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(QLabel("Choose every collection for this paper."))
        self.list_widget = QListWidget()
        for collection in collections:
            item = QListWidgetItem(str(collection["name"]))
            item.setData(Qt.ItemDataRole.UserRole, int(collection["id"]))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if int(collection["id"]) in selected
                else Qt.CheckState.Unchecked
            )
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_ids(self) -> list[int]:
        return [
            int(item.data(Qt.ItemDataRole.UserRole))
            for index in range(self.list_widget.count())
            if (item := self.list_widget.item(index)).checkState()
            == Qt.CheckState.Checked
        ]


class TaxonomyManagerDialog(QDialog):
    taxonomy_changed = Signal()

    def __init__(
        self,
        parent=None,
        *,
        project_id: int | None = None,
        collection_repository: type[CollectionRepository] = CollectionRepository,
        tag_repository: type[TagRepository] = TagRepository,
    ) -> None:
        super().__init__(parent)
        self.collection_repository = collection_repository
        self.project_id = int(project_id) if project_id is not None else None
        self.tag_repository = tag_repository
        self.setWindowTitle("Manage Tags and Collections")
        self.resize(600, 470)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        self.tabs = QTabWidget()
        self.collection_list = QListWidget()
        self.tag_list = QListWidget()
        self.tabs.addTab(
            self._build_tab(
                self.collection_list,
                self._create_collection,
                self._rename_collection,
                self._delete_collection,
            ),
            app_icon("folder"),
            "Collections",
        )
        self.tabs.addTab(
            self._build_tab(
                self.tag_list,
                self._create_tag,
                self._rename_tag,
                self._delete_tag,
            ),
            app_icon("tag"),
            "Tags",
        )
        layout.addWidget(self.tabs)
        close_button = QPushButton("Done")
        set_widget_icon(close_button, "circle-check", size=16)
        close_button.clicked.connect(self.accept)
        footer = QHBoxLayout()
        footer.addStretch()
        footer.addWidget(close_button)
        layout.addLayout(footer)
        self._refresh()

    @staticmethod
    def _build_tab(list_widget, create_callback, rename_callback, delete_callback):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 12, 8, 8)
        layout.addWidget(list_widget)
        actions = QHBoxLayout()
        add_button = QPushButton("Add")
        rename_button = QPushButton("Rename")
        delete_button = QPushButton("Delete")
        set_widget_icon(add_button, "plus", size=16)
        set_widget_icon(rename_button, "pencil", size=16)
        set_widget_icon(
            delete_button,
            "trash",
            size=16,
            color=DANGER_ICON_COLOR,
            active_color="#7E2525",
        )
        add_button.clicked.connect(create_callback)
        rename_button.clicked.connect(rename_callback)
        delete_button.clicked.connect(delete_callback)
        actions.addWidget(add_button)
        actions.addWidget(rename_button)
        actions.addStretch()
        actions.addWidget(delete_button)
        layout.addLayout(actions)
        return widget

    def _refresh(self) -> None:
        try:
            collections = self.collection_repository.list_all(self.project_id)
            tags = self.tag_repository.list_all()
        except (sqlite3.Error, OSError, ValueError) as error:
            self._error("Could not load tags and collections", error)
            return
        self._fill(self.collection_list, collections)
        self._fill(self.tag_list, tags)

    @staticmethod
    def _fill(widget: QListWidget, rows) -> None:
        widget.clear()
        for row in rows:
            item = QListWidgetItem(str(row["name"]))
            item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
            if "description" in row.keys():
                item.setData(Qt.ItemDataRole.UserRole + 1, row["description"])
            widget.addItem(item)

    def _current(self, widget: QListWidget) -> tuple[int, str] | None:
        item = widget.currentItem()
        if item is None:
            return None
        return int(item.data(Qt.ItemDataRole.UserRole)), item.text()

    def _create_collection(self) -> None:
        name, accepted = QInputDialog.getText(self, "New Collection", "Name:")
        if accepted and name.strip():
            self._mutate(
                lambda: self.collection_repository.create(
                    name, project_id=self.project_id
                )
            )

    def _rename_collection(self) -> None:
        current = self._current(self.collection_list)
        if current is None:
            return
        collection_id, old_name = current
        name, accepted = QInputDialog.getText(
            self, "Rename Collection", "Name:", text=old_name
        )
        item = self.collection_list.currentItem()
        description = item.data(Qt.ItemDataRole.UserRole + 1) if item else None
        if accepted and name.strip():
            self._mutate(
                lambda: self.collection_repository.rename(
                    collection_id,
                    name,
                    description=description,
                    project_id=self.project_id,
                )
            )

    def _delete_collection(self) -> None:
        current = self._current(self.collection_list)
        if current is None:
            return
        collection_id, name = current
        answer = QMessageBox.question(
            self,
            "Delete Collection",
            f'Delete collection "{name}"? Papers will remain in Library.',
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._mutate(
                lambda: self.collection_repository.delete(
                    collection_id, project_id=self.project_id
                )
            )

    def _create_tag(self) -> None:
        name, accepted = QInputDialog.getText(self, "New Tag", "Name:")
        if accepted and name.strip():
            self._mutate(lambda: self.tag_repository.create(name))

    def _rename_tag(self) -> None:
        current = self._current(self.tag_list)
        if current is None:
            return
        tag_id, old_name = current
        name, accepted = QInputDialog.getText(
            self, "Rename Tag", "Name:", text=old_name
        )
        if accepted and name.strip():
            self._mutate(lambda: self.tag_repository.rename(tag_id, name))

    def _delete_tag(self) -> None:
        current = self._current(self.tag_list)
        if current is None:
            return
        tag_id, name = current
        answer = QMessageBox.question(
            self,
            "Delete Tag",
            f'Delete tag "{name}" from every paper?',
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._mutate(lambda: self.tag_repository.delete(tag_id))

    def _mutate(self, operation) -> None:
        try:
            operation()
        except (sqlite3.Error, OSError, ValueError) as error:
            self._error("Could not update the library taxonomy", error)
            return
        self._refresh()
        self.taxonomy_changed.emit()

    def _error(self, message: str, error: Exception) -> None:
        LOGGER.error(message, exc_info=(type(error), error, error.__traceback__))
        QMessageBox.critical(self, "Library", f"{message}:\n{error}")
