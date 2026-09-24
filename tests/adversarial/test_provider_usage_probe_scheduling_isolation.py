"""Issue #242 acceptance: one broken quota probe cannot cancel a fresh run.

Provider usage is only an input to fresh-work selection. If a provider's
probe raises unexpectedly, that provider must be unavailable for the current
pass, but later enabled probes must still run and the healthy provider with
the most remaining quota must receive the selected issue.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import test_swarm_issue_worker as fixtures  # noqa: E402
from swarm_issue_worker import IssueContext, ProviderUsage  # noqa: E402


class ProviderUsageProbeSchedulingIsolationTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def test_first_probe_failure_still_probes_later_providers_and_schedules_healthiest(self) -> None:
        self.worker.config = dataclasses.replace(self.worker.config, dry_run=True)
        issue = IssueContext(
            242,
            "Isolate provider quota probe failures",
            "",
            [],
            "https://example.invalid/issues/242",
        )
        calls: list[str] = []

        def claude_usage() -> ProviderUsage:
            calls.append("Claude")
            raise RuntimeError("temporary quota endpoint failure")

        def codex_usage() -> ProviderUsage:
            calls.append("Codex")
            return ProviderUsage(0, 81.0)

        def grok_usage() -> ProviderUsage:
            calls.append("Grok")
            return ProviderUsage(0, 63.0)

        with (
            mock.patch("swarm_issue_worker.command_available", return_value=True),
            mock.patch.object(self.worker, "deliver_pending"),
            mock.patch.object(self.worker, "reconcile_issue_pull_requests"),
            mock.patch.object(self.worker, "reconcile_orphan_issue_branches"),
            mock.patch.object(self.worker, "prepare_paused_resume", return_value=False),
            mock.patch.object(self.worker, "monitor_repository_actions", return_value=None),
            mock.patch.object(self.worker, "select_issue", return_value=issue),
            mock.patch.object(self.worker, "claude_usage", side_effect=claude_usage),
            mock.patch.object(self.worker, "codex_usage", side_effect=codex_usage),
            mock.patch.object(self.worker, "grok_usage", side_effect=grok_usage),
            mock.patch.object(self.worker, "ensure_bot_auth"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(self.worker.run(), 0)

        self.assertEqual(calls, ["Claude", "Codex", "Grok"])
        self.assertEqual(self.worker.provider_usages["Claude"], ProviderUsage(2))
        self.assertEqual(self.worker.choice.name, "Codex")
        self.assertIn(
            "Claude quota unavailable: usage probe failed: temporary quota endpoint failure",
            output.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
