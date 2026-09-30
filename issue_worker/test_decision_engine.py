"""Decision engine contracts, confidence policy, fallback, and persistence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_execution_history import ExecutionHistoryRepository
from decision_engine import (
    complexity_out_of_ten,
    _field_score,
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
from dynamic_router import blend_complexity
from jev_cli import JevError, JevResponse, JevSettings, JevUsage, clamp_confidence_threshold


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


    def test_unactionable_irreversible_recommendation_fails_closed_without_default(self) -> None:
        from decision_engine import DecisionResult

        settings = JevSettings(enabled=True)
        cases = {
            ("WORKFLOW", "FAIL"): "HUMAN_REVIEW",
            ("WORKFLOW", "SKIP_UAT"): "RUN_UAT",
            ("WORKFLOW", "SKIP_CYBER"): "RUN_CYBER",
            ("WORKFLOW", "COMPLETE"): "HUMAN_REVIEW",
            ("COMPLETION", "COMPLETE"): "NEEDS_HUMAN_REVIEW",
            ("UAT_FINDING", "PASS"): "FIX_NOW",
            ("WORKFLOW", "CONTINUE"): "CONTINUE",
        }
        for (kind, decision), expected in cases.items():
            result = DecisionResult(decision_type=kind, decision=decision, confidence=0.8, source="jev")
            self.assertEqual(
                swarm_policy_action(result, settings=settings, default=""),
                expected,
                f"{kind} {decision}",
            )
        result = DecisionResult(decision_type="WORKFLOW", decision="FAIL", confidence=0.8, source="jev")
        self.assertEqual(swarm_policy_action(result, settings=settings, default="RETRY"), "RETRY")


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

    def test_feedback_date_range_is_inclusive_on_both_ends(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = ExecutionHistoryRepository(Path(directory) / "history.sqlite3")
            for day in ("2026-01-01", "2026-01-15", "2026-02-01", "2026-03-01"):
                repo.record_jev_score_comparison({
                    "comparison_id": day,
                    "execution_id": f"exec-{day}",
                    "repository": "acme/app",
                    "issue_number": 1,
                    "jev_status": "enabled",
                    "created_at": f"{day}T10:00:00+00:00",
                    "baseline": {"normalized_score": 0.5},
                    "jev": {"normalized_score": 0.6, "confidence": 0.9},
                    "modified": {"normalized_score": 0.55},
                    "delta": {"absolute": 0.05, "percent": 10.0, "routing_changed": False},
                })
            page = repo.jev_feedback(["acme/app"], created_after="2026-01-15", created_before="2026-02-01")
            self.assertEqual(page["total"], 2)
            self.assertEqual(repo.jev_feedback(["acme/app"], created_before="2026-01-01")["total"], 1)

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
        self.assertIn("**Complexity:** 8/10 (very complex)", markdown)
        self.assertNotIn("RAW PROMPT", markdown)
        self.assertEqual(format_jev_markdown([], verbose=True), "")


def _levels(score: float, count: int, confidence: float = 0.8) -> dict:
    """A Jev ``score`` answer as the CLI returns it: the expected level, 0..count-1."""
    return {
        "type": "score", "score": score, "confidence": confidence,
        "probabilities": {str(level): 0.0 for level in range(count)},
    }


class ScoreScaleTests(unittest.TestCase):
    """Regression: Jev scores are rubric levels, not fractions (issue #369 read as 4%)."""

    def preflight(self, **answers):
        response = JevResponse(
            answers={"task_type": {"type": "choice", "choice": "ENHANCEMENT", "confidence": 0.83}, **answers},
            usage=JevUsage(), raw_shape="answers", model="jev-test",
        )
        return interpret_jev_response("TASK_CLASSIFICATION", {"title": "t"}, response)

    def test_a_very_complex_issue_is_not_read_as_four_percent(self):
        # Level 4 of the six-level complexity rubric (very_complex) is 80%, which
        # agrees with the router's 8/10 for issue #369.
        result = self.preflight(complexity=_levels(4.0, 6), security_risk=_levels(2.0, 5))
        result.source = Source.JEV.value
        self.assertAlmostEqual(result.scores["complexity"], 0.8)
        self.assertAlmostEqual(result.scores["securityRisk"], 0.5)
        text = format_jev_markdown([result])
        self.assertIn("**Complexity:** 8/10 (very complex)", text)
        self.assertNotIn("**Complexity:** 8/10 (very complex);", text)  # no router grade given
        self.assertIn("**Security sensitivity:** 50% (moderate)", text)

    def test_the_comment_compares_jev_with_the_routers_grade_on_one_scale(self):
        result = self.preflight(complexity=_levels(4.0, 6))
        result.source = Source.JEV.value
        text = format_jev_markdown([result], router_complexity=10)
        self.assertIn("**Complexity:** 8/10 (very complex); the router graded 10/10", text)
        self.assertEqual([complexity_out_of_ten(f) for f in (0.0, 0.2, 0.5, 0.8, 1.0)], [1, 3, 6, 8, 10])
        self.assertEqual(blend_complexity(10, 0.8, 0.83), 9)   # Jev's real level barely moves the grade
        self.assertEqual(blend_complexity(10, 0.04, 0.83), 8)  # what the misread 4% used to do

    def test_a_simple_issue_is_not_read_as_ninety_six_percent(self):
        result = self.preflight(complexity=_levels(0.96, 6))
        self.assertAlmostEqual(result.scores["complexity"], 0.192)

    def test_real_cli_values_scale_to_their_rubric_ends(self):
        for field, count in (("complexity", 6), ("security_risk", 5), ("ambiguity", 5),
                             ("failure_risk", 3), ("reasoning_intensity", 4), ("context_size", 5)):
            low = self.preflight(**{field: _levels(0.0, count)})
            high = self.preflight(**{field: _levels(float(count - 1), count)})
            key = {"security_risk": "securityRisk", "failure_risk": "failureRisk",
                   "reasoning_intensity": "reasoningIntensity", "context_size": "contextSize"}.get(field, field)
            self.assertEqual(low.scores.get(key), 0.0, field)
            self.assertEqual(high.scores.get(key), 1.0, field)

    def test_out_of_range_and_broken_levels_never_escape_zero_to_one(self):
        self.assertEqual(_field_score(_levels(9.0, 6), "complexity"), 1.0)
        self.assertEqual(_field_score(_levels(-3.0, 6), "complexity"), 0.0)
        # A broken level is "no score", never 0.0 (which for security risk means "no risk").
        broken = {"type": "score", "score": float("nan"), "probabilities": {"0": 0, "1": 1}}
        self.assertIsNone(_field_score(broken, "unknown_field"))
        self.assertIsNone(_field_score({**broken, "score": float("inf")}, "complexity"))
        self.assertEqual(clamp_confidence_threshold(float("nan"), 0.7), 0.7)

    def test_bare_numbers_and_labels_keep_their_existing_reading(self):
        self.assertEqual(_field_score(0.4, "complexity"), 0.4)
        self.assertEqual(_field_score({"value": "critical"}, "security_risk"), 1.0)
        self.assertEqual(_field_score({"value": "very_complex"}, "complexity"), 0.8)


class EngineFactoryTests(unittest.TestCase):
    def test_disabled_factory_does_not_require_cli(self) -> None:
        engine = engine_from_settings(JevSettings(enabled=False))
        result = engine.evaluate("WORKFLOW", {})
        self.assertEqual(result.source, "disabled")


if __name__ == "__main__":
    unittest.main()
