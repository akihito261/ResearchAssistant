from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from app.runtime_paths import resource_path


ICON_ROOT = resource_path("resources", "icons")

NEUTRAL_ICON_COLOR = "#5D6570"
ACTIVE_ICON_COLOR = "#252A31"
SELECTED_ICON_COLOR = "#252A31"
DISABLED_ICON_COLOR = "#A8AEB7"
DANGER_ICON_COLOR = "#A44A4A"
IMPORTANT_ICON_COLOR = "#A66F00"

READING_STATUS_STYLES = {
    "Unread": {
        "icon": "circle",
        "color": "#68727E",
        "text": "#4F5965",
        "tint": "#F1F3F5",
        "border": "#D9DEE3",
    },
    "Reading": {
        "icon": "book-open",
        "color": "#2D78BD",
        "text": "#245F96",
        "tint": "#EAF3FC",
        "border": "#C9DEF2",
    },
    "Completed": {
        "icon": "circle-check",
        "color": "#33865A",
        "text": "#286C48",
        "tint": "#EAF6EF",
        "border": "#CBE4D5",
    },
}

# Covers the exact physical pixels requested by 14–20 px UI icons at
# Windows 100%, 125%, and 150% scaling, with a few larger fallbacks.
_RENDER_SIZES = (*range(14, 28), 30, 32, 36, 40, 48)


def icon_path(name: str) -> Path:
    """Resolve an icon name without allowing paths outside the icon directory."""
    if not name or Path(name).name != name or name.endswith(".svg"):
        raise ValueError(f"Invalid icon name: {name!r}")
    path = ICON_ROOT / f"{name}.svg"
    if not path.is_file():
        raise FileNotFoundError(f"Missing application icon: {path}")
    return path


def available_icon_names() -> frozenset[str]:
    if not ICON_ROOT.is_dir():
        return frozenset()
    return frozenset(path.stem for path in ICON_ROOT.glob("*.svg") if path.is_file())


@lru_cache(maxsize=128)
def app_icon(
    name: str,
    color: str = NEUTRAL_ICON_COLOR,
    active_color: str = ACTIVE_ICON_COLOR,
    selected_color: str = SELECTED_ICON_COLOR,
    disabled_color: str = DISABLED_ICON_COLOR,
    checked_color: str = ACTIVE_ICON_COLOR,
) -> QIcon:
    """Return a DPI-friendly, consistently colored icon from the local SVG set."""
    source = icon_path(name).read_text(encoding="utf-8")
    result = QIcon()
    mode_state_colors = (
        (QIcon.Mode.Normal, QIcon.State.Off, color),
        (QIcon.Mode.Normal, QIcon.State.On, checked_color),
        (QIcon.Mode.Active, QIcon.State.Off, active_color),
        (QIcon.Mode.Active, QIcon.State.On, checked_color),
        (QIcon.Mode.Selected, QIcon.State.Off, selected_color),
        (QIcon.Mode.Selected, QIcon.State.On, selected_color),
        (QIcon.Mode.Disabled, QIcon.State.Off, disabled_color),
        (QIcon.Mode.Disabled, QIcon.State.On, disabled_color),
    )
    rendered_colors: dict[str, list[QPixmap]] = {}
    for mode, state, mode_color in mode_state_colors:
        pixmaps = rendered_colors.get(mode_color)
        if pixmaps is None:
            renderer = QSvgRenderer(
                QByteArray(
                    source.replace("currentColor", mode_color).encode("utf-8")
                )
            )
            if not renderer.isValid():
                raise ValueError(f"Invalid SVG icon: {icon_path(name)}")
            pixmaps = []
            for pixels in _RENDER_SIZES:
                pixmap = QPixmap(pixels, pixels)
                pixmap.fill(Qt.GlobalColor.transparent)
                painter = QPainter(pixmap)
                try:
                    renderer.render(painter, QRectF(0, 0, pixels, pixels))
                finally:
                    painter.end()
                pixmaps.append(pixmap)
            rendered_colors[mode_color] = pixmaps
        for pixmap in pixmaps:
            result.addPixmap(pixmap, mode, state)
    return result


def reading_status_icon(status: str) -> QIcon:
    visual = READING_STATUS_STYLES.get(status, READING_STATUS_STYLES["Unread"])
    color = str(visual["color"])
    return app_icon(
        str(visual["icon"]),
        color=color,
        active_color=color,
        selected_color=color,
        checked_color=color,
    )


def populate_reading_status_combo(combo: Any, current: str = "Unread") -> None:
    combo.clear()
    combo.setIconSize(QSize(16, 16))
    for status, visual in READING_STATUS_STYLES.items():
        combo.addItem(reading_status_icon(status), status)
        combo.setItemData(
            combo.count() - 1,
            QColor(str(visual["text"])),
            Qt.ItemDataRole.ForegroundRole,
        )
    index = combo.findText(current)
    combo.setCurrentIndex(max(0, index))


def set_widget_icon(
    widget: Any,
    name: str,
    *,
    size: int = 16,
    tooltip: str | None = None,
    color: str = NEUTRAL_ICON_COLOR,
    active_color: str = ACTIVE_ICON_COLOR,
    selected_color: str = SELECTED_ICON_COLOR,
    checked_color: str = ACTIVE_ICON_COLOR,
) -> None:
    widget.setIcon(
        app_icon(
            name,
            color=color,
            active_color=active_color,
            selected_color=selected_color,
            checked_color=checked_color,
        )
    )
    if hasattr(widget, "setIconSize"):
        widget.setIconSize(QSize(size, size))
    if tooltip:
        widget.setToolTip(tooltip)
        if hasattr(widget, "setAccessibleName") and not widget.accessibleName():
            widget.setAccessibleName(tooltip)


def set_action_icon(
    action: Any,
    name: str,
    *,
    color: str = NEUTRAL_ICON_COLOR,
    active_color: str = ACTIVE_ICON_COLOR,
    checked_color: str = ACTIVE_ICON_COLOR,
) -> None:
    action.setIcon(
        app_icon(
            name,
            color=color,
            active_color=active_color,
            checked_color=checked_color,
        )
    )


def icon_pixmap(
    name: str,
    size: int = 16,
    *,
    color: str = NEUTRAL_ICON_COLOR,
) -> QPixmap:
    return app_icon(name, color=color, active_color=color).pixmap(QSize(size, size))
