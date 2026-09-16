from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.database.summary_repository import SUMMARY_FIELDS
from app.ui.citation_widgets import citation_number_map
from app.ui.icons import icon_pixmap
from app.ui.markdown_math import AutoExpandingMarkdownEdit
from app.ui.ui_styles import MODERN_SCROLLBAR_QSS


LABELS = {
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
ICONS = {
    "problem": "target",
    "contribution": "sparkles",
    "method": "workflow",
    "dataset": "database",
    "baseline": "layers",
    "results": "trending-up",
    "limitations": "triangle-alert",
    "unclear_points": "circle-help",
    "ideas": "lightbulb",
}


class AISummaryPreviewDialog(QDialog):
    citation_requested = Signal(object)
    REGENERATE_RESULT = 2

    def __init__(
        self,
        result: Any,
        existing_summary: Mapping[str, Any] | None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.result = result
        self.existing_summary = dict(existing_summary or {})
        self.editors: dict[str, AutoExpandingMarkdownEdit] = {}
        self.setWindowTitle("AI Summary Preview")
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        max_width = int(available.width() * 0.88) if available is not None else 720
        max_height = int(available.height() * 0.86) if available is not None else 760
        self.resize(min(720, max_width), min(760, max_height))
        self.setMaximumSize(max_width, max_height)
        self.setMinimumSize(min(520, max_width), min(440, max_height))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        title = QLabel("AI Summary Preview")
        title.setObjectName("aiSummaryPreviewTitle")
        subtitle = QLabel(
            f"{str(getattr(result, 'provider', '')).title()} · "
            f"{str(getattr(result, 'model', ''))} · Original English PDF"
        )
        subtitle.setObjectName("aiSummaryPreviewSubtitle")
        outer.addWidget(title)
        outer.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setProperty("modernScroll", True)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 10, 8, 8)
        layout.setSpacing(9)
        citation_numbering = citation_number_map(
            tuple(
                citation
                for field in SUMMARY_FIELDS
                for citation in result.fields[field].citations
            )
        )
        for field in SUMMARY_FIELDS:
            field_result = result.fields[field]
            heading = QHBoxLayout()
            heading.setContentsMargins(0, 0, 0, 0)
            heading.setSpacing(6)
            icon = QLabel()
            icon.setFixedSize(16, 16)
            icon.setPixmap(icon_pixmap(ICONS[field], 15, color="#5D6570"))
            label = QLabel(LABELS[field])
            label.setObjectName("aiSummaryPreviewHeading")
            editor = AutoExpandingMarkdownEdit(
                str(field_result.content),
                minimum_height=72,
                maximum_height=360,
            )
            editor.setObjectName("aiSummaryPreviewEditor")
            editor.setProperty("modernScroll", True)
            self.editors[field] = editor
            heading.addWidget(icon)
            heading.addWidget(label)
            heading.addStretch()
            layout.addLayout(heading)
            layout.addWidget(editor)
            editor.citation_requested.connect(self.citation_requested)
            editor.set_citations(field_result.citations, citation_numbering)
        layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("Cancel")
        regenerate = QPushButton("Regenerate")
        apply_button = QPushButton("Apply to Summary")
        apply_button.setObjectName("aiSummaryApplyButton")
        cancel.clicked.connect(self.reject)
        regenerate.clicked.connect(lambda: self.done(self.REGENERATE_RESULT))
        apply_button.clicked.connect(self._apply)
        actions.addWidget(cancel)
        actions.addWidget(regenerate)
        actions.addWidget(apply_button)
        outer.addLayout(actions)

        self.setStyleSheet(
            """
            QDialog { background: #F7F8FA; }
            #aiSummaryPreviewTitle { font-size: 18px; font-weight: 700; color: #24282E; }
            #aiSummaryPreviewSubtitle { color: #737A84; font-size: 11px; }
            #aiSummaryPreviewHeading { color: #343A42; font-weight: 650; }
            #aiSummaryPreviewEditor {
                border: 1px solid #D9DEE4; border-radius: 7px;
                background: white; color: #30353B; padding: 7px;
            }
            #inlineCitationButton {
                border: none; border-radius: 4px; background: transparent; padding: 0;
            }
            #inlineCitationButton:hover { background: #E8EEF4; }
            #aiSummaryApplyButton {
                border: none; border-radius: 7px; background: #252A31;
                color: white; padding: 7px 13px; font-weight: 600;
            }
            """ + MODERN_SCROLLBAR_QSS
        )

    def values(self) -> dict[str, str]:
        return {field: self.editors[field].toPlainText().strip() for field in SUMMARY_FIELDS}

    def field_results(self) -> dict[str, dict[str, object]]:
        return {
            field: {
                "support_status": self.result.fields[field].support_status,
                "citations": [
                    citation.as_dict() for citation in self.result.fields[field].citations
                ],
            }
            for field in SUMMARY_FIELDS
        }

    def _apply(self) -> None:
        if any(str(self.existing_summary.get(field) or "").strip() for field in SUMMARY_FIELDS):
            answer = QMessageBox.question(
                self,
                "Replace existing Summary?",
                "Applying this preview will replace the existing Summary fields. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.accept()
