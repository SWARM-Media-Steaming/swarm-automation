"""Issue #295 — Usage & cost query, history joins, and worker persistence.

Independent of the implementer's fixture. Spec-derived invariants:

* Every listed filter works alone and in combination; grouping covers every
  dimension; repository isolation matches the other Feedback tabs.
* Date filters use the invocation's own start/completion time, never the
  later batch-persistence ``created_at``.
* Nullable token and cost fields stay NULL through aggregation; missing is
  never coalesced to zero.
* Coverage has five distinct states. Imported/pre-#280 executions are
  usage-unavailable. GitHub comments are never scraped to fill them.
* Queries are read-only, paginated, and do not recost stored rows.
* Router, primary, UAT, cybersecurity, remediation, review, and failure
  records join to prompt grade / issue / attempt. Multiple attempts of one
  issue are both visible.
* Telemetry or pricing failures cannot fail the underlying AI call.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import test_swarm_issue_worker as fixtures  # noqa: E402
import usage_report  # noqa: E402
from ai_execution_history import (  # noqa: E402
    ExecutionHistoryRepository,
    ExecutionStart,
    attach_token_usage,
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
        input_tokens=8_000,
        output_tokens=800,
        reasoning_tokens=None,
        cached_input_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        total_tokens=8_800,
        estimated_cost=0.04,
        currency="USD",
        started_at="2026-02-10T12:00:00+00:00",
        completed_at="2026-02-10T12:01:00+00:00",
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


class UsageQueryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "history.sqlite3"
        self.repository = ExecutionHistoryRepository(self.database_path)
        self.alpha = self._execution("swarm/alpha", 42, "Wire the report", "B", "claude")
        self.retry = self._execution("swarm/alpha", 42, "Wire the report", "B", "claude")
        self.beta = self._execution("swarm/beta", 7, "Other repo", "A", "codex")
        self.imported = self.repository.import_issue(
            "swarm/alpha",
            {
                "number": 100,
                "title": "Legacy issue",
                "state": "open",
                "body": (
                    "### AI Usage\n"
                    "| Agent | Tokens | Estimated cost |\n"
                    "| --- | ---: | ---: |\n"
                    "| Primary | 999999 | $12.34 |\n"
                ),
                "url": "https://github.com/swarm/alpha/issues/100",
            },
            "2026-01-02T00:00:00+00:00",
        )
        self.repository.record_token_usage_batch(self.alpha, "swarm/alpha", 42, [
            _event(
                "router-1",
                agent_type="router",
                prompt_type="initial",
                model="claude-haiku-4-5",
                reasoning_effort="low",
                input_tokens=3_000,
                output_tokens=200,
                total_tokens=3_200,
                estimated_cost=0.004,
                started_at="2026-02-10T11:50:00+00:00",
                pricing_rate_id="claude/claude-haiku-4-5@2026-01-01",
            ),
            _event(
                "primary-1",
                cached_input_tokens=5_000,
                cache_read_tokens=4_000,
                cache_write_tokens=1_000,
                total_tokens=13_800,
                estimated_cost=0.09,
            ),
            _event(
                "uat-1",
                agent_type="adversarial_uat",
                prompt_type="adversarial_scan",
                provider="Codex",
                model="gpt-5.6-terra",
                reasoning_effort="medium",
                input_tokens=12_000,
                output_tokens=1_200,
                cached_input_tokens=6_000,
                cache_read_tokens=6_000,
                total_tokens=13_200,
                estimated_cost=0.03,
                started_at="2026-02-10T13:00:00+00:00",
                pricing_rate_id="codex/gpt-5.6-terra@2026-01-01",
            ),
            _event(
                "cyber-1",
                agent_type="adversarial_cybersecurity",
                prompt_type="adversarial_scan",
                provider="Grok",
                model="grok-4.6",
                input_tokens=6_000,
                output_tokens=600,
                total_tokens=6_600,
                estimated_cost=0.01,
                started_at="2026-02-10T14:00:00+00:00",
                pricing_rate_id="grok/grok-4.6@2026-01-01",
            ),
        ])
        self.repository.record_token_usage_batch(self.retry, "swarm/alpha", 42, [
            _event(
                "retry-1",
                prompt_type="retry",
                attempt_number=2,
                started_at="2026-03-01T09:00:00+00:00",
                estimated_cost=0.05,
            ),
            _event(
                "fix-1",
                agent_type="remediation",
                prompt_type="remediation",
                attempt_number=2,
                started_at="2026-03-01T10:00:00+00:00",
                estimated_cost=0.02,
            ),
            _event(
                "review-1",
                agent_type="review",
                prompt_type="review",
                attempt_number=2,
                provider="Grok",
                model="grok-experimental",
                input_tokens=2_000,
                output_tokens=400,
                total_tokens=2_400,
                estimated_cost=None,
                pricing_status="unknown_model",
                pricing_rate_id="",
                input_rate_per_million=None,
                cached_input_rate_per_million=None,
                cache_write_rate_per_million=None,
                output_rate_per_million=None,
                started_at="2026-03-01T11:00:00+00:00",
            ),
            _event(
                "none-1",
                agent_type="summarizer",
                prompt_type="summary",
                attempt_number=2,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                estimated_cost=None,
                pricing_status="no_usage",
                pricing_rate_id="",
                started_at="2026-03-01T12:00:00+00:00",
            ),
            _event(
                "fail-1",
                prompt_type="retry",
                attempt_number=2,
                success=False,
                error_type="provider_exit_nonzero",
                output_tokens=None,
                total_tokens=None,
                estimated_cost=None,
                started_at="2026-03-01T13:00:00+00:00",
            ),
        ])
        self.repository.record_token_usage_batch(self.beta, "swarm/beta", 7, [
            _event(
                "beta-1",
                provider="Codex",
                model="gpt-5.6-luna",
                reasoning_effort="low",
                input_tokens=500,
                output_tokens=50,
                total_tokens=550,
                estimated_cost=0.001,
                started_at="2026-04-15T08:00:00+00:00",
                pricing_rate_id="codex/gpt-5.6-luna@2026-01-01",
            ),
        ])
        with self.repository.connect() as database:
            database.execute(
                "UPDATE ai_token_usage SET created_at = ? WHERE id = ?",
                ("2019-01-01T00:00:00+00:00", "primary-1"),
            )

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
            "2026-02-10T11:45:00+00:00",
        )

    def report(self, repositories=None, **options):
        return self.repository.usage_report(repositories, **options)


class FilterAndGroupTests(UsageQueryFixture):
    def test_each_filter_narrows_on_its_own(self) -> None:
        cases = {
            "provider": ("Grok", 2),
            "model": ("claude-sonnet-5", 5),
            "effort": ("low", 2),
            "agent_type": ("adversarial_uat", 1),
            "prompt_type": ("remediation", 1),
            "grade": ("A", 1),
            "coverage": ("tokens_only", 1),
            "outcome": ("failure", 1),
        }
        for key, (value, expected) in cases.items():
            with self.subTest(filter=key):
                self.assertEqual(self.report(**{key: value})["summary"]["invocations"], expected)
        self.assertEqual(self.report(issue_number=42)["summary"]["invocations"], 9)
        self.assertEqual(self.report(execution_id=self.alpha)["summary"]["invocations"], 4)
        self.assertEqual(self.report(search="Other repo")["summary"]["invocations"], 1)

    def test_representative_filter_combinations(self) -> None:
        self.assertEqual(
            self.report(["swarm/alpha"], agent_type="router", grade="B")["summary"]["invocations"],
            1,
        )
        self.assertEqual(
            self.report(provider="Claude", prompt_type="retry", outcome="success")["summary"]["invocations"],
            1,
        )
        self.assertEqual(
            self.report(start_date="2026-03-01", end_date="2026-03-01", coverage="unreported")
            ["summary"]["invocations"],
            1,
        )

    def test_repository_isolation_matches_feedback_semantics(self) -> None:
        self.assertEqual(self.report(["swarm/alpha"])["summary"]["invocations"], 9)
        self.assertEqual(self.report(["swarm/beta"])["summary"]["invocations"], 1)
        self.assertEqual(self.report(["swarm/alpha", "swarm/beta"])["summary"]["invocations"], 10)
        self.assertEqual(self.report([])["summary"]["invocations"], 10)
        self.assertEqual(self.report(["nobody/nowhere"])["summary"]["invocations"], 0)
        injected = self.report(["swarm/alpha'; DROP TABLE ai_token_usage;--"])
        self.assertEqual(injected["summary"]["invocations"], 0)
        self.assertEqual(self.report()["summary"]["invocations"], 10)

    def test_every_grouping_dimension_covers_the_filtered_invocations(self) -> None:
        for group_by in usage_report.GROUP_BY_KEYS:
            with self.subTest(group_by=group_by):
                payload = self.report(group_by=group_by)
                self.assertEqual(
                    sum(row["invocations"] for row in payload["groups"]["rows"]),
                    payload["summary"]["invocations"],
                )

    def test_grade_and_issue_joins_survive_a_router_call_recorded_before_the_execution(self) -> None:
        row = self.report(agent_type="router")["invocations"]["rows"][0]
        self.assertEqual(row["issueNumber"], 42)
        self.assertEqual(row["issueTitle"], "Wire the report")
        self.assertEqual(row["grade"], "B")
        self.assertEqual(row["executionId"], self.alpha)
        self.assertEqual(row["issueUrl"], "https://github.com/swarm/alpha/issues/42")

    def test_multiple_attempts_of_one_issue_are_both_visible(self) -> None:
        rows = {row["id"]: row for row in self.report(issue_number=42)["invocations"]["rows"]}
        self.assertEqual(rows["primary-1"]["attemptNumber"], 1)
        self.assertEqual(rows["retry-1"]["attemptNumber"], 2)
        self.assertEqual(self.report(issue_number=42)["summary"]["executions"], 2)

    def test_every_required_agent_stage_is_present(self) -> None:
        agents = {entry["value"] for entry in self.report()["facets"]["agentTypes"]}
        for required in (
            "router",
            "primary",
            "adversarial_uat",
            "adversarial_cybersecurity",
            "remediation",
            "review",
        ):
            self.assertIn(required, agents)


class DateCoverageAndNullTests(UsageQueryFixture):
    def test_date_filter_uses_started_at_even_when_created_at_is_years_earlier(self) -> None:
        with self.repository.connect() as database:
            created = database.execute(
                "SELECT created_at FROM ai_token_usage WHERE id = 'primary-1'"
            ).fetchone()[0]
        self.assertTrue(str(created).startswith("2019"))
        february = self.report(start_date="2026-02-01", end_date="2026-02-28")
        ids = {row["id"] for row in february["invocations"]["rows"]}
        self.assertIn("primary-1", ids)
        self.assertNotIn("retry-1", ids)
        ancient = self.report(start_date="2019-01-01", end_date="2019-12-31")
        self.assertEqual(ancient["summary"]["invocations"], 0)

    def test_coverage_five_states_and_failed_rows_keep_returned_usage(self) -> None:
        coverage = self.report()["coverage"]
        self.assertEqual(coverage["tokens_only"], 1)
        self.assertEqual(coverage["unreported"], 1)
        self.assertEqual(coverage["failed"], 1)
        self.assertEqual(coverage["complete"], 7)
        self.assertEqual(sum(coverage.values()), 10)
        failed = self.report(outcome="failure")["invocations"]["rows"][0]
        self.assertEqual(failed["coverage"], "failed")
        self.assertEqual(failed["inputTokens"], 8_000)
        self.assertIsNone(failed["outputTokens"])
        self.assertFalse(failed["success"])

    def test_missing_totals_stay_null_and_a_genuine_zero_stays_zero(self) -> None:
        summary = self.report()["summary"]
        self.assertIsNone(summary["reasoningTokens"])
        self.assertEqual(summary["reasoningReported"], 0)
        self.repository.record_token_usage_batch(self.retry, "swarm/alpha", 42, [
            _event("zero-reason", reasoning_tokens=0, started_at="2026-03-02T09:00:00+00:00"),
        ])
        again = self.report()["summary"]
        self.assertEqual(again["reasoningTokens"], 0)
        self.assertEqual(again["reasoningReported"], 1)

    def test_unreported_and_unpriced_rows_do_not_count_as_priced(self) -> None:
        summary = self.report()["summary"]
        self.assertEqual(summary["pricedInvocations"], 7)
        self.assertLess(summary["pricedInvocations"], summary["invocations"])
        review = self.report(agent_type="review")
        self.assertIsNone(review["summary"]["estimatedCost"])
        self.assertEqual(review["summary"]["pricedInvocations"], 0)
        self.assertEqual(review["summary"]["totalTokens"], 2_400)
        silent = self.report(agent_type="summarizer")
        self.assertIsNone(silent["summary"]["totalTokens"])
        self.assertIsNone(silent["summary"]["estimatedCost"])

    def test_an_unreported_row_stored_with_a_zero_cost_must_not_look_priced(self) -> None:
        # If persistence ever writes estimated_cost=0.0 for a call that
        # reported no tokens, the report still has to treat that as
        # unreported / unpriced. $0.00 on an unreported row is a lie.
        self.repository.record_token_usage_batch(self.retry, "swarm/alpha", 42, [
            _event(
                "ghost-cost",
                agent_type="other",
                prompt_type="summary",
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                estimated_cost=0.0,
                pricing_status="priced",
                started_at="2026-03-03T09:00:00+00:00",
            ),
        ])
        payload = self.report(agent_type="other")
        self.assertEqual(payload["coverage"]["unreported"], 1)
        self.assertIsNone(
            payload["summary"]["estimatedCost"],
            "unreported invocations must not contribute a $0 estimated cost",
        )
        self.assertEqual(payload["summary"]["pricedInvocations"], 0)

    def test_usage_report_survives_executions_that_have_no_routing_decision(self) -> None:
        # History is independent of Dynamic Model Routing. An execution with
        # an empty routing_decision (the default when the router did not run)
        # must still be queryable; a JSON extract that raises aborts the
        # whole Usage & cost tab for the database.
        unrouted = self.repository.create(
            ExecutionStart(
                repository="swarm/alpha",
                issue_number=55,
                issue_url="https://github.com/swarm/alpha/issues/55",
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
        self.repository.record_token_usage_batch(unrouted, "swarm/alpha", 55, [
            _event("plain-1", started_at="2026-02-11T09:05:00+00:00"),
        ])
        with self.repository.connect() as database:
            stored = database.execute(
                "SELECT routing_decision FROM ai_executions WHERE execution_id = ?",
                (unrouted,),
            ).fetchone()[0]
        self.assertIn(stored, ("", None, "{}"))
        try:
            payload = self.repository.usage_report(["swarm/alpha"], issue_number=55)
        except Exception as error:  # noqa: BLE001
            self.fail(
                "usage_report must not raise for executions with no routing_decision "
                f"(dynamic routing off is the default); got {type(error).__name__}: {error}"
            )
        self.assertEqual(payload["summary"]["invocations"], 1)
        self.assertEqual(payload["invocations"]["rows"][0]["id"], "plain-1")

    def test_imported_github_issue_does_not_backfill_usage_from_the_comment_body(self) -> None:
        self.assertNotIn(self.imported, self.repository.usage_summaries_for_executions([self.imported]))
        payload = self.report(["swarm/alpha"])
        self.assertEqual(payload["executionsWithoutUsage"], 1)
        with self.repository.connect() as database:
            count = database.execute(
                "SELECT COUNT(*) FROM ai_token_usage WHERE execution_id = ?",
                (self.imported,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_empty_states_distinguish_no_activity_no_usage_and_no_match(self) -> None:
        with TemporaryDirectory() as tmp:
            empty = ExecutionHistoryRepository(Path(tmp) / "empty.sqlite3").usage_report()
        self.assertFalse(empty["hasAnyUsage"])
        self.assertFalse(empty["hasAnyActivity"])
        self.assertIsNone(empty["summary"]["estimatedCost"])
        only_import = self.report(["swarm/alpha"], search="this matches nothing")
        self.assertEqual(only_import["summary"]["invocations"], 0)
        self.assertTrue(only_import["hasAnyUsage"])
        legacy = self.report(issue_number=100)
        self.assertEqual(legacy["summary"]["invocations"], 0)
        self.assertTrue(legacy["hasAnyActivity"])


class PaginationSortDrillAndReadonlyTests(UsageQueryFixture):
    def test_pages_are_capped_and_a_large_history_is_not_returned_whole(self) -> None:
        self.repository.record_token_usage_batch(self.retry, "swarm/alpha", 42, [
            _event(f"bulk-{index}", started_at="2026-05-01T00:00:00+00:00")
            for index in range(200)
        ])
        payload = self.report(limit=10_000)
        self.assertLessEqual(len(payload["invocations"]["rows"]), usage_report.USAGE_PAGE_SIZE)
        self.assertLessEqual(payload["invocations"]["limit"], usage_report.USAGE_PAGE_SIZE)
        self.assertEqual(payload["invocations"]["total"], 210)
        groups = self.report(group_by="model", limit=2, group_offset=500)["groups"]
        self.assertLessEqual(len(groups["rows"]), 2)
        self.assertLess(groups["offset"], groups["total"])

    def test_unavailable_costs_sort_last_in_both_directions(self) -> None:
        descending = [row["group"] for row in
                      self.report(group_by="model", sort="cost", direction="desc")["groups"]["rows"]]
        ascending = [row["group"] for row in
                     self.report(group_by="model", sort="cost", direction="asc")["groups"]["rows"]]
        self.assertEqual(descending[-1], "grok-experimental")
        self.assertEqual(ascending[-1], "grok-experimental")

    def test_selecting_a_group_row_drills_into_its_invocations(self) -> None:
        payload = self.report(group_by="agent", group_value="adversarial_cybersecurity")
        self.assertEqual(payload["invocations"]["total"], 1)
        self.assertEqual(payload["invocations"]["rows"][0]["agentType"], "adversarial_cybersecurity")
        self.assertGreater(len(payload["groups"]["rows"]), 1)

    def test_building_a_report_never_writes_or_recosts(self) -> None:
        with self.repository.connect() as database:
            before = database.execute(
                "SELECT id, estimated_cost, pricing_rate_id, total_tokens FROM ai_token_usage ORDER BY id"
            ).fetchall()
        with mock.patch("token_usage.estimate_cost", side_effect=AssertionError("recost")):
            with mock.patch("model_pricing.estimate_invocation_cost", side_effect=AssertionError("recost")):
                for group_by in usage_report.GROUP_BY_KEYS:
                    self.report(group_by=group_by)
        with self.repository.connect() as database:
            after = database.execute(
                "SELECT id, estimated_cost, pricing_rate_id, total_tokens FROM ai_token_usage ORDER BY id"
            ).fetchall()
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])

    def test_malformed_selectors_never_reach_sql(self) -> None:
        self.assertEqual(usage_report.normalize_group_by("1; DROP TABLE ai_token_usage"), "issue")
        self.assertEqual(usage_report.normalize_sort("estimated_cost + 1"), "cost")
        payload = self.report(search="%' OR '1'='1", group_by="'; DROP TABLE")
        self.assertEqual(payload["groupBy"], "issue")
        self.assertEqual(payload["summary"]["invocations"], 0)

    def test_cli_usage_mode_applies_grade_and_does_not_need_the_whole_table(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = history_main([
                "--db", str(self.database_path), "--usage",
                "--grade", "B", "--group-by", "agent",
            ])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["summary"]["invocations"], 9)
        self.assertLessEqual(len(payload["invocations"]["rows"]), usage_report.USAGE_PAGE_SIZE)
        self.assertEqual(payload["filters"]["grade"], "B")

    def test_execution_history_cards_get_a_headline_or_unavailable_not_zeroes(self) -> None:
        rows = attach_token_usage(
            self.repository,
            [
                {"execution_id": self.alpha},
                {"execution_id": self.imported},
            ],
        )
        self.assertEqual(rows[0]["token_usage_summary"]["invocations"], 4)
        self.assertIsNotNone(rows[0]["token_usage_summary"]["estimatedCost"])
        self.assertIsNone(rows[1]["token_usage_summary"])
        self.assertEqual(rows[1]["token_usage"], [])


class WorkerTelemetryToReportTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def _enable_history(self) -> Path:
        history_db = self.state / "issue295-history.sqlite3"
        self.worker.config = fixtures.dataclasses.replace(
            self.worker.config,
            ai_execution_history_enabled=True,
            execution_history_db=history_db,
        )
        self.worker.history = fixtures.ExecutionHistoryService(True, history_db)
        return history_db

    def _give_execution_a_grade(self, history_db: Path, grade: str = "B") -> None:
        # Isolate persistence/pricing assertions from the separate finding
        # that json_extract() on an empty routing_decision aborts the report.
        repository = ExecutionHistoryRepository(history_db)
        with repository.connect() as database:
            database.execute(
                "UPDATE ai_executions SET routing_decision = ? WHERE routing_decision IS NULL OR routing_decision = ''",
                (json.dumps({"prompt_grade": grade}),),
            )

    def test_a_catalogued_call_persists_provenance_the_report_can_read(self) -> None:
        history_db = self._enable_history()
        self.worker.issue = fixtures.IssueContext(295, "Title", "body", [], "https://example.invalid/295")
        self.worker.choice = fixtures.ProviderChoice("Claude", "claude-sonnet-5", "high", "session-295")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()

        def fake_run_claude(prompt: str, env: dict[str, str], activity: str = "") -> int:
            self.worker._last_ai_raw_output = json.dumps(
                {
                    "type": "result",
                    "result": "done",
                    "usage": {
                        "input_tokens": 100_000,
                        "output_tokens": 10_000,
                        "cache_read_input_tokens": 20_000,
                        "cache_creation_input_tokens": 5_000,
                    },
                }
            )
            self.worker.ai_output_file.write_text("done\n", encoding="utf-8")
            return 0

        with mock.patch.object(self.worker, "_run_claude", side_effect=fake_run_claude):
            self.assertEqual(self.worker.run_ai("do the work"), 0)
        self.worker.flush_token_usage_to_history()
        self._give_execution_a_grade(history_db)
        payload = ExecutionHistoryRepository(history_db).usage_report()
        row = payload["invocations"]["rows"][0]
        self.assertEqual(row["coverage"], "complete")
        self.assertEqual(row["cacheReadTokens"], 20_000)
        self.assertEqual(row["cacheWriteTokens"], 5_000)
        self.assertTrue(row["pricingRateId"])
        self.assertIsNotNone(row["estimatedCost"])
        self.assertGreater(row["estimatedCost"], 0)

    def test_empty_provider_usage_must_not_be_stored_as_a_priced_zero(self) -> None:
        history_db = self._enable_history()
        self.worker.issue = fixtures.IssueContext(296, "Title", "body", [], "https://example.invalid/296")
        self.worker.choice = fixtures.ProviderChoice("Claude", "claude-sonnet-5", "high", "session-empty")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()

        def fake_run_claude(prompt: str, env: dict[str, str], activity: str = "") -> int:
            self.worker._last_ai_raw_output = json.dumps(
                {"type": "result", "result": "done", "usage": {}}
            )
            self.worker.ai_output_file.write_text("done\n", encoding="utf-8")
            return 0

        with mock.patch.object(self.worker, "_run_claude", side_effect=fake_run_claude):
            self.assertEqual(self.worker.run_ai("do the work"), 0)
        event = self.worker.read_state()["token_usage_events"][0]
        self.assertIsNone(event["input_tokens"])
        self.assertIsNone(
            event["estimated_cost"],
            f"empty usage was stored as estimated_cost={event['estimated_cost']!r} "
            f"status={event.get('pricing_status')!r}; unreported must not look free",
        )
        self.assertNotEqual(event.get("pricing_status"), "priced")
        self.worker.flush_token_usage_to_history()
        self._give_execution_a_grade(history_db)
        payload = ExecutionHistoryRepository(history_db).usage_report()
        self.assertEqual(payload["coverage"]["unreported"], 1)
        self.assertEqual(payload["summary"]["pricedInvocations"], 0)
        self.assertIsNone(payload["summary"]["estimatedCost"])

    def test_pricing_exceptions_cannot_fail_the_ai_call_or_drop_tokens(self) -> None:
        self._enable_history()
        self.worker.issue = fixtures.IssueContext(297, "Title", "body", [], "https://example.invalid/297")
        self.worker.choice = fixtures.ProviderChoice("Claude", "claude-sonnet-5", "high", "session-boom")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)

        def fake_run_claude(prompt: str, env: dict[str, str], activity: str = "") -> int:
            self.worker._last_ai_raw_output = json.dumps(
                {"type": "result", "result": "ok", "usage": {"input_tokens": 111, "output_tokens": 22}}
            )
            self.worker.ai_output_file.write_text("done\n", encoding="utf-8")
            return 0

        with mock.patch.object(self.worker, "_run_claude", side_effect=fake_run_claude):
            with mock.patch(
                "swarm_issue_worker.estimate_cost_detailed",
                side_effect=RuntimeError("pricing catalog exploded"),
            ):
                self.assertEqual(self.worker.run_ai("do the work"), 0)
        # Telemetry may be skipped when the recorder itself raises, but the
        # AI call must still succeed. If an event was stored, tokens remain.
        events = self.worker.read_state().get("token_usage_events") or []
        for event in events:
            self.assertEqual(event.get("input_tokens"), 111)

    def test_unknown_model_keeps_tokens_and_does_not_fail_the_call(self) -> None:
        history_db = self._enable_history()
        self.worker.issue = fixtures.IssueContext(298, "Title", "body", [], "https://example.invalid/298")
        self.worker.choice = fixtures.ProviderChoice("Claude", "claude-unreleased-9", "high", "session-u")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()

        def fake_run_claude(prompt: str, env: dict[str, str], activity: str = "") -> int:
            self.worker._last_ai_raw_output = json.dumps(
                {"type": "result", "result": "done", "usage": {"input_tokens": 50, "output_tokens": 5}}
            )
            self.worker.ai_output_file.write_text("done\n", encoding="utf-8")
            return 0

        with mock.patch.object(self.worker, "_run_claude", side_effect=fake_run_claude):
            self.assertEqual(self.worker.run_ai("do the work"), 0)
        event = self.worker.read_state()["token_usage_events"][0]
        self.assertEqual(event["input_tokens"], 50)
        self.assertIsNone(event["estimated_cost"])
        self.assertEqual(event["pricing_status"], "unknown_model")
        self.worker.flush_token_usage_to_history()
        self._give_execution_a_grade(history_db)
        payload = ExecutionHistoryRepository(history_db).usage_report()
        self.assertEqual(payload["coverage"]["tokens_only"], 1)
        self.assertIsNone(payload["summary"]["estimatedCost"])
        self.assertEqual(payload["summary"]["totalTokens"], 55)

    def test_mixed_agent_flush_is_queryable_by_agent_type(self) -> None:
        history_db = self._enable_history()
        worker = self.worker
        worker.issue = fixtures.IssueContext(299, "Title", "body", [], "https://example.invalid/299")
        worker.choice = fixtures.ProviderChoice("Claude", "claude-sonnet-5", "high", "session-mix")
        worker.save_new_state(worker.issue, worker.choice, self.base_sha)
        worker.start_execution_history()

        def run_with(tokens: int):
            def _run(prompt: str, env: dict, activity: str = "") -> int:
                worker._last_ai_raw_output = json.dumps(
                    {"type": "result", "result": "done", "usage": {"input_tokens": tokens, "output_tokens": 1}}
                )
                worker.ai_output_file.write_text("done\n", encoding="utf-8")
                return 0
            return _run

        host = worker.config.spec("claude")

        def fake_router(*, usage_sink=None, **_kwargs) -> str:
            if usage_sink is not None:
                from token_usage import NormalizedUsage
                usage_sink.append(NormalizedUsage(input_tokens=10, output_tokens=1, total_tokens=11))
            return json.dumps({"prompt_grade": "B", "selected_provider": "claude"})

        with mock.patch("swarm_issue_worker.run_provider_router", side_effect=fake_router):
            worker.run_router(host, "grade", [])
        with mock.patch.object(worker, "_run_claude", side_effect=run_with(20)):
            worker.run_ai("implement", activity="working")
        worker.update_state(**{fixtures.UAT_STAGE.key: {"phase": "test", "round": 0, "active": True}})
        with mock.patch.object(worker, "_run_claude", side_effect=run_with(30)):
            worker.run_ai("uat scan", activity="testing")
        worker.update_state(
            **{
                fixtures.UAT_STAGE.key: {"phase": "test", "round": 0, "active": False},
                fixtures.SECURITY_STAGE.key: {"phase": "test", "round": 0, "active": True},
            }
        )
        with mock.patch.object(worker, "_run_claude", side_effect=run_with(40)):
            worker.run_ai("cyber scan", activity="testing")
        worker.flush_token_usage_to_history()
        self._give_execution_a_grade(history_db)
        repo = ExecutionHistoryRepository(history_db)
        self.assertEqual(repo.usage_report(agent_type="router")["summary"]["invocations"], 1)
        self.assertEqual(repo.usage_report(agent_type="primary")["summary"]["invocations"], 1)
        self.assertEqual(repo.usage_report(agent_type="adversarial_uat")["summary"]["invocations"], 1)
        self.assertEqual(
            repo.usage_report(agent_type="adversarial_cybersecurity")["summary"]["invocations"], 1
        )
        agents = {row["agentType"] for row in repo.usage_report()["invocations"]["rows"]}
        self.assertGreaterEqual(
            agents,
            {"router", "primary", "adversarial_uat", "adversarial_cybersecurity"},
        )


if __name__ == "__main__":
    unittest.main()
