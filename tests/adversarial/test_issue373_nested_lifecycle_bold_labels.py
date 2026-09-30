"""Issue #373: conditional lifecycle reports also bold key:value labels.

The lifecycle renderers append worker-authored routing, complexity, and Jev
reports to started/completed comments.  Those conditional reports are part of
the GitHub lifecycle comment, so their key:value labels have the same
``**Label:**`` contract as usage totals and the cybersecurity review.
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
from swarm_issue_worker import (  # noqa: E402
    IssueContext,
    ProviderChoice,
    ProviderUsage,
    Worker,
)


class Issue373NestedLifecycleBoldLabelTests(unittest.TestCase):
    @staticmethod
    def assert_bold_label(body: str, label: str, *, bullet: bool = False) -> None:
        prefix = r"- " if bullet else ""
        expected = rf"(?m)^{prefix}\*\*{re.escape(label)}:\*\*\s+\S.*$"
        plain = rf"(?m)^{prefix}{re.escape(label)}:\s+\S.*$"
        if not re.search(expected, body):
            raise AssertionError(f"missing bold lifecycle label {label!r} in:\n{body}")
        if re.search(plain, body):
            raise AssertionError(f"found plain lifecycle label {label!r} in:\n{body}")

    @staticmethod
    def started_worker(routing: dict[str, object]) -> Worker:
        worker = Worker.__new__(Worker)
        worker.issue = IssueContext(
            373,
            "Bold nested lifecycle labels",
            "",
            [],
            "https://example.invalid/issues/373",
        )
        worker.choice = ProviderChoice("Codex", "gpt-test", "high", "session-373")
        worker.config = SimpleNamespace(github_repository="example/swarm")
        worker.github = SimpleNamespace(gh=mock.Mock(return_value=""))
        worker.start_usage = ProviderUsage(0, 75.0, "week 75% remaining")
        worker.routing = routing
        worker.read_state = lambda: {}
        worker.update_state = lambda **_values: None
        worker.comments = lambda _issue_number: []
        worker.expected_branch = lambda: "ai/codex/issue-373"
        worker.render_jev_report = lambda: ""
        return worker

    @staticmethod
    def posted_body(worker: Worker) -> str:
        calls = [
            call
            for call in worker.github.gh.call_args_list
            if call.args and list(call.args[0][:2]) == ["issue", "comment"]
        ]
        if len(calls) != 1:
            raise AssertionError(f"expected one issue comment, got {len(calls)}")
        return str(calls[0].args[2])

    def test_started_comment_bolds_dynamic_routing_key_values(self) -> None:
        worker = self.started_worker(
            {
                "provider": "codex",
                "provider_name": "Codex",
                "selected_model": "gpt-test",
                "reasoning_effort": "high",
                "prompt_grade": "B+",
                "complexity": 6,
                "confidence": 0.91,
                "provider_candidates": ["codex", "grok"],
                "dynamic_model_routing": True,
                "applied": True,
            }
        )

        worker.post_started_comment()
        body = self.posted_body(worker)

        for label in (
            "Prompt Grade",
            "Complexity",
            "Selected AI",
            "Selected Model",
            "Reasoning",
            "Routing Confidence",
            "AI Tools Considered",
        ):
            self.assert_bold_label(body, label)

    def test_started_comment_bolds_complexity_analysis_key_values(self) -> None:
        analysis = {
            "vector": {
                "repository_complexity": 50,
                "relevant_component_complexity": 40,
                "implementation_complexity": 30,
                "change_surface": 20,
                "architecture_risk": 10,
                "security_risk": 5,
                "uncertainty": 15,
                "confidence": 0.8,
            },
            "scope": {
                "estimated_files": 2,
                "estimated_modules": 1,
                "services_affected": 0,
                "database_change_likely": False,
                "api_change_likely": False,
                "infrastructure_change_likely": False,
            },
            "requirements": {
                "recommended_capability_floor": 40,
                "recommended_reasoning": "medium",
                "context_requirement": "standard",
                "estimated_fix_rounds": 1,
            },
            "source": "deterministic fixture",
            "drivers": ["small worker-only formatting change"],
            "fallback_used": False,
            "unavailable": [],
            "scoring_version": "fixture-v1",
            "profile_version": 1,
            "repo_commit": "a" * 40,
        }
        routing = {
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
        worker = self.started_worker(routing)

        worker.post_started_comment()
        body = self.posted_body(worker)

        for label in (
            "Repository Complexity",
            "Confidence",
            "Files/modules likely affected",
            "Reasoning",
            "Complexity Scoring Version",
            "Repository Commit",
        ):
            self.assert_bold_label(
                body,
                label,
                bullet=label in {"Files/modules likely affected", "Reasoning"},
            )

    def test_completed_comment_bolds_jev_key_values(self) -> None:
        report = format_jev_markdown(
            [
                DecisionResult(
                    decision_type="TASK_CLASSIFICATION",
                    decision="FEATURE",
                    confidence=0.93,
                    scores={
                        "complexity": 0.5,
                        "crossRepoProbability": 0.1,
                        "securityRisk": 0.0,
                    },
                    metadata={"ragScope": "LOCAL"},
                    source="jev",
                    latency_ms=250,
                    estimated_cost=0.001,
                )
            ],
            router_complexity=6,
        )
        worker = Worker.__new__(Worker)
        worker.render_jev_report = lambda: ""

        body = worker.render_pending_comment(
            {
                "ai": "Codex",
                "model": "gpt-test",
                "effort": "high",
                "commit_sha": "b" * 40,
                "commit_message": "Bold nested reports (#373)",
                "ai_usage_report": " ",
                "jev_report": report,
                "ai_output": "Completed.",
            }
        )

        for label in (
            "Task",
            "Complexity",
            "Cross-repository likelihood",
            "Security sensitivity",
            "Recommended context",
            "Confidence",
        ):
            self.assert_bold_label(body, label, bullet=True)
        for label in ("Jev calls", "Total decision latency", "Estimated cost"):
            self.assert_bold_label(body, label)


if __name__ == "__main__":
    unittest.main()
