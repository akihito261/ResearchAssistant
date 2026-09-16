from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import fitz
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import app.database.database as database
from app.pdf.pdfjs_scheme import register_pdfjs_scheme
from app.services.vietnamese_pdf_service import VietnamesePdfService


class FixtureTextProvider:
    def translate(self, _text: str, **_kwargs) -> str:
        return "Bản dịch thử nghiệm"


def main() -> int:
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    database.DATA_DIR = root / "data"
    database.DATABASE_PATH = database.DATA_DIR / "research.db"
    database.init_database()

    pdf_path = root / "paper.pdf"
    document = fitz.open()
    for page_number in range(2):
        page = document.new_page()
        page.insert_text((72, 72), f"Reader smoke page {page_number + 1}")
    document.save(pdf_path)
    document.close()
    vi_path = root / "paper-vi.pdf"
    document = fitz.open()
    for page_number in range(2):
        page = document.new_page()
        page.insert_text((72, 72), f"Ban dich thu trang {page_number + 1}")
    document.save(vi_path)
    document.close()

    from app.database.note_repository import NoteRepository
    from app.database.paper_repository import PaperRepository

    paper_id = PaperRepository.add_paper(
        "Reader smoke", None, 2026, None, str(pdf_path), "b" * 64, 2
    )
    paper = PaperRepository.get_paper_by_id(paper_id)
    register_pdfjs_scheme()
    app = QApplication(sys.argv)
    from app.ui.pdf_reader_window import PdfReaderWindow

    service = VietnamesePdfService(
        managed_root=root / "library" / "papers" / "translations",
        trash_function=lambda path: Path(path).unlink(missing_ok=True) is None,
    )
    reader = PdfReaderWindow(
        paper,
        vietnamese_pdf_service=service,
        translation_provider=FixtureTextProvider(),
    )
    states: list[tuple[str, int]] = []

    def run_js(source: str, callback=None) -> None:
        reader.web_view.page().runJavaScript(source, callback or (lambda _value: None))

    def select_and_click(title: str) -> None:
        run_js(
            f"""
            (() => {{
              const span = Array.from(document.querySelectorAll(
                '.page[data-page-number="1"] .textLayer span'
              )).find(node => (node.textContent || '').trim());
              if (!span) return false;
              const range = document.createRange();
              range.selectNodeContents(span);
              const selection = window.getSelection();
              selection.removeAllRanges();
              selection.addRange(range);
              document.dispatchEvent(new Event('selectionchange'));
              return true;
            }})()
            """,
            lambda selected: QTimer.singleShot(
                250,
                lambda: run_js(
                    f"document.querySelector('.ra-selection-action[title=\"{title}\"]')?.click();"
                ),
            ),
        )

    def count_located_notes() -> int:
        with database.connection_scope() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM notes WHERE kind != 'scratchpad'"
                ).fetchone()[0]
            )

    def begin_note_flow() -> None:
        scratchpad = reader.reader_sidebar.notes.scratchpad
        scratchpad.setPlainText("Shared scratchpad")
        if not reader.reader_sidebar.notes.save_scratchpad():
            print("scratchpad smoke failed")
            app.exit(11)
            return
        select_and_click("Note")
        QTimer.singleShot(700, finish_note_flow)

    def finish_note_flow() -> None:
        draft = reader.reader_sidebar.notes.draft_card
        if draft is None or count_located_notes() != 0:
            print("note draft smoke failed")
            app.exit(3)
            return
        draft.editor.setPlainText("Saved only after non-empty content")
        if not draft.save():
            app.exit(4)
            return
        QTimer.singleShot(500, begin_translate_flow)

    def begin_translate_flow() -> None:
        run_js(
            "(() => { const note=document.querySelector('.ra-note-rect');"
            "const style=note ? getComputedStyle(note) : null;"
            "return JSON.stringify({"
            "markers:document.querySelectorAll('.ra-note-marker').length,"
            "notes:document.querySelectorAll('.ra-note-rect').length,"
            "underlines:document.querySelectorAll('.ra-note-underline').length,"
            "background:style?.backgroundColor || '',"
            "underline:style?.borderBottomWidth || ''}); })()",
            verify_note_visual,
        )

    def verify_note_visual(raw) -> None:
        visual = json.loads(raw)
        if (
            visual["markers"] != 0
            or visual["notes"] < 1
            or visual["underlines"] < 1
            or visual["background"] in {"", "rgba(0, 0, 0, 0)"}
            or visual["underline"] != "2px"
        ):
            print("persistent note visual smoke failed")
            app.exit(8)
            return
        for card in reader.reader_sidebar.notes.note_cards.values():
            card.setProperty("activeNote", False)
        run_js(
            """
            (() => {
              const note = document.querySelector('.ra-note-rect');
              if (!note) return false;
              const rect = note.getBoundingClientRect();
              const x = (rect.left + rect.right) / 2;
              const y = (rect.top + rect.bottom) / 2;
              const target = document.elementFromPoint(x, y);
              target?.dispatchEvent(new MouseEvent('click', {
                bubbles: true, clientX: x, clientY: y
              }));
              return Boolean(target);
            })()
            """,
            lambda _clicked: QTimer.singleShot(300, verify_note_text_click),
        )

    def verify_note_text_click() -> None:
        if not any(
            bool(card.property("activeNote"))
            for card in reader.reader_sidebar.notes.note_cards.values()
        ):
            print("note text click did not focus its card")
            app.exit(10)
            return
        select_and_click("Translate")
        QTimer.singleShot(900, finish_translate_flow)

    def finish_translate_flow() -> None:
        card = reader.reader_sidebar.notes.translation_card
        if not card.isVisible() or not card.translation.toPlainText().strip():
            print("translation card smoke failed")
            app.exit(5)
            return
        run_js(
            "window.ResearchAssistantReader.debugState()",
            lambda raw: verify_translate_overlay(card, raw),
        )

    def verify_translate_overlay(card, raw) -> None:
        debug = json.loads(raw)
        if not debug.get("transientSelection"):
            print("translation overlay ended too early")
            app.exit(6)
            return
        card._copy()
        QTimer.singleShot(
            100,
            lambda: run_js(
                "window.ResearchAssistantReader.debugState()",
                lambda copied_raw: verify_overlay_after_copy(card, copied_raw),
            ),
        )

    def verify_overlay_after_copy(card, raw) -> None:
        if not json.loads(raw).get("transientSelection"):
            print("copy removed translation overlay")
            app.exit(9)
            return
        card._save()
        QTimer.singleShot(600, switch_to_vi)

    def switch_to_vi() -> None:
        if count_located_notes() != 2:
            print("translation note was not persisted")
            app.exit(7)
            return
        reader._start_vietnamese_pdf_import(vi_path)

    def ready(pages: int) -> None:
        states.append((reader.current_document_version, pages))
        if reader.current_document_version == "en" and len(states) == 1:
            QTimer.singleShot(500, begin_note_flow)
            return
        if reader.current_document_version == "vi":
            if reader.reader_sidebar.notes.note_cards:
                print("EN notes leaked into VI sidebar")
                app.exit(12)
                return
            if reader.reader_sidebar.notes.scratchpad.toPlainText() != "Shared scratchpad":
                print("scratchpad was not shared with VI")
                app.exit(13)
                return
            vi_note_id = NoteRepository.create(
                paper_id,
                "Ghi chu tieng Viet",
                title="VI",
                kind="manual",
                document_version="vi",
            )
            reader.reader_sidebar.notes.refresh_for_document()
            if set(reader.reader_sidebar.notes.note_cards) != {vi_note_id}:
                print("VI note separation smoke failed")
                app.exit(14)
                return
            QTimer.singleShot(200, lambda: reader._switch_document_version("en"))
            return
        if set(reader.reader_sidebar.notes.note_cards) == set():
            print("EN notes were not restored after switch")
            app.exit(15)
            return
        if reader.reader_sidebar.notes.scratchpad.toPlainText() != "Shared scratchpad":
            print("scratchpad changed after EN/VI switch")
            app.exit(16)
            return
        reader.close()
        # Re-run startup initialization before the persistence assertions. This
        # exercises the same on-disk records a fresh process will reopen.
        database.init_database()
        cached = reader.document_version_repository.get(paper_id, "vi")
        with database.connection_scope() as connection:
            vi_anchors = connection.execute(
                "SELECT COUNT(*) FROM annotation_anchors WHERE document_version='vi'"
            ).fetchone()[0]
            versions = dict(
                connection.execute(
                    """
                    SELECT document_version, COUNT(*)
                    FROM notes
                    WHERE kind != 'scratchpad'
                    GROUP BY document_version
                    """
                ).fetchall()
            )
        if cached is None or vi_anchors != 0 or versions != {"en": 2, "vi": 1}:
            print("document-version persistence smoke failed")
            app.exit(17)
            return
        print(json.dumps({
            "states": states,
            "cached": cached is not None,
            "notes": count_located_notes(),
            "viAnchors": vi_anchors,
            "noteVersions": versions,
        }, ensure_ascii=False))
        app.quit()

    reader.document_ready.connect(ready)
    reader.show()
    QTimer.singleShot(30000, app.quit)
    result = app.exec()
    ok = states == [("en", 2), ("vi", 2), ("en", 2)]
    temporary.cleanup()
    return result if result else (0 if ok else 2)


if __name__ == "__main__":
    raise SystemExit(main())
