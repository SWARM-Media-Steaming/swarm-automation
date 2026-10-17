"""Issue #468: canonical issue fixtures default to no prerequisites."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER = ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

from decision_engine import DecisionResult, DecisionType, Source  # noqa: E402
from jev_cli import JevSettings  # noqa: E402
from swarm_issue_worker import IssueContext, Worker  # noqa: E402


class DefaultDependencyContextTests(unittest.TestCase):
    def test_default_issue_context_reaches_initial_and_expanded_jev_requests(self) -> None:
        # IssueContext is the worker's fixture/model contract. Omitting the
        # optional dependencies argument means the issue has no prerequisites.
        issue = IssueContext(
            number=468,
            title="Default prerequisite context",
            body=(
                "Implement the requested behavior without inventing prerequisites.\n"
                + ("Background context that is intentionally long. " * 80)
            ),
            labels=[],
            url="https://example.invalid/issues/468",
        )
        self.assertEqual(issue.dependencies, [])

        worker = object.__new__(Worker)
        worker.issue = issue
        worker.config = type("Config", (), {
            "jev": JevSettings(
                enabled=True,
                context_max_raw_chars=100,
                context_max_summary_chars=60,
                context_max_excerpt_chars=100,
            ),
            "github_repository": "example/repo",
            "adversarial_uat_enabled": False,
            "adversarial_security_enabled": False,
        })()
        observed_contexts = []

        class Engine:
            def evaluate(self, decision_type, payload):
                observed_contexts.append(payload["issue_context"])
                if len(observed_contexts) == 1:
                    return DecisionResult(
                        decision_type=decision_type,
                        decision="UNKNOWN",
                        confidence=0.2,
                        source=Source.LOW_CONFIDENCE.value,
                    )
                return DecisionResult(
                    decision_type=decision_type,
                    decision="FEATURE",
                    confidence=0.9,
                    source=Source.JEV.value,
                )

        class History:
            execution_id = "issue468-default-dependencies"

            def record_jev_decision(self, _record):
                pass

        worker._decision_engine = Engine()
        worker.history = History()
        result = worker.evaluate_decision(DecisionType.TASK_CLASSIFICATION.value)

        self.assertEqual(result["source"], Source.JEV.value)
        self.assertEqual(len(observed_contexts), 2)
        for context in observed_contexts:
            self.assertEqual(context["metadata"]["dependencies"], 0)
            self.assertNotIn("Prerequisite issues already merged", context["text"])
        self.assertTrue(observed_contexts[1]["metadata"]["expandedRetry"])


if __name__ == "__main__":
    unittest.main()
