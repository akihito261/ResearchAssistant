from __future__ import annotations


UI_COLORS = {
    "APP_BG": "#F5F7FA",
    "SURFACE": "#FFFFFF",
    "SURFACE_SUBTLE": "#F3F6FA",
    "PRIMARY": "#4F7DF3",
    "PRIMARY_HOVER": "#3F6DE0",
    "PRIMARY_SOFT": "#EEF4FF",
    "PRIMARY_TEXT": "#315FBF",
    "AI": "#756CE8",
    "AI_HOVER": "#675DD8",
    "AI_SOFT": "#F1EFFF",
    "TEXT": "#202733",
    "TEXT_MUTED": "#697483",
    "BORDER": "#DEE4EC",
    "BORDER_SOFT": "#E9EDF2",
}


def apply_ui_palette(stylesheet: str) -> str:
    """Expand the small shared color token set in a focused Qt stylesheet."""
    result = stylesheet
    for name, value in UI_COLORS.items():
        result = result.replace(f"@{name}@", value)
    return result


MODERN_SCROLLBAR_QSS = """
QAbstractScrollArea[modernScroll="true"] {
    border: none;
    border-radius: 8px;
    background: transparent;
}
QAbstractScrollArea[modernScroll="true"]::corner {
    background: transparent;
    border: none;
}
QAbstractScrollArea[modernScroll="true"] QWidget#qt_scrollarea_viewport {
    border-radius: 8px;
    background: transparent;
}
QAbstractScrollArea[modernScroll="true"] QScrollBar:vertical {
    width: 7px;
    margin: 2px 0;
    border: none;
    background: transparent;
}
QAbstractScrollArea[modernScroll="true"] QScrollBar::handle:vertical {
    min-height: 28px;
    border: none;
    border-radius: 3px;
    background: #C8CDD4;
}
QAbstractScrollArea[modernScroll="true"] QScrollBar::handle:vertical:hover {
    background: #AEB5BE;
}
QAbstractScrollArea[modernScroll="true"] QScrollBar::add-line:vertical,
QAbstractScrollArea[modernScroll="true"] QScrollBar::sub-line:vertical {
    height: 0;
    border: none;
    background: transparent;
}
QAbstractScrollArea[modernScroll="true"] QScrollBar::add-page:vertical,
QAbstractScrollArea[modernScroll="true"] QScrollBar::sub-page:vertical {
    background: transparent;
}
QAbstractScrollArea[modernScroll="true"] QScrollBar:horizontal {
    height: 7px;
    margin: 0 2px 2px 2px;
    border: none;
    background: transparent;
}
QAbstractScrollArea[modernScroll="true"] QScrollBar::handle:horizontal {
    min-width: 28px;
    border: none;
    border-radius: 3px;
    background: #C8CDD4;
}
QAbstractScrollArea[modernScroll="true"] QScrollBar::handle:horizontal:hover {
    background: #AEB5BE;
}
QAbstractScrollArea[modernScroll="true"] QScrollBar::add-line:horizontal,
QAbstractScrollArea[modernScroll="true"] QScrollBar::sub-line:horizontal {
    width: 0;
    border: none;
    background: transparent;
}
QAbstractScrollArea[modernScroll="true"] QScrollBar::add-page:horizontal,
QAbstractScrollArea[modernScroll="true"] QScrollBar::sub-page:horizontal {
    background: transparent;
}
"""
