from __future__ import annotations

from app.runtime_paths import resource_path


__version__ = resource_path("VERSION").read_text(encoding="utf-8").strip()
