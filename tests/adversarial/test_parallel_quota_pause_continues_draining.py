"""Issue #219: QUOTA_PAUSED_EXIT_CODE must drive the same immediate,
without-a-barrier recheck as ISSUE_COMPLETED_EXIT_CODE in continuous mode --
and must not stall a sibling repository either way.

`Runner._parallel_repo_loop` (issue_worker/install_swarm_issue_cron.py)
treats the two exit codes identically when deciding whether to keep
draining a repository without waiting for a fresh wake-up:

    status = self.work_repo(state.repo)
    if (
        self.args.schedule_mode == "continuous"
        and status in (ISSUE_COMPLETED_EXIT_CODE, QUOTA_PAUSED_EXIT_CODE)
    ):
        continue

The regression test the fix itself added for #219
(`test_parallel_repositories_continue_without_waiting_for_a_slow_sibling` in
issue_worker/test_swarm_issue_worker.py) only ever drives the
ISSUE_COMPLETED_EXIT_CODE half of that tuple. Nothing in the suite proves
the QUOTA_PAUSED_EXIT_CODE half actually behaves the same way: a repository
whose worker keeps returning "quota paused, session saved" should be
rechecked immediately (matching work_repo's own
"checking this repository again immediately" log line, issue #219's whole
point), not silently starved until some external tick, and a healthy
sibling must keep draining its own queue independently the entire time.

This test drives `Runner.run_parallel_repos` directly (the same entry point
`Runner.run()` uses for --parallel-repos in continuous mode) with two repos:
"quota" reports QUOTA_PAUSED_EXIT_CODE twice before blocking (standing in for
a provider that is still out of capacity), "fast" has an independent
backlog. A correct implementation lets "quota" be re-invoked twice without
any external wake, and lets "fast" finish its own backlog the entire time
"quota" is blocked.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import install_swarm_issue_cron as runner_module  # noqa: E402
import test_swarm_issue_worker as fixtures  # noqa: E402


class ParallelQuotaPauseContinuesDrainingTests(unittest.TestCase):
    def test_quota_paused_repository_is_rechecked_immediately_without_stalling_a_sibling(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-adversarial-quota-drain.") as temporary:
            root = Path(temporary)
            repos_file = fixtures.RunnerTestCase._repos_file(root, ("quota", "fast"))
            args = runner_module.build_parser().parse_args(
                [
                    "--repos-file", str(repos_file), "--state-dir", str(root / "state"),
                    "--parallel-repos", "--interval-seconds", "600", "--pgrep-bin", "",
                ]
            )
            runner = runner_module.Runner(args, [])
            self.assertTrue(runner.parallel_repos)
            self.assertEqual(runner.args.schedule_mode, "continuous")

            release_quota = threading.Event()
            quota_blocked = threading.Event()
            fast_drained = threading.Event()
            calls: dict[str, int] = {"quota": 0, "fast": 0}
            lock = threading.Lock()

            def work_repo(repo: dict[str, object]) -> int:
                label = str(repo["label"])
                with lock:
                    calls[label] += 1
                    call = calls[label]
                if label == "quota":
                    if call <= 2:
                        return runner_module.QUOTA_PAUSED_EXIT_CODE
                    quota_blocked.set()
                    release_quota.wait(5)
                    return 0
                if call <= 2:
                    return runner_module.ISSUE_COMPLETED_EXIT_CODE
                fast_drained.set()
                return 0

            supervisor = threading.Thread(
                target=runner.run_parallel_repos,
                kwargs={"start_immediately": True},
            )
            with (
                mock.patch.object(runner, "work_repo", side_effect=work_repo),
                mock.patch.object(runner, "transcode_active", return_value=False),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                supervisor.start()
                self.assertTrue(
                    quota_blocked.wait(2),
                    "the quota-paused repository should be rechecked immediately, twice, "
                    "without waiting for any external wake-up, before it genuinely has "
                    "nothing left to report",
                )
                self.assertTrue(
                    fast_drained.wait(2),
                    "the healthy sibling should drain its own backlog independently while "
                    "the quota-paused repository is still blocked",
                )
                with lock:
                    quota_calls, fast_calls = calls["quota"], calls["fast"]
                runner.stop_requested = True
                release_quota.set()
                supervisor.join(5)

            self.assertFalse(supervisor.is_alive(), "the supervisor thread did not stop")
            self.assertEqual(
                quota_calls, 3,
                "the quota-paused repository should have been invoked exactly twice via "
                "the immediate-recheck path plus the one in-flight blocking call",
            )
            self.assertEqual(
                fast_calls, 3,
                "the fast repository should have drained its own three-call backlog "
                "independently of its quota-paused sibling",
            )


if __name__ == "__main__":
    unittest.main()
