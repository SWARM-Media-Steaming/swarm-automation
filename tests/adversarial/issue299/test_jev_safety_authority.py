"""Issue #299: Jev is advisory. Swarm keeps execution and safety authority.

Oracle is the issue, not the current worker: invalid or low-confidence Jev
output never controls irreversible actions; configured UAT/Cyber stay on;
failed tests and blocking security findings stay blocking; Jev is optional.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import unittest
from pathlib import Path
from unittest import mock

from harness import SECRET_TOKEN, JevWorkerFixture, pack_with
from adversarial_uat import UAT_STAGE
from decision_engine import (
    CompletionVerdict,
    DecisionType,
    Source,
    WorkflowAction,
    format_jev_markdown,
    may_act_on,
    swarm_policy_action,
)
from jev_cli import JevCli, JevError, JevSettings, context_fingerprint, redact_cli_text
from swarm_issue_worker import KNOWN_PROVIDER_KEYS, Worker


class JevIsNotACodingProviderTests(unittest.TestCase):
    def test_jev_is_absent_from_the_implementation_provider_set(self) -> None:
        self.assertNotIn("jev", KNOWN_PROVIDER_KEYS)
        self.assertEqual(set(KNOWN_PROVIDER_KEYS), {"claude", "codex", "grok"})

    def test_first_implementation_has_no_http_client(self) -> None:
        source = Path(__file__).resolve().parents[3] / "issue_worker" / "jev_cli.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module.split(".", 1)[0])
        for module in ("httpx", "aiohttp", "urllib", "requests"):
            self.assertNotIn(module, imported)


class DisabledAndFallbackTests(JevWorkerFixture, unittest.TestCase):
    def test_disabled_jev_adds_no_cli_call_cost_or_latency(self) -> None:
        self.bind_issue()
        self.assertFalse(self.worker.config.jev.enabled)
        with mock.patch.object(JevCli, "ask", side_effect=AssertionError("Jev CLI must not run")) as ask:
            result = self.worker.evaluate_decision("TASK_CLASSIFICATION", {"title": "Fix login"})
        ask.assert_not_called()
        self.assertEqual(result["source"], Source.DISABLED.value)
        self.assertEqual(result["latencyMs"], 0)
        self.assertIsNone(result["estimatedCost"])
        self.assertEqual(result["llmCallsAvoided"], 0)

    def test_timeout_malformed_and_auth_failures_fall_back_instead_of_raising(self) -> None:
        self.bind_issue()
        for error_type, expected_source in (
            ("timeout", Source.TIMEOUT.value),
            ("malformed", Source.MALFORMED.value),
            ("authentication", Source.AUTHENTICATION.value),
        ):
            with self.subTest(error_type=error_type):
                jev = self.install_jev_engine(
                    mock.Mock(side_effect=JevError(error_type, error_type=error_type))
                )
                result = self.worker.evaluate_decision("ISSUE_TRIAGE", {"title": "bug in parser"})
                self.assertEqual(result["source"], expected_source)
                self.assertEqual(result["fallbackUsed"], "rules")
                self.assertTrue(jev.evaluate.called)

    def test_low_confidence_skip_uat_cannot_disable_configured_uat(self) -> None:
        self.bind_issue()
        self.worker.config = dataclasses.replace(
            self.worker.config,
            adversarial_uat_enabled=True,
            jev=JevSettings(enabled=True),
        )
        self.install_jev_engine(
            lambda *_args, **_kwargs: self.decision(
                decision_type=DecisionType.WORKFLOW.value,
                decision=WorkflowAction.SKIP_UAT.value,
                confidence=0.4,
            )
        )
        result = self.worker.evaluate_decision(
            "WORKFLOW",
            {"default_action": WorkflowAction.CONTINUE.value},
        )
        self.assertNotEqual(result["swarmAction"], WorkflowAction.SKIP_UAT.value)
        self.assertIn(UAT_STAGE.key, {stage.key for stage in self.worker.adversarial_stages()})


class SafetyGateTests(JevWorkerFixture, unittest.TestCase):
    def test_configured_uat_and_cyber_stages_ignore_jev_skip_recommendations(self) -> None:
        self.bind_issue()
        self.worker.config = dataclasses.replace(
            self.worker.config,
            adversarial_uat_enabled=True,
            adversarial_security_enabled=True,
            jev=JevSettings(enabled=True),
        )
        self.install_jev_engine(
            lambda *_a, **_k: self.decision(
                decision_type=DecisionType.WORKFLOW.value,
                decision=WorkflowAction.SKIP_CYBER.value,
                confidence=0.99,
            )
        )
        result = self.worker.evaluate_decision("WORKFLOW", {"default_action": "CONTINUE"})
        self.assertEqual(result["swarmAction"], WorkflowAction.RUN_CYBER.value)
        keys = [stage.key for stage in self.worker.adversarial_stages()]
        self.assertEqual(len(keys), 2)

    def test_high_confidence_skip_uat_still_runs_required_uat(self) -> None:
        settings = JevSettings(enabled=True)
        skip = self.decision(
            decision_type=DecisionType.WORKFLOW.value,
            decision=WorkflowAction.SKIP_UAT.value,
            confidence=0.99,
        )
        self.assertEqual(
            swarm_policy_action(skip, settings=settings, uat_required=True),
            WorkflowAction.RUN_UAT.value,
        )

    def test_blocking_cyber_finding_cannot_be_accepted_as_pass(self) -> None:
        self.bind_issue()
        self.enable_history()
        self.worker.config = dataclasses.replace(
            self.worker.config,
            adversarial_security_enabled=True,
            jev=JevSettings(enabled=True, use_cyber=True),
        )
        self.install_jev_engine(
            lambda *_a, **_k: self.decision(
                decision_type=DecisionType.CYBER_FINDING.value,
                decision=WorkflowAction.PASS.value,
                confidence=0.99,
                metadata={"security": True},
            )
        )
        result = self.worker.classify_finding_with_jev(
            kind=DecisionType.CYBER_FINDING.value,
            finding={"title": "SQL injection in login", "severity": "CRITICAL"},
            in_scope=True,
            blocking=True,
            default_action=WorkflowAction.FIX_NOW.value,
        )
        self.assertNotEqual(result.get("swarmAction"), WorkflowAction.PASS.value)
        self.assertIn(
            result.get("swarmAction"),
            {
                WorkflowAction.FIX_NOW.value,
                WorkflowAction.CREATE_NEW_ISSUE.value,
                WorkflowAction.HUMAN_REVIEW.value,
            },
        )

    def test_low_confidence_cyber_pass_never_suppresses_a_finding(self) -> None:
        settings = JevSettings(enabled=True, confidence_security=0.95)
        result = self.decision(
            decision_type=DecisionType.CYBER_FINDING.value,
            decision=WorkflowAction.PASS.value,
            confidence=0.4,
            metadata={"security": True},
        )
        self.assertFalse(may_act_on(result, settings))
        self.assertEqual(
            swarm_policy_action(
                result,
                settings=settings,
                blocking_security=True,
                default=WorkflowAction.FIX_NOW.value,
            ),
            WorkflowAction.FIX_NOW.value,
        )

    def test_failed_tests_cannot_be_marked_complete(self) -> None:
        self.bind_issue()
        self.install_jev_engine(
            lambda *_a, **_k: self.decision(
                decision_type=DecisionType.COMPLETION.value,
                decision=CompletionVerdict.COMPLETE.value,
                confidence=0.99,
            )
        )
        action = self.worker.evaluate_completion_gate(
            failed_tests=True, blocking_security=False, default="INCOMPLETE"
        )
        self.assertEqual(action, "INCOMPLETE")
        payload = self.worker.evaluate_decision(
            "COMPLETION",
            {"failed_tests": True, "blocking_security": False, "default_action": "COMPLETE"},
        )
        self.assertEqual(payload["swarmAction"], CompletionVerdict.INCOMPLETE.value)

    def test_blocking_security_cannot_be_marked_complete(self) -> None:
        self.bind_issue()
        self.install_jev_engine(
            lambda *_a, **_k: self.decision(
                decision_type=DecisionType.COMPLETION.value,
                decision=CompletionVerdict.COMPLETE.value,
                confidence=0.99,
            )
        )
        action = self.worker.evaluate_completion_gate(
            failed_tests=False, blocking_security=True, default="INCOMPLETE"
        )
        self.assertEqual(action, "INCOMPLETE")

    def test_finalize_asks_jev_to_assess_completion_when_enabled(self) -> None:
        self.bind_issue()
        self.enable_history()
        self.enable_jev(use_completion=True)
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()
        sha = self.git("rev-parse", "HEAD")
        with (
            mock.patch.object(
                self.worker,
                "deliver_pull_request",
                return_value=("https://example.invalid/pull/9", "ai/claude/issue-299", sha),
            ),
            mock.patch.object(
                self.worker,
                "post_pending_comment",
                side_effect=lambda pending: {**pending, "github_comment_posted": True},
            ),
            mock.patch.object(self.worker, "add_pending_label", side_effect=lambda pending: pending),
            mock.patch.object(self.worker, "record_completed"),
            mock.patch.object(self.worker, "clear_in_progress"),
            mock.patch.object(
                self.worker,
                "evaluate_completion_gate",
                wraps=self.worker.evaluate_completion_gate,
            ) as gate,
        ):
            self.worker.finalize_issue(sha, "## Summary\nImplemented the decision engine.")
        gate.assert_called()

    def test_rag_never_discards_the_whole_pack_on_low_scores(self) -> None:
        self.bind_issue()

        def evaluate(kind, context):
            if kind == DecisionType.RAG_SCOPE.value:
                return self.decision(decision_type=kind, decision="REPOSITORY", confidence=0.4)
            return self.decision(
                decision_type=DecisionType.CONTEXT_RELEVANCE.value,
                decision="LOW",
                confidence=0.2,
                scores={"relevance": 0.08},
            )

        self.install_jev_engine(evaluate)
        pack = pack_with(
            {"title": "Architecture document", "summary": "pipeline"},
            {"title": "Historical security incident", "summary": "incident"},
            {"title": "Unrelated README", "summary": "readme"},
        )
        kept = self.worker.score_knowledge_pack(pack)
        self.assertGreaterEqual(len(kept.items), 1)

    def test_secrets_never_appear_in_cli_redaction_fingerprints_or_github_markdown(self) -> None:
        redacted = redact_cli_text(f"unauthorized api_key={SECRET_TOKEN}")
        self.assertNotIn(SECRET_TOKEN, redacted)
        fingerprint = context_fingerprint(
            {"title": "Fix login", "prompt": SECRET_TOKEN, "api_key": SECRET_TOKEN, "body": SECRET_TOKEN}
        )
        self.assertNotIn(SECRET_TOKEN, fingerprint)
        markdown = format_jev_markdown(
            [
                self.decision(
                    decision_type=DecisionType.TASK_CLASSIFICATION.value,
                    decision="BUG",
                    confidence=0.94,
                    scores={"complexity": 0.2},
                    metadata={"explanation": f"see {SECRET_TOKEN}"},
                )
            ]
        )
        self.assertIn("### Jev Decision Engine", markdown)
        self.assertNotIn(SECRET_TOKEN, markdown)
        self.assertNotIn("RAW PROMPT", markdown)


class IrreversibleActionInventoryTests(unittest.TestCase):
    def test_decision_engine_does_not_merge_approve_or_delete(self) -> None:
        source = inspect.getsource(Worker.evaluate_decision) + inspect.getsource(
            swarm_policy_action
        )
        for forbidden in ("merge_pull_request", "approve_pull_request", "gh api --method DELETE"):
            self.assertNotIn(forbidden, source)
