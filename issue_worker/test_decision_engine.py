"""Decision engine contracts, confidence policy, fallback, and persistence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_execution_history import ExecutionHistoryRepository
from decision_engine import (
    CompletionVerdict,
    CompositeDecisionEngine,
    DecisionType,
    JevDecisionEngine,
    RuleBasedDecisionEngine,
    Source,
    WorkflowAction,
    confidence_band,
    engine_from_settings,
    format_jev_markdown,
    interpret_jev_response,
    may_act_on,
    swarm_policy_action,
)
from jev_cli import JevError, JevResponse, JevSettings, JevUsage


class ConfidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = JevSettings(confidence_automation=0.9, confidence_fallback=0.7, confidence_security=0.95)

    def test_bands(self) -> None:
        self.assertEqual(confidence_band(0.94, self.settings).value, "automation")
        self.assertEqual(confidence_band(0.8, self.settings).value, "policy")
        self.assertEqual(confidence_band(0.5, self.settings).value, "fallback")
        self.assertEqual(confidence_band(0.92, self.settings, security=True).value, "policy")


class RuleEngineTests(unittest.TestCase):
    def test_security_label_triage(self) -> None:
        result = RuleBasedDecisionEngine().evaluate(
            "ISSUE_TRIAGE", {"labels": ["security"], "title": "CVE in login"}
        )
        self.assertEqual(result.decision, "SECURITY")
        self.assertEqual(result.source, Source.RULES.value)

    def test_completion_never_ignores_failed_tests(self) -> None:
        result = RuleBasedDecisionEngine().evaluate(
            "COMPLETION", {"failed_tests": True, "blocking_security": False}
        )
        self.assertEqual(result.decision, CompletionVerdict.INCOMPLETE.value)
        self.assertEqual(result.confidence, 1.0)


class JevEngineTests(unittest.TestCase):
    def test_preflight_typed_output(self) -> None:
        cli = mock.Mock()
        cli.settings = JevSettings(enabled=True)
        cli.ask.return_value = JevResponse(
            answers={
                "task_type": {"value": "ARCHITECTURE_REFACTOR", "confidence": 0.94},
                "complexity": {"value": 0.81, "confidence": 0.9},
                "security_risk": {"value": 0.34},
                "cross_repo": {"value": 0.88},
                "rag_scope": {"value": "ORGANIZATION"},
                "uat_recommended": {"value": True},
                "cyber_recommended": {"value": False},
                "ambiguity": {"value": 0.22},
            },
            usage=JevUsage(input_tokens=20, estimated_cost=0.000001, model="jev-latest"),
            latency_ms=12.0,
            model="jev-latest",
        )
        result = JevDecisionEngine(cli).evaluate("TASK_CLASSIFICATION", {"title": "Split the media pipeline"})
        self.assertEqual(result.decision, "ARCHITECTURE_REFACTOR")
        self.assertGreaterEqual(result.confidence, 0.9)
        self.assertAlmostEqual(result.scores["complexity"], 0.81)
        self.assertEqual(result.metadata["ragScope"], "ORGANIZATION")
        self.assertTrue(result.metadata["uatRecommended"])
        self.assertFalse(result.metadata["cyberRecommended"])
        self.assertEqual(result.source, "jev")
        self.assertEqual(result.llm_calls_avoided, 1)

    def test_invalid_choice_falls_to_unknown(self) -> None:
        result = interpret_jev_response(
            "ISSUE_TRIAGE",
            {},
            JevResponse(answers={"task_type": {"value": "NOT_A_TYPE", "confidence": 0.99}}),
        )
        self.assertEqual(result.decision, "UNKNOWN")


class CompositeTests(unittest.TestCase):
    def test_disabled_skips_jev(self) -> None:
        jev = mock.Mock()
        engine = CompositeDecisionEngine(JevSettings(enabled=False), jev=jev)
        result = engine.evaluate("WORKFLOW", {"default_action": "CONTINUE"})
        self.assertEqual(result.source, "disabled")
        jev.evaluate.assert_not_called()
        self.assertEqual(result.latency_ms, 0)

    def test_malformed_falls_back_to_rules(self) -> None:
        jev = mock.Mock()
        jev.evaluate.side_effect = JevError("bad json", error_type="malformed")
        engine = CompositeDecisionEngine(JevSettings(enabled=True, fallback="rules"), jev=jev)
        result = engine.evaluate("WORKFLOW", {"default_action": "CONTINUE"})
        self.assertEqual(result.source, "malformed")
        self.assertEqual(result.fallback_used, "rules")
        self.assertEqual(result.decision, "CONTINUE")

    def test_timeout_falls_back(self) -> None:
        jev = mock.Mock()
        jev.evaluate.side_effect = JevError("timeout", error_type="timeout")
        engine = CompositeDecisionEngine(JevSettings(enabled=True), jev=jev)
        result = engine.evaluate("ISSUE_TRIAGE", {"labels": ["bug"]})
        self.assertEqual(result.source, "timeout")
        self.assertEqual(result.fallback_used, "rules")

    def test_auth_failure_falls_back(self) -> None:
        jev = mock.Mock()
        jev.evaluate.side_effect = JevError("no key", error_type="authentication")
        engine = CompositeDecisionEngine(JevSettings(enabled=True), jev=jev)
        result = engine.evaluate("TASK_CLASSIFICATION", {"title": "x"})
        self.assertEqual(result.source, "authentication")

    def test_low_confidence_does_not_act(self) -> None:
        jev = mock.Mock()
        from decision_engine import DecisionResult

        jev.evaluate.return_value = DecisionResult(
            decision_type="WORKFLOW",
            decision="SKIP_UAT",
            confidence=0.4,
            source="jev",
        )
        settings = JevSettings(enabled=True)
        engine = CompositeDecisionEngine(settings, jev=jev)
        result = engine.evaluate("WORKFLOW", {"default_action": "CONTINUE"})
        self.assertEqual(result.source, "low_confidence")
        self.assertFalse(may_act_on(result, settings))

    def test_policy_never_lets_jev_skip_required_uat_or_complete_failed_tests(self) -> None:
        from decision_engine import DecisionResult

        settings = JevSettings(enabled=True)
        skip = DecisionResult(decision_type="WORKFLOW", decision="SKIP_UAT", confidence=0.99, source="jev")
        self.assertEqual(
            swarm_policy_action(skip, settings=settings, uat_required=True),
            "RUN_UAT",
        )
        complete = DecisionResult(decision_type="COMPLETION", decision="COMPLETE", confidence=0.99, source="jev")
        self.assertEqual(
            swarm_policy_action(complete, settings=settings, failed_tests=True),
            "INCOMPLETE",
        )
        cyber = DecisionResult(decision_type="CYBER_FINDING", decision="PASS", confidence=0.4, source="jev")
        self.assertEqual(
            swarm_policy_action(cyber, settings=settings, blocking_security=True, default="FIX_NOW"),
            "FIX_NOW",
        )
        high_conf_pass = DecisionResult(
            decision_type="CYBER_FINDING",
            decision="PASS",
            confidence=0.99,
            source="jev",
            metadata={"security": True},
        )
        self.assertEqual(
            swarm_policy_action(
                high_conf_pass,
                settings=settings,
                blocking_security=True,
                default="FIX_NOW",
            ),
            "FIX_NOW",
        )


class PersistenceAndReportTests(unittest.TestCase):
    def test_decisions_and_score_deltas_persist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "history.sqlite3"
            repo = ExecutionHistoryRepository(db)
            repo.record_jev_decision({
                "execution_id": "exec-1",
                "repository": "acme/app",
                "issue_number": 12,
                "decision_type": "TASK_CLASSIFICATION",
                "decision": "BUG",
                "confidence": 0.93,
                "scores": {"complexity": 0.2},
                "reason_codes": ["BUG_LABEL"],
                "source": "jev",
                "latency_ms": 11,
                "estimated_cost": 0.0001,
            })
            repo.record_jev_score_comparison({
                "execution_id": "exec-1",
                "repository": "acme/app",
                "issue_number": 12,
                "jev_status": "enabled",
                "baseline": {
                    "normalized_score": 0.70,
                    "native_score": 1.2,
                    "provider": "claude",
                    "model": "claude-sonnet-5",
                    "effort": "low",
                    "prompt_grade": "B",
                    "complexity": 4,
                },
                "jev": {"normalized_score": 0.91, "confidence": 0.91, "scores": {"complexity": 0.4}},
                "modified": {
                    "normalized_score": 0.74,
                    "provider": "claude",
                    "model": "claude-haiku-4-5",
                    "effort": "low",
                },
                "delta": {"absolute": 0.04, "percent": 5.7, "routing_changed": True},
                "workflow_outcome": "completed",
            })
            disabled = {
                "execution_id": "exec-2",
                "repository": "acme/app",
                "issue_number": 13,
                "jev_status": "disabled",
                "baseline": {"normalized_score": 0.80, "provider": "grok", "model": "grok-4.6", "effort": "low"},
                "jev": None,
                "modified": {"normalized_score": 0.80, "provider": "grok", "model": "grok-4.6", "effort": "low"},
                "delta": {"absolute": 0.0, "percent": 0.0, "routing_changed": False},
            }
            repo.record_jev_score_comparison(disabled)
            page = repo.jev_feedback(["acme/app"])
            self.assertEqual(page["total"], 2)
            enabled = next(row for row in page["records"] if row["jevStatus"] == "enabled")
            self.assertEqual(enabled["baselineScore"], 0.70)
            self.assertEqual(enabled["jevScore"], 0.91)
            self.assertEqual(enabled["modifiedScore"], 0.74)
            self.assertTrue(enabled["routingChanged"])
            self.assertNotEqual(enabled["baselineScore"], enabled["modifiedScore"])
            off = next(row for row in page["records"] if row["jevStatus"] == "disabled")
            self.assertTrue(off["baselineOnly"])
            self.assertIsNone(off["jevScore"])
            fallback = repo.jev_feedback(["acme/app"], jev_status="disabled")
            self.assertEqual(fallback["total"], 1)

    def test_github_report_is_concise(self) -> None:
        from decision_engine import DecisionResult

        markdown = format_jev_markdown([
            DecisionResult(
                decision_type="TASK_CLASSIFICATION",
                decision="ARCHITECTURE_REFACTOR",
                confidence=0.94,
                scores={"complexity": 0.81, "crossRepoProbability": 0.88, "securityRisk": 0.34},
                metadata={"ragScope": "ORGANIZATION"},
                source="jev",
                estimated_cost=0.0002,
                latency_ms=400,
            ),
            DecisionResult(
                decision_type="COMPLETION",
                decision="COMPLETE",
                confidence=0.95,
                source="jev",
                latency_ms=200,
            ),
        ])
        self.assertIn("### Jev Decision Engine", markdown)
        self.assertIn("Architecture Refactor", markdown)
        self.assertIn("Complexity: 81%", markdown)
        self.assertNotIn("RAW PROMPT", markdown)
        self.assertEqual(format_jev_markdown([], verbose=True), "")


class EngineFactoryTests(unittest.TestCase):
    def test_disabled_factory_does_not_require_cli(self) -> None:
        engine = engine_from_settings(JevSettings(enabled=False))
        result = engine.evaluate("WORKFLOW", {})
        self.assertEqual(result.source, "disabled")


if __name__ == "__main__":
    unittest.main()
