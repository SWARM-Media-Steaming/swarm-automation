"""Issue #299 Feedback analysis: filters and savings must cover full history.

AC 22 / 29: users analyze score changes, routing changes, completion, retries,
fallback, latency, and total cost via date range, provider/model, score-delta,
and cost filters. PAGE_SIZE is 10, so those filters have to run in the query.
Client-side filtering of the current page hides matching rows on later pages.

AC 30: retries and failures are never treated as dollar savings. The Feedback
summary must not count a retried execution as savings just because a savings
estimate was stored on the row.
"""

from __future__ import annotations

import inspect
import unittest

from harness import JevWorkerFixture
from ai_execution_history import PAGE_SIZE, ExecutionHistoryRepository


class FeedbackQueryFilterTests(JevWorkerFixture, unittest.TestCase):
    def test_date_cost_and_delta_are_query_parameters(self) -> None:
        params = set(inspect.signature(ExecutionHistoryRepository.jev_feedback).parameters)
        missing = []
        if not params & {"created_after", "from_date", "since", "jev_from", "date_from", "created_at_from"}:
            missing.append("date range")
        if not params & {"max_cost", "cost", "jev_cost", "max_jev_cost"}:
            missing.append("cost")
        if not params & {"min_delta", "score_delta", "min_score_delta", "delta"}:
            missing.append("score-delta")
        self.assertFalse(
            missing,
            "jev_feedback cannot filter "
            + ", ".join(missing)
            + f" at query time (parameters={sorted(params)}). "
            "AC 29 requires date-range, score-delta, and cost filters for historical "
            f"analysis. PAGE_SIZE is {PAGE_SIZE}, so filtering only the current "
            "Feedback page cannot see matching rows on later pages.",
        )

    def test_cheap_old_row_is_not_trapped_behind_the_page_when_cost_filtered(self) -> None:
        """Demonstrate the page-size trap: 12 rows, cheapest is oldest so it
        sorts off page 1. A working cost filter must still return it.
        """
        db = self.enable_history()
        repo = ExecutionHistoryRepository(db)
        for index in range(12):
            repo.record_jev_score_comparison(
                {
                    "comparison_id": f"cmp-{index}",
                    "execution_id": f"exec-{index}",
                    "repository": "acme/app",
                    "issue_number": index + 1,
                    "created_at": f"2026-09-{index + 1:02d}T12:00:00+00:00",
                    "jev_status": "enabled",
                    "baseline": {"normalized_score": 0.50},
                    "jev": {"normalized_score": 0.60, "confidence": 0.9},
                    "modified": {"normalized_score": 0.55},
                    "delta": {
                        "absolute": 0.40 if index == 0 else 0.01,
                        "percent": 80.0 if index == 0 else 2.0,
                        "routing_changed": False,
                    },
                    "estimated_jev_cost": 0.0001 if index == 0 else 0.05,
                    "workflow_outcome": "completed",
                }
            )
        page = repo.jev_feedback(["acme/app"])
        self.assertEqual(page["limit"], PAGE_SIZE)
        self.assertGreater(page["total"], PAGE_SIZE)
        page_ids = {row["executionId"] for row in page["records"]}
        self.assertNotIn(
            "exec-0",
            page_ids,
            "precondition: the cheap/old row must sort off page 1 so this test "
            "actually exercises cross-page filtering",
        )

        params = set(inspect.signature(ExecutionHistoryRepository.jev_feedback).parameters)
        kwargs = {}
        if "max_cost" in params:
            kwargs["max_cost"] = 0.001
        elif "max_jev_cost" in params:
            kwargs["max_jev_cost"] = 0.001
        elif "jev_cost" in params:
            kwargs["jev_cost"] = 0.001
        elif "cost" in params:
            kwargs["cost"] = 0.001
        else:
            self.fail(
                "jev_feedback has no cost filter parameter, so the cheap row on "
                f"page 2 (estimated_jev_cost=0.0001, execution exec-0) cannot be "
                f"selected. AC 29 requires a cost filter over historical analysis, "
                f"not only the current {PAGE_SIZE}-row page. ids_on_page={sorted(page_ids)}"
            )
        filtered = repo.jev_feedback(["acme/app"], **kwargs)
        filtered_ids = {row["executionId"] for row in filtered["records"]}
        self.assertIn(
            "exec-0",
            filtered_ids,
            "cost filter did not return the cheap row that lives past page 1. "
            f"returned={sorted(filtered_ids)} kwargs={kwargs}",
        )


class SavingsAndRetryTests(JevWorkerFixture, unittest.TestCase):
    def test_retried_executions_are_not_counted_as_dollar_savings(self) -> None:
        """AC 30: failures and retries are never treated as cost savings.

        The completed-only case is already covered. A row stamped 'retry' /
        'retried' with a savings estimate must not add to estimatedDollarSavings.
        """
        db = self.enable_history()
        repo = ExecutionHistoryRepository(db)
        repo.record_jev_score_comparison(
            {
                "comparison_id": "win",
                "execution_id": "exec-win",
                "repository": "acme/app",
                "issue_number": 1,
                "jev_status": "enabled",
                "baseline": {"normalized_score": 0.5},
                "jev": {"normalized_score": 0.6, "confidence": 0.9},
                "modified": {"normalized_score": 0.55},
                "delta": {"absolute": 0.05, "percent": 10.0, "routing_changed": False},
                "estimated_dollar_savings": 2.00,
                "workflow_outcome": "completed",
            }
        )
        for outcome, execution_id, savings in (
            ("retry", "exec-retry", 8.00),
            ("retried", "exec-retried", 7.00),
            ("needs_retry", "exec-needs-retry", 6.00),
        ):
            repo.record_jev_score_comparison(
                {
                    "comparison_id": execution_id,
                    "execution_id": execution_id,
                    "repository": "acme/app",
                    "issue_number": 2,
                    "jev_status": "enabled",
                    "baseline": {"normalized_score": 0.5},
                    "jev": {"normalized_score": 0.6, "confidence": 0.9},
                    "modified": {"normalized_score": 0.55},
                    "delta": {"absolute": 0.05, "percent": 10.0, "routing_changed": False},
                    "estimated_dollar_savings": savings,
                    "workflow_outcome": outcome,
                }
            )
        page = repo.jev_feedback(["acme/app"])
        self.assertEqual(
            page["summary"]["estimatedDollarSavings"],
            2.00,
            "retry/retried/needs_retry rows contributed to estimatedDollarSavings. "
            "AC 30: retries are never treated as cost savings. "
            f"summary={page['summary']!r}",
        )


if __name__ == "__main__":
    unittest.main()
