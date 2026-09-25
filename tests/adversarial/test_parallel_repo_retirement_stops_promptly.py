"""Issue #219: removing a repository from the live repos file while its
persistent worker thread is mid-issue must stop that thread promptly once
the issue finishes, and must not disturb a surviving sibling's independent
progress or hang supervisor shutdown.

`Runner._sync_parallel_repo_workers` (issue_worker/install_swarm_issue_cron.py)
is new code introduced by #219's persistent-per-repo-thread redesign. It
retires a repository the desktop app removed from --repos-file (e.g. the
user disables or deletes a repository while the scheduler is running):

    for key in set(active) - current:
        state = active.pop(key)
        state.retired = True
        state.wake.set()

Every existing parallel-repos test either keeps the repository set fixed for
the whole run, or exercises `reload_repos()`'s raw list mutation directly
(`test_scheduler_picks_up_repositories_added_after_it_started`) without ever
driving the actual thread-lifecycle path
(`_sync_parallel_repo_workers` / `_parallel_repo_loop`) that has to notice a
repository disappeared out from under a live worker thread and stop calling
it -- including the harder case where removal lands while that thread is
already inside a `work_repo` call, since `state.retired` can only take
effect at the next loop boundary, not by interrupting in-flight work.

This test starts two repos ("drop", "keep") under `run_parallel_repos`,
blocks "drop"'s first call to simulate it being busy at the moment the
repos file is rewritten to remove it, rewrites the file and asks for an
immediate recheck (the same "Run now" file the desktop app uses), then
releases "drop". A correct implementation must not call "drop" a second
time, must keep "keep" progressing through its own independent backlog the
entire time, and must let the supervisor shut down cleanly afterward.
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


class ParallelRepoRetirementStopsPromptlyTests(unittest.TestCase):
    def test_removing_a_busy_repository_stops_it_without_disturbing_a_sibling(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-adversarial-retirement.") as temporary:
            root = Path(temporary)
            repos_file = fixtures.RunnerTestCase._repos_file(root, ("drop", "keep"))
            args = runner_module.build_parser().parse_args(
                [
                    "--repos-file", str(repos_file), "--state-dir", str(root / "state"),
                    "--parallel-repos", "--interval-seconds", "600", "--pgrep-bin", "",
                ]
            )
            runner = runner_module.Runner(args, [])
            self.assertTrue(runner.parallel_repos)

            drop_busy = threading.Event()
            release_drop = threading.Event()
            keep_drained_once = threading.Event()
            calls: dict[str, int] = {"drop": 0, "keep": 0}
            lock = threading.Lock()

            def work_repo(repo: dict[str, object]) -> int:
                label = str(repo["label"])
                with lock:
                    calls[label] += 1
                    call = calls[label]
                if label == "drop":
                    drop_busy.set()
                    release_drop.wait(5)
                    return 0
                if call <= 2:
                    return runner_module.ISSUE_COMPLETED_EXIT_CODE
                keep_drained_once.set()
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
                    drop_busy.wait(2), "the 'drop' repository should have started its first issue"
                )
                self.assertTrue(
                    keep_drained_once.wait(2),
                    "the 'keep' repository should independently drain its own backlog while "
                    "'drop' is still busy",
                )

                # The desktop app rewrites the file to remove "drop" while its worker
                # is still mid-issue, then the user asks the running scheduler for an
                # immediate recheck -- the same mechanism "Run now" uses.
                fixtures.RunnerTestCase._repos_file(root, ("keep",))
                runner.run_now_path.parent.mkdir(parents=True, exist_ok=True)
                runner.run_now_path.touch()

                with lock:
                    keep_calls_before_removal = calls["keep"]

                # Give the supervisor's poll loop (every 0.2s) room to notice the
                # request, reload the repos file, and mark "drop" retired while it is
                # still blocked in its one in-flight call.
                import time as _time

                _time.sleep(1.0)

                # Now let "drop"'s in-flight call finish. A correct implementation
                # must not invoke it again afterward.
                release_drop.set()
                _time.sleep(1.0)

                with lock:
                    drop_calls_final = calls["drop"]
                    keep_calls_final = calls["keep"]

                runner.stop_requested = True
                supervisor.join(5)

            self.assertFalse(supervisor.is_alive(), "the supervisor thread did not stop")
            self.assertEqual(
                drop_calls_final, 1,
                "'drop' was removed from the repos file while its one in-flight call was "
                "still running; it must not be invoked again once that call finishes",
            )
            self.assertGreater(
                keep_calls_final, keep_calls_before_removal,
                "'keep' must keep making independent progress across the removal of its "
                "sibling, proving the retirement of one repository does not disturb another",
            )


if __name__ == "__main__":
    unittest.main()
