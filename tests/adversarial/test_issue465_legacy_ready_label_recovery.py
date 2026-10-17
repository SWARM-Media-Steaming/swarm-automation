"""Issue #465: recover legacy pending deliveries with a stale ready label."""

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
from swarm_issue_worker import WorkerError


class LegacyReadyLabelRecoveryTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def test_legacy_checkpoint_with_ready_label_still_closes_agent_finding(self):
        """A label-added bit cannot hide the origin of an unfinished delivery."""
        calls = []
        remote_labels = ["bug", "adversarial-uat", self.worker.config.ready_label]

        def gh(args, provider=None, body=None):
            calls.append(list(args))
            if args[:2] == ["issue", "view"]:
                return json.dumps({"labels": [{"name": name} for name in remote_labels]})
            if args[:2] == ["issue", "close"]:
                return ""
            if args[:2] == ["issue", "edit"]:
                if "--remove-label" in args:
                    remote_labels.remove(args[args.index("--remove-label") + 1])
                return ""
            raise WorkerError(f"unexpected fixture command: {args!r}")

        legacy_pending = {
            "issue_number": 465104,
            "ai_tool": "claude",
            "github_comment_posted": True,
            "ready_for_testing_label_added": True,
            # This is an old completion record, before origin was persisted.
        }
        with mock.patch.object(self.worker.github, "gh", side_effect=gh):
            result = self.worker.add_pending_label(legacy_pending)

        self.assertTrue(
            any(args[:2] == ["issue", "close"] for args in calls),
            "Origin must be recovered even when an old checkpoint says Ready For Testing was added",
        )
        self.assertFalse(
            any(args[:2] == ["issue", "edit"] and "--add-label" in args for args in calls),
            "Recovery must not newly mark an adversarial finding Ready For Testing",
        )
        self.assertNotIn(self.worker.config.ready_label, remote_labels)
        self.assertTrue(result["close_on_completion"])
        self.assertTrue(result["issue_closed"])


if __name__ == "__main__":
    unittest.main()
