from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.database.summary_repository import SUMMARY_FIELDS


SUMMARY_LABELS = {
    "problem": "Problem",
    "contribution": "Contribution",
    "method": "Method",
    "dataset": "Dataset",
    "baseline": "Baseline",
    "results": "Results",
    "limitations": "Limitations",
    "unclear_points": "Unclear points",
    "ideas": "Ideas",
}


class SummaryPreviewDialog(QDialog):
    def __init__(
        self,
        paper: Mapping[str, Any],
        summary: Mapping[str, Any] | None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Paper Summary")
        self.resize(650, 680)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        title = QLabel(str(paper.get("title") or "Paper Summary"))
        title.setObjectName("summaryTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(3, 8, 8, 8)
        row = dict(summary) if summary is not None else {}
        populated = False
        for field in SUMMARY_FIELDS:
            value = str(row.get(field) or "")
            if not value.strip():
                continue
            populated = True
            heading = QLabel(SUMMARY_LABELS[field])
            heading.setObjectName("summaryHeading")
            heading.setTextFormat(Qt.TextFormat.PlainText)
            text = QLabel(value)
            text.setObjectName("summaryText")
            text.setTextFormat(Qt.TextFormat.PlainText)
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            content_layout.addWidget(heading)
            content_layout.addWidget(text)
            content_layout.addSpacing(10)
        if not populated:
            empty = QLabel("No summary has been written for this paper yet.")
            empty.setObjectName("summaryEmpty")
            empty.setTextFormat(Qt.TextFormat.PlainText)
            empty.setWordWrap(True)
            content_layout.addWidget(empty)
        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setStyleSheet(
            """
            QDialog { background: #F7F8FA; }
            #summaryTitle { font-size: 20px; font-weight: 700; color: #202328; }
            #summaryHeading { font-size: 13px; font-weight: 700; color: #34383E; }
            #summaryText { font-size: 13px; color: #4E535B; line-height: 1.4; }
            #summaryEmpty { color: #777D86; font-size: 13px; }
            QScrollArea { background: transparent; }
            """
        )
