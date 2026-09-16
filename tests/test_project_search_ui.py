from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication, QWidget

import app.database.database as database
from app.database.project_repository import ProjectRepository
from app.ui.project_ai_search_dialog import (
    ProjectAISearchPanel,
    ProjectSearchConversationController,
)


class ProjectSearchUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.old_data_dir = database.DATA_DIR
        self.old_database_path = database.DATABASE_PATH
        database.DATA_DIR = self.root / "data"
        database.DATABASE_PATH = database.DATA_DIR / "research.db"
        database.init_database()
        self.project = ProjectRepository.list_all()[0]

    def tearDown(self) -> None:
        database.DATA_DIR = self.old_data_dir
        database.DATABASE_PATH = self.old_database_path
        self.temporary.cleanup()

    def test_library_and_reader_views_share_state_without_reparenting(self) -> None:
        controller = ProjectSearchConversationController(
            int(self.project["id"]), str(self.project["name"])
        )
        library_host = QWidget()
        reader_host = QWidget()
        library = ProjectAISearchPanel(
            controller=controller, parent=library_host
        )
        reader = ProjectAISearchPanel(
            controller=controller,
            parent=reader_host,
            presentation_mode="reader",
        )
        self.assertIsNot(library, reader)
        self.assertIs(library.controller, reader.controller)
        self.assertIs(library.parentWidget(), library_host)
        self.assertIs(reader.parentWidget(), reader_host)
        library.composer.setPlainText("shared draft")
        self.app.processEvents()
        self.assertEqual(reader.composer.toPlainText(), "shared draft")
        reader.provider.setCurrentIndex(
            max(0, reader.provider.findData("custom"))
        )
        reader.model.setEditText("detected-model")
        self.app.processEvents()
        self.assertEqual(library.provider.currentData(), "custom")
        self.assertEqual(library.model.currentText(), "detected-model")

    def test_empty_state_and_ai_composer_resize(self) -> None:
        panel = ProjectAISearchPanel(
            int(self.project["id"]), str(self.project["name"])
        )
        panel.resize(360, 640)
        panel.show()
        self.app.processEvents()
        initial = panel.composer.height()
        self.assertEqual(panel.conversation_pages.currentIndex(), 0)
        panel.composer.setPlainText("one\ntwo\nthree\nfour\nfive\nsix")
        self.app.processEvents()
        expanded = panel.composer.height()
        panel.composer.setPlainText("short")
        self.app.processEvents()
        self.assertGreater(expanded, initial)
        self.assertEqual(panel.composer.height(), initial)
        self.assertEqual(panel.scroll.horizontalScrollBar().maximum(), 0)

    def test_library_is_wide_while_reader_remains_sidebar_sized(self) -> None:
        controller = ProjectSearchConversationController(
            int(self.project["id"]), str(self.project["name"])
        )
        controller.messages = [
            {
                "role": "user",
                "content": "Compare ROI compression results across the project.",
            },
            {
                "role": "assistant",
                "content": "A responsive answer with Vietnamese prose: kết quả nén vùng quan tâm.",
                "provider": "custom",
                "model": "a-long-openai-compatible-model-name",
                "references": [],
            },
        ]
        library = ProjectAISearchPanel(
            controller=controller, presentation_mode="library"
        )
        library.resize(1600, 760)
        library.show()
        self.app.processEvents()
        self.assertGreaterEqual(library.conversation_pages.width(), 1119)
        self.assertLessEqual(library.conversation_pages.width(), 1121)
        self.assertEqual(
            library.composer_shell.width(), library.conversation_pages.width()
        )
        self.assertLessEqual(
            library.composer_shell.geometry().bottom(), library.rect().bottom()
        )
        self.assertEqual(library.scroll.horizontalScrollBar().maximum(), 0)

        reader = ProjectAISearchPanel(
            controller=controller, presentation_mode="reader"
        )
        reader.resize(380, 700)
        reader.show()
        self.app.processEvents()
        self.assertEqual(reader.width(), 380)
        self.assertEqual(reader.conversation_pages.width(), 380)
        self.assertEqual(reader.composer_shell.width(), 380)
        self.assertEqual(reader.context_label.text(), "Context: Default Project")
        self.assertFalse(reader.suggestions.isVisible())


if __name__ == "__main__":
    unittest.main()
