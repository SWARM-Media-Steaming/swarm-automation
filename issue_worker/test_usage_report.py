"""Tests for the Usage & cost query API (issue #295).

The fixture below is one small but deliberately awkward history: two
repositories, one issue worked twice, a router call, both adversarial
stages, a remediation, a review, a priced model and an unpriced one, a
provider that reported nothing, and a failure. Nearly every test reads that
same fixture, so a filter that quietly widens or a join that quietly drops
rows shows up as a wrong number rather than as an empty result.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

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
        input_tokens=10_000,
        output_tokens=1_000,
        reasoning_tokens=None,
        cached_input_tokens=2_000,
        cache_read_tokens=2_000,
        cache_write_tokens=None,
        total_tokens=13_000,
        estimated_cost=0.05,
        currency="USD",
        started_at="2026-03-10T10:00:00+00:00",
        completed_at="2026-03-10T10:05:00+00:00",
        duration_ms=300_000,
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


class UsageReportTestCase(unittest.TestCase):
    """One shared fixture; see the module docstring for what it contains."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "history.sqlite3"
        self.repository = ExecutionHistoryRepository(self.database_path)

        self.first = self._execution("acme/app", 7, "Add the thing", "B+", "claude")
        self.second = self._execution("acme/app", 7, "Add the thing", "B+", "claude")
        self.other_repo = self._execution("acme/other", 12, "Fix the other thing", "A", "codex")
        # An execution that never produced telemetry: an imported issue, or a
        # run from before #280. It must read as "usage unavailable".
        self.imported = self.repository.import_issue(
            "acme/app", {"number": 99, "title": "Imported", "state": "open"}, "2026-01-01T00:00:00+00:00"
        )

        self.repository.record_token_usage_batch(self.first, "acme/app", 7, [
            # A pre-flight routing call: recorded before the execution row
            # existed, linked to it when the batch was persisted.
            _event("r1", agent_type="router", prompt_type="initial", model="claude-haiku-4-5",
                   reasoning_effort="low", input_tokens=4_000, output_tokens=300,
                   cached_input_tokens=None, cache_read_tokens=None, total_tokens=4_300,
                   estimated_cost=0.0055, started_at="2026-03-10T09:58:00+00:00"),
            _event("p1"),
            _event("u1", agent_type="adversarial_uat", prompt_type="adversarial_scan",
                   provider="Codex", model="gpt-5.6-terra", reasoning_effort="medium",
                   input_tokens=20_000, output_tokens=2_000, cached_input_tokens=5_000,
                   cache_read_tokens=5_000, total_tokens=22_000, estimated_cost=0.045,
                   started_at="2026-03-10T11:00:00+00:00",
                   pricing_rate_id="codex/gpt-5.6-terra@2026-01-01"),
            _event("s1", agent_type="adversarial_cybersecurity", prompt_type="adversarial_scan",
                   provider="Grok", model="grok-4.6", input_tokens=8_000, output_tokens=900,
                   cached_input_tokens=None, cache_read_tokens=None, total_tokens=8_900,
                   estimated_cost=0.0075, started_at="2026-03-10T12:00:00+00:00"),
        ])
        self.repository.record_token_usage_batch(self.second, "acme/app", 7, [
            # Attempt 2: a retry, a remediation, and a review.
            _event("p2", prompt_type="retry", attempt_number=2,
                   started_at="2026-04-02T09:00:00+00:00", estimated_cost=0.06),
            _event("m1", agent_type="remediation", prompt_type="remediation", attempt_number=2,
                   started_at="2026-04-02T10:00:00+00:00", estimated_cost=0.02),
            # An unpriced model: real tokens, no matching catalog rate. Must
            # be "tokens only", never treated as free.
            _event("v1", agent_type="review", prompt_type="review", attempt_number=2,
                   provider="Grok", model="grok-experimental", total_tokens=5_000,
                   input_tokens=4_000, output_tokens=1_000, cached_input_tokens=None,
                   cache_read_tokens=None, estimated_cost=None, pricing_status="unknown_model",
                   pricing_rate_id="", input_rate_per_million=None,
                   cached_input_rate_per_million=None, cache_write_rate_per_million=None,
                   output_rate_per_million=None, started_at="2026-04-02T11:00:00+00:00"),
            # The provider returned no usage object at all.
            _event("n1", agent_type="summarizer", prompt_type="summary", attempt_number=2,
                   input_tokens=None, output_tokens=None, cached_input_tokens=None,
                   cache_read_tokens=None, total_tokens=None, estimated_cost=None,
                   pricing_status="no_usage", pricing_rate_id="",
                   started_at="2026-04-02T12:00:00+00:00"),
            # A failed call that still reported partial usage.
            _event("f1", prompt_type="retry", attempt_number=2, success=False,
                   error_type="provider_exit_nonzero", output_tokens=None, total_tokens=None,
                   estimated_cost=None, pricing_status="priced",
                   started_at="2026-04-02T13:00:00+00:00"),
        ])
        self.repository.record_token_usage_batch(self.other_repo, "acme/other", 12, [
            _event("o1", provider="Codex", model="gpt-5.6-luna", reasoning_effort="low",
                   input_tokens=1_000, output_tokens=100, cached_input_tokens=None,
                   cache_read_tokens=None, total_tokens=1_100, estimated_cost=0.001,
                   started_at="2026-05-01T08:00:00+00:00"),
        ])

    def _execution(self, repository: str, number: int, title: str, grade: str, router: str) -> str:
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
                routing_decision={"prompt_grade": grade, "router_provider": router},
            ),
            "2026-03-10T09:57:00+00:00",
        )

    def report(self, repositories=None, **options):
        return self.repository.usage_report(repositories, **options)


class SummaryAndCoverageTests(UsageReportTestCase):
    def test_totals_cover_every_recorded_invocation(self) -> None:
        summary = self.report()["summary"]
        self.assertEqual(summary["invocations"], 10)
        self.assertEqual(summary["issues"], 2)
        self.assertEqual(summary["executions"], 3)

    def test_coverage_separates_priced_unpriced_unreported_and_failed(self) -> None:
        coverage = self.report()["coverage"]
        self.assertEqual(coverage["tokens_only"], 1)  # the unpriced model
        self.assertEqual(coverage["unreported"], 1)  # the provider that reported nothing
        self.assertEqual(coverage["failed"], 1)
        self.assertEqual(coverage["partial"], 0)
        self.assertEqual(coverage["complete"], 7)
        self.assertEqual(sum(coverage.values()), 10)

    def test_a_successful_call_missing_some_fields_is_partial_not_unreported(self) -> None:
        self.repository.record_token_usage_batch(self.second, "acme/app", 7, [
            _event("partial-1", input_tokens=500, output_tokens=None, total_tokens=None,
                   estimated_cost=None, started_at="2026-04-03T09:00:00+00:00"),
        ])
        coverage = self.report()["coverage"]
        self.assertEqual(coverage["partial"], 1)
        self.assertEqual(coverage["unreported"], 1)

    def test_estimated_cost_reports_how_much_of_it_was_priced(self) -> None:
        summary = self.report()["summary"]
        # 7 priced rows; the unpriced, unreported and failed rows carry no cost.
        self.assertEqual(summary["pricedInvocations"], 7)
        self.assertLess(summary["pricedInvocations"], summary["invocations"])
        self.assertAlmostEqual(summary["estimatedCost"], 0.1890, places=4)

    def test_a_stored_zero_cost_on_an_unreported_row_is_ignored(self) -> None:
        self.repository.record_token_usage_batch(self.second, "acme/app", 7, [
            _event(
                "ghost-cost",
                agent_type="other",
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                estimated_cost=0.0,
                pricing_status="priced",
                started_at="2026-04-06T09:00:00+00:00",
            ),
        ])
        payload = self.report(agent_type="other")
        self.assertEqual(payload["coverage"]["unreported"], 1)
        self.assertIsNone(payload["summary"]["estimatedCost"])
        self.assertEqual(payload["summary"]["pricedInvocations"], 0)

    def test_unreported_totals_stay_null_rather_than_becoming_zero(self) -> None:
        # Nothing in this fixture reports reasoning tokens, so the reasoning
        # total must be unavailable — not 0, which would claim the providers
        # did no reasoning.
        summary = self.report()["summary"]
        self.assertIsNone(summary["reasoningTokens"])
        self.assertEqual(summary["reasoningReported"], 0)
        self.assertIsNotNone(summary["inputTokens"])
        self.assertEqual(summary["inputReported"], 9)

    def test_a_genuine_zero_is_not_confused_with_a_missing_value(self) -> None:
        self.repository.record_token_usage_batch(self.second, "acme/app", 7, [
            _event("zero-1", reasoning_tokens=0, started_at="2026-04-04T09:00:00+00:00"),
        ])
        summary = self.report()["summary"]
        self.assertEqual(summary["reasoningTokens"], 0)
        self.assertEqual(summary["reasoningReported"], 1)

    def test_cache_reads_and_writes_are_reported_separately(self) -> None:
        self.repository.record_token_usage_batch(self.second, "acme/app", 7, [
            _event("cw-1", cached_input_tokens=3_000, cache_read_tokens=1_000,
                   cache_write_tokens=2_000, started_at="2026-04-05T09:00:00+00:00"),
        ])
        summary = self.report()["summary"]
        # Only one row in the whole fixture reported a cache write, so the
        # write total must describe that row alone rather than absorbing
        # every cache read.
        self.assertEqual(summary["cacheWriteTokens"], 2_000)
        self.assertEqual(summary["cacheWriteReported"], 1)
        self.assertEqual(summary["cacheReadTokens"], 14_000)
        # The combined figure stays the providers' own reported total.
        self.assertEqual(summary["cachedTokens"], 16_000)

    def test_cached_and_reasoning_tokens_are_never_added_into_the_total(self) -> None:
        # The provider's own total is authoritative. Summing it must not pick
        # up cached or reasoning tokens a second time.
        summary = self.report(issue_number=12)["summary"]
        self.assertEqual(summary["totalTokens"], 1_100)
        self.assertEqual(summary["inputTokens"], 1_000)
        self.assertEqual(summary["outputTokens"], 100)


class FilterTests(UsageReportTestCase):
    def test_repository_filtering_is_isolated(self) -> None:
        self.assertEqual(self.report(["acme/app"])["summary"]["invocations"], 9)
        self.assertEqual(self.report(["acme/other"])["summary"]["invocations"], 1)
        self.assertEqual(self.report(["acme/app", "acme/other"])["summary"]["invocations"], 10)
        # An empty selection is the app-wide default, not "nothing".
        self.assertEqual(self.report([])["summary"]["invocations"], 10)

    def test_repository_filtering_also_scopes_the_facets(self) -> None:
        facets = self.report(["acme/other"])["facets"]
        self.assertEqual([entry["value"] for entry in facets["models"]], ["gpt-5.6-luna"])
        self.assertEqual([entry["number"] for entry in facets["issues"]], [12])

    def test_each_filter_narrows_on_its_own(self) -> None:
        cases = {
            "provider": ("Codex", 2),
            "model": ("claude-sonnet-5", 5),
            "effort": ("low", 2),
            "agent_type": ("adversarial_uat", 1),
            "prompt_type": ("retry", 2),
            "grade": ("A", 1),
            "coverage": ("tokens_only", 1),
        }
        for key, (value, expected) in cases.items():
            with self.subTest(filter=key):
                self.assertEqual(
                    self.report(**{key: value})["summary"]["invocations"], expected
                )
        self.assertEqual(self.report(issue_number=7)["summary"]["invocations"], 9)
        self.assertEqual(self.report(outcome="failure")["summary"]["invocations"], 1)
        self.assertEqual(self.report(outcome="success")["summary"]["invocations"], 9)
        self.assertEqual(self.report(execution_id=self.first)["summary"]["invocations"], 4)

    def test_provider_filtering_accepts_a_key_or_a_display_name(self) -> None:
        # Records store the display name; router cross-links pass the key.
        self.assertEqual(self.report(provider="codex")["summary"]["invocations"], 2)
        self.assertEqual(self.report(provider="Codex")["summary"]["invocations"], 2)

    def test_filters_combine(self) -> None:
        self.assertEqual(
            self.report(provider="Claude", prompt_type="retry", outcome="success")
            ["summary"]["invocations"],
            1,
        )
        self.assertEqual(
            self.report(["acme/app"], agent_type="router", grade="B+")
            ["summary"]["invocations"],
            1,
        )
        self.assertEqual(
            self.report(provider="Codex", effort="low")["summary"]["invocations"], 1
        )

    def test_search_matches_issue_number_title_provider_and_model(self) -> None:
        self.assertEqual(self.report(search="other thing")["summary"]["invocations"], 1)
        self.assertEqual(self.report(search="12")["summary"]["invocations"], 1)
        self.assertEqual(self.report(search="grok")["summary"]["invocations"], 2)
        self.assertEqual(self.report(search="haiku")["summary"]["invocations"], 1)
        self.assertEqual(self.report(search="nothing here")["summary"]["invocations"], 0)

    def test_date_filtering_uses_the_invocation_timestamp_not_the_write_time(self) -> None:
        # Every row in this fixture was written by the batch just now, so a
        # created_at filter would match all of them regardless of range.
        march = self.report(start_date="2026-03-01", end_date="2026-03-31")
        self.assertEqual(march["summary"]["invocations"], 4)
        april = self.report(start_date="2026-04-01", end_date="2026-04-30")
        self.assertEqual(april["summary"]["invocations"], 5)
        self.assertEqual(self.report(start_date="2026-04-02", end_date="2026-04-02")
                         ["summary"]["invocations"], 5)
        self.assertEqual(self.report(start_date="2027-01-01")["summary"]["invocations"], 0)

    def test_an_execution_with_empty_routing_decision_is_still_queryable(self) -> None:
        unrouted = self.repository.create(
            ExecutionStart(
                repository="acme/app",
                issue_number=55,
                issue_url="https://github.com/acme/app/issues/55",
                issue_title="No router",
                issue_body="",
                provider="Claude",
                model="claude-sonnet-5",
                effort="high",
                branch_name="ai/claude/issue-55",
                application_version="0.1.0",
            ),
            "2026-02-11T09:00:00+00:00",
        )
        self.repository.record_token_usage_batch(unrouted, "acme/app", 55, [
            _event("plain-1", started_at="2026-02-11T09:05:00+00:00"),
        ])
        payload = self.report(["acme/app"], issue_number=55)
        self.assertEqual(payload["summary"]["invocations"], 1)
        self.assertEqual(payload["invocations"]["rows"][0]["id"], "plain-1")
        self.assertEqual(payload["invocations"]["rows"][0]["grade"], "")

    def test_a_router_call_before_its_execution_row_still_joins(self) -> None:
        rows = self.report(agent_type="router")["invocations"]["rows"]
        self.assertEqual(len(rows), 1)
        # Recorded 09:58, two minutes before the execution started, and still
        # carries the issue title, grade and execution the batch linked it to.
        self.assertEqual(rows[0]["issueNumber"], 7)
        self.assertEqual(rows[0]["issueTitle"], "Add the thing")
        self.assertEqual(rows[0]["grade"], "B+")
        self.assertEqual(rows[0]["executionId"], self.first)

    def test_multiple_attempts_of_one_issue_are_both_counted(self) -> None:
        rows = {row["id"]: row for row in self.report(issue_number=7)["invocations"]["rows"]}
        self.assertEqual(rows["p1"]["attemptNumber"], 1)
        self.assertEqual(rows["p2"]["attemptNumber"], 2)
        self.assertEqual(self.report(issue_number=7)["summary"]["executions"], 2)

    def test_every_agent_stage_is_represented(self) -> None:
        agents = {
            entry["value"] for entry in self.report()["facets"]["agentTypes"]
        }
        self.assertEqual(
            agents,
            {"router", "primary", "adversarial_uat", "adversarial_cybersecurity",
             "remediation", "review", "summarizer"},
        )


class GroupingTests(UsageReportTestCase):
    def test_every_grouping_dimension_produces_rows(self) -> None:
        for group_by in usage_report.GROUP_BY_KEYS:
            with self.subTest(group_by=group_by):
                payload = self.report(group_by=group_by)
                self.assertEqual(payload["groupBy"], group_by)
                self.assertTrue(payload["groups"]["rows"])
                self.assertEqual(
                    sum(row["invocations"] for row in payload["groups"]["rows"]),
                    payload["summary"]["invocations"],
                )

    def test_grouping_by_issue_carries_the_issue_context(self) -> None:
        rows = {row["group"]: row for row in self.report(group_by="issue")["groups"]["rows"]}
        self.assertIn("acme/app#7", rows)
        row = rows["acme/app#7"]
        self.assertEqual(row["issueTitle"], "Add the thing")
        self.assertEqual(row["issueUrl"], "https://github.com/acme/app/issues/7")
        self.assertEqual(row["invocations"], 9)
        self.assertEqual(row["issues"], 1)

    def test_grouping_by_time_buckets_on_the_activity_date(self) -> None:
        months = {row["group"]: row["invocations"]
                  for row in self.report(group_by="month")["groups"]["rows"]}
        self.assertEqual(months, {"2026-03": 4, "2026-04": 5, "2026-05": 1})
        days = {row["group"]: row["invocations"]
                for row in self.report(group_by="day")["groups"]["rows"]}
        self.assertEqual(days["2026-03-10"], 4)
        self.assertEqual(days["2026-04-02"], 5)
        weeks = self.report(group_by="week")["groups"]["rows"]
        self.assertTrue(all(row["group"].startswith("2026-W") for row in weeks))

    def test_group_rows_carry_their_own_coverage_and_priced_counts(self) -> None:
        rows = {row["group"]: row for row in self.report(group_by="agent")["groups"]["rows"]}
        self.assertEqual(rows["review"]["coverage"]["tokens_only"], 1)
        self.assertEqual(rows["review"]["pricedInvocations"], 0)
        self.assertIsNone(rows["review"]["estimatedCost"])
        self.assertIsNotNone(rows["review"]["totalTokens"])
        self.assertEqual(rows["summarizer"]["coverage"]["unreported"], 1)
        self.assertIsNone(rows["summarizer"]["totalTokens"])

    def test_grouping_by_grade_uses_the_execution_join(self) -> None:
        rows = {row["group"]: row["invocations"]
                for row in self.report(group_by="grade")["groups"]["rows"]}
        self.assertEqual(rows, {"B+": 9, "A": 1})

    def test_grouping_by_outcome_partitions_success_from_failure(self) -> None:
        payload = self.report(group_by="outcome")
        self.assertEqual(payload["groupBy"], "outcome")
        rows = {row["group"]: row for row in payload["groups"]["rows"]}
        self.assertEqual(set(rows), {"success", "failure"})
        self.assertEqual(rows["success"]["invocations"], 9)
        self.assertEqual(rows["failure"]["invocations"], 1)
        self.assertEqual(rows["failure"]["pricedInvocations"], 0)
        self.assertIsNone(rows["failure"]["estimatedCost"])
        drilled = self.report(group_by="outcome", group_value="failure")
        self.assertEqual(drilled["invocations"]["total"], 1)
        self.assertEqual(drilled["invocations"]["rows"][0]["id"], "f1")
        self.assertEqual(drilled["invocations"]["rows"][0]["inputTokens"], 10_000)
        self.assertIsNone(drilled["invocations"]["rows"][0]["outputTokens"])


class SortingAndPagingTests(UsageReportTestCase):
    def test_sorting_orders_by_the_requested_column_and_direction(self) -> None:
        descending = [row["group"] for row in
                      self.report(group_by="model", sort="cost", direction="desc")["groups"]["rows"]]
        ascending = [row["group"] for row in
                     self.report(group_by="model", sort="cost", direction="asc")["groups"]["rows"]]
        self.assertEqual(descending[0], "claude-sonnet-5")
        # Unavailable costs sort last in *both* directions: a missing value
        # is not a small one.
        self.assertEqual(descending[-1], "grok-experimental")
        self.assertEqual(ascending[-1], "grok-experimental")
        by_name = [row["group"] for row in
                   self.report(group_by="model", sort="group", direction="asc")["groups"]["rows"]]
        self.assertEqual(by_name, sorted(by_name))

    def test_group_pages_are_bounded_and_snap_back_from_past_the_end(self) -> None:
        page = self.report(group_by="model", limit=2, group_offset=0)["groups"]
        self.assertEqual(len(page["rows"]), 2)
        self.assertEqual(page["total"], 6)
        self.assertEqual(page["offset"], 0)
        last = self.report(group_by="model", limit=2, group_offset=500)["groups"]
        self.assertEqual(last["offset"], 4)
        self.assertEqual(len(last["rows"]), 2)

    def test_detail_pages_are_bounded_and_never_exceed_the_cap(self) -> None:
        page = self.report(limit=3)["invocations"]
        self.assertEqual(len(page["rows"]), 3)
        self.assertEqual(page["total"], 10)
        capped = self.report(limit=10_000)["invocations"]
        self.assertLessEqual(capped["limit"], usage_report.USAGE_PAGE_SIZE)

    def test_a_large_history_stays_paged_rather_than_returned_whole(self) -> None:
        self.repository.record_token_usage_batch(self.second, "acme/app", 7, [
            _event(f"bulk-{index}", started_at="2026-06-01T00:00:00+00:00")
            for index in range(500)
        ])
        payload = self.report()
        self.assertEqual(payload["summary"]["invocations"], 510)
        self.assertLessEqual(len(payload["invocations"]["rows"]), usage_report.USAGE_PAGE_SIZE)
        self.assertEqual(payload["invocations"]["total"], 510)
        self.assertLessEqual(len(payload["facets"]["issues"]), usage_report.FACET_LIMIT)

    def test_invocations_are_newest_first(self) -> None:
        rows = self.report(["acme/app"])["invocations"]["rows"]
        timestamps = [row["startedAt"] for row in rows]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))


class DrillDownTests(UsageReportTestCase):
    def test_selecting_an_aggregate_row_narrows_the_invocation_list(self) -> None:
        payload = self.report(group_by="agent", group_value="adversarial_uat")
        rows = payload["invocations"]["rows"]
        self.assertEqual(payload["invocations"]["total"], 1)
        self.assertEqual(rows[0]["agentType"], "adversarial_uat")
        self.assertEqual(payload["invocations"]["groupValue"], "adversarial_uat")
        # The aggregate table itself is unchanged: selecting a row must not
        # make the other rows disappear.
        self.assertGreater(len(payload["groups"]["rows"]), 1)

    def test_drilling_into_an_issue_reaches_its_individual_invocations(self) -> None:
        payload = self.report(group_by="issue", group_value="acme/app#7")
        self.assertEqual(payload["invocations"]["total"], 9)
        self.assertTrue(all(row["issueNumber"] == 7 for row in payload["invocations"]["rows"]))

    def test_no_selection_shows_every_invocation_in_the_filter(self) -> None:
        payload = self.report(group_by="agent")
        self.assertIsNone(payload["invocations"]["groupValue"])
        self.assertEqual(payload["invocations"]["total"], 10)


class InvocationDetailTests(UsageReportTestCase):
    def test_detail_rows_expose_every_column_the_report_specifies(self) -> None:
        rows = {row["id"]: row for row in self.report(execution_id=self.first)["invocations"]["rows"]}
        row = rows["p1"]
        for key in ("agentType", "provider", "model", "promptType", "reasoningEffort",
                    "attemptNumber", "inputTokens", "cachedInputTokens", "reasoningTokens",
                    "outputTokens", "totalTokens", "estimatedCost", "durationMs", "coverage"):
            self.assertIn(key, row)
        self.assertEqual(row["coverage"], "complete")
        self.assertEqual(row["durationMs"], 300_000)

    def test_pricing_provenance_travels_with_every_priced_row(self) -> None:
        rows = {row["id"]: row for row in self.report(issue_number=7)["invocations"]["rows"]}
        self.assertEqual(rows["p1"]["pricingRateId"], "claude/claude-sonnet-5@2026-01-01")
        self.assertEqual(rows["p1"]["pricingVersion"], "2026-09-28")
        self.assertTrue(rows["p1"]["pricingSource"])
        # The unpriced row says why instead of carrying an empty cost with no
        # explanation.
        self.assertIsNone(rows["v1"]["estimatedCost"])
        self.assertEqual(rows["v1"]["pricingStatus"], "unknown_model")

    def test_a_failed_invocation_still_shows_the_usage_it_returned(self) -> None:
        rows = {row["id"]: row for row in self.report(outcome="failure")["invocations"]["rows"]}
        self.assertEqual(rows["f1"]["coverage"], "failed")
        self.assertFalse(rows["f1"]["success"])
        self.assertEqual(rows["f1"]["errorType"], "provider_exit_nonzero")
        self.assertEqual(rows["f1"]["inputTokens"], 10_000)
        self.assertIsNone(rows["f1"]["outputTokens"])


class EmptyStateTests(UsageReportTestCase):
    def test_availability_separates_no_telemetry_from_no_matches(self) -> None:
        payload = self.report()
        self.assertTrue(payload["hasAnyUsage"])
        self.assertTrue(payload["hasAnyActivity"])
        # The imported execution has no usage rows of its own.
        self.assertEqual(payload["executionsWithoutUsage"], 1)
        filtered = self.report(search="nothing matches this")
        self.assertEqual(filtered["summary"]["invocations"], 0)
        self.assertTrue(filtered["hasAnyUsage"])

    def test_a_repository_with_activity_but_no_usage_says_so(self) -> None:
        with TemporaryDirectory() as tmp:
            repository = ExecutionHistoryRepository(Path(tmp) / "h.sqlite3")
            repository.import_issue(
                "acme/legacy", {"number": 1, "title": "Old", "state": "closed"},
                "2026-01-01T00:00:00+00:00",
            )
            payload = repository.usage_report(["acme/legacy"])
        self.assertFalse(payload["hasAnyUsage"])
        self.assertTrue(payload["hasAnyActivity"])
        self.assertEqual(payload["executionsWithoutUsage"], 1)

    def test_an_empty_database_reports_neither_usage_nor_activity(self) -> None:
        with TemporaryDirectory() as tmp:
            payload = ExecutionHistoryRepository(Path(tmp) / "h.sqlite3").usage_report()
        self.assertFalse(payload["hasAnyUsage"])
        self.assertFalse(payload["hasAnyActivity"])
        self.assertEqual(payload["summary"]["invocations"], 0)
        self.assertIsNone(payload["summary"]["estimatedCost"])

    def test_the_missing_database_payload_matches_a_real_one(self) -> None:
        # The desktop has one rendering path, so an absent database must not
        # produce a differently-shaped object.
        empty = usage_report.empty_report()
        real = self.report()
        self.assertEqual(set(empty), set(real))
        self.assertEqual(set(empty["summary"]), set(real["summary"]))
        self.assertEqual(set(empty["coverage"]), set(real["coverage"]))
        self.assertEqual(set(empty["facets"]), set(real["facets"]))


class ExecutionAttachmentTests(UsageReportTestCase):
    def test_per_execution_summaries_are_one_query_for_the_page(self) -> None:
        summaries = self.repository.usage_summaries_for_executions(
            [self.first, self.second, self.imported]
        )
        self.assertEqual(summaries[self.first]["invocations"], 4)
        self.assertEqual(summaries[self.second]["invocations"], 5)
        # An execution with no telemetry is absent, not present at zero.
        self.assertNotIn(self.imported, summaries)

    def test_per_execution_records_are_grouped_by_execution(self) -> None:
        records = self.repository.usage_records_for_executions([self.first, self.second])
        self.assertEqual(len(records[self.first]), 4)
        self.assertEqual(len(records[self.second]), 5)
        self.assertNotIn(self.imported, records)
        # Oldest first, so a card reads in the order the calls happened.
        timestamps = [row["startedAt"] for row in records[self.first]]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_more_executions_than_one_bind_batch_are_still_all_returned(self) -> None:
        # The unpaged history export can hand over a whole repository's
        # executions at once, which is more bound parameters than SQLite
        # accepts in one statement.
        ids = [self.first, self.second] + [f"absent-{index}" for index in range(900)]
        summaries = self.repository.usage_summaries_for_executions(ids)
        self.assertEqual(set(summaries), {self.first, self.second})
        records = self.repository.usage_records_for_executions(ids)
        self.assertEqual(len(records[self.first]), 4)

    def test_no_execution_ids_means_no_query(self) -> None:
        self.assertEqual(self.repository.usage_summaries_for_executions([]), {})
        self.assertEqual(self.repository.usage_records_for_executions([""]), {})


class NormalizationTests(unittest.TestCase):
    def test_unknown_selectors_fall_back_instead_of_reaching_sql(self) -> None:
        self.assertEqual(usage_report.normalize_group_by("'; DROP TABLE"), "issue")
        self.assertEqual(usage_report.normalize_sort("rowid"), "cost")
        self.assertEqual(usage_report.normalize_direction("sideways"), "desc")
        self.assertEqual(usage_report.normalize_outcome("maybe"), "all")
        self.assertEqual(usage_report.normalize_coverage("invented"), "")

    def test_issue_numbers_accept_a_hash_prefix_and_reject_nonsense(self) -> None:
        self.assertEqual(usage_report.UsageFilters(issue_number="#295").issue_number, 295)
        self.assertEqual(usage_report.UsageFilters(issue_number=295).issue_number, 295)
        self.assertIsNone(usage_report.UsageFilters(issue_number="").issue_number)
        self.assertIsNone(usage_report.UsageFilters(issue_number="abc").issue_number)
        self.assertIsNone(usage_report.UsageFilters(issue_number="0").issue_number)

    def test_limits_are_clamped_to_the_page_size(self) -> None:
        self.assertEqual(usage_report.clamp_limit(5), 5)
        self.assertEqual(usage_report.clamp_limit(10_000), usage_report.USAGE_PAGE_SIZE)
        self.assertEqual(usage_report.clamp_limit(0), usage_report.USAGE_PAGE_SIZE)
        self.assertEqual(usage_report.clamp_limit("nope"), usage_report.USAGE_PAGE_SIZE)
        self.assertEqual(usage_report.clamp_offset(-4), 0)


class CommandLineTests(UsageReportTestCase):
    def _run(self, argv: list[str]) -> dict:
        from io import StringIO
        from contextlib import redirect_stdout

        buffer = StringIO()
        with redirect_stdout(buffer):
            code = history_main(argv)
        self.assertEqual(code, 0)
        return json.loads(buffer.getvalue())

    def test_the_usage_mode_prints_the_whole_report(self) -> None:
        payload = self._run(["--db", str(self.database_path), "--usage", "--group-by", "provider"])
        self.assertEqual(payload["groupBy"], "provider")
        self.assertEqual(payload["summary"]["invocations"], 10)
        self.assertIn("coverage", payload)
        self.assertIn("facets", payload)

    def test_the_usage_mode_groups_by_outcome(self) -> None:
        payload = self._run([
            "--db", str(self.database_path), "--usage", "--group-by", "outcome",
        ])
        self.assertEqual(payload["groupBy"], "outcome")
        self.assertEqual(
            {row["group"] for row in payload["groups"]["rows"]},
            {"success", "failure"},
        )

    def test_the_usage_mode_applies_repository_and_filter_arguments(self) -> None:
        payload = self._run([
            "--db", str(self.database_path), "--usage",
            "--repository", "acme/app", "--agent-type", "router",
        ])
        self.assertEqual(payload["summary"]["invocations"], 1)
        self.assertEqual(payload["filters"]["agentType"], "router")
        self.assertEqual(payload["filters"]["repositories"], ["acme/app"])

    def test_a_missing_database_still_prints_a_usable_report(self) -> None:
        payload = self._run([
            "--db", str(self.database_path.parent / "absent.sqlite3"), "--usage",
        ])
        self.assertFalse(payload["hasAnyUsage"])
        self.assertEqual(payload["summary"]["invocations"], 0)

    def test_validate_pricing_needs_no_database(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buffer = StringIO()
        with redirect_stdout(buffer):
            code = history_main(["--validate-pricing"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["problems"], [])


class ReadOnlyTests(UsageReportTestCase):
    def test_building_a_report_never_changes_the_stored_rows(self) -> None:
        with self.repository.connect() as database:
            before = database.execute(
                "SELECT id, total_tokens, estimated_cost, pricing_rate_id FROM ai_token_usage "
                "ORDER BY id"
            ).fetchall()
        for group_by in usage_report.GROUP_BY_KEYS:
            self.report(group_by=group_by)
        with self.repository.connect() as database:
            after = database.execute(
                "SELECT id, total_tokens, estimated_cost, pricing_rate_id FROM ai_token_usage "
                "ORDER BY id"
            ).fetchall()
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])


if __name__ == "__main__":
    unittest.main()
