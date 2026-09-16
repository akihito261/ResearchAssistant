from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable


LATEST_SCHEMA_VERSION = 16

Migration = Callable[[sqlite3.Connection], None]


_SUMMARY_FIELDS = (
    ("problem", "Problem"),
    ("contribution", "Contribution"),
    ("method", "Method"),
    ("dataset", "Dataset"),
    ("baseline", "Baseline"),
    ("results", "Results"),
    ("limitations", "Limitations"),
    ("unclear_points", "Unclear points"),
    ("ideas", "Ideas"),
)


class UnsupportedDatabaseVersionError(RuntimeError):
    """Raised when a database was created by a newer application version."""


def _execute_statements(
    connection: sqlite3.Connection,
    statements: tuple[str, ...],
) -> None:
    for statement in statements:
        connection.execute(statement)


def _has_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: set[str],
) -> bool:
    available = {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }
    return columns.issubset(available)


def _has_table(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _upsert_search_document(
    connection: sqlite3.Connection,
    *,
    source_key: str,
    paper_id: int,
    source_type: str,
    source_id: int,
    title: str,
    content: str,
    page_number: int | None = None,
) -> None:
    """Populate FTS through the same external-content trigger path as runtime writes."""
    connection.execute(
        """
        INSERT INTO search_documents (
            source_key,
            paper_id,
            source_type,
            source_id,
            page_number,
            title,
            content
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            paper_id = excluded.paper_id,
            source_type = excluded.source_type,
            source_id = excluded.source_id,
            page_number = excluded.page_number,
            title = excluded.title,
            content = excluded.content,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            source_key,
            paper_id,
            source_type,
            source_id,
            page_number,
            title,
            content,
        ),
    )


def _backfill_structured_search_documents(
    connection: sqlite3.Connection,
) -> None:
    """Make pre-v3 structured content searchable immediately after migration."""
    # A few legacy/test databases only contain the original `papers.id`
    # contract. Keep migrations compatible with those minimal schemas; the
    # full application base schema always has these metadata columns.
    if not _has_columns(
        connection,
        "papers",
        {"id", "title", "authors", "year", "doi", "status", "is_important"},
    ):
        return

    papers = list(
        connection.execute(
            """
            SELECT id, title, authors, year, doi, status, is_important
            FROM papers
            ORDER BY id
            """
        )
    )
    for paper_id, title, authors, year, doi, status, is_important in papers:
        tags = ", ".join(
            str(row[0])
            for row in connection.execute(
                """
                SELECT tags.name
                FROM tags
                JOIN paper_tags ON paper_tags.tag_id = tags.id
                WHERE paper_tags.paper_id = ?
                ORDER BY tags.name COLLATE NOCASE
                """,
                (paper_id,),
            )
        )
        content = "\n".join(
            (
                f"Authors: {authors or ''}",
                f"Year: {year or ''}",
                f"DOI: {doi or ''}",
                f"Tags: {tags}",
                f"Status: {status or ''}",
                f"Important: {'yes' if is_important else 'no'}",
            )
        )
        _upsert_search_document(
            connection,
            source_key=f"metadata:{paper_id}",
            paper_id=int(paper_id),
            source_type="metadata",
            source_id=int(paper_id),
            title=str(title),
            content=content,
        )

    for row in connection.execute(
        """
        SELECT
            notes.id,
            notes.paper_id,
            notes.title,
            notes.content,
            notes.source_text,
            notes.page_number,
            papers.title
        FROM notes
        JOIN papers ON papers.id = notes.paper_id
        ORDER BY notes.id
        """
    ):
        (
            note_id,
            paper_id,
            note_title,
            note_content,
            source_text,
            page_number,
            paper_title,
        ) = row
        parts = []
        if source_text:
            parts.append(f"Source:\n{source_text}")
        if note_content:
            parts.append(str(note_content))
        _upsert_search_document(
            connection,
            source_key=f"note:{note_id}",
            paper_id=int(paper_id),
            source_type="note",
            source_id=int(note_id),
            page_number=page_number,
            title=str(note_title or f"{paper_title} \u2014 Note"),
            content="\n\n".join(parts),
        )

    for highlight_id, paper_id, selected_text, page_number, paper_title in connection.execute(
        """
        SELECT
            highlights.id,
            highlights.paper_id,
            highlights.selected_text,
            highlights.page_number,
            papers.title
        FROM highlights
        JOIN papers ON papers.id = highlights.paper_id
        ORDER BY highlights.id
        """
    ):
        _upsert_search_document(
            connection,
            source_key=f"highlight:{highlight_id}",
            paper_id=int(paper_id),
            source_type="highlight",
            source_id=int(highlight_id),
            page_number=int(page_number),
            title=f"{paper_title} \u2014 Highlight",
            content=str(selected_text),
        )

    summary_columns = ", ".join(
        f"paper_summaries.{field}" for field, _label in _SUMMARY_FIELDS
    )
    summary_rows = connection.execute(
        f"""
        SELECT
            paper_summaries.paper_id,
            papers.title,
            {summary_columns}
        FROM paper_summaries
        JOIN papers ON papers.id = paper_summaries.paper_id
        ORDER BY paper_summaries.paper_id
        """
    )
    for row in summary_rows:
        paper_id, paper_title, *values = row
        content = "\n\n".join(
            f"{label}:\n{value}"
            for (_field, label), value in zip(_SUMMARY_FIELDS, values)
            if value
        )
        _upsert_search_document(
            connection,
            source_key=f"summary:{paper_id}",
            paper_id=int(paper_id),
            source_type="summary",
            source_id=int(paper_id),
            title=f"{paper_title} \u2014 Summary",
            content=content,
        )


def _migration_1(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_notes (
            paper_id INTEGER PRIMARY KEY,
            content TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (paper_id)
                REFERENCES papers(id)
                ON DELETE CASCADE
        )
        """
    )


def _migration_2(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id INTEGER NOT NULL,
                title TEXT,
                content TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'manual',
                source_text TEXT,
                page_number INTEGER,
                location_data TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paper_id)
                    REFERENCES papers(id)
                    ON DELETE CASCADE,
                CHECK (kind IN ('scratchpad', 'manual', 'selection', 'translation')),
                CHECK (page_number IS NULL OR page_number >= 1)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_notes_paper_updated
            ON notes(paper_id, updated_at DESC)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_notes_one_scratchpad
            ON notes(paper_id)
            WHERE kind = 'scratchpad'
            """,
            """
            CREATE TABLE IF NOT EXISTS paper_summaries (
                paper_id INTEGER PRIMARY KEY,
                problem TEXT NOT NULL DEFAULT '',
                contribution TEXT NOT NULL DEFAULT '',
                method TEXT NOT NULL DEFAULT '',
                dataset TEXT NOT NULL DEFAULT '',
                baseline TEXT NOT NULL DEFAULT '',
                results TEXT NOT NULL DEFAULT '',
                limitations TEXT NOT NULL DEFAULT '',
                unclear_points TEXT NOT NULL DEFAULT '',
                ideas TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paper_id)
                    REFERENCES papers(id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS highlights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id INTEGER NOT NULL,
                selected_text TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                location_data TEXT NOT NULL,
                anchor_version INTEGER NOT NULL DEFAULT 1,
                color TEXT NOT NULL,
                note_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paper_id)
                    REFERENCES papers(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (note_id)
                    REFERENCES notes(id)
                    ON DELETE SET NULL,
                CHECK (page_number >= 1),
                CHECK (color IN ('yellow', 'blue', 'green', 'red', 'orange', 'purple'))
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_highlights_paper_page
            ON highlights(paper_id, page_number)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_highlights_note
            ON highlights(note_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ),
    )

    # Version 1 stored one scratchpad per paper. Keep that table as a
    # rollback aid, and copy every row exactly once into the generalized model.
    connection.execute(
        """
        INSERT OR IGNORE INTO notes (
            paper_id,
            content,
            kind,
            created_at,
            updated_at
        )
        SELECT
            paper_id,
            content,
            'scratchpad',
            created_at,
            updated_at
        FROM paper_notes
        """
    )

    highlight_meanings = json.dumps(
        {
            "yellow": "Important",
            "blue": "Method",
            "green": "Result",
            "red": "Limitation / Problem",
            "orange": "Unclear / Question",
            "purple": "Personal Idea",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    connection.executemany(
        """
        INSERT OR IGNORE INTO app_settings (key, value)
        VALUES (?, ?)
        """,
        (
            ("backup_path", ""),
            ("translation_target", "vi"),
            ("theme", "system"),
            ("highlight_meanings", highlight_meanings),
        ),
    )


def _migration_3(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE IF NOT EXISTS search_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                paper_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                page_number INTEGER,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paper_id)
                    REFERENCES papers(id)
                    ON DELETE CASCADE,
                CHECK (source_type IN ('metadata', 'pdf', 'note', 'highlight', 'summary')),
                CHECK (page_number IS NULL OR page_number >= 1)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_search_documents_paper_type
            ON search_documents(paper_id, source_type)
            """,
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                title,
                content,
                content='search_documents',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS search_documents_ai
            AFTER INSERT ON search_documents BEGIN
                INSERT INTO search_fts(rowid, title, content)
                VALUES (new.id, new.title, new.content);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS search_documents_ad
            AFTER DELETE ON search_documents BEGIN
                INSERT INTO search_fts(search_fts, rowid, title, content)
                VALUES ('delete', old.id, old.title, old.content);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS search_documents_au
            AFTER UPDATE ON search_documents BEGIN
                INSERT INTO search_fts(search_fts, rowid, title, content)
                VALUES ('delete', old.id, old.title, old.content);
                INSERT INTO search_fts(rowid, title, content)
                VALUES (new.id, new.title, new.content);
            END
            """,
            """
            CREATE TABLE IF NOT EXISTS paper_index_state (
                paper_id INTEGER PRIMARY KEY,
                indexed_file_hash TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                indexed_at TEXT,
                error TEXT,
                FOREIGN KEY (paper_id)
                    REFERENCES papers(id)
                    ON DELETE CASCADE,
                CHECK (status IN ('pending', 'indexing', 'ready', 'error'))
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_paper_index_state_status
            ON paper_index_state(status)
            """,
        ),
    )

    conditional_indexes = (
        (
            "papers",
            {"status", "created_at"},
            "CREATE INDEX IF NOT EXISTS idx_papers_status_created "
            "ON papers(status, created_at DESC)",
        ),
        (
            "papers",
            {"is_important", "created_at"},
            "CREATE INDEX IF NOT EXISTS idx_papers_important_created "
            "ON papers(is_important, created_at DESC)",
        ),
        (
            "papers",
            {"year"},
            "CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year)",
        ),
        (
            "papers",
            {"updated_at"},
            "CREATE INDEX IF NOT EXISTS idx_papers_updated "
            "ON papers(updated_at DESC)",
        ),
        (
            "papers",
            {"doi"},
            "CREATE INDEX IF NOT EXISTS idx_papers_doi_normalized "
            "ON papers(lower(trim(doi))) "
            "WHERE doi IS NOT NULL AND trim(doi) <> ''",
        ),
        (
            "paper_tags",
            {"tag_id", "paper_id"},
            "CREATE INDEX IF NOT EXISTS idx_paper_tags_tag_paper "
            "ON paper_tags(tag_id, paper_id)",
        ),
        (
            "paper_collections",
            {"collection_id", "paper_id"},
            "CREATE INDEX IF NOT EXISTS idx_paper_collections_collection_paper "
            "ON paper_collections(collection_id, paper_id)",
        ),
    )
    for table, columns, statement in conditional_indexes:
        if _has_columns(connection, table, columns):
            connection.execute(statement)

    _backfill_structured_search_documents(connection)

    connection.execute(
        """
        INSERT OR IGNORE INTO paper_index_state (paper_id, status)
        SELECT id, 'pending'
        FROM papers
        """
    )


def _migration_4(connection: sqlite3.Connection) -> None:
    """Add cached document variants and per-document annotation anchors."""
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE IF NOT EXISTS paper_document_versions (
                paper_id INTEGER NOT NULL,
                version_code TEXT NOT NULL,
                language_code TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_hash TEXT,
                source_file_hash TEXT,
                provider TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (paper_id, version_code),
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                CHECK (version_code IN ('en', 'vi'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS annotation_anchors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id INTEGER NOT NULL,
                note_id INTEGER,
                highlight_id INTEGER,
                document_version TEXT NOT NULL,
                selected_text TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                location_data TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE,
                FOREIGN KEY (highlight_id) REFERENCES highlights(id) ON DELETE CASCADE,
                CHECK (document_version IN ('en', 'vi')),
                CHECK (page_number >= 1),
                CHECK ((note_id IS NOT NULL) != (highlight_id IS NOT NULL))
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_annotation_anchor_note_version
            ON annotation_anchors(note_id, document_version)
            WHERE note_id IS NOT NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_annotation_anchor_highlight_version
            ON annotation_anchors(highlight_id, document_version)
            WHERE highlight_id IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_annotation_anchors_paper_version
            ON annotation_anchors(paper_id, document_version)
            """,
        ),
    )

    if _has_columns(connection, "papers", {"id", "file_path", "file_hash"}):
        connection.execute(
            """
            INSERT OR IGNORE INTO paper_document_versions (
                paper_id, version_code, language_code, file_path,
                file_hash, source_file_hash, provider
            )
            SELECT id, 'en', 'en', file_path, file_hash, file_hash, 'original'
            FROM papers
            """
        )
    if _has_columns(
        connection,
        "notes",
        {"id", "paper_id", "source_text", "page_number", "location_data"},
    ):
        connection.execute(
            """
            INSERT OR IGNORE INTO annotation_anchors (
                paper_id, note_id, document_version, selected_text,
                page_number, location_data
            )
            SELECT paper_id, id, 'en', COALESCE(source_text, ''),
                   page_number, location_data
            FROM notes
            WHERE location_data IS NOT NULL AND page_number IS NOT NULL
            """
        )
    if _has_columns(
        connection,
        "highlights",
        {"id", "paper_id", "selected_text", "page_number", "location_data"},
    ):
        connection.execute(
            """
            INSERT OR IGNORE INTO annotation_anchors (
                paper_id, highlight_id, document_version, selected_text,
                page_number, location_data
            )
            SELECT paper_id, id, 'en', selected_text, page_number, location_data
            FROM highlights
            """
        )


def _migration_5(connection: sqlite3.Connection) -> None:
    """Make Notes and Highlights local to one document version."""
    has_notes = _has_columns(connection, "notes", {"id"})
    has_highlights = _has_columns(connection, "highlights", {"id"})
    has_anchors = _has_columns(
        connection,
        "annotation_anchors",
        {"note_id", "highlight_id", "document_version", "location_data"},
    )
    if has_notes and not _has_columns(connection, "notes", {"document_version"}):
        connection.execute(
            """
            ALTER TABLE notes
            ADD COLUMN document_version TEXT NOT NULL DEFAULT 'en'
                CHECK (document_version IN ('en', 'vi'))
            """
        )
    if has_highlights and not _has_columns(
        connection, "highlights", {"document_version"}
    ):
        connection.execute(
            """
            ALTER TABLE highlights
            ADD COLUMN document_version TEXT NOT NULL DEFAULT 'en'
                CHECK (document_version IN ('en', 'vi'))
            """
        )
    notes_can_convert = _has_columns(
        connection, "notes", {"id", "kind", "location_data", "document_version"}
    )
    highlights_can_convert = _has_columns(
        connection,
        "highlights",
        {"id", "location_data", "document_version"},
    )

    # A pre-v5 annotation could have a generated anchor in both versions. The
    # canonical row retained its creation location, so an exact location match
    # identifies a VI-origin annotation conservatively. Ambiguous legacy rows
    # remain EN, preserving the historical default.
    if notes_can_convert and has_anchors:
        connection.execute(
            """
            UPDATE notes
            SET document_version = 'vi'
            WHERE kind != 'scratchpad'
              AND EXISTS (
                  SELECT 1 FROM annotation_anchors AS anchor
                  WHERE anchor.note_id = notes.id
                    AND anchor.document_version = 'vi'
                    AND anchor.location_data = notes.location_data
              )
              AND NOT EXISTS (
                  SELECT 1 FROM annotation_anchors AS anchor
                  WHERE anchor.note_id = notes.id
                    AND anchor.document_version = 'en'
                    AND anchor.location_data = notes.location_data
              )
            """
        )
    if highlights_can_convert and has_anchors:
        connection.execute(
            """
            UPDATE highlights
            SET document_version = 'vi'
            WHERE EXISTS (
                  SELECT 1 FROM annotation_anchors AS anchor
                  WHERE anchor.highlight_id = highlights.id
                    AND anchor.document_version = 'vi'
                    AND anchor.location_data = highlights.location_data
              )
              AND NOT EXISTS (
                  SELECT 1 FROM annotation_anchors AS anchor
                  WHERE anchor.highlight_id = highlights.id
                    AND anchor.document_version = 'en'
                    AND anchor.location_data = highlights.location_data
              )
            """
        )

    # Retain only the anchor belonging to the annotation's owning document.
    # The logical Note/Highlight rows themselves are never removed.
    if notes_can_convert and has_anchors:
        connection.execute(
            """
            DELETE FROM annotation_anchors
            WHERE note_id IS NOT NULL
              AND document_version != (
                  SELECT notes.document_version
                  FROM notes
                  WHERE notes.id = annotation_anchors.note_id
              )
            """
        )
    if highlights_can_convert and has_anchors:
        connection.execute(
            """
            DELETE FROM annotation_anchors
            WHERE highlight_id IS NOT NULL
              AND document_version != (
                  SELECT highlights.document_version
                  FROM highlights
                  WHERE highlights.id = annotation_anchors.highlight_id
              )
            """
        )
    if _has_columns(
        connection, "notes", {"paper_id", "document_version", "updated_at"}
    ):
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_notes_paper_version_updated
            ON notes(paper_id, document_version, updated_at DESC)
            """
        )
    if _has_columns(
        connection,
        "highlights",
        {"paper_id", "document_version", "page_number"},
    ):
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_highlights_paper_version_page
            ON highlights(paper_id, document_version, page_number)
            """
        )


def _migration_6(connection: sqlite3.Connection) -> None:
    """Persist paper-scoped AI chats and reusable provider document handles."""
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE IF NOT EXISTS ai_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                remote_state_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                CHECK (provider IN ('gemini', 'openai')),
                UNIQUE (paper_id, provider, model)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ai_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                selected_text TEXT,
                selected_page INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id)
                    REFERENCES ai_conversations(id) ON DELETE CASCADE,
                CHECK (role IN ('user', 'assistant')),
                CHECK (selected_page IS NULL OR selected_page >= 1)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ai_document_refs (
                paper_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                source_file_hash TEXT NOT NULL,
                remote_file_id TEXT NOT NULL,
                remote_uri TEXT,
                mime_type TEXT NOT NULL DEFAULT 'application/pdf',
                expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (paper_id, provider),
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                CHECK (provider IN ('gemini', 'openai'))
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_ai_messages_conversation_created
            ON ai_messages(conversation_id, id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_ai_conversations_paper_updated
            ON ai_conversations(paper_id, updated_at DESC)
            """,
        ),
    )


def _migration_7(connection: sqlite3.Connection) -> None:
    """Extend AI conversation/document provider constraints safely."""
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE ai_conversations_v7 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                remote_state_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                CHECK (provider IN ('gemini', 'openai', 'claude', 'deepseek')),
                UNIQUE (paper_id, provider, model)
            )
            """,
            """
            CREATE TABLE ai_messages_v7 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                selected_text TEXT,
                selected_page INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id)
                    REFERENCES ai_conversations_v7(id) ON DELETE CASCADE,
                CHECK (role IN ('user', 'assistant')),
                CHECK (selected_page IS NULL OR selected_page >= 1)
            )
            """,
            """
            INSERT INTO ai_conversations_v7 (
                id, paper_id, provider, model, remote_state_id, created_at, updated_at
            )
            SELECT id, paper_id, provider, model, remote_state_id, created_at, updated_at
            FROM ai_conversations
            """,
            """
            INSERT INTO ai_messages_v7 (
                id, conversation_id, role, content,
                selected_text, selected_page, created_at
            )
            SELECT id, conversation_id, role, content,
                   selected_text, selected_page, created_at
            FROM ai_messages
            """,
            "DROP TABLE ai_messages",
            "DROP TABLE ai_conversations",
            "ALTER TABLE ai_conversations_v7 RENAME TO ai_conversations",
            "ALTER TABLE ai_messages_v7 RENAME TO ai_messages",
            """
            CREATE TABLE ai_document_refs_v7 (
                paper_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                source_file_hash TEXT NOT NULL,
                remote_file_id TEXT NOT NULL,
                remote_uri TEXT,
                mime_type TEXT NOT NULL DEFAULT 'application/pdf',
                expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (paper_id, provider),
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                CHECK (provider IN ('gemini', 'openai', 'claude', 'deepseek'))
            )
            """,
            """
            INSERT INTO ai_document_refs_v7 (
                paper_id, provider, source_file_hash, remote_file_id,
                remote_uri, mime_type, expires_at, created_at, updated_at
            )
            SELECT paper_id, provider, source_file_hash, remote_file_id,
                   remote_uri, mime_type, expires_at, created_at, updated_at
            FROM ai_document_refs
            """,
            "DROP TABLE ai_document_refs",
            "ALTER TABLE ai_document_refs_v7 RENAME TO ai_document_refs",
            """
            CREATE INDEX idx_ai_messages_conversation_created
            ON ai_messages(conversation_id, id)
            """,
            """
            CREATE INDEX idx_ai_conversations_paper_updated
            ON ai_conversations(paper_id, updated_at DESC)
            """,
        ),
    )


def _migration_8(connection: sqlite3.Connection) -> None:
    """Make chats paper-owned and keep engine identity on messages/state."""
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE ai_conversations_v8 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT 'New Chat',
                rolling_summary TEXT,
                memory_through_message_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE ai_messages_v8 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                selected_text TEXT,
                selected_page INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id)
                    REFERENCES ai_conversations_v8(id) ON DELETE CASCADE,
                CHECK (role IN ('user', 'assistant')),
                CHECK (provider IS NULL OR provider IN (
                    'gemini', 'openai', 'claude', 'deepseek'
                )),
                CHECK (selected_page IS NULL OR selected_page >= 1)
            )
            """,
            """
            CREATE TABLE ai_remote_states_v8 (
                conversation_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                remote_state_id TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (conversation_id, provider, model),
                FOREIGN KEY (conversation_id)
                    REFERENCES ai_conversations_v8(id) ON DELETE CASCADE,
                CHECK (provider IN ('gemini', 'openai', 'claude', 'deepseek'))
            )
            """,
            """
            INSERT INTO ai_conversations_v8 (
                id, paper_id, title, created_at, updated_at
            )
            SELECT conversation.id,
                   conversation.paper_id,
                   COALESCE(NULLIF(
                       (
                           SELECT CASE
                               WHEN length(trim(message.content)) > 60
                               THEN substr(trim(message.content), 1, 57) || '…'
                               ELSE trim(message.content)
                           END
                           FROM ai_messages AS message
                           WHERE message.conversation_id = conversation.id
                             AND message.role = 'user'
                           ORDER BY message.id
                           LIMIT 1
                       ),
                       ''),
                       'New Chat'
                   ),
                   conversation.created_at,
                   conversation.updated_at
            FROM ai_conversations AS conversation
            """,
            """
            INSERT INTO ai_messages_v8 (
                id, conversation_id, role, content, provider, model,
                selected_text, selected_page, created_at
            )
            SELECT message.id,
                   message.conversation_id,
                   message.role,
                   message.content,
                   CASE WHEN message.role = 'assistant'
                        THEN conversation.provider END,
                   CASE WHEN message.role = 'assistant'
                        THEN conversation.model END,
                   message.selected_text,
                   message.selected_page,
                   message.created_at
            FROM ai_messages AS message
            JOIN ai_conversations AS conversation
              ON conversation.id = message.conversation_id
            """,
            """
            INSERT INTO ai_remote_states_v8 (
                conversation_id, provider, model, remote_state_id, updated_at
            )
            SELECT id, provider, model, remote_state_id, updated_at
            FROM ai_conversations
            WHERE remote_state_id IS NOT NULL AND trim(remote_state_id) != ''
            """,
            "DROP TABLE ai_messages",
            "DROP TABLE ai_conversations",
            "ALTER TABLE ai_conversations_v8 RENAME TO ai_conversations",
            "ALTER TABLE ai_messages_v8 RENAME TO ai_messages",
            "ALTER TABLE ai_remote_states_v8 RENAME TO ai_remote_states",
            """
            CREATE INDEX idx_ai_messages_conversation_created
            ON ai_messages(conversation_id, id)
            """,
            """
            CREATE INDEX idx_ai_conversations_paper_updated
            ON ai_conversations(paper_id, updated_at DESC, id DESC)
            """,
        ),
    )


def _migration_9(connection: sqlite3.Connection) -> None:
    """Persist verified AI chat and per-field Summary citations."""
    _execute_statements(
        connection,
        (
            """
            ALTER TABLE ai_messages ADD COLUMN support_status TEXT
                CHECK (support_status IS NULL OR support_status IN (
                    'supported', 'partially_supported', 'not_found'
                ))
            """,
            """
            ALTER TABLE ai_messages ADD COLUMN metadata_valid INTEGER
                NOT NULL DEFAULT 1 CHECK (metadata_valid IN (0, 1))
            """,
            """
            CREATE TABLE ai_message_citations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                paper_id INTEGER NOT NULL,
                page_hint INTEGER,
                resolved_page INTEGER,
                section TEXT,
                evidence TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 0,
                verification_status TEXT NOT NULL DEFAULT 'unverified',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (message_id) REFERENCES ai_messages(id) ON DELETE CASCADE,
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                CHECK (page_hint IS NULL OR page_hint >= 1),
                CHECK (resolved_page IS NULL OR resolved_page >= 1),
                CHECK (verified IN (0, 1))
            )
            """,
            """
            CREATE INDEX idx_ai_message_citations_message
            ON ai_message_citations(message_id, id)
            """,
            """
            CREATE TABLE summary_field_statuses (
                paper_id INTEGER NOT NULL,
                field_name TEXT NOT NULL,
                support_status TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (paper_id, field_name),
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                CHECK (field_name IN (
                    'problem', 'contribution', 'method', 'dataset', 'baseline',
                    'results', 'limitations', 'unclear_points', 'ideas'
                )),
                CHECK (support_status IN (
                    'supported', 'partially_supported', 'not_found'
                ))
            )
            """,
            """
            CREATE TABLE summary_citations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id INTEGER NOT NULL,
                field_name TEXT NOT NULL,
                page_hint INTEGER,
                resolved_page INTEGER,
                section TEXT,
                evidence TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 0,
                verification_status TEXT NOT NULL DEFAULT 'unverified',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                CHECK (field_name IN (
                    'problem', 'contribution', 'method', 'dataset', 'baseline',
                    'results', 'limitations', 'unclear_points', 'ideas'
                )),
                CHECK (page_hint IS NULL OR page_hint >= 1),
                CHECK (resolved_page IS NULL OR resolved_page >= 1),
                CHECK (verified IN (0, 1))
            )
            """,
            """
            CREATE INDEX idx_summary_citations_paper_field
            ON summary_citations(paper_id, field_name, id)
            """,
        ),
    )


def _migration_10(connection: sqlite3.Connection) -> None:
    """Associate every AI source with the exact rendered claim it supports."""
    _execute_statements(
        connection,
        (
            "ALTER TABLE ai_message_citations ADD COLUMN claim_text TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE summary_citations ADD COLUMN claim_text TEXT NOT NULL DEFAULT ''",
        ),
    )


def _migration_11(connection: sqlite3.Connection) -> None:
    """Persist shared AI conversation membership and stable paper aliases."""
    _execute_statements(
        connection,
        (
            """
            ALTER TABLE ai_conversations ADD COLUMN conversation_type TEXT
                NOT NULL DEFAULT 'solo'
                CHECK (conversation_type IN ('solo', 'group'))
            """,
            """
            CREATE TABLE ai_conversation_members (
                conversation_id INTEGER NOT NULL,
                paper_id INTEGER NOT NULL,
                alias_index INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (conversation_id, paper_id),
                UNIQUE (conversation_id, alias_index),
                FOREIGN KEY (conversation_id)
                    REFERENCES ai_conversations(id) ON DELETE CASCADE,
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                CHECK (alias_index >= 1),
                CHECK (is_active IN (0, 1))
            )
            """,
            """
            INSERT INTO ai_conversation_members (
                conversation_id, paper_id, alias_index
            )
            SELECT id, paper_id, 1 FROM ai_conversations
            """,
            """
            CREATE INDEX idx_ai_conversation_members_paper
            ON ai_conversation_members(paper_id, conversation_id)
            """,
        ),
    )


def _migration_12(connection: sqlite3.Connection) -> None:
    """Allow ViLao as an independent persisted AI provider."""
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE ai_messages_v12 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                selected_text TEXT,
                selected_page INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                support_status TEXT CHECK (support_status IS NULL OR support_status IN (
                    'supported', 'partially_supported', 'not_found'
                )),
                metadata_valid INTEGER NOT NULL DEFAULT 1
                    CHECK (metadata_valid IN (0, 1)),
                FOREIGN KEY (conversation_id)
                    REFERENCES ai_conversations(id) ON DELETE CASCADE,
                CHECK (role IN ('user', 'assistant')),
                CHECK (provider IS NULL OR provider IN (
                    'gemini', 'openai', 'claude', 'deepseek', 'vilao'
                )),
                CHECK (selected_page IS NULL OR selected_page >= 1)
            )
            """,
            """
            CREATE TABLE ai_message_citations_v12 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                paper_id INTEGER NOT NULL,
                page_hint INTEGER,
                resolved_page INTEGER,
                section TEXT,
                evidence TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 0,
                verification_status TEXT NOT NULL DEFAULT 'unverified',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                claim_text TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (message_id)
                    REFERENCES ai_messages_v12(id) ON DELETE CASCADE,
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                CHECK (page_hint IS NULL OR page_hint >= 1),
                CHECK (resolved_page IS NULL OR resolved_page >= 1),
                CHECK (verified IN (0, 1))
            )
            """,
            """
            INSERT INTO ai_messages_v12 (
                id, conversation_id, role, content, provider, model,
                selected_text, selected_page, created_at,
                support_status, metadata_valid
            )
            SELECT id, conversation_id, role, content, provider, model,
                   selected_text, selected_page, created_at,
                   support_status, metadata_valid
            FROM ai_messages
            """,
            """
            INSERT INTO ai_message_citations_v12 (
                id, message_id, paper_id, page_hint, resolved_page, section,
                evidence, verified, verification_status, created_at, claim_text
            )
            SELECT id, message_id, paper_id, page_hint, resolved_page, section,
                   evidence, verified, verification_status, created_at, claim_text
            FROM ai_message_citations
            """,
            "DROP TABLE ai_message_citations",
            "DROP TABLE ai_messages",
            "ALTER TABLE ai_messages_v12 RENAME TO ai_messages",
            "ALTER TABLE ai_message_citations_v12 RENAME TO ai_message_citations",
            """
            CREATE INDEX idx_ai_messages_conversation_created
            ON ai_messages(conversation_id, id)
            """,
            """
            CREATE INDEX idx_ai_message_citations_message
            ON ai_message_citations(message_id, id)
            """,
            """
            CREATE TABLE ai_remote_states_v12 (
                conversation_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                remote_state_id TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (conversation_id, provider, model),
                FOREIGN KEY (conversation_id)
                    REFERENCES ai_conversations(id) ON DELETE CASCADE,
                CHECK (provider IN (
                    'gemini', 'openai', 'claude', 'deepseek', 'vilao'
                ))
            )
            """,
            """
            INSERT INTO ai_remote_states_v12 (
                conversation_id, provider, model, remote_state_id, updated_at
            )
            SELECT conversation_id, provider, model, remote_state_id, updated_at
            FROM ai_remote_states
            """,
            "DROP TABLE ai_remote_states",
            "ALTER TABLE ai_remote_states_v12 RENAME TO ai_remote_states",
            """
            CREATE TABLE ai_document_refs_v12 (
                paper_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                source_file_hash TEXT NOT NULL,
                remote_file_id TEXT NOT NULL,
                remote_uri TEXT,
                mime_type TEXT NOT NULL DEFAULT 'application/pdf',
                expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (paper_id, provider),
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                CHECK (provider IN (
                    'gemini', 'openai', 'claude', 'deepseek', 'vilao'
                ))
            )
            """,
            """
            INSERT INTO ai_document_refs_v12 (
                paper_id, provider, source_file_hash, remote_file_id,
                remote_uri, mime_type, expires_at, created_at, updated_at
            )
            SELECT paper_id, provider, source_file_hash, remote_file_id,
                   remote_uri, mime_type, expires_at, created_at, updated_at
            FROM ai_document_refs
            """,
            "DROP TABLE ai_document_refs",
            "ALTER TABLE ai_document_refs_v12 RENAME TO ai_document_refs",
        ),
    )


def _migration_13(connection: sqlite3.Connection) -> None:
    """Replace the superseded compatible-provider identity with ViLao."""
    legacy_provider = "api" + "box"
    connection.execute(
        """
        CREATE TABLE ai_messages_v13 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            selected_text TEXT,
            selected_page INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            support_status TEXT CHECK (support_status IS NULL OR support_status IN (
                'supported', 'partially_supported', 'not_found'
            )),
            metadata_valid INTEGER NOT NULL DEFAULT 1
                CHECK (metadata_valid IN (0, 1)),
            FOREIGN KEY (conversation_id)
                REFERENCES ai_conversations(id) ON DELETE CASCADE,
            CHECK (role IN ('user', 'assistant')),
            CHECK (provider IS NULL OR provider IN (
                'gemini', 'openai', 'claude', 'deepseek', 'vilao'
            )),
            CHECK (selected_page IS NULL OR selected_page >= 1)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE ai_message_citations_v13 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            paper_id INTEGER NOT NULL,
            page_hint INTEGER,
            resolved_page INTEGER,
            section TEXT,
            evidence TEXT NOT NULL,
            verified INTEGER NOT NULL DEFAULT 0,
            verification_status TEXT NOT NULL DEFAULT 'unverified',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            claim_text TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (message_id)
                REFERENCES ai_messages_v13(id) ON DELETE CASCADE,
            FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
            CHECK (page_hint IS NULL OR page_hint >= 1),
            CHECK (resolved_page IS NULL OR resolved_page >= 1),
            CHECK (verified IN (0, 1))
        )
        """
    )
    connection.execute(
        """
        INSERT INTO ai_messages_v13 (
            id, conversation_id, role, content, provider, model,
            selected_text, selected_page, created_at,
            support_status, metadata_valid
        )
        SELECT id, conversation_id, role, content,
               CASE WHEN provider = ? THEN 'vilao' ELSE provider END,
               model, selected_text, selected_page, created_at,
               support_status, metadata_valid
        FROM ai_messages
        """,
        (legacy_provider,),
    )
    connection.execute(
        """
        INSERT INTO ai_message_citations_v13 (
            id, message_id, paper_id, page_hint, resolved_page, section,
            evidence, verified, verification_status, created_at, claim_text
        )
        SELECT id, message_id, paper_id, page_hint, resolved_page, section,
               evidence, verified, verification_status, created_at, claim_text
        FROM ai_message_citations
        """
    )
    _execute_statements(
        connection,
        (
            "DROP TABLE ai_message_citations",
            "DROP TABLE ai_messages",
            "ALTER TABLE ai_messages_v13 RENAME TO ai_messages",
            "ALTER TABLE ai_message_citations_v13 RENAME TO ai_message_citations",
            """
            CREATE INDEX idx_ai_messages_conversation_created
            ON ai_messages(conversation_id, id)
            """,
            """
            CREATE INDEX idx_ai_message_citations_message
            ON ai_message_citations(message_id, id)
            """,
            """
            CREATE TABLE ai_remote_states_v13 (
                conversation_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                remote_state_id TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (conversation_id, provider, model),
                FOREIGN KEY (conversation_id)
                    REFERENCES ai_conversations(id) ON DELETE CASCADE,
                CHECK (provider IN (
                    'gemini', 'openai', 'claude', 'deepseek', 'vilao'
                ))
            )
            """,
        ),
    )
    connection.execute(
        """
        INSERT INTO ai_remote_states_v13 (
            conversation_id, provider, model, remote_state_id, updated_at
        )
        SELECT conversation_id,
               CASE WHEN provider = ? THEN 'vilao' ELSE provider END,
               model, remote_state_id, updated_at
        FROM ai_remote_states
        """,
        (legacy_provider,),
    )
    _execute_statements(
        connection,
        (
            "DROP TABLE ai_remote_states",
            "ALTER TABLE ai_remote_states_v13 RENAME TO ai_remote_states",
            """
            CREATE TABLE ai_document_refs_v13 (
                paper_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                source_file_hash TEXT NOT NULL,
                remote_file_id TEXT NOT NULL,
                remote_uri TEXT,
                mime_type TEXT NOT NULL DEFAULT 'application/pdf',
                expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (paper_id, provider),
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                CHECK (provider IN (
                    'gemini', 'openai', 'claude', 'deepseek', 'vilao'
                ))
            )
            """,
        ),
    )
    connection.execute(
        """
        INSERT INTO ai_document_refs_v13 (
            paper_id, provider, source_file_hash, remote_file_id,
            remote_uri, mime_type, expires_at, created_at, updated_at
        )
        SELECT paper_id,
               CASE WHEN provider = ? THEN 'vilao' ELSE provider END,
               source_file_hash, remote_file_id, remote_uri, mime_type,
               expires_at, created_at, updated_at
        FROM ai_document_refs
        """,
        (legacy_provider,),
    )
    _execute_statements(
        connection,
        (
            "DROP TABLE ai_document_refs",
            "ALTER TABLE ai_document_refs_v13 RENAME TO ai_document_refs",
        ),
    )

    has_settings = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'app_settings'
        """
    ).fetchone()
    if has_settings is not None:
        old_model_key = f"ai_model_{legacy_provider}"
        old_models_key = f"ai_models_{legacy_provider}"
        for old_key, new_key in (
            (old_model_key, "ai_model_vilao"),
            (old_models_key, "ai_models_vilao"),
        ):
            connection.execute(
                """
                INSERT INTO app_settings (key, value)
                SELECT ?, value FROM app_settings
                WHERE key = ?
                  AND NOT EXISTS (SELECT 1 FROM app_settings WHERE key = ?)
                """,
                (new_key, old_key, new_key),
            )
            connection.execute(
                "DELETE FROM app_settings WHERE key = ?", (old_key,)
            )
        connection.execute(
            """
            UPDATE app_settings SET value = 'vilao'
            WHERE key = 'ai_provider' AND value = ?
            """,
            (legacy_provider,),
        )


def _migration_14(connection: sqlite3.Connection) -> None:
    """Add project scope, research indexing, and a generic compatible API."""
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE project_papers (
                project_id INTEGER NOT NULL,
                paper_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'Unread'
                    CHECK (status IN ('Unread', 'Reading', 'Completed')),
                active_reading_seconds INTEGER NOT NULL DEFAULT 0
                    CHECK (active_reading_seconds >= 0),
                interaction_count INTEGER NOT NULL DEFAULT 0
                    CHECK (interaction_count >= 0),
                last_interaction_at TEXT,
                last_opened TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (project_id, paper_id),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX idx_project_papers_paper
            ON project_papers(paper_id, project_id)
            """,
            """
            CREATE INDEX idx_project_papers_status
            ON project_papers(project_id, status, updated_at)
            """,
            """
            CREATE TABLE project_workspaces (
                project_id INTEGER PRIMARY KEY,
                open_paper_ids_json TEXT NOT NULL DEFAULT '[]',
                active_paper_id INTEGER,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY (active_paper_id) REFERENCES papers(id) ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE research_summaries (
                paper_id INTEGER PRIMARY KEY,
                structured_json TEXT NOT NULL,
                search_text TEXT NOT NULL,
                source_file_hash TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                generation_status TEXT NOT NULL DEFAULT 'ready'
                    CHECK (generation_status IN ('generating', 'ready', 'error', 'stale')),
                error_message TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX idx_research_summaries_status
            ON research_summaries(generation_status, updated_at)
            """,
            """
            CREATE TABLE ai_search_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT 'New Search',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX idx_ai_search_conversations_project
            ON ai_search_conversations(project_id, updated_at DESC, id DESC)
            """,
            """
            CREATE TABLE ai_search_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                references_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id)
                    REFERENCES ai_search_conversations(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX idx_ai_search_messages_conversation
            ON ai_search_messages(conversation_id, id)
            """,
        ),
    )
    cursor = connection.execute(
        "INSERT INTO projects (name) VALUES ('Default Project')"
    )
    default_project_id = int(cursor.lastrowid)
    if _has_table(connection, "papers"):
        connection.execute(
            """
            INSERT INTO project_papers (project_id, paper_id, status)
            SELECT ?, id,
                   CASE WHEN status IN ('Unread', 'Reading', 'Completed')
                        THEN status ELSE 'Unread' END
            FROM papers
            """,
            (default_project_id,),
        )
    connection.execute(
        "INSERT INTO project_workspaces (project_id) VALUES (?)",
        (default_project_id,),
    )
    if _has_table(connection, "app_settings"):
        connection.execute(
            """
            INSERT INTO app_settings (key, value)
            VALUES ('current_project_id', ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (str(default_project_id),),
        )
    if _has_table(connection, "ai_conversations"):
        connection.execute(
            "ALTER TABLE ai_conversations ADD COLUMN project_id INTEGER REFERENCES projects(id)"
        )
        connection.execute(
            "UPDATE ai_conversations SET project_id = ? WHERE project_id IS NULL",
            (default_project_id,),
        )
        connection.execute(
            """
            CREATE INDEX idx_ai_conversations_project_updated
            ON ai_conversations(project_id, updated_at DESC, id DESC)
            """
        )
    if _has_table(connection, "app_settings"):
        connection.execute(
            """
            INSERT INTO app_settings (key, value) VALUES
                ('custom_api_name', ''),
                ('custom_api_base_url', ''),
                ('custom_api_format', 'openai_compatible'),
                ('custom_api_headers', '{}')
            ON CONFLICT(key) DO NOTHING
            """
        )


def _migration_15(connection: sqlite3.Connection) -> None:
    """Make Custom API generic and scope every collection to one project."""
    if _has_table(connection, "app_settings"):
        fallback_project = connection.execute(
            """
            SELECT projects.id
            FROM projects
            LEFT JOIN app_settings
              ON app_settings.key = 'current_project_id'
             AND CAST(app_settings.value AS INTEGER) = projects.id
            ORDER BY (app_settings.key IS NOT NULL) DESC, projects.id
            LIMIT 1
            """
        ).fetchone()
    else:
        fallback_project = connection.execute(
            "SELECT id FROM projects ORDER BY id LIMIT 1"
        ).fetchone()
    if fallback_project is None:
        raise sqlite3.DatabaseError("Collection migration requires a project.")
    fallback_project_id = int(fallback_project[0])

    # Preserve all legacy links. A formerly-global collection is cloned only
    # when its linked papers span multiple projects; paper rows remain shared.
    if not _has_table(connection, "collections"):
        connection.execute(
            """
            CREATE TABLE collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                description TEXT
            )
            """
        )
    if not _has_table(connection, "paper_collections"):
        connection.execute(
            """
            CREATE TABLE paper_collections (
                paper_id INTEGER NOT NULL,
                collection_id INTEGER NOT NULL,
                PRIMARY KEY (paper_id, collection_id),
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                FOREIGN KEY (collection_id)
                    REFERENCES collections(id) ON DELETE CASCADE
            )
            """
        )
    connection.execute("DROP INDEX IF EXISTS idx_paper_collections_collection_paper")
    connection.execute(
        "ALTER TABLE paper_collections RENAME TO paper_collections_v14"
    )
    connection.execute("ALTER TABLE collections RENAME TO collections_v14")
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE,
                description TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                UNIQUE (project_id, name)
            )
            """,
            """
            CREATE TABLE paper_collections (
                paper_id INTEGER NOT NULL,
                collection_id INTEGER NOT NULL,
                PRIMARY KEY (paper_id, collection_id),
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                FOREIGN KEY (collection_id)
                    REFERENCES collections(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX idx_collections_project_name
            ON collections(project_id, name COLLATE NOCASE)
            """,
            """
            CREATE INDEX idx_paper_collections_collection_paper
            ON paper_collections(collection_id, paper_id)
            """,
        ),
    )
    if _has_table(connection, "papers"):
        connection.execute(
            """
            INSERT OR IGNORE INTO project_papers (project_id, paper_id, status)
            SELECT ?, legacy.paper_id,
                   CASE WHEN papers.status IN ('Unread', 'Reading', 'Completed')
                        THEN papers.status ELSE 'Unread' END
            FROM paper_collections_v14 AS legacy
            JOIN papers ON papers.id = legacy.paper_id
            WHERE NOT EXISTS (
                SELECT 1 FROM project_papers
                WHERE project_papers.paper_id = legacy.paper_id
            )
            """,
            (fallback_project_id,),
        )
    collection_map: dict[tuple[int, int], int] = {}
    legacy_collections = list(
        connection.execute(
            "SELECT id, name, description FROM collections_v14 ORDER BY id"
        )
    )
    for collection in legacy_collections:
        legacy_id = int(collection[0])
        project_ids = [
            int(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT membership.project_id
                FROM paper_collections_v14 AS relation
                JOIN project_papers AS membership
                  ON membership.paper_id = relation.paper_id
                WHERE relation.collection_id = ?
                ORDER BY membership.project_id
                """,
                (legacy_id,),
            )
        ]
        if not project_ids:
            project_ids = [fallback_project_id]
        for index, project_id in enumerate(project_ids):
            if index == 0:
                cursor = connection.execute(
                    """
                    INSERT INTO collections (id, project_id, name, description)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        legacy_id,
                        project_id,
                        collection[1],
                        collection[2],
                    ),
                )
                new_id = int(cursor.lastrowid or legacy_id)
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO collections (project_id, name, description)
                    VALUES (?, ?, ?)
                    """,
                    (project_id, collection[1], collection[2]),
                )
                new_id = int(cursor.lastrowid)
            collection_map[(legacy_id, project_id)] = new_id

    for relation in connection.execute(
        """
        SELECT legacy.paper_id, legacy.collection_id, membership.project_id
        FROM paper_collections_v14 AS legacy
        JOIN project_papers AS membership
          ON membership.paper_id = legacy.paper_id
        ORDER BY legacy.paper_id, legacy.collection_id, membership.project_id
        """
    ):
        new_collection_id = collection_map.get(
            (int(relation[1]), int(relation[2]))
        )
        if new_collection_id is not None:
            connection.execute(
                """
                INSERT OR IGNORE INTO paper_collections (paper_id, collection_id)
                VALUES (?, ?)
                """,
                (int(relation[0]), new_collection_id),
            )
    connection.execute("DROP TABLE paper_collections_v14")
    connection.execute("DROP TABLE collections_v14")

    # The compatible-provider slot is now named by its architecture, not by a
    # vendor example. Persisted chats/models are retained under `custom`.
    connection.execute(
        """
        CREATE TABLE ai_messages_v15 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            selected_text TEXT,
            selected_page INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            support_status TEXT CHECK (support_status IS NULL OR support_status IN (
                'supported', 'partially_supported', 'not_found'
            )),
            metadata_valid INTEGER NOT NULL DEFAULT 1 CHECK (metadata_valid IN (0, 1)),
            FOREIGN KEY (conversation_id)
                REFERENCES ai_conversations(id) ON DELETE CASCADE,
            CHECK (role IN ('user', 'assistant')),
            CHECK (provider IS NULL OR provider IN (
                'gemini', 'openai', 'claude', 'deepseek', 'custom'
            )),
            CHECK (selected_page IS NULL OR selected_page >= 1)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE ai_message_citations_v15 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            paper_id INTEGER NOT NULL,
            page_hint INTEGER,
            resolved_page INTEGER,
            section TEXT,
            evidence TEXT NOT NULL,
            verified INTEGER NOT NULL DEFAULT 0,
            verification_status TEXT NOT NULL DEFAULT 'unverified',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            claim_text TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (message_id)
                REFERENCES ai_messages_v15(id) ON DELETE CASCADE,
            FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
            CHECK (page_hint IS NULL OR page_hint >= 1),
            CHECK (resolved_page IS NULL OR resolved_page >= 1),
            CHECK (verified IN (0, 1))
        )
        """
    )
    connection.execute(
        """
        INSERT INTO ai_messages_v15 (
            id, conversation_id, role, content, provider, model,
            selected_text, selected_page, created_at, support_status, metadata_valid
        )
        SELECT id, conversation_id, role, content,
               CASE WHEN provider = 'vilao' THEN 'custom' ELSE provider END,
               model, selected_text, selected_page, created_at,
               support_status, metadata_valid
        FROM ai_messages
        """
    )
    connection.execute(
        """
        INSERT INTO ai_message_citations_v15
        SELECT * FROM ai_message_citations
        """
    )
    _execute_statements(
        connection,
        (
            "DROP TABLE ai_message_citations",
            "DROP TABLE ai_messages",
            "ALTER TABLE ai_messages_v15 RENAME TO ai_messages",
            "ALTER TABLE ai_message_citations_v15 RENAME TO ai_message_citations",
            "CREATE INDEX idx_ai_messages_conversation_created ON ai_messages(conversation_id, id)",
            "CREATE INDEX idx_ai_message_citations_message ON ai_message_citations(message_id, id)",
            """
            CREATE TABLE ai_remote_states_v15 (
                conversation_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                remote_state_id TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (conversation_id, provider, model),
                FOREIGN KEY (conversation_id)
                    REFERENCES ai_conversations(id) ON DELETE CASCADE,
                CHECK (provider IN (
                    'gemini', 'openai', 'claude', 'deepseek', 'custom'
                ))
            )
            """,
        ),
    )
    connection.execute(
        """
        INSERT INTO ai_remote_states_v15
        SELECT conversation_id,
               CASE WHEN provider = 'vilao' THEN 'custom' ELSE provider END,
               model, remote_state_id, updated_at
        FROM ai_remote_states
        """
    )
    _execute_statements(
        connection,
        (
            "DROP TABLE ai_remote_states",
            "ALTER TABLE ai_remote_states_v15 RENAME TO ai_remote_states",
            """
            CREATE TABLE ai_document_refs_v15 (
                paper_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                source_file_hash TEXT NOT NULL,
                remote_file_id TEXT NOT NULL,
                remote_uri TEXT,
                mime_type TEXT NOT NULL DEFAULT 'application/pdf',
                expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (paper_id, provider),
                FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                CHECK (provider IN (
                    'gemini', 'openai', 'claude', 'deepseek', 'custom'
                ))
            )
            """,
        ),
    )
    connection.execute(
        """
        INSERT INTO ai_document_refs_v15
        SELECT paper_id,
               CASE WHEN provider = 'vilao' THEN 'custom' ELSE provider END,
               source_file_hash, remote_file_id, remote_uri, mime_type,
               expires_at, created_at, updated_at
        FROM ai_document_refs
        """
    )
    connection.execute("DROP TABLE ai_document_refs")
    connection.execute("ALTER TABLE ai_document_refs_v15 RENAME TO ai_document_refs")

    if _has_table(connection, "research_summaries"):
        connection.execute(
            "UPDATE research_summaries SET provider = 'custom' WHERE provider = 'vilao'"
        )
    if _has_table(connection, "ai_search_messages"):
        connection.execute(
            "UPDATE ai_search_messages SET provider = 'custom' WHERE provider = 'vilao'"
        )
    if _has_table(connection, "app_settings"):
        for old_key, new_key in (
            ("ai_model_vilao", "ai_model_custom"),
            ("ai_models_vilao", "ai_models_custom"),
        ):
            connection.execute(
                """
                INSERT INTO app_settings (key, value)
                SELECT ?, value FROM app_settings
                WHERE key = ?
                  AND NOT EXISTS (SELECT 1 FROM app_settings WHERE key = ?)
                """,
                (new_key, old_key, new_key),
            )
            connection.execute("DELETE FROM app_settings WHERE key = ?", (old_key,))
        connection.execute(
            """
            UPDATE app_settings SET value = 'custom'
            WHERE key = 'ai_provider' AND value = 'vilao'
            """
        )


def _migration_16(connection: sqlite3.Connection) -> None:
    """Add explicit Research Profile lifecycle metadata and an FTS shortlist."""
    columns = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in connection.execute("PRAGMA table_info(research_summaries)")
    }
    if "profile_status" not in columns:
        connection.execute(
            """
            ALTER TABLE research_summaries ADD COLUMN profile_status TEXT
            NOT NULL DEFAULT 'ready'
            CHECK (profile_status IN ('pending', 'ready', 'failed', 'stale'))
            """
        )
    if "generated_at" not in columns:
        connection.execute(
            "ALTER TABLE research_summaries ADD COLUMN generated_at TEXT"
        )
    connection.execute(
        """
        UPDATE research_summaries
        SET profile_status = CASE generation_status
            WHEN 'generating' THEN 'pending'
            WHEN 'error' THEN 'failed'
            ELSE generation_status
        END,
        generated_at = CASE WHEN generation_status = 'ready'
                            THEN COALESCE(generated_at, updated_at)
                            ELSE generated_at END
        """
    )
    connection.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS research_profile_fts USING fts5(
            paper_id UNINDEXED,
            search_text,
            tokenize = 'unicode61 remove_diacritics 2'
        )
        """
    )
    connection.execute("DELETE FROM research_profile_fts")
    connection.execute(
        """
        INSERT INTO research_profile_fts (paper_id, search_text)
        SELECT paper_id, search_text
        FROM research_summaries
        WHERE profile_status = 'ready'
        """
    )
    _backfill_structured_search_documents(connection)

_MIGRATIONS: tuple[tuple[int, Migration], ...] = (
    (1, _migration_1),
    (2, _migration_2),
    (3, _migration_3),
    (4, _migration_4),
    (5, _migration_5),
    (6, _migration_6),
    (7, _migration_7),
    (8, _migration_8),
    (9, _migration_9),
    (10, _migration_10),
    (11, _migration_11),
    (12, _migration_12),
    (13, _migration_13),
    (14, _migration_14),
    (15, _migration_15),
    (16, _migration_16),
)


def run_migrations(connection: sqlite3.Connection) -> None:
    """Apply pending schema migrations atomically without replacing data."""
    current_version = int(
        connection.execute("PRAGMA user_version").fetchone()[0]
    )
    if current_version > LATEST_SCHEMA_VERSION:
        raise UnsupportedDatabaseVersionError(
            "Database schema version "
            f"{current_version} is newer than supported version "
            f"{LATEST_SCHEMA_VERSION}."
        )

    for version, migrate in _MIGRATIONS:
        if version <= current_version:
            continue

        try:
            connection.execute("BEGIN")
            migrate(connection)
            connection.execute(f"PRAGMA user_version = {version}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

        current_version = version
