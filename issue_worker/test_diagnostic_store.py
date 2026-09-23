#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from diagnostic_store import DiagnosticProblem, DiagnosticRepository


def problem(**overrides: object) -> DiagnosticProblem:
    fields: dict[str, object] = dict(
        repository="SWARM-Media-Steaming/swarm",
        signature="sig-abc",
        source="ai",
        provider="claude",
        model="claude-sonnet-5",
        explanation="It broke because of X.",
        confidence="high",
        evidence=({"source": "cron.log", "excerpt": "ERROR: boom"},),
        actionable_items=("Retry the fetch",),
        is_bug=True,
        suggested_issue_title="Boom",
        suggested_issue_body="Standalone triage body.",
    )
    fields.update(overrides)
    return DiagnosticProblem(**fields)  # type: ignore[arg-type]


class DiagnosticStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp(prefix="swarm-diagnostic-store."))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state, ignore_errors=True))
        self.db_path = self.state / "swarm-automation.sqlite3"

    def now(self, offset_seconds: int = 0) -> str:
        return (dt.datetime.now().astimezone() + dt.timedelta(seconds=offset_seconds)).isoformat(
            timespec="seconds"
        )

    def test_migrate_is_idempotent_and_creates_the_table_once(self) -> None:
        DiagnosticRepository(self.db_path)
        DiagnosticRepository(self.db_path)  # second construction must not raise or duplicate
        repo = DiagnosticRepository(self.db_path)
        with repo.connect() as database:
            versions = [row[0] for row in database.execute("SELECT version FROM diagnostic_schema_migrations")]
        self.assertEqual(versions, [1])

    def test_insert_and_get_round_trip_every_field(self) -> None:
        repo = DiagnosticRepository(self.db_path)
        problem_id = repo.insert("run-1", self.now(), problem())
        record = repo.get(problem_id)
        assert record is not None
        self.assertEqual(record["repository"], "SWARM-Media-Steaming/swarm")
        self.assertEqual(record["signature"], "sig-abc")
        self.assertEqual(record["source"], "ai")
        self.assertEqual(record["provider"], "claude")
        self.assertEqual(record["model"], "claude-sonnet-5")
        self.assertEqual(record["explanation"], "It broke because of X.")
        self.assertEqual(record["confidence"], "high")
        self.assertEqual(record["evidence"], [{"source": "cron.log", "excerpt": "ERROR: boom"}])
        self.assertEqual(record["actionable_items"], ["Retry the fetch"])
        self.assertIs(record["is_bug"], True)
        self.assertEqual(record["suggested_issue_title"], "Boom")
        self.assertEqual(record["suggested_issue_body"], "Standalone triage body.")
        self.assertIsNone(record["filed_issue_url"])
        self.assertIsNone(record["filed_at"])

    def test_get_missing_problem_returns_none(self) -> None:
        repo = DiagnosticRepository(self.db_path)
        self.assertIsNone(repo.get("does-not-exist"))

    def test_find_by_signature_returns_none_when_nothing_matches(self) -> None:
        repo = DiagnosticRepository(self.db_path)
        self.assertIsNone(repo.find_by_signature("nope"))

    def test_find_by_signature_honors_the_freshness_window(self) -> None:
        repo = DiagnosticRepository(self.db_path)
        repo.insert("run-1", self.now(offset_seconds=-1000), problem(signature="stale-sig"))
        self.assertIsNone(repo.find_by_signature("stale-sig", max_age_seconds=900))
        repo.insert("run-2", self.now(offset_seconds=-10), problem(signature="fresh-sig"))
        found = repo.find_by_signature("fresh-sig", max_age_seconds=900)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["signature"], "fresh-sig")

    def test_find_by_signature_returns_the_newest_match(self) -> None:
        repo = DiagnosticRepository(self.db_path)
        repo.insert("run-1", self.now(offset_seconds=-20), problem(signature="dup", explanation="older"))
        repo.insert("run-2", self.now(offset_seconds=-5), problem(signature="dup", explanation="newer"))
        found = repo.find_by_signature("dup", max_age_seconds=900)
        assert found is not None
        self.assertEqual(found["explanation"], "newer")

    def test_mark_filed_sets_url_and_timestamp(self) -> None:
        repo = DiagnosticRepository(self.db_path)
        problem_id = repo.insert("run-1", self.now(), problem())
        repo.mark_filed(problem_id, "https://example.invalid/issues/42", self.now())
        record = repo.get(problem_id)
        assert record is not None
        self.assertEqual(record["filed_issue_url"], "https://example.invalid/issues/42")
        self.assertIsNotNone(record["filed_at"])

    def test_insert_sanitizes_secrets_in_free_text_fields(self) -> None:
        repo = DiagnosticRepository(self.db_path)
        problem_id = repo.insert(
            "run-1", self.now(),
            problem(
                explanation="Auth failed: Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz012345",
                evidence=({"source": "cron.log", "excerpt": "token=sk-abcdefghijklmnopqrstuvwx"},),
                actionable_items=("Rotate api_key=abcdefghijklmnop123456",),
            ),
        )
        record = repo.get(problem_id)
        assert record is not None
        self.assertNotIn("ghp_", record["explanation"])
        self.assertIn("[REDACTED]", record["explanation"])
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", record["evidence"][0]["excerpt"])
        self.assertNotIn("abcdefghijklmnop123456", record["actionable_items"][0])

    def test_two_repositories_share_the_same_database_file_without_conflict(self) -> None:
        # diagnostic_explanations lives in the same sqlite file as
        # ai_executions; opening both repositories against it must not raise.
        from ai_execution_history import ExecutionHistoryRepository

        DiagnosticRepository(self.db_path)
        ExecutionHistoryRepository(self.db_path)
        DiagnosticRepository(self.db_path)
        with DiagnosticRepository(self.db_path).connect() as database:
            tables = {
                row[0]
                for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        self.assertIn("diagnostic_explanations", tables)
        self.assertIn("ai_executions", tables)


if __name__ == "__main__":
    unittest.main()
