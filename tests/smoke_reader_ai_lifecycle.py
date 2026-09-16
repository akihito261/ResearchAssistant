from __future__ import annotations

import tempfile
from pathlib import Path

import pymupdf
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import app.database.database as database
from app.database.paper_repository import PaperRepository
from app.pdf.pdfjs_scheme import register_pdfjs_scheme


def _make_pdf(path: Path, label: str) -> None:
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), label)
    document.save(path)
    document.close()


def _flush_deferred_deletes() -> None:
    QApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


def main() -> int:
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    old_data_dir = database.DATA_DIR
    old_database_path = database.DATABASE_PATH
    database.DATA_DIR = root / "data"
    database.DATABASE_PATH = database.DATA_DIR / "research.db"
    try:
        database.init_database()
        paper_ids: list[int] = []
        for index, title in enumerate(("Lifecycle A", "Lifecycle B", "Lifecycle C"), 1):
            pdf_path = root / f"paper-{index}.pdf"
            _make_pdf(pdf_path, title)
            paper_ids.append(
                PaperRepository.add_paper(
                    title,
                    f"Author {index}",
                    2020 + index,
                    None,
                    str(pdf_path),
                    f"{index}" * 64,
                    1,
                )
            )

        register_pdfjs_scheme()
        app = QApplication.instance() or QApplication([])
        from app.ui.main_window import MainWindow

        main_window = MainWindow(start_background_tasks=False)
        main_window.show()
        reader_a = main_window.open_paper_by_id(paper_ids[0])
        reader_b = main_window.open_paper_by_id(paper_ids[1])
        assert reader_a is not None and reader_b is not None
        main_window._add_to_ai_comparison(paper_ids[0])
        # B is active, so adding A creates one shared group panel.
        QApplication.processEvents()
        group_panel = main_window._ai_group_panel
        assert group_panel is not None
        assert group_panel.provider.parentWidget() is group_panel
        assert group_panel.model.parentWidget() is group_panel.composer_shell

        reader_a.close()
        reader_b.close()
        _flush_deferred_deletes()
        assert not main_window._workspace_paper_ids
        assert group_panel.parentWidget() is main_window._ai_panel_parking
        assert group_panel.provider.parentWidget() is group_panel
        assert shiboken6.isValid(group_panel)
        assert shiboken6.isValid(group_panel.provider)
        assert main_window.isVisible()

        for cycle in range(3):
            reader = main_window.open_paper_by_id(paper_ids[cycle])
            assert reader is not None
            QApplication.processEvents()
            assert reader.reader_sidebar.ai is group_panel
            assert group_panel.provider.parentWidget() is group_panel
            assert reader.reader_sidebar.ai_header_provider.parentWidget() is reader.reader_sidebar
            assert shiboken6.isValid(reader.reader_sidebar.ai_header_provider)
            assert shiboken6.isValid(group_panel.provider)
            assert shiboken6.isValid(group_panel.model)
            assert group_panel.model.parentWidget() is group_panel.composer_shell

            proxy_index = (group_panel.provider.currentIndex() + 1) % max(
                1, group_panel.provider.count()
            )
            reader.reader_sidebar.ai_header_provider.setCurrentIndex(proxy_index)
            assert group_panel.provider.currentIndex() == proxy_index
            panel_index = (proxy_index + 1) % max(1, group_panel.provider.count())
            group_panel.provider.setCurrentIndex(panel_index)
            assert (
                reader.reader_sidebar.ai_header_provider.currentIndex()
                == panel_index
            )

            for section in ("ai", "notes", "summary", "info", "ai"):
                reader.reader_sidebar.show_section(section)
                QApplication.processEvents()

            reader.reader_sidebar.ai_header_provider.showPopup()
            QApplication.processEvents()
            reader.reader_sidebar.ai_header_provider.hidePopup()
            group_panel.model.showPopup()
            QApplication.processEvents()
            group_panel.model.hidePopup()

            reader.close()
            _flush_deferred_deletes()
            assert not main_window._workspace_paper_ids
            assert group_panel.parentWidget() is main_window._ai_panel_parking
            assert group_panel.provider.parentWidget() is group_panel
            assert shiboken6.isValid(group_panel.provider)
            assert main_window.isVisible()

        print(
            {
                "close_all_reopen_cycles": 3,
                "provider_owner": "AIChatPanel",
                "header_provider_owner": "ReaderSidebar",
                "group_panel_parked": True,
            }
        )
        main_window.close()
        QTest.qWait(50)
        return 0
    finally:
        database.DATA_DIR = old_data_dir
        database.DATABASE_PATH = old_database_path
        temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
