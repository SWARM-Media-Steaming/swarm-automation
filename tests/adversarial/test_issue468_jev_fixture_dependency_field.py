"""Issue #468 regression checks for Jev worker issue fixture shape."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class JevFixtureDependencyFieldTests(unittest.TestCase):
    def test_issue_context_defaults_dependencies_to_an_empty_list(self) -> None:
        import sys

        issue_worker = str(ROOT / "issue_worker")
        if issue_worker not in sys.path:
            sys.path.insert(0, issue_worker)
        from swarm_issue_worker import IssueContext

        issue = IssueContext(number=468, title="fixture contract", body="", labels=[], url="")

        self.assertEqual(issue.dependencies, [])

    def test_jev_ingestion_worker_fixtures_explicitly_supply_dependencies(self) -> None:
        """SimpleNamespace does not inherit IssueContext's dataclass defaults."""
        path = ROOT / "tests/adversarial/test_issue337_jev_context_ingestion.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        issue_fixtures: list[ast.Call] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            assigned_to_worker_issue = any(
                isinstance(target, ast.Attribute)
                and target.attr == "issue"
                and isinstance(target.value, ast.Name)
                and target.value.id == "worker"
                for target in node.targets
            )
            value = node.value
            if (
                assigned_to_worker_issue
                and isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "SimpleNamespace"
            ):
                issue_fixtures.append(value)

        self.assertTrue(issue_fixtures, "expected to find Jev worker issue fixtures")
        missing = [
            fixture.lineno
            for fixture in issue_fixtures
            if not any(keyword.arg == "dependencies" for keyword in fixture.keywords)
        ]
        self.assertEqual(
            missing,
            [],
            "SimpleNamespace issue fixtures must explicitly provide dependencies; "
            "they do not inherit IssueContext's dataclass default",
        )


if __name__ == "__main__":
    unittest.main()
