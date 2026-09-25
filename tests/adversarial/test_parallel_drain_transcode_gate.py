"""Issue #219: fixing the cross-repo cycle barrier must not create a new way
to violate the pre-existing "defer AI and build work while a SWARM media
transcode is active" gate.

Before this issue, every scheduling path -- the default sequential loop, and
the old --parallel-repos `executor.map()` barrier -- rechecked
`transcode_active()` once per full cycle across all repos, before doing any
of that cycle's work (`Runner.run`, ~line 916). The fix for #219 replaces
that barrier with a persistent per-repo thread (`Runner._parallel_repo_loop`)
that, in continuous mode, drains a repository's ready queue immediately after
each completed issue without ever returning control to the supervisor
between issues. That inner drain loop never calls `self.transcode_active()`
itself, and the supervisor's own transcode check (`run_parallel_repos` ->
`defer_for_transcode`) only runs when deciding whether to *start* a new
wake-up tick -- not while a repository already mid-drain keeps calling
`work_repo()` in a tight loop on its own thread.

Net effect: once a repository starts draining a backlog (exactly the
scenario issue #219 optimizes for), a transcode that begins mid-drain no
longer defers that repository's build work, unlike every other scheduling
path in this file. This is a regression against a documented invariant
("A SWARM media transcode is active; deferring AI and build work..."), not
a new feature request, so it is in scope for this issue's fix.

This test drives `Runner.run_parallel_repos` directly (the same entry point
`Runner.run()` uses for --parallel-repos in continuous mode) with two repos:
"alpha" has an endless supply of ready issues (its mocked worker always
returns ISSUE_COMPLETED_EXIT_CODE), "beta" has none. A fake
`transcode_active()` starts returning True once alpha's mocked worker has
been called 3 times, simulating a transcode starting mid-drain. A correct
implementation must recheck the gate between drain iterations and stop
calling alpha's worker at (or immediately after) that point, the same way
the sequential and --once parallel paths already do. The mocked worker also
carries an unconditional safety cap (independent of the assertions) so the
test cannot hang even if the drain truly is unbounded.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import install_swarm_issue_cron as runner_module  # noqa: E402
import test_swarm_issue_worker as fixtures  # noqa: E402

# How many mocked issues alpha must drain before the fake transcode engages.
ENGAGE_AFTER_CALLS = 3
# Unconditional safety cap, independent of any assertion below: guarantees
# the background supervisor thread cannot spin forever even if the drain
# loop truly never re-checks the transcode gate.
SAFETY_CAP_CALLS = 5000


class ParallelDrainRespectsTranscodeGateTests(unittest.TestCase):
    def test_continuous_drain_stops_after_a_transcode_starts_mid_backlog(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-adversarial-transcode-drain.") as temporary:
            root = Path(temporary)
            repos_file = fixtures.RunnerTestCase._repos_file(root, ("alpha", "beta"))
            args = runner_module.build_parser().parse_args(
                [
                    "--repos-file", str(repos_file), "--state-dir", str(root / "state"),
                    "--parallel-repos", "--interval-seconds", "600", "--pgrep-bin", "",
                ]
            )
            runner = runner_module.Runner(args, [])
            self.assertTrue(runner.parallel_repos)
            self.assertEqual(runner.args.schedule_mode, "continuous")

            lock = threading.Lock()
            alpha_calls = 0
            reached_engage_threshold = threading.Event()

            def fake_transcode_active() -> bool:
                with lock:
                    return alpha_calls >= ENGAGE_AFTER_CALLS

            def fake_run_worker(repo: dict[str, object], _password: str, _prefix: str = "") -> int:
                nonlocal alpha_calls
                if str(repo["label"]) != "alpha":
                    # beta has nothing queued; work_repo treats 0 as idle.
                    return 0
                with lock:
                    alpha_calls += 1
                    count = alpha_calls
                if count == ENGAGE_AFTER_CALLS:
                    reached_engage_threshold.set()
                if count >= SAFETY_CAP_CALLS:
                    runner.stop_requested = True
                return runner_module.ISSUE_COMPLETED_EXIT_CODE

            supervisor = threading.Thread(
                target=runner.run_parallel_repos,
                kwargs={"start_immediately": True},
            )
            with (
                mock.patch.object(runner, "synchronize_repository", return_value=True),
                mock.patch.object(runner, "run_worker", side_effect=fake_run_worker),
                mock.patch.object(runner, "prune_cargo_target"),
                mock.patch.object(runner, "transcode_active", side_effect=fake_transcode_active),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                supervisor.start()
                self.assertTrue(
                    reached_engage_threshold.wait(5),
                    "alpha's mocked worker never reached the call count needed to "
                    "engage the fake transcode; the drain never started.",
                )
                # Grace window: give the drain loop every opportunity to make (or
                # correctly refuse to make) additional calls once the fake
                # transcode is already engaged, without depending on the drain
                # loop itself ever re-checking the gate to end the test.
                time.sleep(0.5)
                with lock:
                    calls_after_grace = alpha_calls
                runner.stop_requested = True
                supervisor.join(10)

            self.assertFalse(supervisor.is_alive(), "the supervisor thread did not stop")
            self.assertLessEqual(
                calls_after_grace,
                ENGAGE_AFTER_CALLS + 1,
                "alpha's worker kept being called "
                f"({calls_after_grace} calls total, allowing for one already in-flight) "
                f"well after transcode_active() started returning True at call "
                f"{ENGAGE_AFTER_CALLS}; a repository already mid-drain must recheck "
                "and honor the transcode gate between issues, the same way the "
                "sequential and --once parallel scheduling paths already do between "
                "cycles.",
            )


if __name__ == "__main__":
    unittest.main()
