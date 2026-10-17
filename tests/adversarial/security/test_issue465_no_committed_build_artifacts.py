"""Issue #465 security: compiled build output must not be tracked by git.

The delivery commits everything in the worktree. Cargo output under
``web/target/`` is not covered by the root ``/target`` ignore rule, so it is
pushed to GitHub: unreviewable executables/rlibs and files embedding the
developer's absolute local paths. Read-only: inspects the git index only.
"""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ARTIFACT_SUFFIXES = (".rlib", ".rmeta", ".o", ".dylib", ".a")


def tracked_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


class NoCommittedBuildArtifacts(unittest.TestCase):
    def test_no_cargo_target_directory_is_tracked(self):
        offenders = [
            p for p in tracked_files()
            if "target" in p.split("/")[:-1]
            and (p.endswith("CACHEDIR.TAG") or "/debug/" in p or "/release/" in p)
        ]
        self.assertEqual(offenders[:5], [], f"{len(offenders)} cargo build outputs are tracked")

    def test_no_compiled_artifacts_are_tracked(self):
        offenders = [p for p in tracked_files() if p.endswith(ARTIFACT_SUFFIXES)]
        self.assertEqual(offenders[:5], [], f"{len(offenders)} compiled artifacts are tracked")

    def test_web_target_is_ignored(self):
        res = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", "web/target/debug/swarm-web"], cwd=ROOT
        )
        self.assertEqual(res.returncode, 0, "web/target is not covered by .gitignore")


if __name__ == "__main__":
    unittest.main()
