"""Issue #299: baseline vs Jev vs combined scores, outcomes, and Feedback.

The original Swarm score must remain queryable. Disabled and fallback rows are
labelled rather than stored as a zero Jev score. Failures are not treated as
dollar savings. GitHub comments stay concise and secret-free.
"""

from __future__ import annotations

import json
import unittest
from io import StringIO
from unittest import mock

from harness import SECRET_TOKEN, JevWorkerFixture
from ai_execution_history import ExecutionHistoryRepository, main as history_main
from decision_engine import DecisionType, format_jev_markdown
from test_dynamic_router import candidates, resolve, sample_payload


class PersistenceContractTests(JevWorkerFixture, unittest.TestCase):
    def test_baseline_is_not_overwritten_when_equal_or_different(self) -> None:
        db = self.enable_history()
        repo = ExecutionHistoryRepository(db)
        repo.record_jev_score_comparison(
            {
                "comparison_id": "cmp-equal",
                "execution_id": "exec-equal",
                "repository": "acme/app",
                "issue_number": 1,
                "jev_status": "enabled",
                "baseline": {"normalized_score": 0.70, "native_score": 1.1, "model": "claude-haiku-4-5"},
                "jev": {"normalized_score": 0.70, "confidence": 0.91, "scores": {"complexity": 0.4}},
                "modified": {"normalized_score": 0.70, "model": "claude-haiku-4-5"},
                "delta": {"absolute": 0.0, "percent": 0.0, "routing_changed": False},
            }
        )
        repo.record_jev_score_comparison(
            {
                "comparison_id": "cmp-changed",
                "execution_id": "exec-changed",
                "repository": "acme/app",
                "issue_number": 2,
                "jev_status": "enabled",
                "baseline": {"normalized_score": 0.70, "native_score": 1.1, "model": "claude-sonnet-5"},
                "jev": {"normalized_score": 0.91, "confidence": 0.91, "scores": {"complexity": 0.2}},
                "modified": {"normalized_score": 0.74, "model": "claude-haiku-4-5"},
                "delta": {"absolute": 0.04, "percent": 5.7, "routing_changed": True},
            }
        )
        page = repo.jev_feedback(["acme/app"])
        by_id = {row["executionId"]: row for row in page["records"]}
        equal = by_id["exec-equal"]
        self.assertEqual(equal["baselineScore"], 0.70)
        self.assertEqual(equal["jevScore"], 0.70)
        self.assertEqual(equal["modifiedScore"], 0.70)
        changed = by_id["exec-changed"]
        self.assertEqual(changed["baselineScore"], 0.70)
        self.assertEqual(changed["jevScore"], 0.91)
        self.assertEqual(changed["modifiedScore"], 0.74)
        self.assertTrue(changed["routingChanged"])

    def test_disabled_and_fallback_rows_are_baseline_only_not_a_zero_jev_score(self) -> None:
        db = self.enable_history()
        repo = ExecutionHistoryRepository(db)
        for status, execution_id in (("disabled", "exec-off"), ("timeout", "exec-timeout"), ("fallback", "exec-fb")):
            repo.record_jev_score_comparison(
                {
                    "comparison_id": execution_id,
                    "execution_id": execution_id,
                    "repository": "acme/app",
                    "issue_number": 3,
                    "jev_status": status,
                    "baseline": {"normalized_score": 0.80, "model": "grok-4.6"},
                    "jev": None,
                    "modified": {"normalized_score": 0.80, "model": "grok-4.6"},
                    "delta": {"absolute": 0.0, "percent": 0.0, "routing_changed": False},
                }
            )
        page = repo.jev_feedback(["acme/app"])
        self.assertEqual(page["total"], 3)
        for row in page["records"]:
            self.assertIsNone(row["jevScore"])
            self.assertFalse(row["jevPresent"])
            if row["jevStatus"] == "disabled":
                self.assertTrue(row["baselineOnly"])
            else:
                self.assertTrue(row["fallback"])
            self.assertEqual(row["baselineScore"], 0.80)

    def test_failed_executions_are_not_counted_as_dollar_savings(self) -> None:
        db = self.enable_history()
        repo = ExecutionHistoryRepository(db)
        repo.record_jev_score_comparison(
            {
                "comparison_id": "win",
                "execution_id": "exec-win",
                "repository": "acme/app",
                "issue_number": 4,
                "jev_status": "enabled",
                "baseline": {"normalized_score": 0.5},
                "jev": {"normalized_score": 0.6, "confidence": 0.9},
                "modified": {"normalized_score": 0.55},
                "delta": {"absolute": 0.05, "percent": 10.0, "routing_changed": False},
                "estimated_dollar_savings": 1.25,
                "workflow_outcome": "completed",
            }
        )
        repo.record_jev_score_comparison(
            {
                "comparison_id": "fail",
                "execution_id": "exec-fail",
                "repository": "acme/app",
                "issue_number": 5,
                "jev_status": "enabled",
                "baseline": {"normalized_score": 0.5},
                "jev": {"normalized_score": 0.6, "confidence": 0.9},
                "modified": {"normalized_score": 0.55},
                "delta": {"absolute": 0.05, "percent": 10.0, "routing_changed": False},
                "estimated_dollar_savings": 9.99,
                "workflow_outcome": "failed",
            }
        )
        page = repo.jev_feedback(["acme/app"])
        self.assertEqual(page["summary"]["estimatedDollarSavings"], 1.25)
        self.assertNotEqual(page["summary"]["estimatedDollarSavings"], 1.25 + 9.99)

    def test_outcomes_stamp_onto_open_jev_rows(self) -> None:
        db = self.enable_history()
        repo = ExecutionHistoryRepository(db)
        repo.record_jev_decision(
            {
                "decision_id": "d1",
                "execution_id": "exec-out",
                "repository": "acme/app",
                "issue_number": 6,
                "decision_type": "TASK_CLASSIFICATION",
                "decision": "BUG",
                "confidence": 0.9,
                "source": "jev",
            }
        )
        repo.record_jev_score_comparison(
            {
                "comparison_id": "c1",
                "execution_id": "exec-out",
                "repository": "acme/app",
                "issue_number": 6,
                "jev_status": "enabled",
                "baseline": {"normalized_score": 0.4},
                "jev": {"normalized_score": 0.5, "confidence": 0.9},
                "modified": {"normalized_score": 0.45},
                "delta": {"absolute": 0.05, "percent": 12.5, "routing_changed": False},
            }
        )
        repo.finish_jev_outcomes("exec-out", "completed")
        page = repo.jev_feedback(["acme/app"])
        self.assertEqual(page["records"][0]["outcome"], "completed")

    def test_feedback_cli_and_query_omit_raw_prompts_and_secrets(self) -> None:
        db = self.enable_history()
        repo = ExecutionHistoryRepository(db)
        repo.record_jev_decision(
            {
                "decision_id": "secret-row",
                "execution_id": "exec-secret",
                "repository": "acme/app",
                "issue_number": 7,
                "decision_type": "TASK_CLASSIFICATION",
                "decision": "BUG",
                "confidence": 0.9,
                "source": "jev",
                "scores": {"complexity": 0.2},
                "reason_codes": ["BUG_LABEL"],
                "prompt": SECRET_TOKEN,
                "body": SECRET_TOKEN,
            }
        )
        repo.record_jev_score_comparison(
            {
                "comparison_id": "secret-cmp",
                "execution_id": "exec-secret",
                "repository": "acme/app",
                "issue_number": 7,
                "jev_status": "enabled",
                "baseline": {"normalized_score": 0.4, "inputs": {"prompt": SECRET_TOKEN}},
                "jev": {"normalized_score": 0.5, "confidence": 0.9, "scores": {"complexity": 0.2}},
                "modified": {"normalized_score": 0.45},
                "delta": {"absolute": 0.05, "percent": 12.5, "routing_changed": False},
            }
        )
        page = repo.jev_feedback(["acme/app"])
        blob = json.dumps(page)
        self.assertNotIn(SECRET_TOKEN, blob)
        for row in page["records"]:
            self.assertNotIn("prompt", row)
            self.assertNotIn("body", row)
        with repo.connect() as database:
            stored = " ".join(
                str(cell)
                for row in database.execute("SELECT * FROM jev_score_comparisons")
                for cell in row
            )
        self.assertNotIn(SECRET_TOKEN, stored)

        stdout = StringIO()
        with mock.patch("sys.stdout", stdout):
            code = history_main(
                ["--db", str(db), "--repository", "acme/app", "--jev-feedback"]
            )
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET_TOKEN, stdout.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertGreaterEqual(payload["total"], 1)

    def test_github_report_is_concise_and_omits_cli_transcripts(self) -> None:
        markdown = format_jev_markdown(
            [
                self.decision(
                    decision_type=DecisionType.TASK_CLASSIFICATION.value,
                    decision="ARCHITECTURE_REFACTOR",
                    confidence=0.94,
                    scores={
                        "complexity": 0.81,
                        "crossRepoProbability": 0.88,
                        "securityRisk": 0.34,
                    },
                    metadata={"ragScope": "ORGANIZATION", "raw": "FULL CLI STDOUT"},
                    latency_ms=400,
                    estimated_cost=0.0002,
                )
            ]
        )
        self.assertIn("### Jev Decision Engine", markdown)
        self.assertIn("Architecture Refactor", markdown)
        self.assertIn("Complexity: 81%", markdown)
        self.assertNotIn("FULL CLI STDOUT", markdown)
        self.assertNotIn(SECRET_TOKEN, markdown)
        self.assertLess(len(markdown.splitlines()), 40)

    def test_attach_jev_routing_persists_a_comparison_when_history_is_on(self) -> None:
        db = self.enable_history()
        self.bind_issue()
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()
        decision = resolve(sample_payload(), "claude")
        self.worker.attach_jev_routing(decision, candidates("claude"))
        page = ExecutionHistoryRepository(db).jev_feedback()
        self.assertGreaterEqual(page["total"], 1)
        row = page["records"][0]
        self.assertEqual(row["jevStatus"], "disabled")
        self.assertTrue(row["baselineOnly"])
        self.assertIsNone(row["jevScore"])
