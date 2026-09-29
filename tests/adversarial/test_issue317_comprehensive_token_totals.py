"""Issue #317 adversarial tests: comprehensive edge case coverage for NULL preservation
and started_at date filtering in token_usage_totals.

These tests verify:
1. Edge cases in date filtering (boundaries, formats, time zones)
2. Edge cases in NULL handling (all-NULL rows, partial NULL fields)
3. Numeric precision and rounding
4. Cross-column consistency (NULLs propagate correctly)
5. Interaction between date filtering and NULL preservation
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "issue_worker"))

from ai_execution_history import ExecutionHistoryRepository  # noqa: E402

REPO = "test/repo"


class NullPreservationEdgeCasesTests(unittest.TestCase):
    """Verify NULL values are preserved in all fields, not coalesced to 0/0.0."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = ExecutionHistoryRepository(Path(tmp.name) / "h.sqlite3")

    def add(self, row_id, started, values=(None,) * 6, **extra):
        """Insert a row with optional token fields."""
        cols = ["id", "repository", "issue_number", "started_at", "created_at",
                "agent_type", "input_tokens", "output_tokens", "reasoning_tokens",
                "cached_input_tokens", "total_tokens", "estimated_cost"]
        created = extra.get("created", started)
        vals = [row_id, REPO, 1, started, created, extra.get("agent_type", ""),
                *values]
        with self.repo.connect() as db:
            db.execute(
                f"INSERT INTO ai_token_usage ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})", vals)

    def test_single_null_field_among_many_rows(self):
        """One field NULL across many rows must not accumulate as 0."""
        self.add("a", "2026-05-01T00:00:00+00:00", values=(100, 50, 0, 0, 150, 1.5))
        self.add("b", "2026-05-02T00:00:00+00:00", values=(100, None, 0, 0, 100, 1.0))
        self.add("c", "2026-05-03T00:00:00+00:00", values=(100, 50, 0, 0, 150, 1.5))
        totals = self.repo.token_usage_totals([REPO])
        self.assertEqual(totals["invocations"], 3)
        self.assertEqual(totals["inputTokens"], 300)
        # outputTokens has one NULL: sum should be 50 + NULL + 50 = 100, not 0
        self.assertEqual(totals["outputTokens"], 100)
        self.assertEqual(totals["totalTokens"], 400)

    def test_cost_precision_not_coalesced_to_zero(self):
        """Estimated cost NULL must not become 0.0 (price fields matter)."""
        self.add("a", "2026-05-01T00:00:00+00:00", values=(10, 10, 0, 0, 20, 0.001))
        self.add("b", "2026-05-02T00:00:00+00:00", values=(10, 10, 0, 0, 20, None))
        totals = self.repo.token_usage_totals([REPO])
        self.assertEqual(totals["invocations"], 2)
        self.assertEqual(totals["estimatedCost"], 0.001)
        self.assertNotEqual(totals["estimatedCost"], 0.0, "cost NULL should not become 0.0")

    def test_all_cost_fields_null_stays_null(self):
        """With only cost NULLs, cost should remain None."""
        self.add("a", "2026-05-01T00:00:00+00:00", values=(10, 10, 0, 0, 20, None))
        self.add("b", "2026-05-02T00:00:00+00:00", values=(10, 10, 0, 0, 20, None))
        totals = self.repo.token_usage_totals([REPO])
        self.assertEqual(totals["invocations"], 2)
        self.assertIsNone(totals["estimatedCost"])


class DateFilteringEdgeCasesTests(unittest.TestCase):
    """Verify started_at filtering is applied correctly, not created_at."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = ExecutionHistoryRepository(Path(tmp.name) / "h.sqlite3")

    def add(self, row_id, started, created, tokens=10):
        """Insert a row with distinct started_at and created_at."""
        with self.repo.connect() as db:
            db.execute(
                "INSERT INTO ai_token_usage "
                "(id, repository, issue_number, started_at, created_at, input_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [row_id, REPO, 1, started, created, tokens])

    def test_date_filter_respects_started_not_created(self):
        """Rows must be filtered by started_at, not created_at."""
        # Row that *started* in April but was *recorded* in September.
        self.add("old-start-new-create",
                 started="2026-04-15T12:00:00+00:00",
                 created="2026-09-15T12:00:00+00:00")
        # Row that *started* in May but was *recorded* in April.
        self.add("new-start-old-create",
                 started="2026-05-15T12:00:00+00:00",
                 created="2026-04-15T12:00:00+00:00")

        april_totals = self.repo.token_usage_totals(
            [REPO], start_date="2026-04-01", end_date="2026-04-30")
        self.assertEqual(april_totals["invocations"], 1,
                        "April filter should include only the April-started row")
        self.assertEqual(april_totals["inputTokens"], 10)

        may_totals = self.repo.token_usage_totals(
            [REPO], start_date="2026-05-01", end_date="2026-05-31")
        self.assertEqual(may_totals["invocations"], 1,
                        "May filter should include only the May-started row")

    def test_boundary_date_inclusive(self):
        """Start and end dates should be inclusive (>= and <=)."""
        self.add("start-day", "2026-05-01T00:00:00+00:00", "2026-05-01T00:00:00+00:00")
        self.add("end-day", "2026-05-31T23:59:59+00:00", "2026-05-31T23:59:59+00:00")
        self.add("outside", "2026-06-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00")

        totals = self.repo.token_usage_totals(
            [REPO], start_date="2026-05-01", end_date="2026-05-31")
        self.assertEqual(totals["invocations"], 2,
                        "Both May 1st and May 31st should be included")

    def test_start_date_only(self):
        """Filter with only start_date should include all rows from that date forward."""
        self.add("early", "2026-04-30T23:59:59+00:00", "2026-04-30T23:59:59+00:00")
        self.add("on-boundary", "2026-05-01T00:00:00+00:00", "2026-05-01T00:00:00+00:00")
        self.add("later", "2026-06-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00")

        totals = self.repo.token_usage_totals([REPO], start_date="2026-05-01")
        self.assertEqual(totals["invocations"], 2)

    def test_end_date_only(self):
        """Filter with only end_date should include all rows up to and including that date."""
        self.add("early", "2026-04-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00")
        self.add("on-boundary", "2026-05-31T23:59:59+00:00", "2026-05-31T23:59:59+00:00")
        self.add("later", "2026-06-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00")

        totals = self.repo.token_usage_totals([REPO], end_date="2026-05-31")
        self.assertEqual(totals["invocations"], 2)

    def test_iso_8601_timezone_handling(self):
        """Different UTC representations should work (Z, +00:00, etc)."""
        # Both represent the same instant but with different timezone notations.
        self.add("z-notation", "2026-05-15T12:00:00Z", "2026-05-15T12:00:00Z")
        self.add("plus-notation", "2026-05-15T12:00:00+00:00", "2026-05-15T12:00:00+00:00")

        totals = self.repo.token_usage_totals([REPO])
        self.assertEqual(totals["invocations"], 2)

    def test_midnight_boundary(self):
        """Date boundaries should handle midnight (00:00 and 23:59)."""
        # Exactly at midnight start.
        self.add("at-midnight", "2026-05-01T00:00:00+00:00", "2026-05-01T00:00:00+00:00")
        # Just before midnight end.
        self.add("before-midnight", "2026-05-01T23:59:59+00:00", "2026-05-01T23:59:59+00:00")
        # Just after midnight.
        self.add("after-midnight", "2026-05-02T00:00:01+00:00", "2026-05-02T00:00:01+00:00")

        totals = self.repo.token_usage_totals(
            [REPO], start_date="2026-05-01", end_date="2026-05-01")
        self.assertEqual(totals["invocations"], 2,
                        "Should include both rows from May 1st (at and just before midnight)")


class InteractionEdgeCasesTests(unittest.TestCase):
    """Test interactions between NULL preservation and date filtering."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = ExecutionHistoryRepository(Path(tmp.name) / "h.sqlite3")

    def add(self, row_id, started, created=None, values=(None,) * 6):
        cols = ["id", "repository", "issue_number", "started_at", "created_at",
                "agent_type", "input_tokens", "output_tokens", "reasoning_tokens",
                "cached_input_tokens", "total_tokens", "estimated_cost"]
        created = created if created is not None else started
        vals = [row_id, REPO, 1, started, created, "", *values]
        with self.repo.connect() as db:
            db.execute(
                f"INSERT INTO ai_token_usage ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})", vals)

    def test_null_preservation_with_date_filter(self):
        """NULLs in a date-filtered subset must still be preserved as None."""
        # Both in May, but one row has all NULLs for cost fields.
        self.add("may-complete", "2026-05-15T00:00:00+00:00", values=(10, 10, 0, 0, 20, 0.1))
        self.add("may-partial", "2026-05-20T00:00:00+00:00", values=(10, 10, 0, 0, 20, None))
        self.add("june-row", "2026-06-01T00:00:00+00:00", values=(50, 50, 0, 0, 100, 0.5))

        may_totals = self.repo.token_usage_totals([REPO], start_date="2026-05-01", end_date="2026-05-31")
        self.assertEqual(may_totals["invocations"], 2)
        self.assertEqual(may_totals["inputTokens"], 20)
        self.assertEqual(may_totals["outputTokens"], 20)
        self.assertEqual(may_totals["totalTokens"], 40)
        self.assertEqual(may_totals["estimatedCost"], 0.1, "One NULL cost should leave sum as 0.1")

    def test_date_filter_with_multiple_repositories_and_nulls(self):
        """Combination: date filtering + multi-repo + NULL preservation."""
        # Repo1 with partial data.
        with self.repo.connect() as db:
            db.execute(
                "INSERT INTO ai_token_usage "
                "(id, repository, issue_number, started_at, created_at, "
                "input_tokens, output_tokens, reasoning_tokens, "
                "cached_input_tokens, total_tokens, estimated_cost) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["repo1-may-null", "org/repo1", 1, "2026-05-15T00:00:00+00:00",
                 "2026-05-15T00:00:00+00:00", 5, None, 0, 0, None, None])
        # Repo2 with complete data.
        with self.repo.connect() as db:
            db.execute(
                "INSERT INTO ai_token_usage "
                "(id, repository, issue_number, started_at, created_at, "
                "input_tokens, output_tokens, reasoning_tokens, "
                "cached_input_tokens, total_tokens, estimated_cost) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["repo2-may-complete", "org/repo2", 1, "2026-05-15T00:00:00+00:00",
                 "2026-05-15T00:00:00+00:00", 10, 10, 0, 0, 20, 0.2])

        repo1_totals = self.repo.token_usage_totals(["org/repo1"], start_date="2026-05-01", end_date="2026-05-31")
        self.assertEqual(repo1_totals["invocations"], 1)
        self.assertEqual(repo1_totals["inputTokens"], 5)
        self.assertIsNone(repo1_totals["outputTokens"])

    def test_empty_date_range_returns_nulls_not_zeros(self):
        """A date range with no matches should return None for all fields, not 0."""
        self.add("may-row", "2026-05-15T00:00:00+00:00", values=(10, 10, 0, 0, 20, 0.1))

        totals = self.repo.token_usage_totals([REPO], start_date="2026-06-01", end_date="2026-06-30")
        self.assertEqual(totals["invocations"], 0)
        self.assertIsNone(totals["inputTokens"])
        self.assertIsNone(totals["outputTokens"])
        self.assertIsNone(totals["totalTokens"])
        self.assertIsNone(totals["estimatedCost"])


class NumericPrecisionTests(unittest.TestCase):
    """Verify numeric handling: rounding, large values, precision."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = ExecutionHistoryRepository(Path(tmp.name) / "h.sqlite3")

    def add(self, row_id, tokens=(10,) * 5, cost=None):
        with self.repo.connect() as db:
            db.execute(
                "INSERT INTO ai_token_usage "
                "(id, repository, issue_number, started_at, created_at, "
                "input_tokens, output_tokens, reasoning_tokens, "
                "cached_input_tokens, total_tokens, estimated_cost) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["id-" + str(len([1])), REPO, 1,
                 "2026-05-15T00:00:00+00:00", "2026-05-15T00:00:00+00:00",
                 *tokens, cost])

    def test_cost_rounding_to_6_decimals(self):
        """Estimated cost should round to 6 decimal places."""
        with self.repo.connect() as db:
            db.execute(
                "INSERT INTO ai_token_usage "
                "(id, repository, issue_number, started_at, created_at, "
                "estimated_cost) VALUES (?, ?, ?, ?, ?, ?)",
                ["precise", REPO, 1, "2026-05-15T00:00:00+00:00",
                 "2026-05-15T00:00:00+00:00", 0.123456789])

        totals = self.repo.token_usage_totals([REPO])
        # Should be rounded to 6 decimals.
        self.assertEqual(totals["estimatedCost"], 0.123457)

    def test_large_token_counts(self):
        """Large token counts should accumulate correctly."""
        with self.repo.connect() as db:
            db.executemany(
                "INSERT INTO ai_token_usage "
                "(id, repository, issue_number, started_at, created_at, input_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(f"id-{i}", REPO, 1, "2026-05-15T00:00:00+00:00",
                  "2026-05-15T00:00:00+00:00", 1_000_000) for i in range(5)])

        totals = self.repo.token_usage_totals([REPO])
        self.assertEqual(totals["inputTokens"], 5_000_000)

    def test_zero_is_not_null(self):
        """Actual zero values must remain zero, not become None."""
        with self.repo.connect() as db:
            db.execute(
                "INSERT INTO ai_token_usage "
                "(id, repository, issue_number, started_at, created_at, "
                "reasoning_tokens, estimated_cost) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ["zero-row", REPO, 1, "2026-05-15T00:00:00+00:00",
                 "2026-05-15T00:00:00+00:00", 0, 0.0])

        totals = self.repo.token_usage_totals([REPO])
        self.assertEqual(totals["reasoningTokens"], 0)
        self.assertIsNotNone(totals["reasoningTokens"], "Zero should not become None")
        self.assertEqual(totals["estimatedCost"], 0.0)
        self.assertIsNotNone(totals["estimatedCost"], "Zero cost should not become None")


class RepositoryAndFilteringTests(unittest.TestCase):
    """Test correct scoping by repository and agent type."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = ExecutionHistoryRepository(Path(tmp.name) / "h.sqlite3")

    def add(self, row_id, repository, agent_type="", tokens=(10, 10, 0, 0, 20), cost=0.1):
        cols = ["id", "repository", "issue_number", "started_at", "created_at",
                "agent_type", "input_tokens", "output_tokens", "reasoning_tokens",
                "cached_input_tokens", "total_tokens", "estimated_cost"]
        vals = [row_id, repository, 1, "2026-05-15T00:00:00+00:00",
                "2026-05-15T00:00:00+00:00", agent_type, *tokens, cost]
        with self.repo.connect() as db:
            db.execute(
                f"INSERT INTO ai_token_usage ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})", vals)

    def test_repository_filtering_with_nulls(self):
        """NULLs must be preserved when filtering by repository."""
        # Repo1 row with all-NULL cost fields.
        self.add("repo1-null", "org/repo1", tokens=(5, None, 0, 0, 10), cost=None)
        # Repo1 row with complete data.
        self.add("repo1-complete", "org/repo1", tokens=(5, 5, 0, 0, 10), cost=0.1)
        # Repo2 row (different repo, should not affect repo1).
        self.add("repo2-row", "org/repo2", tokens=(100, 100, 0, 0, 200), cost=1.0)

        repo1_totals = self.repo.token_usage_totals(["org/repo1"])
        self.assertEqual(repo1_totals["invocations"], 2)
        self.assertEqual(repo1_totals["inputTokens"], 10)
        self.assertEqual(repo1_totals["outputTokens"], 5, "One NULL output, one 5")
        self.assertEqual(repo1_totals["estimatedCost"], 0.1, "One NULL cost, one 0.1")

        repo2_totals = self.repo.token_usage_totals(["org/repo2"])
        self.assertEqual(repo2_totals["invocations"], 1)
        self.assertEqual(repo2_totals["inputTokens"], 100)
        self.assertEqual(repo2_totals["estimatedCost"], 1.0)

    def test_agent_type_filter_preserves_nulls(self):
        """agent_type filter should combine with NULL preservation."""
        self.add("primary-null", REPO, agent_type="primary", tokens=(10, None, 0, 0, None), cost=None)
        self.add("uat-complete", REPO, agent_type="uat", tokens=(20, 20, 0, 0, 40), cost=0.2)

        primary_totals = self.repo.token_usage_totals([REPO], agent_type="primary")
        self.assertEqual(primary_totals["invocations"], 1)
        self.assertIsNone(primary_totals["outputTokens"])

        uat_totals = self.repo.token_usage_totals([REPO], agent_type="uat")
        self.assertEqual(uat_totals["invocations"], 1)
        self.assertEqual(uat_totals["outputTokens"], 20)


if __name__ == "__main__":
    unittest.main()
