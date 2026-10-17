"""Issue #465: completion of auto-filed adversarial findings closes the issue.

The issue requires a delivered completion to leave a receipt, then close findings
created by the adversarial agents without applying Ready For Testing. Ordinary
issues retain the human-testing label and remain open. These tests exercise the
pending-delivery recovery boundary with deterministic GitHub responses.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import test_swarm_issue_worker as fixtures  # noqa: E402
from swarm_issue_worker import IssueContext, WorkerError  # noqa: E402


class AdversarialFindingCompletionTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def write_pending(self, **overrides):
        pending = {
            "issue_number": 465,
            "issue_title": "Finding from adversarial UAT",
            "issue_url": "https://example.invalid/issues/465",
            "ai_tool": "claude",
            "commit_sha": "a" * 40,
            "commit_message": "Fix finding",
            "github_comment_posted": False,
            "ready_for_testing_label_added": False,
            "close_on_completion": True,
            "pull_request_url": "https://example.invalid/pull/466",
            **overrides,
        }
        self.worker.pending_file.write_text(json.dumps(pending), encoding="utf-8")
        return pending

    def test_pending_delivery_posts_completion_then_closes_without_ready_label(self):
        pending = self.write_pending()
        calls = []

        def gh(args, provider=None, body=None):
            calls.append((args, provider, body))
            return ""

        with (
            mock.patch.object(self.worker.github, "gh", side_effect=gh),
            mock.patch.object(self.worker, "render_pending_comment", return_value="<!-- completion -->\nCompleted."),
            mock.patch.object(self.worker, "comments", return_value=[]),
            mock.patch.object(self.worker, "record_completed"),
            mock.patch.object(self.worker, "clear_in_progress"),
        ):
            self.worker.deliver_pending()

        self.assertEqual([item[0][:2] for item in calls], [["issue", "comment"], ["issue", "close"]])
        self.assertEqual(calls[0][2], "<!-- completion -->\nCompleted.")
        self.assertEqual(calls[1][1], "claude")
        self.assertNotIn("--add-label", calls[1][0])
        self.assertFalse(self.worker.pending_file.exists())

    def test_close_failure_keeps_pending_checkpoint_and_retry_does_not_duplicate_comment(self):
        self.write_pending()
        calls = []

        def gh(args, provider=None, body=None):
            calls.append(args[:2])
            if args[:2] == ["issue", "close"] and calls.count(["issue", "close"]) == 1:
                raise WorkerError("temporary GitHub failure")
            return ""

        with (
            mock.patch.object(self.worker.github, "gh", side_effect=gh),
            mock.patch.object(self.worker, "render_pending_comment", return_value="<!-- completion -->\nCompleted."),
            mock.patch.object(self.worker, "comments", return_value=[]),
            mock.patch.object(self.worker, "record_completed") as completed,
            mock.patch.object(self.worker, "clear_in_progress"),
        ):
            with self.assertRaisesRegex(WorkerError, "temporary GitHub failure"):
                self.worker.deliver_pending()
            saved = json.loads(self.worker.pending_file.read_text(encoding="utf-8"))
            self.assertTrue(saved["github_comment_posted"])
            self.assertFalse(saved.get("issue_closed", False))
            completed.assert_not_called()

            self.worker.deliver_pending()

        self.assertEqual(calls.count(["issue", "comment"]), 1)
        self.assertEqual(calls.count(["issue", "close"]), 2)
        completed.assert_called_once_with(465)
        self.assertFalse(self.worker.pending_file.exists())

    def test_finding_labels_select_closure_but_protected_and_normal_issues_stay_open(self):
        for source_label in ("adversarial-uat", "Adversarial-Security"):
            self.worker.issue = IssueContext(465, "Finding", "Body", [source_label], "https://example.invalid/465")
            self.assertTrue(self.worker.created_by_adversarial_agent())

        for labels in ([], ["adversarial-protected"], ["bug", "ready-for-testing"]):
            self.worker.issue = IssueContext(466, "Human issue", "Body", labels, "https://example.invalid/466")
            self.assertFalse(self.worker.created_by_adversarial_agent())
            pending = {"issue_number": 466, "ai_tool": "codex", "close_on_completion": False}
            with mock.patch.object(self.worker.github, "gh", return_value="") as gh:
                result = self.worker.add_pending_label(pending)
            self.assertTrue(result["ready_for_testing_label_added"])
            self.assertEqual(gh.call_args.args[0][:2], ["issue", "edit"])
            self.assertIn("--add-label", gh.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
