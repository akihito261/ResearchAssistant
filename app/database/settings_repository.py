from __future__ import annotations

import json
from typing import Any

from app.database.database import connection_scope, transaction


class SettingsRepository:
    @staticmethod
    def get(key: str, default: str | None = None) -> str | None:
        with connection_scope() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (key,),
            ).fetchone()
            return str(row["value"]) if row is not None else default

    @staticmethod
    def set(key: str, value: str) -> None:
        if not key.strip():
            raise ValueError("Setting key cannot be empty")
        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO app_settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )

    @staticmethod
    def get_all() -> dict[str, str]:
        with connection_scope() as connection:
            return {
                str(row["key"]): str(row["value"])
                for row in connection.execute(
                    "SELECT key, value FROM app_settings ORDER BY key"
                )
            }

    @staticmethod
    def get_json(key: str, default: Any = None) -> Any:
        value = SettingsRepository.get(key)
        return default if value is None else json.loads(value)

    @staticmethod
    def set_json(key: str, value: Any) -> None:
        SettingsRepository.set(
            key,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        )
