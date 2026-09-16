from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

import fitz
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

import app.database.database as database
import app.database.migrations as migrations
from app.database.annotation_anchor_repository import AnnotationAnchorRepository
from app.database.highlight_repository import HighlightRepository
from app.database.note_repository import NoteRepository
from app.database.paper_repository import PaperRepository
from app.database.document_version_repository import DocumentVersionRepository
from app.services.pdf_service import calculate_sha256
from app.services.vietnamese_pdf_service import (
    VietnamesePdfError,
    VietnamesePdfService,
)
from app.ui.reader_sidebar import HighlightCard, NotesPanel, SelectionDraftCard
from app.bridge.pdf_reader_bridge import PdfReaderBridge


def _pdf_bytes(text: str = "Hello") -> bytes:
    document = fitz.open()
    document.new_page().insert_text((72, 72), text)
    data = document.tobytes()
    document.close()
    return data


LOCATION = {
    "version": 1,
    "fingerprint": "fixture",
    "segments": [
        {
            "page": 1,
            "start": {"item": 0, "offset": 0},
            "end": {"item": 0, "offset": 5},
            "exact": "Hello",
            "prefix": "",
            "suffix": " world",
            "pdfRects": [[10, 10, 40, 20]],
        }
    ],
}


class DatabaseVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_data_dir = database.DATA_DIR
        self.old_path = database.DATABASE_PATH
        database.DATA_DIR = Path(self.temp.name)
        database.DATABASE_PATH = Path(self.temp.name) / "research.db"
        database.init_database()
        source = Path(self.temp.name) / "paper.pdf"
        source.write_bytes(_pdf_bytes())
        self.paper_id = PaperRepository.add_paper(
            "Paper", None, 2026, None, str(source), "a" * 64, 1
        )

    def tearDown(self) -> None:
        database.DATA_DIR = self.old_data_dir
        database.DATABASE_PATH = self.old_path
        self.temp.cleanup()

    def test_schema_v5_stores_document_version_on_each_annotation(self) -> None:
        note_id = NoteRepository.create(
            self.paper_id,
            "Meaningful note",
            title="Selection note",
            kind="selection",
            source_text="Hello",
            page_number=1,
            location_data=__import__("json").dumps(LOCATION),
            document_version="en",
        )
        highlight_id = HighlightRepository.create(
            self.paper_id,
            "Xin chào",
            1,
            __import__("json").dumps(LOCATION),
            "blue",
            document_version="vi",
        )
        self.assertEqual(
            AnnotationAnchorRepository.get_for_note(note_id, "en")["note_id"],
            note_id,
        )
        self.assertIsNone(AnnotationAnchorRepository.get_for_note(note_id, "vi"))
        self.assertEqual(
            AnnotationAnchorRepository.get_for_highlight(highlight_id, "vi")[
                "highlight_id"
            ],
            highlight_id,
        )
        with database.connection_scope() as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                migrations.LATEST_SCHEMA_VERSION,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM notes WHERE id = ?", (note_id,)).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT document_version FROM notes WHERE id = ?", (note_id,)
                ).fetchone()[0],
                "en",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT document_version FROM highlights WHERE id = ?",
                    (highlight_id,),
                ).fetchone()[0],
                "vi",
            )

    def test_v3_to_v4_backfills_existing_anchors_without_duplicate_records(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy-v3.db"
        connection = sqlite3.connect(legacy_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        database._create_base_schema(connection)
        migrations._migration_1(connection)
        migrations._migration_2(connection)
        migrations._migration_3(connection)
        connection.execute("PRAGMA user_version = 3")
        cursor = connection.execute(
            "INSERT INTO papers (title, file_path, file_hash) VALUES ('Legacy', 'legacy.pdf', 'hash')"
        )
        paper_id = int(cursor.lastrowid)
        location_json = __import__("json").dumps(LOCATION)
        note_id = int(
            connection.execute(
                """
                INSERT INTO notes (paper_id, content, kind, source_text, page_number, location_data)
                VALUES (?, 'body', 'selection', 'Hello', 1, ?)
                """,
                (paper_id, location_json),
            ).lastrowid
        )
        highlight_id = int(
            connection.execute(
                """
                INSERT INTO highlights (
                    paper_id, selected_text, page_number, location_data, color
                ) VALUES (?, 'Hello', 1, ?, 'yellow')
                """,
                (paper_id, location_json),
            ).lastrowid
        )
        connection.commit()
        migrations.run_migrations(connection)
        self.assertEqual(
            connection.execute("PRAGMA user_version").fetchone()[0],
            migrations.LATEST_SCHEMA_VERSION,
        )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM highlights").fetchone()[0], 1)
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM annotation_anchors WHERE document_version='en'"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            connection.execute(
                "SELECT note_id FROM annotation_anchors WHERE note_id IS NOT NULL"
            ).fetchone()[0],
            note_id,
        )
        self.assertEqual(
            connection.execute(
                "SELECT highlight_id FROM annotation_anchors WHERE highlight_id IS NOT NULL"
            ).fetchone()[0],
            highlight_id,
        )
        connection.close()

    def test_v4_to_v5_keeps_origin_anchor_and_discards_generated_peer(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy-v4.db"
        connection = sqlite3.connect(legacy_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        database._create_base_schema(connection)
        migrations._migration_1(connection)
        migrations._migration_2(connection)
        migrations._migration_3(connection)
        migrations._migration_4(connection)
        paper_id = int(
            connection.execute(
                "INSERT INTO papers (title, file_path, file_hash) "
                "VALUES ('Legacy', 'legacy.pdf', 'legacy-v4')"
            ).lastrowid
        )
        en_location = __import__("json").dumps({**LOCATION, "fingerprint": "en"})
        vi_location = __import__("json").dumps({**LOCATION, "fingerprint": "vi"})
        note_id = int(
            connection.execute(
                """
                INSERT INTO notes (
                    paper_id, content, kind, source_text, page_number, location_data
                ) VALUES (?, 'VI origin', 'selection', 'Xin chao', 1, ?)
                """,
                (paper_id, vi_location),
            ).lastrowid
        )
        for version, location in (("en", en_location), ("vi", vi_location)):
            connection.execute(
                """
                INSERT INTO annotation_anchors (
                    paper_id, note_id, document_version, selected_text,
                    page_number, location_data
                ) VALUES (?, ?, ?, 'text', 1, ?)
                """,
                (paper_id, note_id, version, location),
            )
        connection.execute("PRAGMA user_version = 4")
        connection.commit()

        migrations.run_migrations(connection)
        self.assertEqual(
            connection.execute("PRAGMA user_version").fetchone()[0],
            migrations.LATEST_SCHEMA_VERSION,
        )
        self.assertEqual(
            connection.execute(
                "SELECT document_version FROM notes WHERE id = ?", (note_id,)
            ).fetchone()[0],
            "vi",
        )
        self.assertEqual(
            connection.execute(
                "SELECT document_version FROM annotation_anchors WHERE note_id = ?",
                (note_id,),
            ).fetchall()[0][0],
            "vi",
        )
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM annotation_anchors WHERE note_id = ?",
                (note_id,),
            ).fetchone()[0],
            1,
        )
        connection.close()

    def test_v5_migration_tolerates_a_minimal_v4_database(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE future_marker (value TEXT)")
        connection.execute("INSERT INTO future_marker VALUES ('kept')")
        connection.execute("PRAGMA user_version = 4")
        connection.commit()
        migrations.run_migrations(connection)
        self.assertEqual(
            connection.execute("PRAGMA user_version").fetchone()[0],
            migrations.LATEST_SCHEMA_VERSION,
        )
        self.assertEqual(
            connection.execute("SELECT value FROM future_marker").fetchone()[0],
            "kept",
        )
        connection.close()

    def test_bridge_publishes_only_anchor_for_active_document(self) -> None:
        note_id = NoteRepository.create(
            self.paper_id,
            "Shared body",
            kind="selection",
            source_text="Hello",
            page_number=1,
            location_data=__import__("json").dumps(LOCATION),
            document_version="en",
        )
        bridge = PdfReaderBridge(self.paper_id)
        snapshots = []
        bridge.notesSnapshot.connect(snapshots.append)
        bridge.page_count = 1
        bridge.fingerprint = "fixture"
        bridge.publish_notes()
        self.assertEqual(__import__("json").loads(snapshots[-1])[0]["id"], note_id)
        bridge.set_document_version("vi")
        bridge.page_count = 1
        bridge.fingerprint = "translated"
        bridge.publish_notes()
        self.assertEqual(__import__("json").loads(snapshots[-1]), [])

        en_highlight = HighlightRepository.create(
            self.paper_id,
            "English",
            1,
            __import__("json").dumps(LOCATION),
            "yellow",
            document_version="en",
        )
        vi_highlight = HighlightRepository.create(
            self.paper_id,
            "Vietnamese",
            1,
            __import__("json").dumps(LOCATION),
            "blue",
            document_version="vi",
        )
        highlight_snapshots = []
        bridge.highlightsSnapshot.connect(highlight_snapshots.append)
        bridge.publish_highlights()
        self.assertEqual(
            [row["id"] for row in __import__("json").loads(highlight_snapshots[-1])],
            [vi_highlight],
        )
        bridge.set_document_version("en")
        bridge.publish_highlights()
        self.assertEqual(
            [row["id"] for row in __import__("json").loads(highlight_snapshots[-1])],
            [en_highlight],
        )


class _DraftRepository:
    def __init__(self) -> None:
        self.calls = []

    def create(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return 7


class DraftNoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_empty_selection_draft_never_persists(self) -> None:
        repository = _DraftRepository()
        card = SelectionDraftCard(
            1,
            {"requestId": "r1", "selectedText": "Hello", "location": LOCATION},
            repository,
            "en",
        )
        self.assertTrue(card.save())
        self.assertEqual(repository.calls, [])
        card.editor.setPlainText("  actual note  ")
        self.assertTrue(card.save())
        self.assertEqual(len(repository.calls), 1)
        self.assertEqual(repository.calls[0][1]["document_version"], "en")

    def test_highlight_color_dropdown_shows_swatch_for_every_item(self) -> None:
        card = HighlightCard(
            {
                "id": 1,
                "page_number": 1,
                "location_data": __import__("json").dumps(LOCATION),
                "color": "green",
                "selected_text": "Highlighted text",
                "note_id": None,
            }
        )
        self.assertEqual(card.color.count(), 6)
        self.assertEqual(card.color.currentData(), "green")
        self.assertEqual(card.color.currentText(), "Green")
        for index in range(card.color.count()):
            icon = card.color.itemIcon(index)
            self.assertFalse(icon.isNull())
            self.assertFalse(icon.pixmap(card.color.iconSize()).isNull())
        card.close()


class NotesScrollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_data_dir = database.DATA_DIR
        self.old_path = database.DATABASE_PATH
        database.DATA_DIR = Path(self.temp.name)
        database.DATABASE_PATH = Path(self.temp.name) / "research.db"
        database.init_database()
        pdf = Path(self.temp.name) / "paper.pdf"
        pdf.write_bytes(_pdf_bytes())
        self.paper_id = PaperRepository.add_paper(
            "Scroll", None, None, None, str(pdf), "c" * 64, 1
        )

    def tearDown(self) -> None:
        database.DATA_DIR = self.old_data_dir
        database.DATABASE_PATH = self.old_path
        self.temp.cleanup()

    def test_deep_note_scrolls_and_receives_active_style(self) -> None:
        NoteRepository.save_for_paper(
            self.paper_id,
            "Shared scratchpad\n" * 20,
        )
        note_ids = [
            NoteRepository.create(
                self.paper_id, f"Body {index}", title=f"Note {index}", kind="manual"
            )
            for index in range(15)
        ]
        panel = NotesPanel(self.paper_id, PdfReaderBridge(self.paper_id))
        panel.resize(420, 700)
        panel.show()
        self.app.processEvents()
        self.assertGreater(panel.scroll.verticalScrollBar().maximum(), 0)
        for note_id in (note_ids[-1], note_ids[len(note_ids) // 2], note_ids[0]):
            self.assertTrue(panel.open_note(note_id))
            QTest.qWait(80)
            card = panel.note_cards[note_id]
            scrollbar = panel.scroll.verticalScrollBar()
            viewport_height = panel.scroll.viewport().height()
            card_top = card.mapTo(panel.list_widget, QPoint(0, 0)).y()
            card_height = max(card.height(), card.sizeHint().height())
            if card_height >= viewport_height - 32:
                expected = card_top - 16
            else:
                expected = card_top - ((viewport_height - card_height) // 2)
            expected = max(scrollbar.minimum(), min(scrollbar.maximum(), expected))
            self.assertLessEqual(abs(scrollbar.value() - expected), 1)
            self.assertTrue(card.property("activeNote"))
            self.assertTrue(card.editor.hasFocus())
        panel.close()

    def test_notes_are_version_local_but_scratchpad_is_shared(self) -> None:
        NoteRepository.save_for_paper(self.paper_id, "Shared scratchpad")
        en_note = NoteRepository.create(
            self.paper_id,
            "English note",
            title="EN",
            kind="manual",
            document_version="en",
        )
        vi_note = NoteRepository.create(
            self.paper_id,
            "Vietnamese note",
            title="VI",
            kind="manual",
            document_version="vi",
        )
        bridge = PdfReaderBridge(self.paper_id)
        panel = NotesPanel(self.paper_id, bridge)
        self.assertEqual(set(panel.note_cards), {en_note})
        self.assertEqual(panel.scratchpad.toPlainText(), "Shared scratchpad")

        bridge.set_document_version("vi")
        panel.refresh_for_document()
        self.assertEqual(set(panel.note_cards), {vi_note})
        self.assertEqual(panel.scratchpad.toPlainText(), "Shared scratchpad")
        panel.close()


class VietnamesePdfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_data_dir = database.DATA_DIR
        self.old_path = database.DATABASE_PATH
        database.DATA_DIR = self.root / "data"
        database.DATABASE_PATH = database.DATA_DIR / "research.db"
        database.init_database()
        self.en_path = self.root / "en.pdf"
        self.en_path.write_bytes(_pdf_bytes("Hello research paper"))
        self.paper_id = PaperRepository.add_paper(
            "Paper", None, None, None, str(self.en_path), "d" * 64, 1
        )
        self.trashed = []

        def trash(path: str) -> bool:
            self.trashed.append(Path(path))
            Path(path).unlink(missing_ok=True)
            return True

        self.service = VietnamesePdfService(
            managed_root=self.root / "managed" / "translations",
            trash_function=trash,
        )

    def tearDown(self) -> None:
        database.DATA_DIR = self.old_data_dir
        database.DATABASE_PATH = self.old_path
        self.temp.cleanup()

    def test_add_replace_remove_vi_preserves_logical_note(self) -> None:
        vi_one = self.root / "vi-one.pdf"
        vi_one.write_bytes(_pdf_bytes("Xin chao bai bao"))
        first = self.service.import_pdf(self.paper_id, vi_one, "d" * 64)
        self.assertTrue(first.path.is_file())

        note_id = NoteRepository.create(
            self.paper_id,
            "Shared note",
            kind="selection",
            source_text="Xin chao",
            page_number=1,
            location_data=__import__("json").dumps(LOCATION),
            document_version="vi",
        )
        vi_two = self.root / "vi-two.pdf"
        vi_two.write_bytes(_pdf_bytes("Ban dich thay the"))
        second = self.service.import_pdf(self.paper_id, vi_two, "d" * 64)
        self.assertTrue(second.replaced)
        self.assertIsNotNone(NoteRepository.get(note_id))
        self.assertIsNone(AnnotationAnchorRepository.get_for_note(note_id, "vi"))
        self.assertFalse(first.path.exists())

        removed = self.service.remove_pdf(self.paper_id)
        self.assertIsNotNone(removed)
        self.assertIsNone(DocumentVersionRepository.get(self.paper_id, "vi"))
        self.assertIsNotNone(NoteRepository.get(note_id))

    def test_invalid_import_does_not_create_document_version(self) -> None:
        invalid = self.root / "invalid.pdf"
        invalid.write_text("not a pdf", encoding="utf-8")
        with self.assertRaises(VietnamesePdfError):
            self.service.import_pdf(self.paper_id, invalid, "d" * 64)

        with self.assertRaises(VietnamesePdfError):
            self.service.import_pdf(
                self.paper_id,
                self.en_path,
                calculate_sha256(self.en_path),
            )
        self.assertIsNone(DocumentVersionRepository.get(self.paper_id, "vi"))


if __name__ == "__main__":
    unittest.main()
