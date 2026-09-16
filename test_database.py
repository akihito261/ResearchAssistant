"""Legacy database smoke test, isolated from the user's real library.

This module used to insert a fixed fake paper into ``data/research.db`` merely
by being imported.  Keeping the filename is convenient for existing commands,
but all test data now lives in a temporary database.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.database.database as database
from app.database.paper_repository import PaperRepository


class DatabaseSmokeTest(unittest.TestCase):
    def test_paper_round_trip_uses_temporary_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_directory = Path(temporary_directory) / "data"
            database_path = data_directory / "research.db"
            with (
                patch.object(database, "DATA_DIR", data_directory),
                patch.object(database, "DATABASE_PATH", database_path),
            ):
                database.init_database()
                paper_id = PaperRepository.add_paper(
                    title="Test Research Paper",
                    authors="Nguyen et al.",
                    year=2026,
                    doi="10.1234/test-paper",
                    file_path="temporary-test-paper.pdf",
                    file_hash="temporary-test-hash",
                    total_pages=12,
                )

                paper = PaperRepository.get_paper_by_id(paper_id)
                self.assertIsNotNone(paper)
                self.assertEqual(paper["title"], "Test Research Paper")
                self.assertEqual(len(PaperRepository.get_all_papers()), 1)


if __name__ == "__main__":
    unittest.main()
