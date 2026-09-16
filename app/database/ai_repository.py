from __future__ import annotations

from typing import Any

from app.database.database import connection_scope, transaction


def _chat_title(content: str, limit: int = 60) -> str:
    title = " ".join(content.split()).strip()
    if not title:
        return "New Chat"
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


class AIRepository:
    """Solo/shared chats, canonical messages, membership and engine state."""

    @staticmethod
    def _members(
        connection: Any,
        conversation_id: int,
        *,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        active_clause = "AND member.is_active = 1" if active_only else ""
        return [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT paper.*, member.alias_index, member.is_active
                FROM ai_conversation_members AS member
                JOIN papers AS paper ON paper.id = member.paper_id
                WHERE member.conversation_id = ? {active_clause}
                ORDER BY member.alias_index
                """,
                (int(conversation_id),),
            )
        ]

    @staticmethod
    def _conversation_value(connection: Any, row: Any) -> dict[str, Any]:
        value = dict(row)
        value["members"] = AIRepository._members(
            connection, int(value["id"]), active_only=True
        )
        value["all_members"] = AIRepository._members(
            connection, int(value["id"])
        )
        return value

    @staticmethod
    def _has_table(connection: Any, name: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone() is not None

    @staticmethod
    def _has_column(connection: Any, table: str, column: str) -> bool:
        return column in {
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
        }

    @staticmethod
    def _project_for_paper(connection: Any, paper_id: int) -> int | None:
        if not AIRepository._has_table(connection, "project_papers"):
            return None
        row = connection.execute(
            """
            SELECT project_id FROM project_papers
            WHERE paper_id = ? ORDER BY project_id LIMIT 1
            """,
            (int(paper_id),),
        ).fetchone()
        if row is None:
            configured = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'current_project_id'"
            ).fetchone()
            project_id = int(configured["value"]) if configured is not None else int(
                connection.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()["id"]
            )
            paper = connection.execute(
                "SELECT status FROM papers WHERE id = ?", (int(paper_id),)
            ).fetchone()
            if paper is None:
                raise ValueError("The paper no longer exists.")
            connection.execute(
                """
                INSERT INTO project_papers (project_id, paper_id, status)
                VALUES (?, ?, ?)
                """,
                (project_id, int(paper_id), str(paper["status"] or "Unread")),
            )
            return project_id
        return int(row["project_id"])

    @staticmethod
    def create_conversation(
        paper_id: int,
        title: str = "New Chat",
        project_id: int | None = None,
    ) -> dict[str, Any]:
        with transaction() as connection:
            scope_id = int(project_id) if project_id is not None else AIRepository._project_for_paper(connection, paper_id)
            if scope_id is not None and connection.execute(
                "SELECT 1 FROM project_papers WHERE project_id = ? AND paper_id = ?",
                (scope_id, int(paper_id)),
            ).fetchone() is None:
                raise ValueError("The paper is not part of the selected project.")
            if AIRepository._has_column(connection, "ai_conversations", "project_id"):
                cursor = connection.execute(
                    "INSERT INTO ai_conversations (paper_id, title, project_id) VALUES (?, ?, ?)",
                    (int(paper_id), _chat_title(title), scope_id),
                )
            else:
                cursor = connection.execute(
                    "INSERT INTO ai_conversations (paper_id, title) VALUES (?, ?)",
                    (int(paper_id), _chat_title(title)),
                )
            row = connection.execute(
                "SELECT * FROM ai_conversations WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
            if row is None:
                raise RuntimeError("Could not create the AI chat.")
            connection.execute(
                """
                INSERT INTO ai_conversation_members (
                    conversation_id, paper_id, alias_index
                ) VALUES (?, ?, 1)
                """,
                (int(cursor.lastrowid), int(paper_id)),
            )
            return AIRepository._conversation_value(connection, row)

    @staticmethod
    def create_group_conversation(
        paper_ids: list[int],
        title: str = "New Chat",
        project_id: int | None = None,
    ) -> dict[str, Any]:
        members = list(dict.fromkeys(int(value) for value in paper_ids))
        if len(members) < 2:
            raise ValueError("A comparison chat needs at least two papers.")
        with transaction() as connection:
            scope_id = int(project_id) if project_id is not None else AIRepository._project_for_paper(connection, members[0])
            placeholders = ",".join("?" for _ in members)
            if scope_id is not None and AIRepository._has_table(connection, "project_papers"):
                if project_id is None:
                    connection.executemany(
                        """
                        INSERT INTO project_papers (project_id, paper_id, status)
                        SELECT ?, papers.id,
                               CASE WHEN papers.status IN ('Unread', 'Reading', 'Completed')
                                    THEN papers.status ELSE 'Unread' END
                        FROM papers WHERE papers.id = ?
                        ON CONFLICT(project_id, paper_id) DO NOTHING
                        """,
                        ((scope_id, paper_id) for paper_id in members),
                    )
                count = int(connection.execute(
                    f"""
                    SELECT COUNT(*) FROM project_papers
                    WHERE project_id = ? AND paper_id IN ({placeholders})
                    """,
                    [scope_id, *members],
                ).fetchone()[0])
            else:
                count = int(connection.execute(
                    f"SELECT COUNT(*) FROM papers WHERE id IN ({placeholders})", members
                ).fetchone()[0])
            if count != len(members):
                raise ValueError("One or more comparison papers no longer exist.")
            if AIRepository._has_column(connection, "ai_conversations", "project_id"):
                cursor = connection.execute(
                    """
                    INSERT INTO ai_conversations (
                        paper_id, title, conversation_type, project_id
                    ) VALUES (?, ?, 'group', ?)
                    """,
                    (members[0], _chat_title(title), scope_id),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO ai_conversations (paper_id, title, conversation_type)
                    VALUES (?, ?, 'group')
                    """,
                    (members[0], _chat_title(title)),
                )
            conversation_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO ai_conversation_members (
                    conversation_id, paper_id, alias_index
                ) VALUES (?, ?, ?)
                """,
                [
                    (conversation_id, paper_id, index)
                    for index, paper_id in enumerate(members, start=1)
                ],
            )
            row = connection.execute(
                "SELECT * FROM ai_conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Could not create the comparison chat.")
            return AIRepository._conversation_value(connection, row)

    @staticmethod
    def find_group_conversation(
        paper_ids: list[int], project_id: int | None = None
    ) -> dict[str, Any] | None:
        wanted = set(int(value) for value in paper_ids)
        if len(wanted) < 2:
            return None
        with connection_scope() as connection:
            scope_clause = "AND conversation.project_id = ?" if project_id is not None else ""
            candidates = connection.execute(
                """
                SELECT conversation.*
                FROM ai_conversations AS conversation
                WHERE conversation.conversation_type = 'group'
                %s
                ORDER BY conversation.updated_at DESC, conversation.id DESC
                """
                % scope_clause,
                (() if project_id is None else (int(project_id),)),
            ).fetchall()
            for row in candidates:
                value = AIRepository._conversation_value(connection, row)
                if {
                    int(member["id"]) for member in value["members"]
                } == wanted:
                    return value
            return None

    @staticmethod
    def add_group_member(
        conversation_id: int, paper_id: int
    ) -> list[dict[str, Any]]:
        with transaction() as connection:
            conversation = connection.execute(
                """
                SELECT * FROM ai_conversations
                WHERE id = ? AND conversation_type = 'group'
                """,
                (int(conversation_id),),
            ).fetchone()
            if conversation is None:
                raise ValueError("The comparison chat no longer exists.")
            if connection.execute(
                """
                SELECT 1 FROM project_papers
                WHERE project_id = ? AND paper_id = ?
                """,
                (int(conversation["project_id"]), int(paper_id)),
            ).fetchone() is None:
                raise ValueError("The comparison paper is not part of this project.")
            existing = connection.execute(
                """
                SELECT alias_index FROM ai_conversation_members
                WHERE conversation_id = ? AND paper_id = ?
                """,
                (int(conversation_id), int(paper_id)),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE ai_conversation_members SET is_active = 1
                    WHERE conversation_id = ? AND paper_id = ?
                    """,
                    (int(conversation_id), int(paper_id)),
                )
            else:
                next_alias = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(alias_index), 0) + 1
                        FROM ai_conversation_members
                        WHERE conversation_id = ?
                        """,
                        (int(conversation_id),),
                    ).fetchone()[0]
                )
                connection.execute(
                    """
                    INSERT INTO ai_conversation_members (
                        conversation_id, paper_id, alias_index
                    ) VALUES (?, ?, ?)
                    """,
                    (int(conversation_id), int(paper_id), next_alias),
                )
            return AIRepository._members(
                connection, int(conversation_id), active_only=True
            )

    @staticmethod
    def remove_group_member(
        conversation_id: int, paper_id: int
    ) -> list[dict[str, Any]]:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE ai_conversation_members SET is_active = 0
                WHERE conversation_id = ? AND paper_id = ?
                """,
                (int(conversation_id), int(paper_id)),
            )
            members = AIRepository._members(
                connection, int(conversation_id), active_only=True
            )
            if members:
                connection.execute(
                    "UPDATE ai_conversations SET paper_id = ? WHERE id = ?",
                    (int(members[0]["id"]), int(conversation_id)),
                )
            return members

    @staticmethod
    def list_group_members(
        conversation_id: int, *, active_only: bool = True
    ) -> list[dict[str, Any]]:
        with connection_scope() as connection:
            return AIRepository._members(
                connection, int(conversation_id), active_only=active_only
            )

    @staticmethod
    def list_conversations(
        paper_id: int, project_id: int | None = None
    ) -> list[dict[str, Any]]:
        with connection_scope() as connection:
            scope_clause = "AND conversation.project_id = ?" if project_id is not None else ""
            parameters: list[int] = [int(paper_id)]
            if project_id is not None:
                parameters.append(int(project_id))
            rows = connection.execute(
                f"""
                SELECT conversation.*
                FROM ai_conversations AS conversation
                JOIN ai_conversation_members AS member
                  ON member.conversation_id = conversation.id
                WHERE member.paper_id = ? AND member.is_active = 1
                {scope_clause}
                ORDER BY conversation.updated_at DESC, conversation.id DESC
                """,
                parameters,
            ).fetchall()
            return [
                AIRepository._conversation_value(connection, row)
                for row in rows
            ]

    @staticmethod
    def get_conversation(
        conversation_id: int,
        paper_id: int | None = None,
        project_id: int | None = None,
    ) -> dict[str, Any] | None:
        with connection_scope() as connection:
            if paper_id is None:
                scope_clause = " AND project_id = ?" if project_id is not None else ""
                parameters = [int(conversation_id)]
                if project_id is not None:
                    parameters.append(int(project_id))
                row = connection.execute(
                    f"SELECT * FROM ai_conversations WHERE id = ?{scope_clause}",
                    parameters,
                ).fetchone()
            else:
                scope_clause = "AND conversation.project_id = ?" if project_id is not None else ""
                parameters = [int(conversation_id), int(paper_id)]
                if project_id is not None:
                    parameters.append(int(project_id))
                row = connection.execute(
                    f"""
                    SELECT conversation.*
                    FROM ai_conversations AS conversation
                    JOIN ai_conversation_members AS member
                      ON member.conversation_id = conversation.id
                    WHERE conversation.id = ? AND member.paper_id = ?
                      AND member.is_active = 1
                      {scope_clause}
                    """,
                    parameters,
                ).fetchone()
            return (
                AIRepository._conversation_value(connection, row)
                if row is not None
                else None
            )

    @staticmethod
    def delete_conversation(conversation_id: int, paper_id: int) -> bool:
        with transaction() as connection:
            allowed = connection.execute(
                """
                SELECT 1 FROM ai_conversation_members
                WHERE conversation_id = ? AND paper_id = ? AND is_active = 1
                """,
                (int(conversation_id), int(paper_id)),
            ).fetchone()
            if allowed is None:
                return False
            cursor = connection.execute(
                """
                DELETE FROM ai_conversations
                WHERE id = ?
                """,
                (int(conversation_id),),
            )
            return cursor.rowcount > 0

    @staticmethod
    def list_messages(conversation_id: int) -> list[dict[str, Any]]:
        with connection_scope() as connection:
            messages = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM ai_messages
                    WHERE conversation_id = ?
                    ORDER BY id
                    """,
                    (int(conversation_id),),
                )
            ]
            if not messages:
                return messages
            ids = [int(message["id"]) for message in messages]
            placeholders = ",".join("?" for _ in ids)
            citation_rows = connection.execute(
                f"""
                SELECT citation.*,
                       message.conversation_id AS conversation_id,
                       CASE
                           WHEN conversation.conversation_type = 'group'
                           THEN 'P' || member.alias_index
                       END AS alias
                FROM ai_message_citations AS citation
                JOIN ai_messages AS message
                  ON message.id = citation.message_id
                JOIN ai_conversations AS conversation
                  ON conversation.id = message.conversation_id
                LEFT JOIN ai_conversation_members AS member
                  ON member.conversation_id = message.conversation_id
                 AND member.paper_id = citation.paper_id
                WHERE citation.message_id IN ({placeholders})
                ORDER BY citation.message_id, citation.id
                """,
                ids,
            ).fetchall()
            by_message: dict[int, list[dict[str, Any]]] = {}
            for row in citation_rows:
                value = dict(row)
                value["verified"] = bool(value["verified"])
                by_message.setdefault(int(value["message_id"]), []).append(value)
            for message in messages:
                message["citations"] = by_message.get(int(message["id"]), [])
            return messages

    @staticmethod
    def save_message_result(
        message_id: int,
        paper_id: int,
        support_status: str,
        citations: list[dict[str, Any]],
        metadata_valid: bool = True,
    ) -> None:
        if support_status not in {"supported", "partially_supported", "not_found"}:
            raise ValueError("Unsupported AI support status.")
        with transaction() as connection:
            owner = connection.execute(
                """
                SELECT message.id, message.conversation_id,
                       conversation.conversation_type
                FROM ai_messages AS message
                JOIN ai_conversations AS conversation
                  ON conversation.id = message.conversation_id
                WHERE message.id = ? AND message.role = 'assistant'
                  AND EXISTS (
                      SELECT 1 FROM ai_conversation_members AS member
                      WHERE member.conversation_id = conversation.id
                        AND member.paper_id = ? AND member.is_active = 1
                  )
                """,
                (int(message_id), int(paper_id)),
            ).fetchone()
            if owner is None:
                raise ValueError("The AI message does not belong to this paper.")
            valid_paper_ids = {
                int(row["paper_id"])
                for row in connection.execute(
                    """
                    SELECT paper_id
                    FROM ai_conversation_members
                    WHERE conversation_id = ? AND is_active = 1
                    """,
                    (int(owner["conversation_id"]),),
                )
            }
            connection.execute(
                """
                UPDATE ai_messages
                SET support_status = ?, metadata_valid = ?
                WHERE id = ?
                """,
                (support_status, int(bool(metadata_valid)), int(message_id)),
            )
            connection.execute(
                "DELETE FROM ai_message_citations WHERE message_id = ?",
                (int(message_id),),
            )
            for citation in citations:
                evidence = str(citation.get("evidence") or "").strip()
                if not evidence:
                    continue
                try:
                    citation_paper_id = int(
                        citation.get("paper_id") or paper_id
                    )
                except (TypeError, ValueError):
                    continue
                if citation_paper_id not in valid_paper_ids:
                    # Provider metadata must never create a citation identity
                    # outside the persisted conversation membership.
                    continue
                connection.execute(
                    """
                    INSERT INTO ai_message_citations (
                        message_id, paper_id, page_hint, resolved_page,
                        section, evidence, verified, verification_status,
                        claim_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(message_id),
                        citation_paper_id,
                        citation.get("page_hint"),
                        citation.get("resolved_page"),
                        str(citation.get("section") or "").strip() or None,
                        evidence,
                        int(bool(citation.get("verified"))),
                        str(citation.get("verification_status") or "unverified"),
                        str(citation.get("claim_text") or "").strip(),
                    ),
                )

    @staticmethod
    def append_message(
        conversation_id: int,
        role: str,
        content: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        selected_text: str | None = None,
        selected_page: int | None = None,
    ) -> int:
        with transaction() as connection:
            conversation = connection.execute(
                "SELECT id, title FROM ai_conversations WHERE id = ?",
                (int(conversation_id),),
            ).fetchone()
            if conversation is None:
                raise ValueError("The selected AI chat no longer exists.")
            cursor = connection.execute(
                """
                INSERT INTO ai_messages (
                    conversation_id, role, content, provider, model,
                    selected_text, selected_page
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(conversation_id),
                    role,
                    content,
                    provider or None,
                    model or None,
                    selected_text or None,
                    selected_page,
                ),
            )
            if role == "user" and str(conversation["title"]) == "New Chat":
                first_user_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM ai_messages
                        WHERE conversation_id = ? AND role = 'user'
                        """,
                        (int(conversation_id),),
                    ).fetchone()[0]
                )
                if first_user_count == 1:
                    connection.execute(
                        "UPDATE ai_conversations SET title = ? WHERE id = ?",
                        (_chat_title(content), int(conversation_id)),
                    )
            connection.execute(
                """
                UPDATE ai_conversations
                SET updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                """,
                (int(conversation_id),),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def delete_message(message_id: int, conversation_id: int) -> bool:
        """Delete one late/cancelled assistant response without touching the chat."""
        with transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM ai_messages
                WHERE id = ? AND conversation_id = ? AND role = 'assistant'
                """,
                (int(message_id), int(conversation_id)),
            )
            return cursor.rowcount > 0

    @staticmethod
    def update_assistant_message(
        message_id: int,
        conversation_id: int,
        content: str,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> bool:
        with transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE ai_messages
                SET content = ?,
                    provider = COALESCE(?, provider),
                    model = COALESCE(?, model)
                WHERE id = ? AND conversation_id = ? AND role = 'assistant'
                """,
                (
                    content,
                    provider or None,
                    model or None,
                    int(message_id),
                    int(conversation_id),
                ),
            )
            return cursor.rowcount > 0

    @staticmethod
    def update_memory(
        conversation_id: int,
        rolling_summary: str | None,
        memory_through_message_id: int | None,
    ) -> None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE ai_conversations
                SET rolling_summary = ?, memory_through_message_id = ?
                WHERE id = ?
                """,
                (
                    rolling_summary or None,
                    memory_through_message_id,
                    int(conversation_id),
                ),
            )

    @staticmethod
    def get_remote_state(
        conversation_id: int,
        provider: str,
        model: str,
    ) -> str | None:
        with connection_scope() as connection:
            row = connection.execute(
                """
                SELECT remote_state_id FROM ai_remote_states
                WHERE conversation_id = ? AND provider = ? AND model = ?
                """,
                (int(conversation_id), provider, model),
            ).fetchone()
            return str(row["remote_state_id"]) if row is not None else None

    @staticmethod
    def set_remote_state(
        conversation_id: int,
        provider: str,
        model: str,
        remote_state_id: str | None,
    ) -> None:
        with transaction() as connection:
            if not remote_state_id:
                connection.execute(
                    """
                    DELETE FROM ai_remote_states
                    WHERE conversation_id = ? AND provider = ? AND model = ?
                    """,
                    (int(conversation_id), provider, model),
                )
                return
            connection.execute(
                """
                INSERT INTO ai_remote_states (
                    conversation_id, provider, model, remote_state_id
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id, provider, model) DO UPDATE SET
                    remote_state_id = excluded.remote_state_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (int(conversation_id), provider, model, remote_state_id),
            )

    @staticmethod
    def delete_remote_state_if_matches(
        conversation_id: int,
        provider: str,
        model: str,
        remote_state_id: str,
    ) -> bool:
        with transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM ai_remote_states
                WHERE conversation_id = ? AND provider = ? AND model = ?
                  AND remote_state_id = ?
                """,
                (
                    int(conversation_id),
                    provider,
                    model,
                    remote_state_id,
                ),
            )
            return cursor.rowcount > 0

    @staticmethod
    def get_document_ref(paper_id: int, provider: str) -> dict[str, Any] | None:
        with connection_scope() as connection:
            row = connection.execute(
                """
                SELECT * FROM ai_document_refs
                WHERE paper_id = ? AND provider = ?
                """,
                (int(paper_id), provider),
            ).fetchone()
            return dict(row) if row is not None else None

    @staticmethod
    def upsert_document_ref(
        paper_id: int,
        provider: str,
        source_file_hash: str,
        remote_file_id: str,
        *,
        remote_uri: str | None,
        mime_type: str,
        expires_at: str | None,
    ) -> None:
        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO ai_document_refs (
                    paper_id, provider, source_file_hash, remote_file_id,
                    remote_uri, mime_type, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(paper_id, provider) DO UPDATE SET
                    source_file_hash = excluded.source_file_hash,
                    remote_file_id = excluded.remote_file_id,
                    remote_uri = excluded.remote_uri,
                    mime_type = excluded.mime_type,
                    expires_at = excluded.expires_at,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    int(paper_id),
                    provider,
                    source_file_hash,
                    remote_file_id,
                    remote_uri,
                    mime_type,
                    expires_at,
                ),
            )

    @staticmethod
    def delete_document_ref(paper_id: int, provider: str) -> None:
        with transaction() as connection:
            connection.execute(
                "DELETE FROM ai_document_refs WHERE paper_id = ? AND provider = ?",
                (int(paper_id), provider),
            )
