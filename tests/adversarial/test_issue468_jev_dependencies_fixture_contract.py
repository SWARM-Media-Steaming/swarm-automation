"""Issue #468: Jev worker fixtures carry the IssueContext dependency field."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER = ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

from decision_engine import DecisionResult, DecisionType, Source  # noqa: E402
from jev_cli import JevSettings  # noqa: E402
from swarm_issue_worker import Worker  # noqa: E402


class JevDependencyFixtureContractTests(unittest.TestCase):
    def test_evaluation_and_partial_retry_receive_issue_dependencies(self) -> None:
        dependencies = [{"label": "#41", "title": "Prerequisite", "files": ["core.py"]}]
        worker = object.__new__(Worker)
        worker.issue = SimpleNamespace(
            number=468, title="Dependency context", labels=[], body="Implement safely.",
            dependencies=dependencies,
        )
        worker.config = SimpleNamespace(
            jev=JevSettings(enabled=True), github_repository="example/repo",
            adversarial_uat_enabled=False, adversarial_security_enabled=False,
        )
        observed = []

        class Engine:
            def evaluate(self, decision_type, payload):
                observed.append(payload["issue_context"])
                return DecisionResult(
                    decision_type=decision_type, decision="FEATURE", confidence=0.9,
                    source=Source.JEV.value,
                )

        class History:
            execution_id = "fixture"
            def record_jev_decision(self, _record):
                pass

        worker._decision_engine = Engine()
        worker.history = History()
        worker.evaluate_decision(DecisionType.TASK_CLASSIFICATION.value)
        self.assertEqual(observed[0]["metadata"]["dependencies"], 1)
        self.assertIn("#41 Prerequisite", observed[0]["text"])
        self.assertIn("core.py", observed[0]["text"])

        from issue_context import build_issue_context, IssueContextSettings

        partial = build_issue_context("x" * 5000, IssueContextSettings(100, 80, 100), dependencies=dependencies)
        low = DecisionResult(
            decision_type=DecisionType.TASK_CLASSIFICATION.value, decision="UNKNOWN",
            confidence=0.1, source=Source.LOW_CONFIDENCE.value,
        )
        retried = Worker.expand_partial_context(
            worker, low, DecisionType.TASK_CLASSIFICATION.value, {"issue_context": partial}
        )
        self.assertEqual(retried.source, Source.JEV.value)
        self.assertEqual(observed[1]["metadata"]["dependencies"], 1)
        self.assertIn("#41 Prerequisite", observed[1]["text"])


if __name__ == "__main__":
    unittest.main()
