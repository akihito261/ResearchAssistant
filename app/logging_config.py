from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.runtime_paths import writable_path


DEFAULT_LOG_PATH = writable_path("logs", "research_assistant.log")

_HANDLER_MARKER = "_research_assistant_rotating_file_handler"
_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def configure_logging(
    *,
    log_path: str | Path = DEFAULT_LOG_PATH,
    level: int = logging.INFO,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 3,
) -> Path:
    """Configure exactly one application-owned rotating file handler."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be greater than zero")
    if backup_count < 0:
        raise ValueError("backup_count cannot be negative")

    path = Path(log_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    owned_handlers = [
        handler
        for handler in root_logger.handlers
        if getattr(handler, _HANDLER_MARKER, False)
    ]

    handler: RotatingFileHandler | None = None
    for existing_handler in owned_handlers:
        existing_path = Path(existing_handler.baseFilename).resolve()
        if handler is None and existing_path == path:
            handler = existing_handler
            continue
        root_logger.removeHandler(existing_handler)
        existing_handler.close()

    if handler is None:
        handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        setattr(handler, _HANDLER_MARKER, True)
        root_logger.addHandler(handler)
    else:
        handler.maxBytes = max_bytes
        handler.backupCount = backup_count

    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root_logger.setLevel(level)
    return path
