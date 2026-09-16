from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pymupdf
from PySide6.QtCore import QEasingCurve
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTabBar

import app.database.database as database
from app.database.paper_repository import PaperRepository
from app.database.ai_repository import AIRepository
from app.pdf.pdfjs_scheme import register_pdfjs_scheme


def _make_pdf(path: Path, label: str) -> None:
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), label)
    document.save(path)
    document.close()


def main() -> int:
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    old_data_dir = database.DATA_DIR
    old_database_path = database.DATABASE_PATH
    database.DATA_DIR = root / "data"
    database.DATABASE_PATH = database.DATA_DIR / "research.db"
    try:
        database.init_database()
        ids: list[int] = []
        for index, (title, year, status) in enumerate(
            (
                ("Current compression paper", 2023, "Reading"),
                ("Another open paper", 2024, "Unread"),
                ("ROI coding in the library", 2025, "Completed"),
            ),
            start=1,
        ):
            path = root / f"paper-{index}.pdf"
            _make_pdf(path, title)
            paper_id = PaperRepository.add_paper(
                title,
                f"Author {index}",
                year,
                None,
                str(path),
                f"{index}" * 64,
                1,
            )
            PaperRepository.set_status(paper_id, status)
            ids.append(paper_id)

        register_pdfjs_scheme()
        app = QApplication.instance() or QApplication([])
        from app.ui.main_window import MainWindow

        main_window = MainWindow(start_background_tasks=False)
        main_window.show()
        reader_a = main_window.open_paper_by_id(ids[0])
        reader_b = main_window.open_paper_by_id(ids[1])
        assert reader_a is not None and reader_b is not None
        QTest.qWait(1200)

        workspace = main_window._reader_workspace
        assert workspace.isVisible()
        assert not main_window.isVisible()
        assert workspace.reader_stack.count() == 2
        assert workspace.reader_stack.currentWidget() is reader_b
        assert reader_a.parentWidget() is workspace.reader_stack
        assert reader_b.parentWidget() is workspace.reader_stack
        assert not reader_a.isWindow() and not reader_b.isWindow()
        visible_top_levels = {
            widget for widget in QApplication.topLevelWidgets() if widget.isVisible()
        }
        assert visible_top_levels == {workspace}

        main_window._activate_workspace_paper(ids[0])
        QApplication.processEvents()
        pointer_surface = {"widget": None}
        reader_a._global_pointer_inside = (
            lambda widget: pointer_surface["widget"] is widget
        )
        # A quick edge crossing must not survive the 280 ms opening delay.
        pointer_surface["widget"] = reader_a.quick_paper_hotspot
        reader_a._quick_paper_hotspot_entered()
        QTest.qWait(60)
        pointer_surface["widget"] = None
        reader_a._quick_paper_hotspot_left()
        QTest.qWait(310)
        assert not reader_a.quick_paper_switcher.isVisible()
        before_splitter = reader_a.body_splitter.sizes()
        before_content = reader_a.content_stack.geometry()
        pdfjs_resize_calls = {"count": 0}
        reader_a._notify_pdfjs_resize = lambda: pdfjs_resize_calls.__setitem__(
            "count", pdfjs_resize_calls["count"] + 1
        )

        # Sustained hover opens the overlay without touching splitter/PDF geometry.
        pointer_surface["widget"] = reader_a.quick_paper_hotspot
        reader_a._quick_paper_hotspot_entered()
        QTest.qWait(700)
        assert reader_a.quick_paper_switcher.isVisible()
        assert reader_a.quick_paper_switcher.pos() == reader_a._quick_switch_open_position
        assert reader_a._quick_switch_animation.duration() == 210
        assert (
            reader_a._quick_switch_animation.easingCurve().type()
            == QEasingCurve.Type.OutCubic
        )
        assert reader_a.quick_paper_switcher.parent() is reader_a.centralWidget()
        assert reader_a.quick_paper_switcher.parent() is not reader_a.body_splitter
        assert (
            reader_a.quick_paper_switcher.x() - reader_a.body_splitter.geometry().x()
            == 10
        )
        assert (
            reader_a.quick_paper_switcher.y() - reader_a.body_splitter.geometry().y()
            == 11
        )
        resize_positioned = (
            reader_a.quick_paper_switcher.pos()
            == reader_a._quick_switch_open_position
        )
        assert reader_a.quick_paper_switcher.height() == max(
            0, reader_a.body_splitter.height() - 22
        )
        assert reader_a.quick_paper_hotspot.width() == 10
        assert reader_a.quick_paper_hotspot.indicator.size().width() == 3
        assert reader_a.quick_paper_hotspot.indicator.size().height() == 64
        assert reader_a.body_splitter.sizes() == before_splitter
        assert reader_a.content_stack.geometry() == before_content
        assert pdfjs_resize_calls["count"] == 0
        overlay_geometry_unchanged = (
            reader_a.body_splitter.sizes() == before_splitter
            and reader_a.content_stack.geometry() == before_content
            and pdfjs_resize_calls["count"] == 0
        )
        assert reader_a.quick_paper_switcher.paper_ids() == ids

        reader_a.quick_paper_switcher.search.setText("roi 2025")
        assert reader_a.quick_paper_switcher.paper_ids() == [ids[2]]
        reader_a.quick_paper_switcher.search.clear()

        # Reader resize keeps the floating margins/size contract.
        workspace.showNormal()
        workspace.resize(1120, 760)
        QTest.qWait(260)
        assert reader_a.quick_paper_switcher.isVisible()
        assert reader_a.quick_paper_switcher.pos() == reader_a._quick_switch_open_position
        assert (
            reader_a.quick_paper_switcher.x() - reader_a.body_splitter.geometry().x()
            == 10
        )
        assert (
            reader_a.quick_paper_switcher.y() - reader_a.body_splitter.geometry().y()
            == 11
        )

        # Leaving then re-entering within the close delay keeps it open.
        pointer_surface["widget"] = None
        reader_a._schedule_quick_paper_close()
        QTest.qWait(100)
        pointer_surface["widget"] = reader_a.quick_paper_switcher
        reader_a._quick_paper_drawer_entered()
        QTest.qWait(180)
        assert reader_a.quick_paper_switcher.isVisible()

        # Reversing a running close animation starts at the current position.
        reader_a._hide_quick_paper_switcher()
        assert reader_a._quick_switch_animation.duration() == 175
        assert (
            reader_a._quick_switch_animation.easingCurve().type()
            == QEasingCurve.Type.InOutCubic
        )
        QTest.qWait(70)
        reversal_position = reader_a.quick_paper_switcher.pos()
        reader_a._quick_paper_drawer_entered()
        assert reader_a.quick_paper_switcher.pos() == reversal_position
        QTest.qWait(230)
        assert reader_a.quick_paper_switcher.pos() == reader_a._quick_switch_open_position

        # Escape has priority only while the drawer is open.
        reader_a.quick_switcher_escape_shortcut.activated.emit()
        QTest.qWait(320)
        assert not reader_a.quick_paper_switcher.isVisible()

        # Existing Reader identity/state is retained through MainWindow's normal path.
        reader_b._smoke_state_marker = "retained"
        reader_a._quick_paper_selected(ids[1])
        QApplication.processEvents()
        assert main_window._pdf_readers[ids[1]] is reader_b
        assert reader_b._smoke_state_marker == "retained"

        # Left-clicking always adds/activates a tab and cannot duplicate it.
        reader_a._quick_paper_selected(ids[2])
        QApplication.processEvents()
        reader_c = main_window._pdf_readers[ids[2]]
        reader_c._quick_paper_selected(ids[2])
        QApplication.processEvents()
        assert main_window._pdf_readers[ids[2]] is reader_c
        assert main_window._workspace_paper_ids == ids
        assert len(main_window._workspace_paper_ids) == 3
        assert workspace.reader_stack.count() == 3

        # AI comparison membership is explicit and shared only by its members.
        main_window._activate_workspace_paper(ids[0])
        reader_a.reader_sidebar.solo_ai.composer.setPlainText("solo A draft")
        reader_a.workspace_tabs.add_to_ai_requested.emit(ids[1])
        QApplication.processEvents()
        group_panel = main_window._ai_group_panel
        assert group_panel is not None
        assert main_window._ai_group_paper_ids == ids[:2]
        assert reader_a.reader_sidebar.ai is group_panel
        assert group_panel.context_label.text() == "Comparing 2 papers"
        assert group_panel.focus_current_button.isVisibleTo(group_panel)
        group_id = int(main_window._ai_group_conversation_id)
        assert main_window._ai_group_aliases == {ids[0]: "P1", ids[1]: "P2"}
        for tab_bar in (reader_a.workspace_tabs, reader_b.workspace_tabs):
            aliases = {
                int(tab_bar.tabData(index)): (
                    controls.findChild(QLabel, "workspaceTabAlias").text()
                    if (
                        (controls := tab_bar.tabButton(
                            index, QTabBar.ButtonPosition.RightSide
                        ))
                        and controls.findChild(QLabel, "workspaceTabAlias")
                    )
                    else ""
                )
                for index in range(tab_bar.count())
            }
            assert aliases[ids[0]] == "P1" and aliases[ids[1]] == "P2"
            for index in range(tab_bar.count()):
                controls = tab_bar.tabButton(
                    index, QTabBar.ButtonPosition.RightSide
                )
                assert tab_bar.tabButton(
                    index, QTabBar.ButtonPosition.LeftSide
                ) is None
                assert controls is not None
                badge = controls.findChild(QLabel, "workspaceTabAlias")
                close = controls.findChild(QPushButton, "workspaceTabClose")
                assert badge is not None and close is not None
                assert controls.layout().indexOf(badge) < controls.layout().indexOf(close)
                assert badge.toolTip() == tab_bar.tabToolTip(index)

        # Native tab movement updates only workspace order, never group aliases.
        tab_bar = reader_a.workspace_tabs
        index_a = next(i for i in range(tab_bar.count()) if tab_bar.tabData(i) == ids[0])
        index_b = next(i for i in range(tab_bar.count()) if tab_bar.tabData(i) == ids[1])
        tab_bar.moveTab(index_b, index_a)
        QApplication.processEvents()
        assert main_window._workspace_paper_ids == [ids[1], ids[0], ids[2]]
        assert main_window._ai_group_aliases == {ids[0]: "P1", ids[1]: "P2"}
        reordered_aliases = {
            int(tab_bar.tabData(index)): tab_bar.tabButton(
                index, QTabBar.ButtonPosition.RightSide
            ).findChild(QLabel, "workspaceTabAlias").text()
            for index in range(tab_bar.count())
        }
        assert reordered_aliases[ids[0]] == "P1"
        assert reordered_aliases[ids[1]] == "P2"
        group_panel.composer.setPlainText("shared group draft")
        reader_a._show_sidebar("ai")
        QApplication.processEvents()
        assert not reader_a.sidebar_shell.isHidden()
        assert reader_a.reader_sidebar.stack.currentWidget() is group_panel
        main_window._activate_workspace_paper(ids[1])
        QApplication.processEvents()
        assert not reader_b.sidebar_shell.isHidden()
        assert reader_b.reader_sidebar.stack.currentWidget() is group_panel
        assert reader_b.reader_sidebar.ai is group_panel
        assert group_panel.composer.toPlainText() == "shared group draft"
        main_window._activate_workspace_paper(ids[2])
        QApplication.processEvents()
        assert not reader_c.sidebar_shell.isHidden()
        assert reader_c.reader_sidebar.stack.currentWidget() is group_panel
        assert reader_c.reader_sidebar.ai is group_panel
        assert reader_a.reader_sidebar.solo_ai.composer.toPlainText() == "solo A draft"
        main_window._activate_workspace_paper(ids[0])
        QApplication.processEvents()
        assert not reader_a.sidebar_shell.isHidden()
        assert reader_a.reader_sidebar.stack.currentWidget() is group_panel

        # New Chat explicitly enters Solo(A); the saved G1 remains untouched.
        group_panel.new_chat()
        QApplication.processEvents()
        assert main_window._active_ai_conversation_type == "solo"
        assert main_window._active_ai_conversation_id is None
        assert reader_a.reader_sidebar.ai is reader_a.reader_sidebar.solo_ai
        assert not main_window._ai_group_aliases
        for index in range(reader_a.workspace_tabs.count()):
            controls = reader_a.workspace_tabs.tabButton(
                index, QTabBar.ButtonPosition.RightSide
            )
            assert not controls.findChild(
                QLabel, "workspaceTabAlias"
            ).isVisible()

        # Selecting G1 from Solo history restores the exact conversation state.
        reader_a.reader_sidebar.solo_ai.select_chat(group_id)
        QApplication.processEvents()
        group_panel = main_window._ai_group_panel
        assert group_panel is not None
        assert main_window._active_ai_conversation_type == "group"
        assert main_window._active_ai_conversation_id == group_id
        assert main_window._ai_group_aliases == {ids[0]: "P1", ids[1]: "P2"}

        # Moving between tabs keeps the live Reader/WebView instances intact.
        web_view_ids = {
            paper_id: id(main_window._pdf_readers[paper_id].web_view)
            for paper_id in ids
        }
        load_generations = {
            paper_id: main_window._pdf_readers[paper_id]._load_generation
            for paper_id in ids
        }
        retained_tab_button = reader_a.workspace_tabs.tabButton(
            0, QTabBar.ButtonPosition.RightSide
        )
        for paper_id in (ids[0], ids[2], ids[1]):
            main_window._activate_workspace_paper(paper_id)
        assert web_view_ids == {
            paper_id: id(main_window._pdf_readers[paper_id].web_view)
            for paper_id in ids
        }
        assert load_generations == {
            paper_id: main_window._pdf_readers[paper_id]._load_generation
            for paper_id in ids
        }
        assert reader_a.workspace_tabs.tabButton(
            0, QTabBar.ButtonPosition.RightSide
        ) is retained_tab_button

        # One shared group conversation is discoverable from both papers.
        AIRepository.append_message(group_id, "user", "Compare these papers")
        for paper_id in ids[:2]:
            group_rows = [
                row
                for row in AIRepository.list_conversations(paper_id)
                if row["conversation_type"] == "group"
            ]
            assert [int(row["id"]) for row in group_rows] == [group_id]
            assert [int(member["alias_index"]) for member in group_rows[0]["members"]] == [1, 2]
        assert group_panel.active_conversation_id == group_id
        assert AIRepository.list_messages(group_id)[0]["content"] == "Compare these papers"
        assert group_panel.empty_state.compare_group.isVisibleTo(group_panel.empty_state)
        assert not group_panel.empty_state.title.isVisibleTo(group_panel.empty_state)

        # Closing every member tab preserves G1 and its persisted aliases.
        main_window._activate_workspace_paper(ids[1])
        reader_a.close()
        QApplication.processEvents()
        reader_b.close()
        QApplication.processEvents()
        assert ids[0] not in main_window._workspace_paper_ids
        assert ids[1] not in main_window._workspace_paper_ids
        assert main_window._ai_group_paper_ids == ids[:2]
        assert main_window._active_ai_conversation_type == "group"
        assert group_panel.context_label.text() == "Comparing 2 papers"
        persisted_g1 = AIRepository.get_conversation(group_id)
        assert persisted_g1 is not None
        assert [int(member["id"]) for member in persisted_g1["members"]] == ids[:2]

        # Exact regression: Solo(C) -> G1 must hydrate A/B before one UI refresh.
        group_panel.new_chat()
        QApplication.processEvents()
        assert main_window._active_ai_conversation_type == "solo"
        assert main_window._active_paper_id == ids[2]
        for _cycle in range(3):
            assert main_window._activate_ai_conversation(group_id)
            QApplication.processEvents()
            restored_panel = main_window._ai_group_panel
            assert restored_panel is not None
            assert restored_panel.context_label.text() == "Comparing 2 papers"
            assert main_window._ai_group_paper_ids == ids[:2]
            assert main_window._ai_group_aliases == {
                ids[0]: "P1",
                ids[1]: "P2",
            }
            c_index = next(
                index
                for index in range(reader_c.workspace_tabs.count())
                if int(reader_c.workspace_tabs.tabData(index)) == ids[2]
            )
            c_badge = reader_c.workspace_tabs.tabButton(
                c_index, QTabBar.ButtonPosition.RightSide
            ).findChild(QLabel, "workspaceTabAlias")
            assert c_badge.text() == "" and c_badge.isHidden()
            restored_panel.new_chat()
            QApplication.processEvents()
            assert main_window._active_ai_conversation_type == "solo"
            assert c_badge.text() == "" and c_badge.isHidden()

        assert main_window._activate_ai_conversation(group_id)
        QApplication.processEvents()
        group_panel = main_window._ai_group_panel
        assert group_panel is not None
        main_window._open_group_history_paper(group_id, ids[0])
        QApplication.processEvents()
        reopened_a = main_window._pdf_readers[ids[0]]
        a_index = next(
            index
            for index in range(reopened_a.workspace_tabs.count())
            if int(reopened_a.workspace_tabs.tabData(index)) == ids[0]
        )
        assert reopened_a.workspace_tabs.tabButton(
            a_index, QTabBar.ButtonPosition.RightSide
        ).findChild(QLabel, "workspaceTabAlias").text() == "P1"
        c_index = next(
            index
            for index in range(reopened_a.workspace_tabs.count())
            if int(reopened_a.workspace_tabs.tabData(index)) == ids[2]
        )
        assert reopened_a.workspace_tabs.tabButton(
            c_index, QTabBar.ButtonPosition.RightSide
        ).findChild(QLabel, "workspaceTabAlias").text() == ""

        group_panel.history_popup.refresh()
        member_chips = group_panel.history_popup.findChildren(
            QPushButton, "aiHistoryMemberChip"
        )
        chip_b = next(
            chip for chip in member_chips if chip.toolTip() == "Another open paper"
        )
        chip_b.click()
        QApplication.processEvents()
        reopened_b = main_window._pdf_readers[ids[1]]
        assert reopened_b is not reader_b
        assert main_window._ai_group_aliases[ids[1]] == "P2"
        QApplication.processEvents()
        badge_by_paper = {
            int(reopened_b.workspace_tabs.tabData(index)): reopened_b.workspace_tabs.tabButton(
                index, QTabBar.ButtonPosition.RightSide
            ).findChild(QLabel, "workspaceTabAlias").text()
            for index in range(reopened_b.workspace_tabs.count())
        }
        assert badge_by_paper[ids[1]] == "P2"

        # Solo(B) can create a distinct G2 where aliases differ from G1.
        group_panel.new_chat()
        QApplication.processEvents()
        assert main_window._active_ai_conversation_type == "solo"
        reopened_a = main_window.open_paper_by_id(ids[0])
        assert reopened_a is not None and reopened_a is not reader_a
        main_window._activate_workspace_paper(ids[1])
        reopened_b.workspace_tabs.add_to_ai_requested.emit(ids[0])
        QApplication.processEvents()
        group_two_id = int(main_window._active_ai_conversation_id or 0)
        assert group_two_id and group_two_id != group_id
        assert main_window._ai_group_aliases == {
            ids[1]: "P1",
            ids[0]: "P2",
        }

        group_two_panel = main_window._ai_group_panel
        assert group_two_panel is not None
        group_two_panel.select_chat(group_id)
        QApplication.processEvents()
        assert main_window._ai_group_aliases == {
            ids[0]: "P1",
            ids[1]: "P2",
        }
        group_one_panel = main_window._ai_group_panel
        assert group_one_panel is not None
        group_one_panel.select_chat(group_two_id)
        QApplication.processEvents()
        assert main_window._ai_group_aliases == {
            ids[1]: "P1",
            ids[0]: "P2",
        }
        main_window._ai_group_panel.new_chat()
        QApplication.processEvents()
        assert main_window._active_ai_conversation_type == "solo"
        assert not main_window._ai_group_aliases
        reopened_b.reader_sidebar.solo_ai.select_chat(group_id)
        QApplication.processEvents()
        assert main_window._active_ai_conversation_id == group_id
        assert main_window._ai_group_aliases == {
            ids[0]: "P1",
            ids[1]: "P2",
        }

        # Each citation activates its own page; a closed target opens in-stack.
        citation_destinations: list[tuple[int, object]] = []
        reopened_a._navigate_to_english_citation = lambda citation: citation_destinations.append(
            (ids[0], citation)
        )
        citation_a = {"paper_id": ids[0], "resolved_page": 6, "evidence": "A"}
        citation_b = {"paper_id": ids[1], "resolved_page": 11, "evidence": "B"}
        main_window._open_workspace_citation(ids[0], citation_a)
        assert workspace.reader_stack.currentWidget() is reopened_a
        reopened_b.close()
        QApplication.processEvents()
        from app.ui.pdf_reader_window import PdfReaderWindow
        original_navigation = PdfReaderWindow._navigate_to_english_citation
        PdfReaderWindow._navigate_to_english_citation = (
            lambda self, citation: citation_destinations.append((self.paper_id, citation))
        )
        try:
            main_window._open_workspace_citation(ids[1], citation_b)
        finally:
            PdfReaderWindow._navigate_to_english_citation = original_navigation
        citation_reader_b = main_window._pdf_readers[ids[1]]
        assert workspace.reader_stack.currentWidget() is citation_reader_b
        assert citation_destinations == [(ids[0], citation_a), (ids[1], citation_b)]
        assert main_window._ai_group_paper_ids == ids[:2]

        # Library is hidden while reading and returns without destroying pages.
        main_window._show_library_from_workspace()
        QApplication.processEvents()
        assert main_window.isVisible() and not workspace.isVisible()
        assert workspace.reader_stack.count() == 3
        main_window._activate_workspace_paper(ids[1])
        QApplication.processEvents()
        assert workspace.isVisible() and not main_window.isVisible()

        print(
            json.dumps(
                {
                    "ok": True,
                    "open_ids": sorted(main_window._pdf_readers),
                    "workspace_pages": workspace.reader_stack.count(),
                    "shared_ai_group_verified": True,
                    "drawer_width": reopened_a.quick_paper_switcher.width(),
                    "open_delay_ms": reopened_a._quick_switch_open_timer.interval(),
                    "close_delay_ms": reopened_a._quick_switch_close_timer.interval(),
                    "open_animation_ms": 210,
                    "close_animation_ms": 175,
                    "reader_resize_changed_splitter": reopened_a.body_splitter.sizes()
                    != before_splitter,
                    "overlay_geometry_unchanged": overlay_geometry_unchanged,
                    "pdfjs_resize_calls_during_overlay": 0,
                    "resize_positioned": resize_positioned,
                }
            )
        )
        main_window.close()
        QApplication.processEvents()
        return 0
    finally:
        database.DATA_DIR = old_data_dir
        database.DATABASE_PATH = old_database_path
        temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
