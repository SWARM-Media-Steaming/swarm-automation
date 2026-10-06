"""Hosted job launch: fresh clone, checkpoint handoff, and delivery order.

The worker itself is unchanged. ``job_launch`` only prepares an empty
workspace and copies checkpoints around ``SWARM_JOB_WORKER``.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIXTURE = ROOT / "web" / "worker" / "fixture_worker.py"
LAUNCH = HERE / "job_launch.py"
BRANCH = "ai/claude/issue-418"


def _run(command: list[str], cwd: Path | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise AssertionError(f"{command} failed: {detail}")


def _git(root: Path) -> Path:
    bare = root / "remote.git"
    seed = root / "seed"
    _run(["git", "init", "--bare", "-b", "main", str(bare)])
    _run(["git", "init", "-b", "main", str(seed)])
    _run(["git", "-C", str(seed), "config", "user.email", "seed@example.com"])
    _run(["git", "-C", str(seed), "config", "user.name", "Seed"])
    (seed / "README").write_text("seed\n", encoding="utf-8")
    _run(["git", "-C", str(seed), "add", "README"])
    _run(["git", "-C", str(seed), "commit", "-m", "seed"])
    _run(["git", "-C", str(seed), "remote", "add", "origin", str(bare)])
    _run(["git", "-C", str(seed), "push", "origin", "main"])
    return bare


def _wrapper(root: Path) -> Path:
    path = root / "run-fixture"
    path.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(sys.executable)
        + " "
        + shlex.quote(str(FIXTURE))
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _launch(root: Path, bare: Path, name: str, mode: str, **extra: str) -> subprocess.CompletedProcess[str]:
    home = root / name / "home"
    workspace = root / name / "workspace"
    state = root / name / "state"
    for path in (home, workspace, state):
        path.mkdir(parents=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SWARM_JOB_HOME": str(home),
        "SWARM_JOB_WORKSPACE": str(workspace),
        "SWARM_ISSUE_WORKER_STATE_DIR": str(state),
        "SWARM_JOB_REPO_URL": str(bare),
        "SWARM_JOB_STORAGE": f"local:{root / 'durable'}",
        "SWARM_TENANT": "t418",
        "SWARM_JOB_WORKER": str(_wrapper(root)),
        "SWARM_FIXTURE_SCRIPT": mode,
        "SWARM_FIXTURE_REMOTE": str(bare),
        "SWARM_FIXTURE_ISSUE": "418",
    }
    env.update(extra)
    return subprocess.run(
        [sys.executable, "-I", str(LAUNCH)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _branch_exists(bare: Path) -> bool:
    completed = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "--verify", f"refs/heads/{BRANCH}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


class JobLaunchTests(unittest.TestCase):
    def test_epoch_yield_resumes_on_a_fresh_workspace_and_comments_after_push(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-job-launch.") as temporary:
            root = Path(temporary)
            bare = _git(root)
            first = _launch(root, bare, "a", "epoch")
            self.assertEqual(first.returncode, 13, first.stderr + first.stdout)
            durable = root / "durable" / "tenants" / "t418"
            proof = (durable / "delivery-proof").read_text(encoding="utf-8")
            self.assertTrue(proof.startswith("push "), proof)
            self.assertIn("pr https://github.com/acme/demo/pull/418", proof)
            self.assertNotIn("comment", proof)
            self.assertLess(proof.index("push "), proof.index("pr "))
            self.assertTrue((durable / "in-progress-issue.json").is_file())
            self.assertTrue(_branch_exists(bare))
            (root / "a" / "workspace" / "LEFTOVER.txt").write_text("from-a", encoding="utf-8")

            shutil.rmtree(root / "a")
            second = _launch(root, bare, "b", "epoch")
            self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
            proof = (durable / "delivery-proof").read_text(encoding="utf-8")
            self.assertLess(proof.index("push "), proof.index("comment "))
            self.assertIn("label Ready For Testing", proof)
            self.assertFalse((durable / "in-progress-issue.json").is_file())
            self.assertFalse((root / "b" / "workspace" / "LEFTOVER.txt").exists())
            self.assertTrue((root / "b" / "workspace" / "README").is_file())

    def test_quota_pause_resumes_push_then_comment(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-job-quota.") as temporary:
            root = Path(temporary)
            bare = _git(root)
            first = _launch(root, bare, "a", "quota")
            self.assertEqual(first.returncode, 11, first.stderr + first.stdout)
            durable = root / "durable" / "tenants" / "t418"
            self.assertTrue((durable / "quota-paused-issues" / "418.json").is_file())
            self.assertFalse((durable / "delivery-proof").exists())
            self.assertFalse(_branch_exists(bare))
            shutil.rmtree(root / "a")
            second = _launch(root, bare, "b", "quota")
            self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
            proof = (durable / "delivery-proof").read_text(encoding="utf-8")
            self.assertLess(proof.index("push "), proof.index("comment "))
            self.assertFalse((durable / "quota-paused-issues" / "418.json").exists())
            self.assertTrue(_branch_exists(bare))

    def test_a_shared_git_cache_is_dissociated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-job-cache.") as temporary:
            root = Path(temporary)
            bare = _git(root)
            completed = _launch(root, bare, "a", "deliver", SWARM_GIT_CACHE=str(root / "seed"))
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            alternates = root / "a" / "workspace" / ".git" / "objects" / "info" / "alternates"
            if alternates.is_file():
                self.assertEqual(alternates.read_text(encoding="utf-8").strip(), "")

    def test_home_and_workspace_must_be_empty(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-job-fresh.") as temporary:
            root = Path(temporary)
            bare = _git(root)
            home = root / "busy-home"
            home.mkdir()
            (home / "session").write_text("old", encoding="utf-8")
            workspace = root / "busy-workspace"
            workspace.mkdir()
            state = root / "state"
            state.mkdir()
            env = {
                "PATH": os.environ.get("PATH", ""),
                "SWARM_JOB_HOME": str(home),
                "SWARM_JOB_WORKSPACE": str(workspace),
                "SWARM_ISSUE_WORKER_STATE_DIR": str(state),
                "SWARM_JOB_REPO_URL": str(bare),
            }
            home_result = subprocess.run(
                [sys.executable, "-I", str(LAUNCH)],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(home_result.returncode, 1)
            self.assertIn("not empty", home_result.stderr)

            empty_home = root / "empty-home"
            empty_home.mkdir()
            (workspace / "owned").write_text("old", encoding="utf-8")
            env["SWARM_JOB_HOME"] = str(empty_home)
            workspace_result = subprocess.run(
                [sys.executable, "-I", str(LAUNCH)],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(workspace_result.returncode, 1)
            self.assertIn("not empty", workspace_result.stderr)


if __name__ == "__main__":
    unittest.main()
