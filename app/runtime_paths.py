from __future__ import annotations

import os
import sys
from pathlib import Path


APPLICATION_DIRECTORY_NAME = "ResearchAssistant"
SOURCE_ROOT = Path(__file__).resolve().parents[1]


def is_frozen() -> bool:
    """Return whether the application is running from a frozen executable."""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Root for read-only bundled resources in source and PyInstaller builds."""
    if is_frozen():
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            return Path(bundle_root).resolve()
        return Path(sys.executable).resolve().parent
    return SOURCE_ROOT


def resource_path(*parts: str) -> Path:
    return resource_root().joinpath(*parts)


def writable_root() -> Path:
    """Root for mutable application state without changing source-mode paths."""
    if not is_frozen():
        return SOURCE_ROOT
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        base = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
        return (base / APPLICATION_DIRECTORY_NAME).resolve()

    # Follow the XDG base-directory convention for Linux packages while
    # retaining the established application directory name.
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    return (base / APPLICATION_DIRECTORY_NAME).resolve()


def writable_path(*parts: str) -> Path:
    return writable_root().joinpath(*parts)


def ensure_writable_directories() -> Path:
    root = writable_root()
    for relative in (
        ("data",),
        ("logs",),
        ("config",),
        ("library", "papers"),
        ("backups",),
    ):
        root.joinpath(*relative).mkdir(parents=True, exist_ok=True)
    return root
