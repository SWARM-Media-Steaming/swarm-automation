"""Issue #294 acceptance test for the adversarial fix/re-test cap.

The issue defines round zero as the independent assessment.  A continually
failing acceptance test must therefore get exactly three counted repair rounds
(and four tester invocations total), then be delivered as best effort with
the unresolved notes handed to a follow-up issue.  This runs the durable UAT pipeline against a local checkout:
providers and GitHub are the only fakes, while each registered acceptance suite
is a real subprocess.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import adversarial_core  # noqa: E402
import adversarial_uat as uat  # noqa: E402
import test_swarm_issue_worker as fixtures  # noqa: E402
from ai_execution_history import ExecutionHistoryService  # noqa: E402
from swarm_issue_worker import IssueContext, ProviderChoice, ProviderUsage  # noqa: E402


class ThreeRoundCapTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self) -> None:
        self.worker.config = dataclasses.replace(
            self.worker.config,
            adversarial_uat_enabled=True,
            auto_approve=True,
            auto_merge=True,
            auto_promote=True,
            ai_execution_history_enabled=True,
            execution_history_db=self.state / "history.sqlite3",
        )
        self.worker.history = ExecutionHistoryService(True, self.state / "history.sqlite3")
        self.worker.issue = IssueContext(
            294, "Limit adversarial repair rounds", "Stop after three counted repair rounds.",
            [], "https://example.invalid/issues/294",
        )
        self.worker.choice = ProviderChoice("Claude", "fixture-model", "high", "implementer-session")
        self.git("switch", "-c", "ai/claude/issue-294")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()
        (self.repo / "tracked.txt").write_text("broken\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "[claude] Implementation (#294)")
        self.worker.initialize_adversarial(self.git("rev-parse", "HEAD"), "## Summary\nImplementation")
        self.calls: list[tuple[str, int]] = []
        self.comments: list[str] = []

    def add_failing_suite(self) -> None:
        tests = self.repo / "tests/adversarial"
        tests.mkdir(parents=True, exist_ok=True)
        test_file = tests / "test_issue294_fixture.py"
        test_file.write_text(
            "import unittest\nfrom pathlib import Path\n\n"
            "class Requirement(unittest.TestCase):\n"
            "    def test_implementation_is_repaired(self):\n"
            "        self.assertEqual(Path('tracked.txt').read_text().strip(), 'fixed')\n",
            encoding="utf-8",
        )
        definition = uat.read_definition(self.repo)
        definition["suites"] = [suite for suite in definition["suites"] if suite.get("id") != "adversarial-294-cap"]
        definition["suites"].append({
            "id": "adversarial-294-cap", "name": "Issue #294 persistent failure fixture",
            "origin": "adversarial", "enabled": True, "disruptive": False,
            "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests/adversarial", "-p", test_file.name],
            "timeoutSeconds": 20,
        })
        (self.repo / ".swarm/tests.json").write_text(json.dumps(definition), encoding="utf-8")

    def role(self, prompt: str, activity: str = "") -> int:
        loop = self.worker.read_state()["adversarial"]
        self.calls.append((loop["phase"], loop["round"]))
        if loop["phase"] == "test":
            if not (self.repo / "tests/adversarial/test_issue294_fixture.py").exists():
                self.add_failing_suite()
            self.worker.ai_output_file.write_text(
                uat.RESULT_MARKER + ' {"dispute_resolution": "", "out_of_scope": []}',
                encoding="utf-8",
            )
        else:
            # Simulate a fixer that completes normally but leaves the acceptance
            # failure intact; this is the boundary that must hit the cap.
            self.worker.ai_output_file.write_text("Attempted repair did not satisfy the test.\n", encoding="utf-8")
        return 0

    def gh(self, args, provider=None, body=None):
        if args[:2] in (["pr", "list"], ["issue", "list"]):
            return "[]"
        if args[:2] == ["pr", "create"]:
            return "https://example.invalid/pull/294"
        if args[:2] == ["issue", "create"]:
            return "https://example.invalid/issues/295"
        if args[:2] == ["issue", "comment"]:
            self.comments.append(body)
        return ""

    def test_persistent_failure_has_three_repairs_then_best_effort_followup(self) -> None:
        self.prepare()
        with (
            mock.patch.object(self.worker, "provider_usage", return_value=ProviderUsage(0, 80)),
            mock.patch.object(self.worker, "ensure_bot_auth"),
            mock.patch.object(self.worker, "comments", return_value=[]),
            mock.patch.object(self.worker, "run_ai", side_effect=self.role),
            mock.patch.object(self.worker.github, "gh", side_effect=self.gh),
            mock.patch.object(self.worker, "minor_bump_requested_by_trusted_user", return_value=False),
            mock.patch.object(self.worker, "approve_pull_request") as approve,
            mock.patch.object(self.worker, "merge_pull_request") as merge,
            mock.patch.object(self.worker, "auto_promote_integration_branch") as promote,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            merge.return_value = self.git("rev-parse", "HEAD")
            self.assertEqual(self.worker.run_adversarial_delivery(), 10)

        self.assertEqual(adversarial_core.MAX_ROUNDS, 3)
        self.assertEqual(uat.MAX_ROUNDS, adversarial_core.MAX_ROUNDS)
        self.assertEqual(
            self.calls,
            [("test", 0), ("fix", 1), ("test", 1), ("fix", 2), ("test", 2), ("fix", 3), ("test", 3)],
        )
        approve.assert_called_once()
        merge.assert_called_once()
        promote.assert_called_once()
        self.assertEqual(len(self.comments), 1)
        self.assertIn("did not pass after three fix/re-test rounds", self.comments[0])
        self.assertIn("Follow-up issue: https://example.invalid/issues/295", self.comments[0])
        self.assertNotIn("AI needs your input", self.comments[0])
        self.assertIn("starting fix/re-test round 3 of 3.", stdout.getvalue())
        self.assertNotIn("starting fix/re-test round 4", stdout.getvalue())

        row = self.worker.history.repository.for_repository(self.worker.config.github_repository)[0]
        self.assertEqual(
            (row["adversarial_round_count"], row["adversarial_outcome"], row["final_status"]),
            (3, "cap_hit", "completed"),
        )


if __name__ == "__main__":
    unittest.main()
