"""Issue #293 acceptance coverage for an exhausted provider at a 0% reserve.

The configuration's minimum is a *reserve*, not permission to invoke an
account with no headroom.  Therefore a zero reserve admits the smallest
strictly positive remaining value, but an exact 0% window is unavailable.
That distinction has to hold both while selecting fresh work and while
revisiting an already-pinned worker session; otherwise the latter can invoke
the exhausted CLI again on every scheduler pass.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import test_swarm_issue_worker as fixtures  # noqa: E402
from swarm_issue_worker import (  # noqa: E402
    QUOTA_PAUSED_EXIT_CODE,
    Config,
    IssueContext,
    ProviderUsage,
    Worker,
    build_parser,
)


class ZeroQuotaNeverRetriesTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv
    paused_state = fixtures.WorkerTestCase.paused_state

    def zero_reserve_worker(self) -> Worker:
        args = build_parser().parse_args(
            self._worker_argv(auto=False)
            + [
                "--minimum-remaining-percent", "0",
                "--claude-minimum-remaining-percent", "0",
                "--codex-minimum-remaining-percent", "0",
                "--grok-minimum-remaining-percent", "0",
            ]
        )
        return Worker(Config.from_args(args))

    def test_every_provider_rejects_exactly_zero_but_codex_accepts_a_positive_fraction(self) -> None:
        worker = self.zero_reserve_worker()

        claude_output = json.dumps(
            {"result": "Current session: 100% used\nCurrent week: 10% used\n"}
        )
        with (
            mock.patch.object(worker, "provider_bin", return_value="/test/claude"),
            mock.patch("swarm_issue_worker.command_available", return_value=True),
            mock.patch(
                "swarm_issue_worker.run_command",
                return_value=subprocess.CompletedProcess(["claude"], 0, stdout=claude_output, stderr=""),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            claude = worker.claude_usage()

        codex = worker.codex_usage_from_limits(
            {"primary": {"usedPercent": 100.0}, "secondary": {"usedPercent": 20.0}}
        )
        positive_codex = worker.codex_usage_from_limits(
            {"primary": {"usedPercent": 99.999}, "secondary": {"usedPercent": 20.0}}
        )

        grok_home = self.root / "issue293-grok-home"
        (grok_home / ".grok").mkdir(parents=True)
        (grok_home / ".grok" / "auth.json").write_text("{}", encoding="utf-8")
        with (
            mock.patch.object(worker, "provider_bin", return_value="/test/grok"),
            mock.patch("swarm_issue_worker.command_available", return_value=True),
            mock.patch(
                "swarm_issue_worker.run_command",
                return_value=subprocess.CompletedProcess(
                    ["grok-rate-limits"], 0,
                    stdout=json.dumps({"usedPercent": 100.0, "period": "week"}), stderr="",
                ),
            ),
            mock.patch("swarm_issue_worker.time.sleep"),
            mock.patch.dict(os.environ, {"HOME": str(grok_home)}, clear=False),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            os.environ.pop("XAI_API_KEY", None)
            grok = worker.grok_usage()

        self.assertIsNotNone(codex)
        self.assertIsNotNone(positive_codex)
        for provider, usage in (("Claude", claude), ("Codex", codex), ("Grok", grok)):
            self.assertEqual(usage.status, 1, f"{provider} must reject an exhausted 0% window")
            self.assertEqual(usage.remaining_percent, 0.0)
        self.assertEqual(positive_codex.status, 0)
        self.assertGreater(positive_codex.remaining_percent or 0, 0)

    def test_pinned_session_at_exactly_zero_pauses_without_invoking_the_agent(self) -> None:
        worker = self.zero_reserve_worker()
        worker.issue = IssueContext(293, "Zero quota", "", [], "https://example.invalid/issues/293")
        state = self.paused_state(293)
        state["status"] = "active"
        worker.write_state(state)
        claude_output = json.dumps(
            {"result": "Current session: 100% used\nCurrent week: 10% used\n"}
        )

        def provider_usage(provider: str) -> ProviderUsage:
            if provider.lower() != "claude":
                return ProviderUsage(1, 0.0)
            return worker.claude_usage()

        with (
            mock.patch.object(worker, "provider_usage", side_effect=provider_usage),
            mock.patch.object(worker, "provider_bin", return_value="/test/claude"),
            mock.patch("swarm_issue_worker.command_available", return_value=True),
            mock.patch(
                "swarm_issue_worker.run_command",
                return_value=subprocess.CompletedProcess(["claude"], 0, stdout=claude_output, stderr=""),
            ),
            mock.patch.object(worker, "mark_quota_paused") as mark_paused,
            mock.patch.object(worker, "post_quota_comment") as post_pause,
            mock.patch.object(worker, "suspend_paused") as suspend,
            mock.patch.object(worker, "run_ai") as run_ai,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            status = worker.run_selected_issue()

        self.assertEqual(status, QUOTA_PAUSED_EXIT_CODE)
        mark_paused.assert_called_once_with()
        post_pause.assert_called_once_with()
        suspend.assert_called_once_with()
        run_ai.assert_not_called()


if __name__ == "__main__":
    unittest.main()
