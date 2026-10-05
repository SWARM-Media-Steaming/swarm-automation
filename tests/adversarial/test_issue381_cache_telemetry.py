"""Issue #381 acceptance: cache telemetry, reporting, history and routing evidence.

Oracle (from the issue, before reading the diff):

* Cache-hit efficiency is token weighted and uses only observations that carry
  both counters; a missing statistic is "unavailable", never zero, while a
  reported zero stays zero.
* Estimated API-equivalent savings are net of cache-write premiums, may be
  negative, and are never presented as realized billing savings. Provider
  reported cost is a separate figure.
* Existing history survives the schema change untouched and unrepriced.
* Routing may use cache measurements only with sufficient, recent, exact
  (repository, provider, model, effort, role) evidence, and never to override
  capability; absent evidence the routing prompt is unchanged.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ISSUE_WORKER_DIR = Path(__file__).resolve().parents[2] / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

from ai_execution_history import SCHEMA_VERSION, ExecutionHistoryRepository  # noqa: E402
from dynamic_router import RoutingTier, RouterCandidate, build_router_prompt, cache_adjusted_cost  # noqa: E402
from model_pricing import CostEstimate  # noqa: E402
from token_usage import NormalizedUsage, cache_metrics, render_ai_usage_markdown  # noqa: E402
from usage_report import UsageFilters, build_usage_report, cache_routing_evidence  # noqa: E402

REPOSITORY = "acme/project"


def event(event_id: str, **overrides) -> dict:
    base = dict(
        id=event_id, sequence=1, agent_type="primary", prompt_type="initial",
        provider="Claude", model="claude-sonnet-5", reasoning_effort="high",
        attempt_number=1, input_tokens=10_000, output_tokens=1_000, reasoning_tokens=None,
        cached_input_tokens=2_000, cache_read_tokens=2_000, cache_write_tokens=None,
        total_tokens=13_000, estimated_cost=0.05, currency="USD",
        started_at="2026-03-10T10:00:00+00:00", completed_at="2026-03-10T10:05:00+00:00",
        duration_ms=300_000, success=True, error_type="", pricing_status="priced",
        pricing_version="2026-09-28", pricing_rate_id="claude/claude-sonnet-5@2026-01-01",
        pricing_source="https://www.anthropic.com/pricing", input_rate_per_million=3.0,
        cached_input_rate_per_million=0.3, cache_write_rate_per_million=3.75,
        output_rate_per_million=15.0,
    )
    base.update(overrides)
    return base


def priced() -> CostEstimate:
    return CostEstimate(1, "priced", input_rate_per_million=2, cached_input_rate_per_million=.2,
                        cache_write_rate_per_million=2.5)


class CacheMetricsBoundaryTests(unittest.TestCase):
    def test_a_reported_zero_is_zero_and_a_missing_counter_is_unavailable(self) -> None:
        zero = cache_metrics(NormalizedUsage(input_tokens=100, cache_read_tokens=0, cache_write_tokens=0), priced())
        self.assertEqual(zero["cache_input_tokens"], 100)
        self.assertEqual(zero["cache_savings_estimate"], 0)
        for usage in (
            NormalizedUsage(input_tokens=100, cache_write_tokens=0),
            NormalizedUsage(input_tokens=100, cache_read_tokens=5),  # Claude needs the write counter too
            NormalizedUsage(cache_read_tokens=5, cache_write_tokens=0),
            None,
        ):
            with self.subTest(usage=usage):
                self.assertEqual(cache_metrics(usage, priced()),
                                 {"cache_input_tokens": None, "cache_savings_estimate": None})

    def test_everything_zero_does_not_divide_or_invent_savings(self) -> None:
        result = cache_metrics(NormalizedUsage(input_tokens=0, cache_read_tokens=0, cache_write_tokens=0), priced())
        self.assertEqual(result["cache_input_tokens"], 0)
        self.assertEqual(result["cache_savings_estimate"], 0)

    def test_write_premium_can_make_the_net_estimate_negative(self) -> None:
        usage = NormalizedUsage(input_tokens=10, cache_read_tokens=10, cache_write_tokens=1000)
        savings = cache_metrics(usage, priced())["cache_savings_estimate"]
        self.assertAlmostEqual(savings, (10 * 1.8 - 1000 * 0.5) / 1_000_000)
        self.assertLess(savings, 0)

    def test_codex_reads_are_part_of_input_and_have_no_write_premium(self) -> None:
        usage = NormalizedUsage(input_tokens=1000, cache_read_tokens=400, cached_tokens_included_in_input=True,
                                cache_write_tokens=900)
        result = cache_metrics(usage, priced())
        self.assertEqual(result["cache_input_tokens"], 1000)
        self.assertAlmostEqual(result["cache_savings_estimate"], 400 * 1.8 / 1_000_000)

    def test_unpriced_or_partially_priced_invocations_have_no_savings_but_keep_counts(self) -> None:
        usage = NormalizedUsage(input_tokens=100, cache_read_tokens=50, cache_write_tokens=10)
        for estimate in (CostEstimate(None, "unknown_model"),
                         CostEstimate(1, "priced", input_rate_per_million=2),
                         CostEstimate(1, "priced", input_rate_per_million=2, cached_input_rate_per_million=.2)):
            with self.subTest(estimate=estimate):
                result = cache_metrics(usage, estimate)
                self.assertEqual(result["cache_input_tokens"], 160)
                self.assertIsNone(result["cache_savings_estimate"])

    def test_inconsistent_counters_are_unavailable_not_over_100_percent(self) -> None:
        usage = NormalizedUsage(input_tokens=100, cache_read_tokens=900, cached_tokens_included_in_input=True)
        self.assertEqual(cache_metrics(usage, priced()),
                         {"cache_input_tokens": None, "cache_savings_estimate": None})


class GitHubReportTests(unittest.TestCase):
    def test_negative_savings_are_rendered_with_a_conventional_sign(self) -> None:
        text = render_ai_usage_markdown([event(
            "a", cache_read_tokens=0, cache_write_tokens=100, cache_input_tokens=10_100,
            cache_savings_estimate=-1.5, session_reused=False)])
        line = next(item for item in text.splitlines() if "cache savings" in item.lower())
        self.assertNotRegex(line, r"\$-", "a negative amount must read -$1.50, not $-1.50")
        self.assertIn("1.50", line)

    def test_legacy_events_show_unavailable_not_zero(self) -> None:
        text = render_ai_usage_markdown([event("a"), event("b", sequence=2)])
        section = text[text.index("Cache"):]
        self.assertNotRegex(section, r"(?i)cache hit efficiency:\*\* 0")
        self.assertRegex(section, r"(?i)cache hit efficiency:\*\* —")
        self.assertRegex(section, r"(?i)session reuse:\*\* —")
        self.assertRegex(section, r"(?i)provider-reported cost:\*\* —")
        self.assertRegex(section, r"(?i)cache savings:\*\* —")

    def test_zero_reuse_and_zero_efficiency_are_reported_as_zero(self) -> None:
        text = render_ai_usage_markdown([event(
            "a", cache_input_tokens=1000, cache_read_tokens=0, cache_savings_estimate=0.0,
            session_reused=False, reported_cost=0.0)])
        self.assertIn("0.0%", text)
        self.assertIn("0 / 1", text)

    def test_hit_efficiency_is_token_weighted_over_matching_observations(self) -> None:
        text = render_ai_usage_markdown([
            event("a", cache_input_tokens=1000, cache_read_tokens=1000),
            event("b", sequence=2, cache_input_tokens=9000, cache_read_tokens=0),
            event("c", sequence=3, cache_input_tokens=None, cache_read_tokens=500_000),
        ])
        self.assertIn("10.0%", text)  # 1000 / 10000, not the 50% mean nor 500k from the unmatched row

    def test_report_never_presents_estimates_or_reported_cost_as_billing(self) -> None:
        text = render_ai_usage_markdown([event(
            "a", cache_input_tokens=100, cache_read_tokens=50, cache_savings_estimate=0.5,
            reported_cost=0.25, session_reused=True)])
        lowered = text.lower()
        self.assertIn("not verified subscription", lowered)
        self.assertIn("not realized billing savings", lowered)
        self.assertNotRegex(lowered, r"you saved|realized savings:|saved \$")

    def test_unknown_future_keys_in_stored_events_do_not_break_rendering(self) -> None:
        text = render_ai_usage_markdown([event("a", some_future_column="x")])
        self.assertIn("AI Usage", text)


class UsageReportAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = ExecutionHistoryRepository(Path(self.temp.name) / "history.db")

    def record(self, *events: dict, issue: int = 1) -> None:
        self.repo.record_token_usage_batch(f"run-{uuid.uuid4()}", REPOSITORY, issue, list(events))

    def report(self, **kwargs):
        with self.repo.connect() as database:
            return build_usage_report(database, filters=UsageFilters(), **kwargs)

    def test_grouped_efficiency_is_per_group_and_token_weighted(self) -> None:
        self.record(
            event("c1", sequence=1, cache_input_tokens=1000, cache_read_tokens=800, session_reused=True),
            event("x1", sequence=2, provider="Codex", model="gpt-5.6-terra", cache_input_tokens=9000,
                  cache_read_tokens=4500, session_reused=False),
            event("l1", sequence=3, provider="Grok", model="grok-4", cache_read_tokens=None),
            event("p1", sequence=4, cache_input_tokens=None, cache_read_tokens=700_000),
        )
        report = self.report(group_by="provider")
        by_name = {str(row["group"]).lower(): row for row in report["groups"]["rows"]}
        self.assertAlmostEqual(by_name["claude"]["cacheHitEfficiency"], 0.8)
        self.assertAlmostEqual(by_name["codex"]["cacheHitEfficiency"], 0.5)
        self.assertIsNone(by_name["grok"]["cacheHitEfficiency"], "no observations is unavailable, not 0%")
        self.assertEqual(by_name["claude"]["cacheMeasuredInvocations"], 1)
        self.assertAlmostEqual(report["summary"]["cacheHitEfficiency"], 5300 / 10000)
        self.assertEqual(report["summary"]["sessionReused"], 1)
        self.assertEqual(report["summary"]["sessionReuseReported"], 2)

    def test_an_empty_history_reports_nothing_available(self) -> None:
        summary = self.report()["summary"]
        for key in ("cacheHitEfficiency", "sessionReused", "cacheSavingsEstimate", "reportedCost"):
            self.assertIsNone(summary[key], key)
        self.assertEqual(summary["cacheMeasuredInvocations"], 0)

    def test_reported_cost_and_estimated_savings_stay_separate_fields(self) -> None:
        self.record(event("a", cache_savings_estimate=0.25, reported_cost=0.5, estimated_cost=0.75))
        summary = self.report()["summary"]
        self.assertAlmostEqual(summary["cacheSavingsEstimate"], 0.25)
        self.assertAlmostEqual(summary["reportedCost"], 0.5)
        self.assertAlmostEqual(summary["estimatedCost"], 0.75)

    def test_invocation_detail_preserves_none_false_and_true_for_session_reuse(self) -> None:
        sid = str(uuid.uuid4())
        self.record(
            event("n", sequence=1),
            event("f", sequence=2, session_reused=False, session_role="primary", agent_run_id=sid),
            event("t", sequence=3, session_reused=True, session_role="primary", agent_run_id=sid),
        )
        report = self.report(group_by="provider", group_value="Claude")
        details = report["invocations"]["rows"]
        self.assertEqual(len(details), 3)
        reuse = sorted(str(row["sessionReused"]) for row in details)
        self.assertEqual(reuse, ["0", "1", "None"])
        self.assertTrue(all(row["sessionId"] in ("", sid) for row in details))


class HistoryMigrationTests(unittest.TestCase):
    NEW_COLUMNS = ("session_reused", "session_role", "cache_input_tokens", "cache_savings_estimate", "reported_cost")

    @unittest.skipIf(sqlite3.sqlite_version_info < (3, 35), "needs DROP COLUMN")
    def test_a_schema_nine_database_upgrades_without_touching_existing_rows(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 10)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            repo = ExecutionHistoryRepository(path)
            repo.record_token_usage_batch("legacy-run", REPOSITORY, 7, [
                event("legacy", estimated_cost=0.0123, cache_write_tokens=5)])
            with repo.connect() as database:
                for column in self.NEW_COLUMNS:
                    database.execute(f"ALTER TABLE ai_token_usage DROP COLUMN {column}")
                database.execute("DELETE FROM schema_migrations WHERE version = 10")
            for _ in range(2):  # the second open proves idempotence
                upgraded = ExecutionHistoryRepository(path)
                with upgraded.connect() as database:
                    columns = {row[1] for row in database.execute("PRAGMA table_info(ai_token_usage)")}
                    versions = {row[0] for row in database.execute("SELECT version FROM schema_migrations")}
                self.assertTrue(set(self.NEW_COLUMNS) <= columns)
                self.assertIn(10, versions)
            (row,) = ExecutionHistoryRepository(path).token_usage_for_execution("legacy-run")
            self.assertEqual(row["estimated_cost"], 0.0123)
            self.assertEqual(row["cache_write_tokens"], 5)
            for column in ("session_reused", "cache_input_tokens", "cache_savings_estimate", "reported_cost"):
                self.assertIsNone(row[column], f"{column} must stay unavailable for history")
            self.assertEqual(row["session_role"], "")

    def test_new_columns_round_trip_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = ExecutionHistoryRepository(Path(directory) / "history.db")
            sid = str(uuid.uuid4())
            repo.record_token_usage_batch("run", REPOSITORY, 1, [event(
                "a", session_reused=False, session_role="adversarial:fix:2", agent_run_id=sid,
                cache_input_tokens=0, cache_savings_estimate=-0.002, reported_cost=0.0)])
            (row,) = repo.token_usage_for_execution("run")
            self.assertEqual(row["session_reused"], 0)  # False, not NULL
            self.assertEqual(row["cache_input_tokens"], 0)
            self.assertEqual(row["reported_cost"], 0.0)
            self.assertEqual(row["cache_savings_estimate"], -0.002)
            self.assertEqual(row["session_role"], "adversarial:fix:2")
            self.assertEqual(row["agent_run_id"], sid)


class RoutingEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = ExecutionHistoryRepository(Path(self.temp.name) / "history.db")
        self.counter = 0

    def add(self, count: int, *, issues: int = 5, days_ago: float = 1, role: str = "primary",
            repository: str = REPOSITORY, offset_hours: int = 0, **overrides) -> None:
        zone = dt.timezone(dt.timedelta(hours=offset_hours))
        when = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).astimezone(zone).isoformat()
        for index in range(count):
            self.counter += 1
            values = dict(session_role=role, cache_input_tokens=1000, cache_read_tokens=800,
                          estimated_cost=.008, cache_savings_estimate=.002, started_at=when, completed_at=when)
            values.update(overrides)
            self.repo.record_token_usage_batch(f"run-{self.counter}", repository, index % issues + 1, [
                event(f"e{self.counter}", sequence=self.counter, **values)])

    def evidence(self, role: str = "primary", repository: str = REPOSITORY) -> list:
        with self.repo.connect() as database:
            return cache_routing_evidence(database, repository, role)

    def test_sufficient_recent_exact_evidence_qualifies(self) -> None:
        self.add(20)
        (item,) = self.evidence()
        self.assertEqual((item["provider"], item["model"], item["effort"]), ("claude", "claude-sonnet-5", "high"))
        self.assertAlmostEqual(item["api_cost_discount"], 0.2)

    def test_local_timezone_timestamps_count_as_recent(self) -> None:
        self.add(20, offset_hours=-5)
        self.assertEqual(len(self.evidence()), 1)

    def test_stale_future_or_thin_history_is_not_evidence(self) -> None:
        for name, kwargs in {
            "old": dict(count=25, days_ago=45),
            "future": dict(count=25, days_ago=-3),
            "too-few-issues": dict(count=25, issues=4),
            "too-few-samples": dict(count=19),
        }.items():
            with self.subTest(name=name):
                self.setUp()
                self.add(**kwargs)
                self.assertEqual(self.evidence(), [])

    def test_other_repository_or_role_never_contributes(self) -> None:
        self.add(20, role="primary", repository="other/repo")
        self.add(20, role="adversarial:test:1:0")
        self.add(20, role="adversarial_security:fix:1")
        self.assertEqual(self.evidence("primary"), [])
        self.assertEqual(self.evidence("adversarial:fix"), [])
        self.assertEqual(self.evidence("adversarial:test") and 1, 1)  # exact stage family can qualify
        self.assertEqual(self.evidence("adversarial_security:fix") and 1, 1)

    def test_role_families_do_not_prefix_collide(self) -> None:
        self.add(20, role="adversarial_security:fix:1")
        self.assertEqual(self.evidence("adversarial:fix"), [])
        self.add(20, role="adversarial:fixture:1")
        self.assertEqual(self.evidence("adversarial:fix"), [])

    def test_one_unmeasured_or_unpriced_call_disqualifies_the_route(self) -> None:
        for name, bad in {
            "no-cache-counter": dict(cache_input_tokens=None, cache_read_tokens=None, cache_savings_estimate=None),
            "no-cost": dict(estimated_cost=None),
            "failed": dict(success=False, cache_input_tokens=None, cache_read_tokens=None,
                           cache_savings_estimate=None),
            "no-rate": dict(pricing_rate_id=""),
            "other-price-version": dict(pricing_version="2026-10-01", pricing_rate_id="claude/claude-sonnet-5@2026-10-01"),
        }.items():
            with self.subTest(name=name):
                self.setUp()
                self.add(24)
                self.assertEqual(len(self.evidence()), 1, "precondition")
                self.add(1, **bad)
                self.assertEqual(self.evidence(), [])

    def test_no_net_saving_is_not_a_discount(self) -> None:
        for savings in (0.0, -0.001):
            with self.subTest(savings=savings):
                self.setUp()
                self.add(20, cache_savings_estimate=savings)
                self.assertEqual(self.evidence(), [])

    def test_discount_is_capped_and_routes_are_kept_apart(self) -> None:
        self.add(20, estimated_cost=.001, cache_savings_estimate=.009)
        self.add(20, model="claude-opus-5-5", pricing_rate_id="claude/claude-opus-5-5@2026-01-01")
        self.add(10, model="claude-haiku-4-5", pricing_rate_id="claude/claude-haiku-4-5@2026-01-01")
        by_model = {item["model"]: item for item in self.evidence()}
        self.assertEqual(set(by_model), {"claude-sonnet-5", "claude-opus-5-5"})
        self.assertEqual(by_model["claude-sonnet-5"]["api_cost_discount"], 0.5)
        self.assertAlmostEqual(by_model["claude-opus-5-5"]["api_cost_discount"], 0.2)


class EvidenceUseTests(unittest.TestCase):
    ROW = {"provider": "claude", "model": "m", "effort": "high", "samples": 40, "issues": 8,
           "success_rate": 1.0, "api_cost_discount": 0.4}

    def test_only_the_exact_route_is_adjusted(self) -> None:
        self.assertAlmostEqual(cache_adjusted_cost(10.0, "claude", "m", "high", [self.ROW]), 6.0)
        for route in (("codex", "m", "high"), ("claude", "other", "high"), ("claude", "m", "low")):
            self.assertEqual(cache_adjusted_cost(10.0, *route, [self.ROW]), 10.0)

    def test_unknown_cost_stays_unknown(self) -> None:
        self.assertIsNone(cache_adjusted_cost(None, "claude", "m", "high", [self.ROW]))

    def test_weak_or_malformed_evidence_changes_nothing(self) -> None:
        weak = [
            {**self.ROW, "samples": 5}, {**self.ROW, "issues": 1}, {**self.ROW, "success_rate": 0.5},
            {**self.ROW, "api_cost_discount": 0.9}, {**self.ROW, "api_cost_discount": 0},
            {**self.ROW, "api_cost_discount": -0.2}, {**self.ROW, "api_cost_discount": float("nan")},
            {**self.ROW, "api_cost_discount": float("inf")}, {**self.ROW, "api_cost_discount": "much"},
            {k: v for k, v in self.ROW.items() if k != "api_cost_discount"}, {},
        ]
        for row in weak:
            with self.subTest(row=row):
                self.assertEqual(cache_adjusted_cost(10.0, "claude", "m", "high", [row]), 10.0)

    def candidates(self):
        return [RouterCandidate("claude", "Claude", (RoutingTier(1, 10, "claude-sonnet-5-5", "medium"),))]

    def test_router_prompt_is_unchanged_without_evidence(self) -> None:
        kwargs = dict(title="Fix", body="Body", labels=["bug"], candidates=self.candidates())
        plain = build_router_prompt(**kwargs)
        self.assertEqual(build_router_prompt(**kwargs, cache_evidence=()), plain)
        self.assertNotIn("native-cache", plain)

    def test_router_prompt_with_evidence_keeps_the_safety_gates_in_force(self) -> None:
        prompt = build_router_prompt(title="Fix", body="Body", labels=[], candidates=self.candidates(),
                                     cache_evidence=[self.ROW])
        section = prompt[prompt.index("native-cache"):]
        self.assertIn(json.dumps(self.ROW["model"]), section)
        lowered = section.lower()
        for phrase in ("capability", "independence", "not subscription billing"):
            self.assertIn(phrase, lowered)
        self.assertRegex(lowered, r"do not infer.*unmeasured")


if __name__ == "__main__":
    unittest.main()
