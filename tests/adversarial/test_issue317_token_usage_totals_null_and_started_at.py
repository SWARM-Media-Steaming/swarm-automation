"""Issue #317: ``token_usage_totals`` must keep missing token/cost values as
missing (None, never a fabricated zero) and filter dates by ``started_at``.

Expected behavior derived from the issue: a SUM over only-NULL values is
unknown, not 0; a genuine recorded 0 stays 0; and the date window is the
invocation's ``started_at`` (the same column ``usage_report`` uses), not the
row's ``created_at``. Invocation counts are still exact.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "issue_worker"))

from ai_execution_history import ExecutionHistoryRepository  # noqa: E402

REPO = "acme/widgets"
FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens",
          "cached_input_tokens", "total_tokens", "estimated_cost")
KEYS = ("inputTokens", "outputTokens", "reasoningTokens",
        "cachedInputTokens", "totalTokens", "estimatedCost")


class TokenUsageTotalsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = ExecutionHistoryRepository(Path(tmp.name) / "h.sqlite3")

    def add(self, row_id, started, created=None, values=(None,) * 6,
            repository=REPO, **extra):
        cols = ["id", "repository", "issue_number", "started_at", "created_at",
                "agent_type", *FIELDS]
        vals = [row_id, repository, 1, started, created if created is not None else started,
                extra.get("agent_type", ""), *values]
        with self.repo.connect() as db:
            db.execute(
                f"INSERT INTO ai_token_usage ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})", vals)

    def test_all_null_is_none_not_zero(self):
        self.add("a", "2026-05-01T00:00:00+00:00")
        self.add("b", "2026-05-02T00:00:00+00:00")
        totals = self.repo.token_usage_totals([REPO])
        self.assertEqual(totals["invocations"], 2)
        for key in KEYS:
            self.assertIsNone(totals[key], key)

    def test_empty_result_is_none_with_zero_invocations(self):
        totals = self.repo.token_usage_totals([REPO])
        self.assertEqual(totals["invocations"], 0)
        for key in KEYS:
            self.assertIsNone(totals[key], key)

    def test_real_zero_stays_zero_and_nulls_are_skipped_in_sum(self):
        self.add("z", "2026-05-01T00:00:00+00:00", values=(0, 0, 0, 0, 0, 0.0))
        self.add("n", "2026-05-02T00:00:00+00:00")
        self.add("k", "2026-05-03T00:00:00+00:00", values=(5, 6, 1, 2, 11, 0.25))
        totals = self.repo.token_usage_totals([REPO])
        self.assertEqual(totals["invocations"], 3)
        self.assertEqual(
            [totals[k] for k in KEYS], [5, 6, 1, 2, 11, 0.25])
        zero_only = self.repo.token_usage_totals(
            [REPO], start_date="2026-05-01", end_date="2026-05-01T23:59:59")
        self.assertEqual(zero_only["invocations"], 1)
        for key in KEYS:
            self.assertEqual(zero_only[key], 0, key)
            self.assertIsNotNone(zero_only[key], key)

    def test_per_field_independence(self):
        self.add("a", "2026-05-01T00:00:00+00:00", values=(3, None, None, None, None, None))
        totals = self.repo.token_usage_totals([REPO])
        self.assertEqual(totals["inputTokens"], 3)
        for key in KEYS[1:]:
            self.assertIsNone(totals[key], key)

    def test_date_window_uses_started_at_not_created_at(self):
        # Started in April, but written (created) in September.
        self.add("old", "2026-04-15T12:00:00+00:00", created="2026-09-29T00:00:00+00:00",
                 values=(7, 7, 0, 0, 14, 0.5))
        # Created in April but started in May.
        self.add("new", "2026-05-15T12:00:00+00:00", created="2026-04-15T12:00:00+00:00",
                 values=(100, 100, 0, 0, 200, 1.0))
        april = self.repo.token_usage_totals(
            [REPO], start_date="2026-04-01", end_date="2026-04-30")
        self.assertEqual(april["invocations"], 1)
        self.assertEqual(april["inputTokens"], 7)
        self.assertEqual(april["estimatedCost"], 0.5)
        may = self.repo.token_usage_totals([REPO], start_date="2026-05-01")
        self.assertEqual(may["invocations"], 1)
        self.assertEqual(may["inputTokens"], 100)
        before = self.repo.token_usage_totals([REPO], end_date="2026-04-01")
        self.assertEqual(before["invocations"], 0)
        self.assertIsNone(before["totalTokens"])

    def test_filters_combine_with_null_preservation_and_repository_scope(self):
        self.add("u", "2026-05-01T00:00:00+00:00", agent_type="uat")
        self.add("p", "2026-05-01T00:00:00+00:00", agent_type="primary",
                 values=(1, 1, 0, 0, 2, 0.1))
        self.add("o", "2026-05-01T00:00:00+00:00", repository="other/repo",
                 values=(9, 9, 0, 0, 18, 9.0))
        uat = self.repo.token_usage_totals([REPO], agent_type="uat")
        self.assertEqual(uat["invocations"], 1)
        self.assertIsNone(uat["totalTokens"])
        self.assertIsNone(uat["estimatedCost"])
        both = self.repo.token_usage_totals([REPO])
        self.assertEqual(both["invocations"], 2)
        self.assertEqual(both["totalTokens"], 2)


if __name__ == "__main__":
    unittest.main()
