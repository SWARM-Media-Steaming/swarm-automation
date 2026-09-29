"""Issue #299 workflow invariants the first adversarial pass did not pin.

Oracle is the issue text, not the current worker:

- Invalid or low-confidence Jev output never controls irreversible actions.
- Existing Swarm rules determine the action on UAT/Cyber findings; Jev
  cannot reclassify a blocking finding as out of scope or PASS it below
  the security threshold.
- A single low RAG relevance score cannot permanently drop potentially
  critical context (section 9).
- Decision-use toggles and disabled Jev add no Jev call, cost, or latency.
- The CLI adapter retries timeouts, not authentication or malformed JSON,
  and never puts secrets in errors.
- Pre-flight scoring includes context size and expected success (section
  3 / AC 24) and feeds them as bounded inputs, not a replacement grade.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from harness import SECRET_TOKEN, JevWorkerFixture, pack_with
from adversarial_security import SECURITY_STAGE
from adversarial_uat import UAT_STAGE
from decision_engine import (
    DecisionType,
    WorkflowAction,
    build_jev_request,
    interpret_jev_response,
    swarm_policy_action,
)
from dynamic_router import blend_complexity
from jev_cli import JevCli, JevError, JevResponse, JevSettings


class PreflightScoreContractTests(unittest.TestCase):
    def test_preflight_request_asks_for_context_size_and_expected_success(self) -> None:
        _state, questions = build_jev_request(
            DecisionType.TASK_CLASSIFICATION.value,
            {"title": "Split the media pipeline across repos"},
        )
        blob = json.dumps(questions).lower()
        with self.subTest("context size"):
            self.assertRegex(
                blob,
                r"context[_ -]?size|likely context",
                "section 3 and AC 24 require Jev to evaluate likely context size "
                "before model selection; the typed question set never asks for it",
            )
        with self.subTest("expected success"):
            self.assertRegex(
                blob,
                r"expected[_ -]?success|required capability|success probability",
                "AC 24 requires the pre-flight score to include required capability "
                "and expected success; the typed question set never asks for it",
            )

    def test_preflight_result_surfaces_context_size_and_expected_success(self) -> None:
        response = JevResponse(
            answers={
                "task_type": {"value": "ARCHITECTURE_REFACTOR", "confidence": 0.94},
                "complexity": {"value": 0.81, "confidence": 0.9},
                "security_risk": {"value": 0.34, "confidence": 0.9},
                "cross_repo": {"value": 0.88},
                "context_size": {"value": 0.77, "confidence": 0.9},
                "expected_success": {"value": 0.85, "confidence": 0.9},
                "rag_scope": {"value": "ORGANIZATION"},
            }
        )
        result = interpret_jev_response(DecisionType.TASK_CLASSIFICATION.value, {}, response)
        with self.subTest("context size"):
            context_size = result.scores.get("contextSize", result.scores.get("context_size"))
            self.assertIsNotNone(
                context_size,
                "a numeric context_size answer must be kept on the typed result so "
                "the Swarm router can combine it with existing context requirements",
            )
            self.assertAlmostEqual(float(context_size), 0.77, places=2)
        with self.subTest("expected success"):
            expected_success = result.scores.get(
                "expectedSuccess", result.scores.get("expected_success")
            )
            self.assertIsNotNone(
                expected_success,
                "a numeric expected_success answer must be kept on the typed result "
                "so cost-first routing can apply the expected-success gate",
            )
            self.assertAlmostEqual(float(expected_success), 0.85, places=2)


class BoundedJevInputTests(unittest.TestCase):
    def test_jev_cannot_replace_swarm_complexity_with_an_unbounded_jump(self) -> None:
        # Section 26: re-run the existing Swarm formula with bounded Jev
        # inputs. A 0.99-confidence "extreme" signal on a grade-3 issue must
        # not become complexity 10 and yank the task onto a frontier model.
        self.assertLessEqual(blend_complexity(3, 1.0, 0.99), 5)
        self.assertGreaterEqual(blend_complexity(8, 0.0, 0.99), 6)
        self.assertEqual(blend_complexity(5, None, 0.99), 5)


class BlockingFindingAuthorityTests(JevWorkerFixture, unittest.TestCase):
    def test_jev_out_of_scope_cannot_unblock_a_blocking_uat_finding(self) -> None:
        self.bind_issue()
        self.install_jev_engine(
            lambda *_a, **_k: self.decision(
                decision_type=DecisionType.UAT_FINDING.value,
                decision=WorkflowAction.OUT_OF_SCOPE.value,
                confidence=0.99,
                metadata={"scope": "OUT_OF_SCOPE", "blocking": False},
            )
        )
        result = self.worker.classify_finding_with_jev(
            kind=DecisionType.UAT_FINDING.value,
            finding={"title": "Login form no longer submits", "severity": "HIGH"},
            in_scope=True,
            blocking=True,
            default_action=WorkflowAction.FIX_NOW.value,
        )
        self.assertEqual(
            result.get("swarmAction"),
            WorkflowAction.FIX_NOW.value,
            "section 7: existing Swarm rules determine the actual action. A "
            "blocking in-scope UAT finding must not become OUT_OF_SCOPE just "
            f"because Jev said so; got {result.get('swarmAction')!r}",
        )

    def test_jev_out_of_scope_cannot_unblock_a_blocking_cyber_finding(self) -> None:
        self.bind_issue()
        self.install_jev_engine(
            lambda *_a, **_k: self.decision(
                decision_type=DecisionType.CYBER_FINDING.value,
                decision=WorkflowAction.OUT_OF_SCOPE.value,
                confidence=0.99,
                metadata={"scope": "OUT_OF_SCOPE", "security": True},
            )
        )
        result = self.worker.classify_finding_with_jev(
            kind=DecisionType.CYBER_FINDING.value,
            finding={"title": "SQL injection in login", "severity": "CRITICAL"},
            in_scope=True,
            blocking=True,
            default_action=WorkflowAction.FIX_NOW.value,
        )
        self.assertEqual(
            result.get("swarmAction"),
            WorkflowAction.FIX_NOW.value,
            "section 8: Swarm remains the authority on whether a security "
            "finding blocks. Jev OUT_OF_SCOPE on a blocking finding would "
            f"suppress it; got {result.get('swarmAction')!r}",
        )

    def test_cyber_pass_below_security_threshold_does_not_suppress(self) -> None:
        settings = JevSettings(enabled=True, confidence_security=0.95)
        result = self.decision(
            decision_type=DecisionType.CYBER_FINDING.value,
            decision=WorkflowAction.PASS.value,
            confidence=0.92,
            metadata={"security": True},
        )
        action = swarm_policy_action(
            result,
            settings=settings,
            blocking_security=False,
            default=WorkflowAction.FIX_NOW.value,
        )
        self.assertNotEqual(
            action,
            WorkflowAction.PASS.value,
            "section 8 / 12: security-sensitive decisions use the stricter "
            "threshold (0.95). A 0.92 PASS is below that floor and must not "
            "suppress a security finding.",
        )

    def test_disabled_cyber_category_does_not_call_jev_on_findings(self) -> None:
        self.bind_issue()
        settings = self.enable_jev(use_cyber=False)
        jev = mock.Mock()
        from decision_engine import CompositeDecisionEngine

        self.worker._decision_engine = CompositeDecisionEngine(settings, jev=jev)
        self.worker.advise_adversarial_findings(
            SECURITY_STAGE,
            {"findings": [{"title": "SQL injection", "severity": "CRITICAL"}]},
        )
        jev.evaluate.assert_not_called()

    def test_disabled_uat_category_does_not_call_jev_on_findings(self) -> None:
        self.bind_issue()
        settings = self.enable_jev(use_uat=False)
        jev = mock.Mock()
        from decision_engine import CompositeDecisionEngine

        self.worker._decision_engine = CompositeDecisionEngine(settings, jev=jev)
        self.worker.advise_adversarial_findings(
            UAT_STAGE,
            {"findings": [{"title": "Submit button does nothing"}]},
        )
        jev.evaluate.assert_not_called()


class RagFallbackRetrievalTests(JevWorkerFixture, unittest.TestCase):
    def test_a_low_score_does_not_drop_potentially_critical_context(self) -> None:
        """Section 9: scores may reduce irrelevant context; a single low
        score must not permanently discard potentially critical context.

        The issue's own example names a historical security incident as
        context worth scoring. Scoring it 0.08 while keeping an architecture
        document at 0.94 is exactly 'discard based on a single low score'.
        """
        self.bind_issue()

        def evaluate(kind, context):
            if kind == DecisionType.RAG_SCOPE.value:
                return self.decision(
                    decision_type=kind, decision="ORGANIZATION", confidence=0.94
                )
            title = str((context or {}).get("candidate") or "")
            if "Architecture" in title:
                relevance = 0.94
            elif "incident" in title.lower() or "security" in title.lower():
                relevance = 0.08
            else:
                relevance = 0.08
            return self.decision(
                decision_type=DecisionType.CONTEXT_RELEVANCE.value,
                decision="KEEP" if relevance >= 0.35 else "LOW",
                confidence=0.9,
                scores={"relevance": relevance},
                metadata={"keep": relevance >= 0.15},
            )

        self.install_jev_engine(evaluate)
        pack = pack_with(
            {"title": "Architecture document", "summary": "pipeline"},
            {"title": "Historical security incident", "summary": "incident"},
            {"title": "Unrelated repository README", "summary": "readme"},
        )
        kept = self.worker.score_knowledge_pack(pack)
        titles = [str(item.get("title") or "") for item in kept.items]
        self.assertIn(
            "Historical security incident",
            titles,
            "Jev scored the historical security incident 0.08 and an architecture "
            "document 0.94; section 9 forbids permanently discarding potentially "
            f"critical context solely on that single low score. kept={titles!r}",
        )

    def test_rag_category_off_does_not_call_jev_or_drop_items(self) -> None:
        self.bind_issue()
        settings = self.enable_jev(use_rag=False)
        jev = mock.Mock()
        from decision_engine import CompositeDecisionEngine

        self.worker._decision_engine = CompositeDecisionEngine(settings, jev=jev)
        pack = pack_with(
            {"title": "Architecture document", "summary": "pipeline"},
            {"title": "Historical security incident", "summary": "incident"},
        )
        kept = self.worker.score_knowledge_pack(pack)
        jev.evaluate.assert_not_called()
        self.assertEqual(len(kept.items), 2)


class JevCliFailurePathTests(unittest.TestCase):
    def test_authentication_failure_is_not_retried_and_redacts_secrets(self) -> None:
        calls = {"n": 0}

        def runner(command, timeout, stdin):
            calls["n"] += 1
            return SimpleNamespace(
                stdout="",
                stderr=f"unauthorized api_key={SECRET_TOKEN}",
                returncode=1,
            )

        cli = JevCli(
            JevSettings(enabled=True, bin="/usr/bin/jev", max_retries=2),
            runner=runner,
            sleeper=lambda _delay: None,
        )
        cli.bin_path = "/usr/bin/jev"
        with self.assertRaises(JevError) as raised:
            cli.ask(state={"title": "x"}, questions={"q": {}})
        self.assertEqual(raised.exception.error_type, "authentication")
        self.assertEqual(calls["n"], 1, "authentication failures must not be retried")
        self.assertNotIn(SECRET_TOKEN, str(raised.exception))

    def test_malformed_json_is_not_retried(self) -> None:
        calls = {"n": 0}

        def runner(command, timeout, stdin):
            calls["n"] += 1
            return SimpleNamespace(stdout="not-json {", stderr="", returncode=0)

        cli = JevCli(
            JevSettings(enabled=True, bin="/usr/bin/jev", max_retries=2),
            runner=runner,
            sleeper=lambda _delay: None,
        )
        cli.bin_path = "/usr/bin/jev"
        with self.assertRaises(JevError) as raised:
            cli.ask(state={}, questions={"q": {}})
        self.assertEqual(raised.exception.error_type, "malformed")
        self.assertEqual(calls["n"], 1)

    def test_timeout_is_retried_then_falls_back_at_the_engine(self) -> None:
        calls = {"n": 0}

        def runner(command, timeout, stdin):
            calls["n"] += 1
            raise subprocess.TimeoutExpired(command, timeout)

        cli = JevCli(
            JevSettings(enabled=True, bin="/usr/bin/jev", max_retries=2),
            runner=runner,
            sleeper=lambda _delay: None,
        )
        cli.bin_path = "/usr/bin/jev"
        with self.assertRaises(JevError) as raised:
            cli.ask(state={}, questions={"q": {}})
        self.assertEqual(raised.exception.error_type, "timeout")
        self.assertEqual(calls["n"], 3)


if __name__ == "__main__":
    unittest.main()
