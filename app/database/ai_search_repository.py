from __future__ import annotations

import json
from typing import Any

from app.database.database import connection_scope, transaction


def _title(text: str) -> str:
    value = " ".join(str(text).split()).strip()
    return (value[:59].rstrip() + "…") if len(value) > 60 else (value or "New Search")


class AISearchRepository:
    @staticmethod
    def create_conversation(project_id: int) -> int:
        with transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO ai_search_conversations (project_id) VALUES (?)",
                (int(project_id),),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def list_conversations(project_id: int) -> list[dict[str, Any]]:
        with connection_scope() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM ai_search_conversations
                    WHERE project_id = ?
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (int(project_id),),
                )
            ]

    @staticmethod
    def get_conversation(project_id: int, conversation_id: int) -> dict[str, Any] | None:
        with connection_scope() as connection:
            row = connection.execute(
                """
                SELECT * FROM ai_search_conversations
                WHERE id = ? AND project_id = ?
                """,
                (int(conversation_id), int(project_id)),
            ).fetchone()
            return dict(row) if row is not None else None

    @staticmethod
    def append_message(
        conversation_id: int,
        role: str,
        content: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        references: list[dict[str, Any]] | None = None,
    ) -> int:
        if role not in {"user", "assistant"}:
            raise ValueError("Unsupported search message role.")
        with transaction() as connection:
            conversation = connection.execute(
                "SELECT * FROM ai_search_conversations WHERE id = ?",
                (int(conversation_id),),
            ).fetchone()
            if conversation is None:
                raise ValueError("The AI Search conversation no longer exists.")
            cursor = connection.execute(
                """
                INSERT INTO ai_search_messages (
                    conversation_id, role, content, provider, model, references_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(conversation_id), role, str(content), provider, model,
                    json.dumps(references or [], ensure_ascii=False),
                ),
            )
            if role == "user" and str(conversation["title"]) == "New Search":
                connection.execute(
                    "UPDATE ai_search_conversations SET title = ? WHERE id = ?",
                    (_title(content), int(conversation_id)),
                )
            connection.execute(
                """
                UPDATE ai_search_conversations
                SET updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                """,
                (int(conversation_id),),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def list_messages(conversation_id: int) -> list[dict[str, Any]]:
        with connection_scope() as connection:
            values: list[dict[str, Any]] = []
            for row in connection.execute(
                "SELECT * FROM ai_search_messages WHERE conversation_id = ? ORDER BY id",
                (int(conversation_id),),
            ):
                value = dict(row)
                try:
                    value["references"] = json.loads(value["references_json"] or "[]")
                except (TypeError, ValueError):
                    value["references"] = []
                values.append(value)
            return values

    @staticmethod
    def delete_conversation(project_id: int, conversation_id: int) -> bool:
        with transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM ai_search_conversations
                WHERE id = ? AND project_id = ?
                """,
                (int(conversation_id), int(project_id)),
            )
            return cursor.rowcount > 0
