"""Independent Issue #465 boundaries: in-flight pending files, retries, and pauses.

Contract derived from the issue before reading the patch: a completed issue
created by an adversarial agent is closed instead of marked Ready For Testing,
and an issue that is still waiting on a person stays open. A worker upgrade can
resume a delivery that an earlier build wrote to the pending file, so that
resumed completion must follow the same contract. GitHub is a local fixture; no
network service or AI CLI is invoked.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

WORKER_DIR = Path(__file__).resolve().parents[2] / "issue_worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

import test_swarm_issue_worker as fixtures
from swarm_issue_worker import IssueContext, ProviderChoice, WorkerError


class LegacyPendingAndPauseTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def install_github(self, labels):
        """Serve the issue's labels from GitHub and record every mutating call."""
        self.issue_state = "OPEN"
        self.issue_labels = list(labels)
        self.mutations = []

        def gh(args, provider=None, body=None):
            action = tuple(args[:2])
            if action == ("issue", "view"):
                return json.dumps({"labels": [{"name": name} for name in self.issue_labels]})
            if action in {("issue", "close"), ("issue", "edit"), ("issue", "comment"), ("label", "create")}:
                self.mutations.append((list(args), provider))
                if action == ("issue", "close"):
                    self.issue_state = "CLOSED"
                return ""
            raise WorkerError(f"fixture GitHub has no answer for {args!r}")

        patcher = mock.patch.object(self.worker.github, "gh", side_effect=gh)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pending_written_before_upgrade_still_closes_adversarial_issue(self):
        # Written by a build that predates close_on_completion; the issue is an
        # adversarial finding on GitHub, so completion must close it.
        self.install_github(["bug", "adversarial-uat"])
        legacy = {
            "issue_number": 465101,
            "ai_tool": "claude",
            "ready_for_testing_label_added": False,
            "github_comment_posted": True,
        }
        self.worker.add_pending_label(legacy)

        self.assertEqual(self.issue_state, "CLOSED")
        self.assertFalse(
            any("--add-label" in args for args, _ in self.mutations),
            "An adversarial finding must not be marked Ready For Testing",
        )

    def test_close_persisted_before_crash_is_not_repeated_on_retry(self):
        self.install_github(["adversarial-security"])
        persisted = {
            "issue_number": 465102,
            "ai_tool": "grok",
            "close_on_completion": True,
            "issue_closed": True,
        }
        result = self.worker.add_pending_label(persisted)

        self.assertTrue(result["issue_closed"])
        self.assertEqual(self.mutations, [])

    def test_needs_input_on_adversarial_issue_stays_open_and_is_not_ready(self):
        self.install_github(["bug", "adversarial-uat"])
        self.worker.issue = IssueContext(
            465103, "Needs a credential", "Body", ["bug", "adversarial-uat"],
            "https://example.invalid/issues/465103",
        )
        self.worker.choice = ProviderChoice("Claude", "fixture-model", "high", "fixture-session")

        with mock.patch.object(self.worker, "comments", return_value=[]), \
                mock.patch.object(self.worker, "usage_snapshot", return_value=None):
            self.worker.finalize_needs_input(
                "## Summary\nA sign-in is required.\n## Action required\nProvide access."
            )

        self.assertEqual(self.issue_state, "OPEN")
        self.assertFalse(
            any(args[:2] == ["issue", "close"] for args, _ in self.mutations),
            "Paused work is not completion and must not close the issue",
        )
        self.assertFalse(
            any("--add-label" in args and self.worker.config.ready_label in args
                for args, _ in self.mutations),
            "Paused work must not be marked Ready For Testing",
        )


if __name__ == "__main__":
    unittest.main()
