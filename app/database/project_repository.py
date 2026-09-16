from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from app.database.database import connection_scope, transaction
from app.database.paper_repository import PAPER_STATUSES


class ProjectRepository:
    """Project membership, reading activity, and per-project workspace state."""

    @staticmethod
    def list_all() -> list[dict[str, Any]]:
        with connection_scope() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM projects ORDER BY name COLLATE NOCASE, id"
                )
            ]

    @staticmethod
    def get(project_id: int) -> dict[str, Any] | None:
        with connection_scope() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (int(project_id),)
            ).fetchone()
            return dict(row) if row is not None else None

    @staticmethod
    def create(name: str) -> dict[str, Any]:
        normalized = " ".join(str(name).split()).strip()
        if not normalized:
            raise ValueError("Project name cannot be empty.")
        with transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO projects (name) VALUES (?)", (normalized,)
            )
            project_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO project_workspaces (project_id) VALUES (?)",
                (project_id,),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
            )

    @staticmethod
    def rename(project_id: int, name: str) -> bool:
        normalized = " ".join(str(name).split()).strip()
        if not normalized:
            raise ValueError("Project name cannot be empty.")
        with transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE projects SET name = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (normalized, int(project_id)),
            )
            return cursor.rowcount > 0

    @staticmethod
    def delete(project_id: int) -> bool:
        with transaction() as connection:
            if int(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]) <= 1:
                raise ValueError("At least one project must remain.")
            connection.execute(
                "DELETE FROM ai_conversations WHERE project_id = ?",
                (int(project_id),),
            )
            cursor = connection.execute(
                "DELETE FROM projects WHERE id = ?", (int(project_id),)
            )
            return cursor.rowcount > 0

    @staticmethod
    def contains_paper(project_id: int, paper_id: int) -> bool:
        with connection_scope() as connection:
            return connection.execute(
                """
                SELECT 1 FROM project_papers
                WHERE project_id = ? AND paper_id = ?
                """,
                (int(project_id), int(paper_id)),
            ).fetchone() is not None

    @staticmethod
    def add_paper(
        project_id: int, paper_id: int, *, status: str = "Unread"
    ) -> None:
        if status not in PAPER_STATUSES:
            raise ValueError(f"Unsupported paper status: {status}")
        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO project_papers (project_id, paper_id, status)
                VALUES (?, ?, ?)
                ON CONFLICT(project_id, paper_id) DO NOTHING
                """,
                (int(project_id), int(paper_id), status),
            )

    @staticmethod
    def remove_paper(project_id: int, paper_id: int) -> bool:
        with transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM project_papers WHERE project_id = ? AND paper_id = ?",
                (int(project_id), int(paper_id)),
            )
            return cursor.rowcount > 0

    @staticmethod
    def status(project_id: int, paper_id: int) -> str | None:
        with connection_scope() as connection:
            row = connection.execute(
                """
                SELECT status FROM project_papers
                WHERE project_id = ? AND paper_id = ?
                """,
                (int(project_id), int(paper_id)),
            ).fetchone()
            return str(row["status"]) if row is not None else None

    @staticmethod
    def set_status(project_id: int, paper_id: int, status: str) -> bool:
        if status not in PAPER_STATUSES:
            raise ValueError(f"Unsupported paper status: {status}")
        with transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE project_papers
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE project_id = ? AND paper_id = ?
                """,
                (status, int(project_id), int(paper_id)),
            )
            return cursor.rowcount > 0

    @staticmethod
    def record_opened(project_id: int, paper_id: int) -> None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE project_papers
                SET last_opened = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE project_id = ? AND paper_id = ?
                """,
                (int(project_id), int(paper_id)),
            )

    @staticmethod
    def record_activity(
        project_id: int,
        paper_id: int,
        *,
        seconds: int = 0,
        interactions: int = 0,
        reading_seconds_threshold: int = 120,
        interaction_threshold: int = 3,
    ) -> dict[str, Any] | None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE project_papers
                SET active_reading_seconds = active_reading_seconds + ?,
                    interaction_count = interaction_count + ?,
                    last_interaction_at = CASE WHEN ? > 0 THEN CURRENT_TIMESTAMP
                                               ELSE last_interaction_at END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE project_id = ? AND paper_id = ?
                """,
                (
                    max(0, int(seconds)),
                    max(0, int(interactions)),
                    max(0, int(interactions)),
                    int(project_id),
                    int(paper_id),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM project_papers
                WHERE project_id = ? AND paper_id = ?
                """,
                (int(project_id), int(paper_id)),
            ).fetchone()
            if row is None:
                return None
            transitioned = (
                str(row["status"]) == "Unread"
                and int(row["active_reading_seconds"]) >= int(reading_seconds_threshold)
                and int(row["interaction_count"]) >= int(interaction_threshold)
            )
            if transitioned:
                connection.execute(
                    """
                    UPDATE project_papers
                    SET status = 'Reading', updated_at = CURRENT_TIMESTAMP
                    WHERE project_id = ? AND paper_id = ?
                    """,
                    (int(project_id), int(paper_id)),
                )
            value = dict(row)
            value["transitioned_to_reading"] = transitioned
            if transitioned:
                value["status"] = "Reading"
            return value

    @staticmethod
    def mark_ai_engagement(project_id: int, paper_id: int) -> bool:
        """Return True only for the Unread -> Reading transition."""
        with transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE project_papers
                SET status = 'Reading', updated_at = CURRENT_TIMESTAMP
                WHERE project_id = ? AND paper_id = ? AND status = 'Unread'
                """,
                (int(project_id), int(paper_id)),
            )
            return cursor.rowcount > 0

    @staticmethod
    def save_workspace(
        project_id: int,
        paper_ids: Iterable[int],
        active_paper_id: int | None,
    ) -> None:
        ordered = list(dict.fromkeys(int(value) for value in paper_ids))
        active = int(active_paper_id) if active_paper_id in ordered else None
        with transaction() as connection:
            valid = {
                int(row["paper_id"])
                for row in connection.execute(
                    "SELECT paper_id FROM project_papers WHERE project_id = ?",
                    (int(project_id),),
                )
            }
            ordered = [paper_id for paper_id in ordered if paper_id in valid]
            active = active if active in valid else None
            connection.execute(
                """
                INSERT INTO project_workspaces (
                    project_id, open_paper_ids_json, active_paper_id
                ) VALUES (?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    open_paper_ids_json = excluded.open_paper_ids_json,
                    active_paper_id = excluded.active_paper_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (int(project_id), json.dumps(ordered), active),
            )

    @staticmethod
    def load_workspace(project_id: int) -> tuple[list[int], int | None]:
        with connection_scope() as connection:
            row = connection.execute(
                """
                SELECT * FROM project_workspaces WHERE project_id = ?
                """,
                (int(project_id),),
            ).fetchone()
            if row is None:
                return [], None
            try:
                raw = json.loads(str(row["open_paper_ids_json"] or "[]"))
            except (TypeError, ValueError):
                raw = []
            valid = {
                int(item["paper_id"])
                for item in connection.execute(
                    "SELECT paper_id FROM project_papers WHERE project_id = ?",
                    (int(project_id),),
                )
            }
            papers = [
                int(value) for value in raw
                if isinstance(value, int) and int(value) in valid
            ]
            active = row["active_paper_id"]
            active_id = int(active) if active is not None and int(active) in papers else None
            return papers, active_id
