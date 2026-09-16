from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.ui.icons import populate_reading_status_combo, set_widget_icon


class ReviewPaperDialog(QDialog):
    """Shared add/edit form for paper metadata and library relationships."""

    STATUSES = ("Unread", "Reading", "Completed")

    def __init__(
        self,
        metadata: Mapping[str, Any],
        collections: Iterable[Mapping[str, Any]],
        parent=None,
        *,
        dialog_title: str = "Review Paper Information",
        save_label: str = "Save Paper",
    ) -> None:
        super().__init__(parent)
        self.metadata = metadata
        self.setWindowTitle(dialog_title)
        self.setMinimumWidth(580)
        self._setup_ui(collections, save_label)
        self._apply_style()

    def _setup_ui(
        self,
        collections: Iterable[Mapping[str, Any]],
        save_label: str,
    ) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(25, 25, 25, 25)
        main_layout.setSpacing(18)

        title_label = QLabel(self.windowTitle())
        title_label.setObjectName("dialogTitle")
        description = QLabel("Review and organize this paper before saving.")
        description.setObjectName("description")
        main_layout.addWidget(title_label)
        main_layout.addWidget(description)

        form_layout = QFormLayout()
        form_layout.setSpacing(13)

        self.title_input = QLineEdit(str(self.metadata.get("title") or ""))
        self.authors_input = QLineEdit(str(self.metadata.get("authors") or ""))
        self.year_input = QLineEdit(
            str(self.metadata.get("year")) if self.metadata.get("year") else ""
        )
        self.doi_input = QLineEdit(str(self.metadata.get("doi") or ""))

        raw_tags = self.metadata.get("tags") or ()
        if isinstance(raw_tags, str):
            tag_text = raw_tags
        else:
            tag_text = ", ".join(str(tag) for tag in raw_tags)
        self.tags_input = QLineEdit(tag_text)
        self.tags_input.setPlaceholderText("ROI, VVC, Neural Codec")

        selected_collection_ids = {
            int(value)
            for value in (
                self.metadata.get("collection_ids")
                or ([self.metadata["collection_id"]] if self.metadata.get("collection_id") else [])
            )
        }
        self.collections_input = QListWidget()
        self.collections_input.setObjectName("collectionsInput")
        self.collections_input.setMaximumHeight(115)
        for collection in collections:
            item = QListWidgetItem(str(collection["name"]))
            item.setData(Qt.ItemDataRole.UserRole, int(collection["id"]))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if int(collection["id"]) in selected_collection_ids
                else Qt.CheckState.Unchecked
            )
            self.collections_input.addItem(item)

        self.status_input = QComboBox()
        current_status = str(self.metadata.get("status") or "Unread")
        populate_reading_status_combo(self.status_input, current_status)

        self.important_input = QCheckBox("Mark as important")
        self.important_input.setChecked(bool(self.metadata.get("is_important")))

        form_layout.addRow("Title:", self.title_input)
        form_layout.addRow("Authors:", self.authors_input)
        form_layout.addRow("Year:", self.year_input)
        form_layout.addRow("DOI:", self.doi_input)
        form_layout.addRow("Tags:", self.tags_input)
        form_layout.addRow("Collections:", self.collections_input)
        form_layout.addRow("Status:", self.status_input)
        form_layout.addRow("", self.important_input)
        main_layout.addLayout(form_layout)

        button_layout = QHBoxLayout()
        button_layout.addStretch()
        cancel_button = QPushButton("Cancel")
        cancel_button.setObjectName("secondaryButton")
        cancel_button.clicked.connect(self.reject)
        save_button = QPushButton(save_label)
        save_button.setObjectName("primaryButton")
        set_widget_icon(
            save_button,
            "file-plus" if save_label == "Save Paper" else "save",
            size=16,
            color="#FFFFFF",
            active_color="#FFFFFF",
        )
        save_button.clicked.connect(self._validate_and_accept)
        button_layout.addWidget(cancel_button)
        button_layout.addWidget(save_button)
        main_layout.addLayout(button_layout)

    def _validate_and_accept(self) -> None:
        if not self.title_input.text().strip():
            QMessageBox.warning(self, "Missing title", "Paper title cannot be empty.")
            return
        year_text = self.year_input.text().strip()
        if year_text and (not year_text.isdigit() or not 1800 <= int(year_text) <= 2200):
            QMessageBox.warning(
                self,
                "Invalid year",
                "Year must be a number between 1800 and 2200.",
            )
            return
        self.accept()

    def get_data(self) -> dict[str, Any]:
        year_text = self.year_input.text().strip()
        tags = [
            tag.strip()
            for tag in self.tags_input.text().split(",")
            if tag.strip()
        ]
        collection_ids = [
            int(item.data(Qt.ItemDataRole.UserRole))
            for index in range(self.collections_input.count())
            if (item := self.collections_input.item(index)).checkState()
            == Qt.CheckState.Checked
        ]
        return {
            "title": self.title_input.text().strip(),
            "authors": self.authors_input.text().strip(),
            "year": int(year_text) if year_text else None,
            "doi": self.doi_input.text().strip() or None,
            "collection_ids": collection_ids,
            # Compatibility with the original single-collection import flow.
            "collection_id": collection_ids[0] if collection_ids else None,
            "tags": tags,
            "status": self.status_input.currentText(),
            "is_important": self.important_input.isChecked(),
        }

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QDialog { background-color: #F7F8FA; }
            #dialogTitle { font-size: 21px; font-weight: 700; color: #181A1D; }
            #description { font-size: 13px; color: #858A92; }
            QLineEdit, QComboBox, #collectionsInput {
                min-height: 36px;
                background-color: white;
                border: 1px solid #DEE1E5;
                border-radius: 7px;
                padding: 0 10px;
                font-size: 13px;
            }
            #collectionsInput { padding: 5px; }
            QLineEdit:focus, QComboBox:focus, #collectionsInput:focus {
                border-color: #888E96;
            }
            #primaryButton, #secondaryButton {
                min-height: 38px;
                padding: 0 18px;
                border-radius: 7px;
                font-weight: 600;
            }
            #primaryButton { background-color: #22252A; color: white; border: none; }
            #secondaryButton { background-color: white; color: #44484F; border: 1px solid #DEE1E5; }
            """
        )
