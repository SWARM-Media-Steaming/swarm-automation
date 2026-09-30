"""Issue #373: exercise optional key:value fields nested in lifecycle comments.

The issue requires every worker-authored key in lifecycle output to use the
GitHub Markdown form ``**Label:**``.  These fixtures cover optional routing,
complexity, and Jev fields that are absent from the ordinary happy path.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "issue_worker"))

from decision_engine import DecisionResult, format_jev_markdown  # noqa: E402
from swarm_issue_worker import IssueContext, ProviderChoice, ProviderUsage, Worker  # noqa: E402


class Issue373ConditionalLabelMatrixTests(unittest.TestCase):
    def assert_labels(self, body: str, labels: tuple[str, ...], *, bullet: bool = False) -> None:
        prefix = r"- " if bullet else ""
        for label in labels:
            with self.subTest(label=label):
                self.assertRegex(
                    body,
                    rf"(?m)^{prefix}\*\*{re.escape(label)}:\*\*\s+\S.*$",
                )
                self.assertNotRegex(
                    body,
                    rf"(?m)^{prefix}{re.escape(label)}:\s+\S.*$",
                )

    @staticmethod
    def started_body(routing: dict[str, object]) -> str:
        worker = Worker.__new__(Worker)
        worker.issue = IssueContext(
            373,
            "Bold conditional lifecycle labels",
            "",
            [],
            "https://example.invalid/issues/373",
        )
        worker.choice = ProviderChoice("Codex", "gpt-test", "high", "session-373")
        worker.config = SimpleNamespace(github_repository="example/swarm")
        worker.github = SimpleNamespace(gh=mock.Mock(return_value=""))
        worker.start_usage = ProviderUsage(0, 60.0, "week 60% remaining")
        worker.routing = routing
        worker.read_state = lambda: {}
        worker.update_state = lambda **_values: None
        worker.comments = lambda _issue_number: []
        worker.expected_branch = lambda: "ai/codex/issue-373"
        worker.render_jev_report = lambda: ""

        worker.post_started_comment()
        calls = [
            call
            for call in worker.github.gh.call_args_list
            if call.args and list(call.args[0][:2]) == ["issue", "comment"]
        ]
        if len(calls) != 1:
            raise AssertionError(f"expected one issue comment, got {len(calls)}")
        return str(calls[0].args[2])

    def test_started_comment_bolds_every_optional_applied_routing_label(self) -> None:
        body = self.started_body(
            {
                "provider": "codex",
                "provider_name": "Codex",
                "selected_model": "gpt-test",
                "reasoning_effort": "high",
                "prompt_grade": "B+",
                "complexity": 7,
                "confidence": 0.91,
                "provider_candidates": ["codex", "claude", "grok"],
                "dynamic_model_routing": True,
                "applied": True,
                "routing_optimization": "cost",
                "router_provider": "grok",
                "router_model": "grok-test",
                "router_effort": "medium",
                "provider_reason": "Best fit for the deterministic fixture.",
                "provider_override_reason": "Rework: a different provider completed the prior pass.",
                "grade_reason": "Several conditional branches are involved.",
                "complexity_reason": "The output combines multiple reports.",
            }
        )

        self.assert_labels(
            body,
            (
                "Prompt Grade",
                "Complexity",
                "Selected AI",
                "Selected Model",
                "Reasoning",
                "How this was chosen",
                "Routing Confidence",
                "Graded and routed by",
                "Routing Preference",
                "AI Tools Considered",
                "Why Codex",
                "Rework",
                "Why this grade (B+)",
                "How complexity was determined (7/10)",
            ),
        )

    def test_started_comment_bolds_configured_fallback_attribution(self) -> None:
        body = self.started_body(
            {
                "provider": "codex",
                "provider_name": "Codex",
                "selected_model": "gpt-test",
                "reasoning_effort": "medium",
                "dynamic_model_routing": False,
                "fallback": True,
                "router_provider": "claude",
                "router_model": "claude-test",
                "router_effort": "low",
                "grade_reason": "The fixture router returned no decision.",
            }
        )

        self.assert_labels(
            body,
            ("Selected AI", "Selected Model", "Reasoning", "How this was chosen", "Routed by"),
        )

    def test_started_comment_bolds_full_complexity_matrix_and_partial_metrics(self) -> None:
        vector = {
            "repository_complexity": 50,
            "relevant_component_complexity": 40,
            "implementation_complexity": 30,
            "change_surface": 20,
            "architecture_risk": 10,
            "security_risk": 5,
            "uncertainty": 15,
            "confidence": 0.8,
        }
        analysis = {
            "vector": vector,
            "scope": {
                "estimated_files": 2,
                "estimated_modules": 1,
                "services_affected": 1,
                "database_change_likely": False,
                "api_change_likely": True,
                "infrastructure_change_likely": False,
            },
            "requirements": {
                "recommended_capability_floor": 40,
                "recommended_reasoning": "medium",
                "context_requirement": "standard",
                "estimated_fix_rounds": 1,
            },
            "source": "deterministic fixture",
            "drivers": ["conditional lifecycle output"],
            "fallback_used": True,
            "unavailable": ["coverage"],
            "scoring_version": "fixture-v1",
            "profile_version": 2,
            "repo_commit": "a" * 40,
        }
        body = self.started_body(
            {
                "provider": "codex",
                "provider_name": "Codex",
                "selected_model": "gpt-test",
                "reasoning_effort": "high",
                "prompt_grade": "A",
                "complexity": 3,
                "confidence": 0.95,
                "dynamic_model_routing": True,
                "applied": True,
                "complexity_analysis": analysis,
            }
        )

        self.assert_labels(
            body,
            tuple(key.replace("_", " ").title() for key in vector if key != "confidence")
            + (
                "Confidence",
                "Partial metrics",
                "Complexity Scoring Version",
                "Repository Profile Version",
                "Repository Commit",
            ),
        )
        self.assert_labels(
            body,
            (
                "Files/modules likely affected",
                "Services affected",
                "Database changes",
                "API changes",
                "Infrastructure changes",
                "Minimum capability",
                "Reasoning",
                "Context requirement",
                "Estimated repair/adversarial rounds",
            ),
            bullet=True,
        )

    def test_completed_comment_bolds_workflow_and_optional_jev_totals(self) -> None:
        records = [
            DecisionResult(
                decision_type="TASK_CLASSIFICATION",
                decision="FEATURE",
                confidence=0.9,
                scores={"complexity": 0.5},
                source="jev",
                latency_ms=100,
                estimated_cost=0.001,
            ),
            DecisionResult(
                decision_type="WORKFLOW",
                decision="RUN_UAT",
                confidence=0.8,
                source="rules",
                latency_ms=20,
                estimated_cost=0.0,
                llm_calls_avoided=1,
            ),
        ]
        report = format_jev_markdown(records)
        worker = Worker.__new__(Worker)
        worker.render_jev_report = lambda: ""
        body = worker.render_pending_comment(
            {
                "ai": "Codex",
                "model": "gpt-test",
                "effort": "high",
                "commit_sha": "b" * 40,
                "commit_message": "Exercise optional Jev fields (#373)",
                "ai_usage_report": " ",
                "jev_report": report,
                "ai_output": "Completed.",
            }
        )

        self.assert_labels(body, ("Task", "Complexity", "Confidence", "Workflow"), bullet=True)
        self.assert_labels(
            body,
            (
                "Jev calls",
                "Total decision latency",
                "Estimated cost",
                "Estimated LLM decision calls avoided",
            ),
        )


if __name__ == "__main__":
    unittest.main()
