"""Issue #295 — activity-time filtering, pre-#295 migration, coverage
edges, History cross-links, and stored-cost stability.

Independent of the implementer's fixtures. Spec-derived contracts:

* Date filters and time buckets use the invocation's start/completion
  timestamp. ``created_at`` is only a last-resort persistence time.
* Queries are read-only and never recost stored rows when the catalog moves.
* Imported/pre-#280 rows stay usage-unavailable; GitHub comments are not
  scraped. A v6 (issue #280) database must remain readable after migration 7.
* Failed invocations are Failed even when the provider returned no usage.
* Execution identifiers link Usage & cost back to Execution History.
* Monetary values on the GitHub usage table are labelled Estimated cost;
  missing cells stay unavailable, never zero.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import model_pricing  # noqa: E402
from ai_execution_history import (  # noqa: E402
    SCHEMA_VERSION,
    ExecutionHistoryRepository,
    ExecutionStart,
)
from token_usage import render_ai_usage_markdown  # noqa: E402


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
        started_at="2026-06-10T09:00:00+00:00",
        completed_at="2026-06-10T09:01:00+00:00",
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


class ActivityTimeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "history.sqlite3"
        self.repository = ExecutionHistoryRepository(self.database_path)
        self.execution = self.repository.create(
            ExecutionStart(
                repository="acme/app",
                issue_number=22,
                issue_url="https://github.com/acme/app/issues/22",
                issue_title="Activity time",
                issue_body="",
                provider="Claude",
                model="claude-sonnet-5",
                effort="high",
                branch_name="ai/claude/issue-22",
                application_version="0.1.0",
                routing_decision={"prompt_grade": "B"},
            ),
            "2026-06-10T08:00:00+00:00",
        )

    def report(self, **options):
        return self.repository.usage_report(["acme/app"], **options)


class ActivityTimestampFilterTests(ActivityTimeFixture):
    def test_date_filter_falls_back_to_completed_at_when_started_at_is_blank(self) -> None:
        self.repository.record_token_usage_batch(self.execution, "acme/app", 22, [
            _event(
                "completed-only",
                started_at="",
                completed_at="2026-04-15T12:00:00+00:00",
            ),
        ])
        with self.repository.connect() as database:
            database.execute(
                "UPDATE ai_token_usage SET created_at = ? WHERE id = ?",
                ("2019-01-01T00:00:00+00:00", "completed-only"),
            )
        april = self.report(start_date="2026-04-01", end_date="2026-04-30")
        self.assertEqual(april["summary"]["invocations"], 1)
        self.assertEqual(april["invocations"]["rows"][0]["id"], "completed-only")
        ancient = self.report(start_date="2019-01-01", end_date="2019-12-31")
        self.assertEqual(ancient["summary"]["invocations"], 0)

    def test_created_at_is_only_used_when_both_activity_timestamps_are_blank(self) -> None:
        self.repository.record_token_usage_batch(self.execution, "acme/app", 22, [
            _event("write-time", started_at="", completed_at=""),
        ])
        with self.repository.connect() as database:
            database.execute(
                "UPDATE ai_token_usage SET created_at = ? WHERE id = ?",
                ("2026-07-04T00:00:00+00:00", "write-time"),
            )
        july = self.report(start_date="2026-07-01", end_date="2026-07-31")
        self.assertEqual(july["summary"]["invocations"], 1)
        june = self.report(start_date="2026-06-01", end_date="2026-06-30")
        self.assertEqual(june["summary"]["invocations"], 0)

    def test_day_and_month_buckets_follow_activity_time_not_write_time(self) -> None:
        self.repository.record_token_usage_batch(self.execution, "acme/app", 22, [
            _event("march-call", started_at="2026-03-10T09:00:00+00:00"),
        ])
        with self.repository.connect() as database:
            database.execute(
                "UPDATE ai_token_usage SET created_at = ? WHERE id = ?",
                ("2026-12-25T00:00:00+00:00", "march-call"),
            )
        days = {row["group"]: row["invocations"] for row in self.report(group_by="day")["groups"]["rows"]}
        months = {row["group"]: row["invocations"] for row in self.report(group_by="month")["groups"]["rows"]}
        self.assertEqual(days.get("2026-03-10"), 1)
        self.assertNotIn("2026-12-25", days)
        self.assertEqual(months.get("2026-03"), 1)
        self.assertNotIn("2026-12", months)

    def test_an_inverted_date_range_matches_nothing(self) -> None:
        self.repository.record_token_usage_batch(self.execution, "acme/app", 22, [_event("in-range")])
        payload = self.report(start_date="2026-08-01", end_date="2026-07-01")
        self.assertEqual(payload["summary"]["invocations"], 0)
        self.assertTrue(payload["hasAnyUsage"])

    def test_a_single_day_range_is_inclusive(self) -> None:
        self.repository.record_token_usage_batch(self.execution, "acme/app", 22, [
            _event("that-day", started_at="2026-06-10T23:59:00+00:00"),
            _event("next-day", started_at="2026-06-11T00:00:00+00:00"),
        ])
        payload = self.report(start_date="2026-06-10", end_date="2026-06-10")
        ids = {row["id"] for row in payload["invocations"]["rows"]}
        self.assertEqual(ids, {"that-day"})


class CoverageEdgeTests(ActivityTimeFixture):
    def test_a_failed_call_with_no_usage_is_failed_not_unreported(self) -> None:
        self.repository.record_token_usage_batch(self.execution, "acme/app", 22, [
            _event(
                "fail-silent",
                success=False,
                error_type="provider_exit_nonzero",
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                estimated_cost=None,
                pricing_status="no_usage",
            ),
        ])
        payload = self.report()
        self.assertEqual(payload["coverage"]["failed"], 1)
        self.assertEqual(payload["coverage"]["unreported"], 0)
        row = payload["invocations"]["rows"][0]
        self.assertEqual(row["coverage"], "failed")
        self.assertFalse(row["success"])
        self.assertIsNone(row["inputTokens"])
        self.assertIsNone(row["totalTokens"])

    def test_missing_output_and_total_is_partial_even_when_input_exists(self) -> None:
        self.repository.record_token_usage_batch(self.execution, "acme/app", 22, [
            _event("partial", output_tokens=None, total_tokens=None, estimated_cost=None),
        ])
        payload = self.report()
        self.assertEqual(payload["coverage"]["partial"], 1)
        self.assertEqual(payload["invocations"]["rows"][0]["coverage"], "partial")
        self.assertEqual(payload["invocations"]["rows"][0]["inputTokens"], 2_000)
        self.assertIsNone(payload["invocations"]["rows"][0]["outputTokens"])


class StoredCostNeverRestatesTests(ActivityTimeFixture):
    def test_a_catalog_update_does_not_change_a_persisted_estimate(self) -> None:
        self.repository.record_token_usage_batch(self.execution, "acme/app", 22, [
            _event(
                "historical",
                estimated_cost=1.23,
                pricing_rate_id="claude/claude-sonnet-5@2026-01-01",
                pricing_version="2026-01-01",
            ),
        ])
        fake_price = model_pricing.ModelPrice(
            rate_id="claude/claude-sonnet-5@2099-01-01",
            provider="claude",
            model="claude-sonnet-5",
            input_per_million=99.0,
            output_per_million=99.0,
            effective_from="2026-01-01T00:00:00+00:00",
            source="https://example.test/pricing",
        )
        with mock.patch.object(model_pricing, "PRICING_CATALOG", (fake_price,)):
            with mock.patch.object(model_pricing, "PRICING_CATALOG_VERSION", "2099-01-01"):
                payload = self.report()
        row = payload["invocations"]["rows"][0]
        self.assertAlmostEqual(row["estimatedCost"], 1.23)
        self.assertEqual(row["pricingRateId"], "claude/claude-sonnet-5@2026-01-01")
        self.assertEqual(row["pricingVersion"], "2026-01-01")
        self.assertAlmostEqual(payload["summary"]["estimatedCost"], 1.23)


class ExecutionHistoryCrossLinkTests(ActivityTimeFixture):
    def test_execution_history_search_matches_the_execution_id(self) -> None:
        # Usage & cost's History control puts the execution id in the
        # Execution History search box. That search must actually find it.
        rows, total, _, _ = self.repository.page_for_repository(
            ["acme/app"], search=self.execution
        )
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["execution_id"], self.execution)

    def test_a_partial_execution_id_still_matches(self) -> None:
        fragment = self.execution[:8]
        rows, total, _, _ = self.repository.page_for_repository(
            ["acme/app"], search=fragment
        )
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["execution_id"], self.execution)

    def test_malformed_routing_decision_json_does_not_abort_the_report(self) -> None:
        self.repository.record_token_usage_batch(self.execution, "acme/app", 22, [_event("ok")])
        with self.repository.connect() as database:
            database.execute(
                "UPDATE ai_executions SET routing_decision = ? WHERE execution_id = ?",
                ("{not json", self.execution),
            )
        try:
            payload = self.report()
        except Exception as error:  # noqa: BLE001
            self.fail(
                "usage_report must not raise for malformed routing_decision JSON; "
                f"got {type(error).__name__}: {error}"
            )
        self.assertEqual(payload["summary"]["invocations"], 1)
        self.assertEqual(payload["invocations"]["rows"][0]["grade"], "")


class Pre295SchemaMigrationTests(unittest.TestCase):
    def test_a_v6_token_table_migrates_and_remains_queryable(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "v6.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO schema_migrations(version) VALUES (1),(2),(3),(4),(5),(6);
                CREATE TABLE ai_executions (
                    execution_id TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    issue_url TEXT NOT NULL DEFAULT '',
                    issue_title TEXT NOT NULL,
                    original_issue_body TEXT NOT NULL DEFAULT '',
                    effective_prompt TEXT NOT NULL DEFAULT '',
                    ai_provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    effort TEXT NOT NULL DEFAULT '',
                    reasoning_config TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT,
                    duration_seconds REAL,
                    requested_work_summary TEXT NOT NULL DEFAULT '',
                    changes_summary TEXT NOT NULL DEFAULT '',
                    files_changed TEXT NOT NULL DEFAULT '[]',
                    branch_name TEXT NOT NULL DEFAULT '',
                    commit_shas TEXT NOT NULL DEFAULT '[]',
                    pull_request_number INTEGER,
                    pull_request_url TEXT NOT NULL DEFAULT '',
                    operational_notes TEXT NOT NULL DEFAULT '[]',
                    warnings_errors TEXT NOT NULL DEFAULT '[]',
                    final_status TEXT NOT NULL DEFAULT '',
                    attempt_number INTEGER NOT NULL DEFAULT 1,
                    application_version TEXT NOT NULL DEFAULT '',
                    prompt_template_version TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    uploaded_at TEXT,
                    upload_status TEXT NOT NULL DEFAULT 'never_uploaded',
                    upload_error TEXT NOT NULL DEFAULT '',
                    routing_decision TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE ai_token_usage (
                    id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL DEFAULT '',
                    repository TEXT NOT NULL DEFAULT '',
                    issue_number INTEGER NOT NULL DEFAULT 0,
                    workflow_run_id TEXT NOT NULL DEFAULT '',
                    agent_run_id TEXT NOT NULL DEFAULT '',
                    prompt_id TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    reasoning_effort TEXT NOT NULL DEFAULT '',
                    agent_type TEXT NOT NULL DEFAULT '',
                    prompt_type TEXT NOT NULL DEFAULT '',
                    attempt_number INTEGER NOT NULL DEFAULT 1,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    cached_input_tokens INTEGER,
                    total_tokens INTEGER,
                    estimated_cost REAL,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    started_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT '',
                    duration_ms INTEGER,
                    success INTEGER NOT NULL DEFAULT 1,
                    error_type TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT ''
                );
                """
            )
            connection.execute(
                "INSERT INTO ai_executions("
                "execution_id, repository, issue_number, issue_url, issue_title, "
                "ai_provider, started_at, final_status, attempt_number, updated_at, "
                "routing_decision) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "exec-legacy",
                    "acme/legacy",
                    3,
                    "https://github.com/acme/legacy/issues/3",
                    "Old telemetry",
                    "Claude",
                    "2026-02-01T00:00:00+00:00",
                    "completed",
                    1,
                    "2026-02-01T00:00:00+00:00",
                    json.dumps({"prompt_grade": "C"}),
                ),
            )
            connection.execute(
                "INSERT INTO ai_token_usage("
                "id, execution_id, repository, issue_number, provider, model, "
                "reasoning_effort, agent_type, prompt_type, attempt_number, "
                "input_tokens, output_tokens, total_tokens, estimated_cost, "
                "started_at, completed_at, success, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-1",
                    "exec-legacy",
                    "acme/legacy",
                    3,
                    "Claude",
                    "claude-sonnet-5",
                    "high",
                    "primary",
                    "initial",
                    1,
                    500,
                    50,
                    550,
                    0.02,
                    "2026-02-01T10:00:00+00:00",
                    "2026-02-01T10:01:00+00:00",
                    1,
                    "2026-02-01T11:00:00+00:00",
                ),
            )
            connection.execute(
                "INSERT INTO ai_token_usage("
                "id, execution_id, repository, issue_number, provider, model, "
                "agent_type, prompt_type, input_tokens, output_tokens, total_tokens, "
                "estimated_cost, started_at, success, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-unpriced",
                    "exec-legacy",
                    "acme/legacy",
                    3,
                    "Grok",
                    "mystery-model",
                    "review",
                    "review",
                    100,
                    10,
                    110,
                    None,
                    "2026-02-01T12:00:00+00:00",
                    1,
                    "2026-02-01T12:05:00+00:00",
                ),
            )
            connection.commit()
            connection.close()

            repository = ExecutionHistoryRepository(path)
            with repository.connect() as database:
                versions = {row[0] for row in database.execute("SELECT version FROM schema_migrations")}
                columns = {row[1] for row in database.execute("PRAGMA table_info(ai_token_usage)")}
            self.assertIn(SCHEMA_VERSION, versions)
            for name in (
                "cache_read_tokens",
                "cache_write_tokens",
                "pricing_status",
                "pricing_rate_id",
                "input_rate_per_million",
            ):
                self.assertIn(name, columns)

            payload = repository.usage_report(["acme/legacy"])
            self.assertEqual(payload["summary"]["invocations"], 2)
            self.assertEqual(payload["coverage"]["complete"], 1)
            self.assertEqual(payload["coverage"]["tokens_only"], 1)
            self.assertAlmostEqual(payload["summary"]["estimatedCost"], 0.02)
            self.assertEqual(payload["summary"]["pricedInvocations"], 1)
            self.assertEqual(payload["summary"]["totalTokens"], 660)
            unpriced = next(
                row for row in payload["invocations"]["rows"] if row["id"] == "legacy-unpriced"
            )
            self.assertIsNone(unpriced["estimatedCost"])
            self.assertEqual(unpriced["coverage"], "tokens_only")
            self.assertEqual(unpriced["cacheReadTokens"], None)


class GitHubUsageLabelTests(unittest.TestCase):
    def test_the_github_table_labels_money_estimated_cost_and_keeps_gaps(self) -> None:
        markdown = render_ai_usage_markdown(
            [
                {
                    "id": "g1",
                    "sequence": 1,
                    "agent_type": "primary",
                    "prompt_type": "initial",
                    "provider": "Claude",
                    "model": "claude-sonnet-5",
                    "reasoning_effort": "high",
                    "attempt_number": 1,
                    "input_tokens": None,
                    "output_tokens": 0,
                    "reasoning_tokens": None,
                    "cached_input_tokens": None,
                    "total_tokens": None,
                    "estimated_cost": None,
                    "currency": "USD",
                    "started_at": "t",
                    "completed_at": "t",
                    "duration_ms": 1,
                    "success": True,
                    "error_type": "",
                }
            ]
        )
        self.assertIn("| Estimated cost |", markdown)
        self.assertNotRegex(markdown.splitlines()[2], r"\| Cost \|")
        # Row cells: missing stays em-dash; a genuine zero output stays 0.
        data_row = [line for line in markdown.splitlines() if line.startswith("| 1 |")][0]
        self.assertIn("—", data_row)
        self.assertIn("| 0 |", data_row)
        self.assertIn("Estimated Cost:", markdown)


if __name__ == "__main__":
    unittest.main()
