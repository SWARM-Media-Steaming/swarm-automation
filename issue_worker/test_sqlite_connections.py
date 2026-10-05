"""Issue #384: app-wide SQLite `with connect()` must close the handle."""

from __future__ import annotations

import gc
import sqlite3
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ai_execution_history import ClosingConnection, ExecutionHistoryRepository  # noqa: E402
from diagnostic_store import DiagnosticRepository  # noqa: E402
from engineering_knowledge import KnowledgeStore  # noqa: E402


def _assert_closed(test: unittest.TestCase, connection: sqlite3.Connection) -> None:
    with test.assertRaises(sqlite3.ProgrammingError) as raised:
        connection.execute("SELECT 1")
    test.assertIn("closed", str(raised.exception).lower())


class SqliteConnectClosesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="swarm-sqlite-close.")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "swarm-automation.sqlite3"

    def test_history_with_block_commits_and_closes(self) -> None:
        repository = ExecutionHistoryRepository(self.path)
        with repository.connect() as database:
            self.assertIsInstance(database, ClosingConnection)
            database.execute("CREATE TABLE issue384_probe(x INTEGER)")
            database.execute("INSERT INTO issue384_probe VALUES (7)")
            held = database
        _assert_closed(self, held)
        with repository.connect() as database:
            self.assertEqual(database.execute("SELECT x FROM issue384_probe").fetchone()[0], 7)

    def test_history_with_block_rolls_back_on_error(self) -> None:
        repository = ExecutionHistoryRepository(self.path)
        with repository.connect() as database:
            database.execute("CREATE TABLE issue384_probe(x INTEGER)")
            database.execute("INSERT INTO issue384_probe VALUES (7)")
        with self.assertRaises(RuntimeError):
            with repository.connect() as database:
                database.execute("INSERT INTO issue384_probe VALUES (8)")
                raise RuntimeError("boom")
        with repository.connect() as database:
            self.assertEqual(
                [row[0] for row in database.execute("SELECT x FROM issue384_probe")],
                [7],
            )

    def test_history_with_block_does_not_emit_unclosed_database_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            repository = ExecutionHistoryRepository(self.path)
            with repository.connect() as database:
                database.execute("SELECT 1")
            del database
            del repository
            gc.collect()
        leftovers = [item for item in caught if "unclosed database" in str(item.message)]
        self.assertEqual(leftovers, [])

    def test_diagnostic_and_knowledge_stores_close_the_same_way(self) -> None:
        diagnostic = DiagnosticRepository(self.path)
        knowledge = KnowledgeStore(self.path)
        with diagnostic.connect() as database:
            held_diagnostic = database
        with knowledge.connect() as database:
            held_knowledge = database
        _assert_closed(self, held_diagnostic)
        _assert_closed(self, held_knowledge)
        self.assertIsInstance(held_diagnostic, ClosingConnection)
        self.assertIsInstance(held_knowledge, ClosingConnection)


if __name__ == "__main__":
    unittest.main()
