"""Issue #295 — the usage query API must support grouping by outcome.

Independent of the implementer's own fixtures and test files.

The issue's "Query and application API" section is explicit:

    Extend existing aggregation support to include at least issue,
    reasoning effort, prompt grade, outcome, and time bucket.

Four of those five — issue, reasoning effort ("effort"), prompt grade
("grade"), and a time bucket (day/week/month) — are present in
``usage_report.GROUP_BY_KEYS``. "outcome" (success vs. failure) is not: it
exists only as a *filter* (``UsageFilters.outcome``), never as something the
aggregate table can be grouped by. Requesting ``group_by="outcome"`` does not
raise or report an unsupported dimension — ``normalize_group_by`` silently
rewrites it to the default ("issue"), so a caller asking to break totals down
by success/failure instead silently gets a per-issue breakdown with no
indication anything was substituted. That silent substitution, not merely
the missing feature, is what these tests pin down.

Coverage's "failed" bucket is not a substitute: coverage folds a failed
invocation together with field-completeness (partial/tokens_only/complete)
for *successful* calls, so it cannot answer "how many invocations of each
outcome, with each outcome's own token/cost totals" — the shape "Extend
existing aggregation support" calls for, matching every other listed
dimension (issue, effort, grade, time bucket) each of which the aggregate
table already returns full token/cost rows for.
"""

from __future__ import annotations

import json
import sys
import unittest
from io import StringIO
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import usage_report  # noqa: E402
from ai_execution_history import (  # noqa: E402
    ExecutionHistoryRepository,
    ExecutionStart,
    main as history_main,
)


def _event(event_id: str, **overrides) -> dict:
    base = dict(
        id=event_id,
        agent_type="primary",
        prompt_type="initial",
        provider="Claude",
        model="claude-sonnet-5",
        reasoning_effort="high",
        attempt_number=1,
        input_tokens=1_000,
        output_tokens=100,
        reasoning_tokens=None,
        cached_input_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        total_tokens=1_100,
        estimated_cost=0.01,
        currency="USD",
        started_at="2026-05-01T09:00:00+00:00",
        completed_at="2026-05-01T09:01:00+00:00",
        duration_ms=60_000,
        success=True,
        error_type="",
        pricing_status="priced",
        pricing_version="2026-09-28",
        pricing_rate_id="claude/claude-sonnet-5@2026-01-01",
        pricing_source="https://www.anthropic.com/pricing",
        input_rate_per_million=3.0,
        cached_input_rate_per_million=0.3,
        cache_write_rate_per_million=3.75,
        output_rate_per_million=15.0,
    )
    base.update(overrides)
    return base


class OutcomeGroupingFixture(unittest.TestCase):
    """Two issues: one clean, one with a mix of successes and a failure.

    Three successful invocations and one failed one, so a correct
    outcome-grouped report has exactly two rows (success: 3, failure: 1)
    whose invocation counts and cost/token totals do not bleed into each
    other and sum back to the ungrouped totals.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "history.sqlite3"
        self.repository = ExecutionHistoryRepository(self.database_path)
        self.first = self._execution("acme/app", 41, "First issue")
        self.second = self._execution("acme/app", 42, "Second issue")
        self.repository.record_token_usage_batch(self.first, "acme/app", 41, [
            _event("s1", estimated_cost=0.01, total_tokens=1_100),
            _event("s2", estimated_cost=0.02, total_tokens=1_200,
                   started_at="2026-05-01T10:00:00+00:00"),
        ])
        self.repository.record_token_usage_batch(self.second, "acme/app", 42, [
            _event("s3", estimated_cost=0.03, total_tokens=1_300,
                   started_at="2026-05-02T09:00:00+00:00"),
            _event(
                "f1",
                success=False,
                error_type="provider_exit_nonzero",
                output_tokens=None,
                total_tokens=None,
                estimated_cost=None,
                pricing_status="priced",
                started_at="2026-05-02T10:00:00+00:00",
            ),
        ])

    def _execution(self, repository: str, number: int, title: str) -> str:
        return self.repository.create(
            ExecutionStart(
                repository=repository,
                issue_number=number,
                issue_url=f"https://github.com/{repository}/issues/{number}",
                issue_title=title,
                issue_body="",
                provider="Claude",
                model="claude-sonnet-5",
                effort="high",
                branch_name=f"ai/claude/issue-{number}",
                application_version="0.1.0",
                routing_decision={"prompt_grade": "B", "router_provider": "claude"},
            ),
            "2026-05-01T08:00:00+00:00",
        )

    def report(self, **options):
        return self.repository.usage_report(**options)


class OutcomeIsAGroupableDimensionTests(OutcomeGroupingFixture):
    def test_outcome_is_listed_among_the_supported_group_by_keys(self) -> None:
        # The issue names outcome in the same breath as issue, effort, grade
        # and time bucket — all four of which are real entries in this
        # tuple. "outcome" must be too, not silently absorbed elsewhere.
        self.assertIn(
            "outcome",
            usage_report.GROUP_BY_KEYS,
            "the usage query API must support grouping by outcome, per the "
            "issue's \"Extend existing aggregation support to include at "
            "least issue, reasoning effort, prompt grade, outcome, and time "
            "bucket\"",
        )

    def test_requesting_outcome_grouping_does_not_silently_become_issue_grouping(self) -> None:
        requested = usage_report.normalize_group_by("outcome")
        self.assertEqual(
            requested,
            "outcome",
            "group_by='outcome' was silently rewritten to "
            f"'{requested}' instead of being honored or rejected loudly — "
            "a caller asking for a success/failure breakdown gets an "
            "unrelated per-issue breakdown with no indication of the swap",
        )

    def test_grouping_by_outcome_partitions_successes_from_the_failure(self) -> None:
        payload = self.report(group_by="outcome")
        self.assertEqual(payload["groupBy"], "outcome")
        rows = {row["group"]: row for row in payload["groups"]["rows"]}
        self.assertEqual(set(rows), {"success", "failure"})
        self.assertEqual(rows["success"]["invocations"], 3)
        self.assertEqual(rows["failure"]["invocations"], 1)
        # Every successful row in this fixture is fully priced; the failure
        # carries no cost. A correct outcome grouping must keep that
        # separation rather than collapsing it into one mixed bucket.
        self.assertEqual(rows["success"]["pricedInvocations"], 3)
        self.assertEqual(rows["failure"]["pricedInvocations"], 0)
        self.assertAlmostEqual(rows["success"]["estimatedCost"], 0.06, places=6)
        self.assertIsNone(rows["failure"]["estimatedCost"])
        # Grouped rows must still sum back to the ungrouped totals — a
        # grouping dimension that drops or duplicates rows would fail this
        # even if the two group labels themselves looked plausible.
        total = self.report()["summary"]["invocations"]
        self.assertEqual(
            sum(row["invocations"] for row in rows.values()),
            total,
        )

    def test_the_failed_invocation_still_shows_the_usage_it_returned(self) -> None:
        # Acceptance requirement carried over from the Coverage section:
        # a failed call's own row (reachable by drilling into the
        # "failure" outcome group) must show whatever partial usage the
        # provider still returned, not hide it.
        payload = self.report(group_by="outcome", group_value="failure")
        rows = payload["invocations"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "f1")
        self.assertFalse(rows[0]["success"])
        self.assertEqual(rows[0]["inputTokens"], 1_000)
        self.assertIsNone(rows[0]["outputTokens"])


class OutcomeGroupingCommandLineTests(OutcomeGroupingFixture):
    """The same gap, exercised through the actual CLI entry point the Rust
    command shells out to — so this is not just a Python-API-only finding."""

    def _run(self, argv: list[str]) -> dict:
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = history_main(argv)
        self.assertEqual(code, 0)
        return json.loads(buffer.getvalue())

    def test_cli_group_by_outcome_is_honored_not_silently_swapped(self) -> None:
        payload = self._run([
            "--db", str(self.database_path), "--usage", "--group-by", "outcome",
        ])
        self.assertEqual(
            payload["groupBy"],
            "outcome",
            "the --usage CLI silently substituted a different grouping for "
            "an unsupported --group-by value instead of honoring 'outcome'",
        )
        groups = {row["group"] for row in payload["groups"]["rows"]}
        self.assertEqual(groups, {"success", "failure"})


if __name__ == "__main__":
    unittest.main()
