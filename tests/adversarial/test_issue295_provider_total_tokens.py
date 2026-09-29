"""Issue #295 — provider-reported total_tokens is the authoritative total.

Derived from the issue's Token semantics section before inspecting the
normalizers or the report queries:

* Treat provider-reported ``total_tokens`` as authoritative when supplied.
* Never add cached or reasoning tokens to total tokens when they are already
  included in provider totals.
* Preserve the provider-specific distinction between cached tokens included
  in input (OpenAI-shaped) and cache counters reported in addition to input
  (Anthropic).
* Cached and reasoning tokens are never double-counted.
* Aggregate nullable fields without implying that unreported values were zero.

A dashboard that sums input + cached + output (or that ignores a provider
total in favour of that sum) reports a different number than the provider
did. That is the failure these tests pin down.
"""

from __future__ import annotations

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
from ai_execution_history import (  # noqa: E402
    ExecutionHistoryRepository,
    ExecutionStart,
)
from token_usage import (  # noqa: E402
    normalize_claude_usage,
    normalize_codex_usage,
    normalize_grok_usage,
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
        output_tokens=200,
        reasoning_tokens=None,
        cached_input_tokens=400,
        cache_read_tokens=400,
        cache_write_tokens=None,
        total_tokens=1_200,
        estimated_cost=0.01,
        currency="USD",
        started_at="2026-06-01T12:00:00+00:00",
        completed_at="2026-06-01T12:00:01+00:00",
        duration_ms=1_000,
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


class ClaudeProviderTotalIsAuthoritativeTests(unittest.TestCase):
    def test_explicit_total_tokens_is_kept_rather_than_recomputed(self) -> None:
        # Anthropic's usage object can carry total_tokens alongside the
        # additive cache counters. The issue requires that supplied total to
        # win: recomputing input + cache + output (180 here) would invent a
        # different figure than the provider reported (999).
        raw = json.dumps(
            {
                "type": "result",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 10,
                    "total_tokens": 999,
                },
            }
        )
        usage = normalize_claude_usage(raw)
        self.assertIsNotNone(usage)
        self.assertEqual(
            usage.total_tokens,
            999,
            "provider-reported total_tokens must be authoritative when supplied; "
            f"got {usage.total_tokens} (looks like input+cache+output was used instead)",
        )
        self.assertEqual(usage.input_tokens, 100)
        self.assertEqual(usage.cache_read_tokens, 50)
        self.assertEqual(usage.cache_write_tokens, 10)
        self.assertEqual(usage.cached_input_tokens, 60)

    def test_a_total_only_usage_object_is_not_dropped(self) -> None:
        # A provider that returned only the authoritative total still reported
        # usage. Dropping that figure (and classifying the call as unreported)
        # is how "missing" and "we had a total" collapse into each other.
        raw = json.dumps({"type": "result", "usage": {"total_tokens": 5_000}})
        usage = normalize_claude_usage(raw)
        self.assertIsNotNone(usage)
        self.assertEqual(usage.total_tokens, 5_000)
        self.assertIsNone(usage.input_tokens)
        self.assertIsNone(usage.output_tokens)

    def test_absent_total_tokens_still_uses_anthropic_additive_semantics(self) -> None:
        # When the provider does *not* supply total_tokens, Anthropic's cache
        # counters are additional to input, so the computed total is
        # input + cache + output. That path must survive; it is not a guess,
        # it is the provider's documented wire semantics.
        raw = json.dumps(
            {
                "type": "result",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 10,
                },
            }
        )
        usage = normalize_claude_usage(raw)
        self.assertIsNotNone(usage)
        self.assertEqual(usage.total_tokens, 180)
        self.assertFalse(usage.cached_tokens_included_in_input)


class OpenAIShapedProviderTotalIsAuthoritativeTests(unittest.TestCase):
    def test_codex_total_tokens_is_not_inflated_by_cached_or_reasoning(self) -> None:
        events = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "thread.started", "thread_id": "t-1"},
                {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "total_tokens": 999,
                            "input_tokens_details": {"cached_tokens": 40},
                            "output_tokens_details": {"reasoning_tokens": 8},
                        }
                    },
                },
            )
        )
        usage = normalize_codex_usage(events)
        self.assertIsNotNone(usage)
        self.assertEqual(usage.total_tokens, 999)
        self.assertEqual(usage.cached_input_tokens, 40)
        self.assertEqual(usage.reasoning_tokens, 8)
        self.assertTrue(usage.cached_tokens_included_in_input)
        self.assertNotEqual(
            usage.total_tokens,
            (usage.input_tokens or 0)
            + (usage.cached_input_tokens or 0)
            + (usage.reasoning_tokens or 0)
            + (usage.output_tokens or 0),
            "cached/reasoning must not be added on top of a provider total",
        )

    def test_grok_total_tokens_is_not_inflated_by_cached_or_reasoning(self) -> None:
        raw = json.dumps(
            {
                "text": "done",
                "usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 50,
                    "total_tokens": 777,
                    "prompt_tokens_details": {"cached_tokens": 80},
                    "completion_tokens_details": {"reasoning_tokens": 12},
                },
            }
        )
        usage = normalize_grok_usage(raw)
        self.assertIsNotNone(usage)
        self.assertEqual(usage.total_tokens, 777)
        self.assertEqual(usage.cached_input_tokens, 80)
        self.assertEqual(usage.reasoning_tokens, 12)
        self.assertNotEqual(
            usage.total_tokens,
            200 + 80 + 12 + 50,
        )


class ReportDoesNotRecomputeTotalsTests(unittest.TestCase):
    def test_aggregate_total_is_the_stored_provider_total(self) -> None:
        # Parts (1000 + 400 + 200 = 1600, plus 50 reasoning) would overstate
        # the provider's own total of 1200. The report must sum the stored
        # total_tokens column, never reconstruct it.
        with TemporaryDirectory() as tmp:
            repository = ExecutionHistoryRepository(Path(tmp) / "h.sqlite3")
            execution = repository.create(
                ExecutionStart(
                    repository="acme/app",
                    issue_number=8,
                    issue_url="https://github.com/acme/app/issues/8",
                    issue_title="Totals",
                    issue_body="",
                    provider="Claude",
                    model="claude-sonnet-5",
                    effort="high",
                    branch_name="ai/claude/issue-8",
                    application_version="0.1.0",
                    routing_decision={"prompt_grade": "B"},
                ),
                "2026-06-01T11:00:00+00:00",
            )
            repository.record_token_usage_batch(execution, "acme/app", 8, [_event("kept-total")])
            payload = repository.usage_report()
        summary = payload["summary"]
        self.assertEqual(summary["totalTokens"], 1_200)
        self.assertEqual(summary["inputTokens"], 1_000)
        self.assertEqual(summary["cachedTokens"], 400)
        self.assertEqual(summary["outputTokens"], 200)
        self.assertNotEqual(
            summary["totalTokens"],
            summary["inputTokens"] + summary["cachedTokens"] + summary["outputTokens"],
        )
        self.assertEqual(payload["invocations"]["rows"][0]["totalTokens"], 1_200)

    def test_a_null_total_stays_null_even_when_other_fields_are_present(self) -> None:
        with TemporaryDirectory() as tmp:
            repository = ExecutionHistoryRepository(Path(tmp) / "h.sqlite3")
            execution = repository.create(
                ExecutionStart(
                    repository="acme/app",
                    issue_number=9,
                    issue_url="https://github.com/acme/app/issues/9",
                    issue_title="Partial",
                    issue_body="",
                    provider="Claude",
                    model="claude-sonnet-5",
                    effort="high",
                    branch_name="ai/claude/issue-9",
                    application_version="0.1.0",
                    routing_decision={"prompt_grade": "B"},
                ),
                "2026-06-01T11:00:00+00:00",
            )
            repository.record_token_usage_batch(
                execution,
                "acme/app",
                9,
                [_event("no-total", total_tokens=None, output_tokens=None, estimated_cost=None)],
            )
            payload = repository.usage_report()
        self.assertIsNone(payload["summary"]["totalTokens"])
        self.assertEqual(payload["coverage"]["partial"], 1)
        self.assertEqual(payload["invocations"]["rows"][0]["inputTokens"], 1_000)
        self.assertIsNone(payload["invocations"]["rows"][0]["totalTokens"])


class WorkerPersistsProviderTotalTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def test_run_ai_stores_the_provider_total_not_a_recomputed_sum(self) -> None:
        history_db = self.state / "issue295-totals.sqlite3"
        self.worker.config = fixtures.dataclasses.replace(
            self.worker.config,
            ai_execution_history_enabled=True,
            execution_history_db=history_db,
        )
        self.worker.history = fixtures.ExecutionHistoryService(True, history_db)
        self.worker.issue = fixtures.IssueContext(
            295, "Title", "body", [], "https://example.invalid/295"
        )
        self.worker.choice = fixtures.ProviderChoice(
            "Claude", "claude-sonnet-5", "high", "session-total"
        )
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()

        def fake_run_claude(prompt: str, env: dict[str, str], activity: str = "") -> int:
            self.worker._last_ai_raw_output = json.dumps(
                {
                    "type": "result",
                    "result": "done",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 50,
                        "cache_creation_input_tokens": 10,
                        "total_tokens": 999,
                    },
                }
            )
            self.worker.ai_output_file.write_text("done\n", encoding="utf-8")
            return 0

        with mock.patch.object(self.worker, "_run_claude", side_effect=fake_run_claude):
            self.assertEqual(self.worker.run_ai("do the work"), 0)
        event = self.worker.read_state()["token_usage_events"][0]
        self.assertEqual(
            event["total_tokens"],
            999,
            "the worker must persist the provider total; "
            f"got {event['total_tokens']!r} (recomputed additive sum would be 180)",
        )
        self.assertEqual(event["cache_read_tokens"], 50)
        self.assertEqual(event["cache_write_tokens"], 10)
        self.worker.flush_token_usage_to_history()
        with ExecutionHistoryRepository(history_db).connect() as database:
            database.execute(
                "UPDATE ai_executions SET routing_decision = ? "
                "WHERE routing_decision IS NULL OR routing_decision = ''",
                (json.dumps({"prompt_grade": "B"}),),
            )
        payload = ExecutionHistoryRepository(history_db).usage_report()
        self.assertEqual(payload["summary"]["totalTokens"], 999)
        self.assertEqual(payload["invocations"]["rows"][0]["totalTokens"], 999)


if __name__ == "__main__":
    unittest.main()
