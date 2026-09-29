"""Issue #295 — coverage of cache-only usage, empty group drill-down,
week buckets, timezone date filters, and no guessed cache prices.

Independent of earlier adversarial fixtures. Spec-derived invariants:

* Partial vs Unreported: an invocation that still carries provider usage
  (cache-read/write or reasoning) is not "unreported". Unreported means
  *no* authoritative usage at all.
* Selecting an aggregate row whose group value is the empty string
  (Not graded, Model not recorded, …) must drill into those rows. The
  query API already distinguishes "no row selected" (group_value is None)
  from "the selected group is empty" (group_value is "").
* Group by week/day/month partitions by the invocation's activity
  timestamp. Days in the same Monday-based week share a bucket.
* Date range filters use the calendar date encoded in the invocation
  timestamp (the YYYY-MM-DD of that ISO value in its own offset), not
  a UTC conversion that can move a late-evening local instant to the
  next day, and never the later batch ``created_at``.
* Unavailable component prices must not become a guessed dollar figure.
  A catalog entry with no cached-input rate must not invent the
  conventional 10% discount and still mark the call priced.
* Every advertised sort key must actually order the aggregate table.
"""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import model_pricing  # noqa: E402
import usage_report  # noqa: E402
from ai_execution_history import (  # noqa: E402
    ExecutionHistoryRepository,
    ExecutionStart,
    main as history_main,
)
from model_pricing import (  # noqa: E402
    PRICING_STATUS_PRICED,
    ModelPrice,
    estimate_invocation_cost,
)


def _price(**overrides) -> ModelPrice:
    base = dict(
        rate_id="test/model@2026-01-01",
        provider="claude",
        model="test-model",
        input_per_million=10.0,
        output_per_million=20.0,
        cached_input_per_million=None,
        cache_write_per_million=None,
        effective_from="2020-01-01T00:00:00+00:00",
        source="https://example.test/pricing",
    )
    base.update(overrides)
    return ModelPrice(**base)


class _catalog:
    def __init__(self, catalog) -> None:
        self.catalog = tuple(catalog)
        self.previous = ()

    def __enter__(self):
        self.previous = model_pricing.PRICING_CATALOG
        model_pricing.PRICING_CATALOG = self.catalog
        return self.catalog

    def __exit__(self, *exc) -> None:
        model_pricing.PRICING_CATALOG = self.previous


def _event(event_id: str, **overrides) -> dict:
    base = dict(
        id=event_id,
        agent_type="primary",
        prompt_type="initial",
        provider="Claude",
        model="claude-sonnet-5",
        reasoning_effort="high",
        attempt_number=1,
        input_tokens=2_000,
        output_tokens=200,
        reasoning_tokens=None,
        cached_input_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        total_tokens=2_200,
        estimated_cost=0.04,
        currency="USD",
        started_at="2026-03-10T09:00:00+00:00",
        completed_at="2026-03-10T09:01:00+00:00",
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


class UsageFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "history.sqlite3"
        self.repository = ExecutionHistoryRepository(self.database_path)

    def _execution(
        self,
        repository: str = "acme/app",
        number: int = 10,
        title: str = "Usage contracts",
        grade: str | None = "B",
    ) -> str:
        routing = {"prompt_grade": grade} if grade is not None else None
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
                routing_decision=routing,
            ),
            "2026-03-10T08:00:00+00:00",
        )

    def report(self, repositories=None, **options):
        return self.repository.usage_report(repositories, **options)


class CacheOnlyCoverageTests(UsageFixture):
    """Cache/reasoning counters are provider usage. Coverage that only looks
    at input/output/total will call those rows Unreported and then drop
    their estimated cost from the summary — both of which the spec forbids.
    """

    def test_cache_only_usage_is_partial_not_unreported(self) -> None:
        execution = self._execution()
        self.repository.record_token_usage_batch(execution, "acme/app", 10, [
            _event(
                "cache-only",
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                cached_input_tokens=1_000,
                cache_read_tokens=800,
                cache_write_tokens=200,
                estimated_cost=0.003,
            ),
        ])
        payload = self.report()
        row = payload["invocations"]["rows"][0]
        self.assertEqual(
            row["coverage"],
            "partial",
            "cache-read/write counters are provider usage; missing input/"
            "output/total makes the row Partial, not Unreported. "
            f"got {row['coverage']!r}",
        )
        self.assertEqual(payload["coverage"]["partial"], 1)
        self.assertEqual(payload["coverage"]["unreported"], 0)
        self.assertEqual(row["cacheReadTokens"], 800)
        self.assertEqual(row["cacheWriteTokens"], 200)
        self.assertIsNone(row["inputTokens"])
        self.assertIsNone(row["totalTokens"])

    def test_cache_only_priced_cost_is_kept_in_the_summary(self) -> None:
        # Surface priced/total next to every estimated-cost aggregate. A
        # priced cache-only row must not vanish from the total just because
        # input/output/total happened to be null.
        execution = self._execution()
        self.repository.record_token_usage_batch(execution, "acme/app", 10, [
            _event(
                "cache-only-priced",
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                cached_input_tokens=1_000,
                cache_read_tokens=1_000,
                estimated_cost=0.0123,
            ),
        ])
        summary = self.report()["summary"]
        self.assertEqual(summary["pricedInvocations"], 1)
        self.assertAlmostEqual(summary["estimatedCost"], 0.0123)
        self.assertEqual(summary["cachedTokens"], 1_000)
        self.assertEqual(summary["cacheReadTokens"], 1_000)
        self.assertIsNone(summary["inputTokens"])
        self.assertIsNone(summary["totalTokens"])

    def test_reasoning_only_usage_is_partial_not_unreported(self) -> None:
        execution = self._execution()
        self.repository.record_token_usage_batch(execution, "acme/app", 10, [
            _event(
                "reason-only",
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                reasoning_tokens=400,
                estimated_cost=None,
                pricing_status="no_usage",
            ),
        ])
        payload = self.report()
        self.assertEqual(payload["invocations"]["rows"][0]["coverage"], "partial")
        self.assertEqual(payload["coverage"]["unreported"], 0)
        self.assertEqual(payload["summary"]["reasoningTokens"], 400)
        self.assertIsNone(payload["summary"]["estimatedCost"])


class EmptyGroupDrilldownTests(UsageFixture):
    def setUp(self) -> None:
        super().setUp()
        self.graded = self._execution(number=11, title="Graded issue", grade="B")
        self.ungraded = self._execution(number=12, title="No router grade", grade=None)
        self.repository.record_token_usage_batch(self.graded, "acme/app", 11, [
            _event("graded-1", started_at="2026-03-10T09:00:00+00:00"),
        ])
        self.repository.record_token_usage_batch(self.ungraded, "acme/app", 12, [
            _event("ungraded-1", started_at="2026-03-10T10:00:00+00:00"),
        ])

    def test_grouping_by_grade_keeps_an_empty_bucket_for_ungraded_work(self) -> None:
        payload = self.report(group_by="grade")
        rows = {row["group"]: row for row in payload["groups"]["rows"]}
        self.assertIn("B", rows)
        self.assertIn(
            "",
            rows,
            "ungraded executions must appear as their own aggregate row so "
            "the UI can label them 'Not graded' and drill into them",
        )
        self.assertEqual(rows[""]["invocations"], 1)
        self.assertEqual(rows["B"]["invocations"], 1)

    def test_group_value_empty_string_drills_into_ungraded_rows_only(self) -> None:
        # None = no drill-down; "" = the empty group. Collapsing those two
        # is how "Not graded" becomes unreachable.
        all_rows = self.report(group_by="grade", group_value=None)
        self.assertEqual(all_rows["invocations"]["total"], 2)
        drilled = self.report(group_by="grade", group_value="")
        ids = [row["id"] for row in drilled["invocations"]["rows"]]
        self.assertEqual(ids, ["ungraded-1"])
        self.assertEqual(drilled["invocations"]["groupValue"], "")
        graded = self.report(group_by="grade", group_value="B")
        self.assertEqual([row["id"] for row in graded["invocations"]["rows"]], ["graded-1"])

    def test_empty_model_group_is_likewise_selectable(self) -> None:
        execution = self._execution(number=13, title="No model", grade="C")
        self.repository.record_token_usage_batch(execution, "acme/app", 13, [
            _event("no-model", model="", started_at="2026-03-11T09:00:00+00:00"),
        ])
        payload = self.report(group_by="model", group_value="")
        ids = [row["id"] for row in payload["invocations"]["rows"]]
        self.assertEqual(ids, ["no-model"])


class WeekAndTimezoneDateTests(UsageFixture):
    def test_week_buckets_group_monday_through_sunday_together(self) -> None:
        # 2026-03-09 is Monday, 2026-03-15 is Sunday, 2026-03-16 is the next Monday.
        execution = self._execution()
        self.repository.record_token_usage_batch(execution, "acme/app", 10, [
            _event("mon", started_at="2026-03-09T08:00:00+00:00"),
            _event("sun", started_at="2026-03-15T18:00:00+00:00"),
            _event("next-mon", started_at="2026-03-16T08:00:00+00:00"),
        ])
        rows = {row["group"]: row["invocations"] for row in self.report(group_by="week")["groups"]["rows"]}
        self.assertEqual(len(rows), 2, rows)
        monday_week = next(key for key, count in rows.items() if count == 2)
        next_week = next(key for key, count in rows.items() if count == 1)
        self.assertNotEqual(monday_week, next_week)
        drilled = self.report(group_by="week", group_value=monday_week)
        self.assertEqual(
            {row["id"] for row in drilled["invocations"]["rows"]},
            {"mon", "sun"},
        )

    def test_date_filter_uses_the_timestamp_calendar_date_not_utc_conversion(self) -> None:
        # 23:00 on 10 June in PDT is 06:00 on 11 June UTC. A date picker set
        # to 2026-06-10 (the calendar date written in the timestamp) must
        # still find the invocation. SQLite date() converting to UTC would
        # silently move it to the 11th.
        execution = self._execution()
        self.repository.record_token_usage_batch(execution, "acme/app", 10, [
            _event(
                "evening-pdt",
                started_at="2026-06-10T23:00:00-07:00",
                completed_at="2026-06-10T23:01:00-07:00",
            ),
        ])
        with self.repository.connect() as database:
            database.execute(
                "UPDATE ai_token_usage SET created_at = ? WHERE id = ?",
                ("2019-01-01T00:00:00+00:00", "evening-pdt"),
            )
        tenth = self.report(start_date="2026-06-10", end_date="2026-06-10")
        self.assertEqual(
            {row["id"] for row in tenth["invocations"]["rows"]},
            {"evening-pdt"},
            "date filters must use the calendar date in the invocation "
            "timestamp's own offset, not a UTC conversion of that instant",
        )
        eleventh = self.report(start_date="2026-06-11", end_date="2026-06-11")
        self.assertEqual(
            eleventh["summary"]["invocations"],
            0,
            "UTC-shifting 2026-06-10T23:00:00-07:00 onto 11 June puts the "
            "row on a day the timestamp does not name",
        )
        days = {row["group"] for row in self.report(group_by="day")["groups"]["rows"]}
        self.assertEqual(days, {"2026-06-10"})

    def test_started_at_wins_when_completion_falls_on_the_next_day(self) -> None:
        execution = self._execution()
        self.repository.record_token_usage_batch(execution, "acme/app", 10, [
            _event(
                "overnight",
                started_at="2026-04-01T23:30:00+00:00",
                completed_at="2026-04-02T00:10:00+00:00",
            ),
        ])
        first = self.report(start_date="2026-04-01", end_date="2026-04-01")
        self.assertEqual({row["id"] for row in first["invocations"]["rows"]}, {"overnight"})
        second = self.report(start_date="2026-04-02", end_date="2026-04-02")
        self.assertEqual(second["summary"]["invocations"], 0)


class SortAndFilterCombinationTests(UsageFixture):
    def setUp(self) -> None:
        super().setUp()
        alpha = self._execution("swarm/alpha", 42, "Alpha widget", "A")
        beta = self._execution("swarm/beta", 7, "Beta gadget", "B")
        self.repository.record_token_usage_batch(alpha, "swarm/alpha", 42, [
            _event("a1", estimated_cost=0.05, total_tokens=100, started_at="2026-05-01T09:00:00+00:00"),
            _event(
                "a2",
                model="claude-haiku-4-5",
                estimated_cost=0.01,
                total_tokens=50,
                started_at="2026-05-02T09:00:00+00:00",
            ),
        ])
        self.repository.record_token_usage_batch(beta, "swarm/beta", 7, [
            _event(
                "b1",
                provider="Grok",
                model="grok-4.6",
                estimated_cost=0.02,
                total_tokens=80,
                started_at="2026-05-01T12:00:00+00:00",
            ),
        ])

    def test_every_sort_key_returns_a_stable_complete_page(self) -> None:
        for key in usage_report.SORT_KEYS:
            for direction in ("asc", "desc"):
                with self.subTest(sort=key, direction=direction):
                    payload = self.report(group_by="model", sort=key, direction=direction)
                    self.assertEqual(payload["sort"], key)
                    self.assertEqual(payload["direction"], direction)
                    rows = payload["groups"]["rows"]
                    self.assertGreaterEqual(len(rows), 1)
                    self.assertEqual(
                        sum(row["invocations"] for row in rows),
                        payload["summary"]["invocations"],
                    )

    def test_date_grade_model_and_repository_combine(self) -> None:
        payload = self.report(
            ["swarm/alpha"],
            start_date="2026-05-01",
            end_date="2026-05-01",
            grade="A",
            model="claude-sonnet-5",
        )
        self.assertEqual(payload["summary"]["invocations"], 1)
        self.assertEqual(payload["invocations"]["rows"][0]["id"], "a1")

    def test_search_matches_issue_title(self) -> None:
        payload = self.report(search="gadget")
        self.assertEqual(payload["summary"]["invocations"], 1)
        self.assertEqual(payload["invocations"]["rows"][0]["id"], "b1")
        self.assertEqual(payload["invocations"]["rows"][0]["issueTitle"], "Beta gadget")


class NoGuessedCacheRateTests(unittest.TestCase):
    def test_a_missing_cache_rate_does_not_invent_a_ten_percent_discount(self) -> None:
        # Spec: unknown / unavailable prices yield Tokens only / Unpriced,
        # never a guessed figure. The conventional 10% cache-hit factor is
        # a guess when the catalog entry has no cached_input_per_million.
        with _catalog((_price(),)):
            estimate = estimate_invocation_cost(
                model="test-model",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=1_000_000,
            )
        guessed = 10.0 * model_pricing.CACHED_INPUT_RATE_FACTOR
        self.assertNotEqual(
            estimate.status,
            PRICING_STATUS_PRICED,
            "a catalog entry with no cached-input rate must not still mark "
            "cache-only tokens priced; that is how a 10% guess becomes a "
            f"dollar figure (got cost={estimate.cost!r})",
        )
        self.assertIsNone(estimate.cost)
        self.assertNotAlmostEqual(estimate.cost or 0.0, guessed)

    def test_shipped_catalog_entries_that_meter_cache_name_the_rate(self) -> None:
        # The fallback factor is only a hazard if a shipped model relies on
        # it. Every catalogued model that the estimator might cache-bill
        # must carry an explicit cached_input_per_million.
        missing = [
            entry.rate_id
            for entry in model_pricing.PRICING_CATALOG
            if entry.cached_input_per_million is None
        ]
        self.assertEqual(
            missing,
            [],
            "shipped catalog entries with no cached-input rate would fall "
            f"through to a guessed 10% discount: {missing}",
        )


class CliContractsTests(UsageFixture):
    def _run(self, argv: list[str]) -> tuple[int, dict]:
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = history_main(argv)
        return code, json.loads(buffer.getvalue() or "null")

    def test_validate_pricing_accepts_the_shipped_catalog(self) -> None:
        code, payload = self._run(["--validate-pricing"])
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["problems"], [])
        self.assertEqual(payload["summary"]["version"], model_pricing.PRICING_CATALOG_VERSION)

    def test_cli_group_by_week_and_empty_group_value(self) -> None:
        graded = self._execution(number=21, grade="A")
        ungraded = self._execution(number=22, grade=None)
        self.repository.record_token_usage_batch(graded, "acme/app", 21, [_event("g")])
        self.repository.record_token_usage_batch(ungraded, "acme/app", 22, [_event("u")])
        code, payload = self._run([
            "--db", str(self.database_path),
            "--usage",
            "--group-by", "grade",
            "--group-value", "",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(payload["groupBy"], "grade")
        self.assertEqual(
            [row["id"] for row in payload["invocations"]["rows"]],
            ["u"],
            "CLI --group-value '' must drill into the ungraded bucket rather "
            "than being treated as 'no selection'",
        )


if __name__ == "__main__":
    unittest.main()
