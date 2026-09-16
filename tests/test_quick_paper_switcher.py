from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication

from app.ui.quick_paper_switcher import (
    CURRENT_ROLE,
    DRAWER_CLOSE_DURATION_MS,
    DRAWER_OPEN_DURATION_MS,
    EDGE_HIT_WIDTH,
    EDGE_INDICATOR_HEIGHT,
    EDGE_INDICATOR_WIDTH,
    KIND_ROLE,
    OPEN_ROLE,
    PAPER_ROLE,
    QuickPaperSwitcher,
    quick_drawer_width,
)


PAPERS = (
    {
        "id": 1,
        "title": "Current compression paper",
        "authors": "Ada Author",
        "year": 2023,
        "status": "Reading",
    },
    {
        "id": 2,
        "title": "Another open paper",
        "authors": "Bao Writer",
        "year": 2024,
        "status": "Unread",
    },
    {
        "id": 3,
        "title": "ROI coding in the library",
        "authors": "Chi Researcher",
        "year": 2025,
        "status": "Completed",
    },
)


class QuickPaperSwitcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.drawer = QuickPaperSwitcher()
        self.drawer.resize(304, 700)
        self.drawer.set_catalog(PAPERS, {1, 2}, 1)

    def tearDown(self) -> None:
        self.drawer.close()
        self.drawer.deleteLater()
        self.app.processEvents()

    def _paper_items(self):
        return [
            self.drawer.paper_list.item(row)
            for row in range(self.drawer.paper_list.count())
            if self.drawer.paper_list.item(row).data(KIND_ROLE) == "paper"
        ]

    def test_open_and_library_sections_do_not_duplicate_papers(self) -> None:
        self.assertEqual(self.drawer.paper_ids(), [1, 2, 3])
        items = self._paper_items()
        self.assertTrue(items[0].data(CURRENT_ROLE))
        self.assertTrue(items[0].data(OPEN_ROLE))
        self.assertTrue(items[1].data(OPEN_ROLE))
        self.assertFalse(items[2].data(OPEN_ROLE))

    def test_search_matches_title_author_and_year_locally(self) -> None:
        self.drawer.search.setText("roi 2025")
        self.assertEqual(self.drawer.paper_ids(), [3])
        self.drawer.search.setText("bao")
        self.assertEqual(self.drawer.paper_ids(), [2])
        self.drawer.search.clear()
        self.assertEqual(self.drawer.paper_ids(), [1, 2, 3])

    def test_click_emits_existing_paper_id(self) -> None:
        selected: list[int] = []
        self.drawer.paper_selected.connect(selected.append)
        item = next(
            item
            for item in self._paper_items()
            if int(item.data(PAPER_ROLE)["id"]) == 2
        )
        self.drawer._activate_item(item)
        self.assertEqual(selected, [2])

    def test_width_is_logical_bounded_and_fractional_before_cap(self) -> None:
        self.assertEqual(quick_drawer_width(500), 200)
        self.assertEqual(quick_drawer_width(760), 304)
        self.assertEqual(quick_drawer_width(1800), 304)
        self.assertEqual(quick_drawer_width(250), 100)
        self.assertEqual(quick_drawer_width(0), 0)
        self.assertEqual(DRAWER_OPEN_DURATION_MS, 210)
        self.assertEqual(DRAWER_CLOSE_DURATION_MS, 175)
        self.assertEqual(EDGE_HIT_WIDTH, 10)
        self.assertEqual((EDGE_INDICATOR_WIDTH, EDGE_INDICATOR_HEIGHT), (3, 64))


if __name__ == "__main__":
    unittest.main()
