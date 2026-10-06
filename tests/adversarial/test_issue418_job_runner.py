"""Issue #418 acceptance: one container image, a fresh resume, delivery order.

Oracle (from the issue, before treating the implementation as correct):

* Docker and Fargate run the same image and entrypoint. The image is non-root,
  pins git, gh and the provider CLIs, and does not select the fixture worker.
* A repository installation token names exactly one repository and is not cached.
* Exit 13 yields on a fresh workspace: the branch is pushed and the pull request
  recorded before any comment, and the next container finishes the comment and
  label from the stored checkpoint. The two workspaces do not share files.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER = ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

import github_app_auth as auth_module  # noqa: E402

BRANCH = "ai/claude/issue-418"
PINS = (
    "GH_VERSION=2.79.0",
    "NODE_VERSION=22.14.0",
    "CLAUDE_CODE_VERSION=2.1.285",
    "CODEX_VERSION=0.160.1",
    "GROK_VERSION=1.0.46",
)


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise AssertionError(f"{command} failed: {(completed.stderr or completed.stdout).strip()}")


def _bare(root: Path) -> Path:
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


def _launch(root: Path, bare: Path, name: str) -> subprocess.CompletedProcess[str]:
    home = root / name / "home"
    workspace = root / name / "workspace"
    state = root / name / "state"
    for path in (home, workspace, state):
        path.mkdir(parents=True)
    wrapper = root / f"wrap-{name}"
    fixture = ROOT / "web" / "worker" / "fixture_worker.py"
    wrapper.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(sys.executable)
        + " "
        + shlex.quote(str(fixture))
        + "\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SWARM_JOB_HOME": str(home),
        "SWARM_JOB_WORKSPACE": str(workspace),
        "SWARM_ISSUE_WORKER_STATE_DIR": str(state),
        "SWARM_JOB_REPO_URL": str(bare),
        "SWARM_JOB_STORAGE": f"local:{root / 'durable'}",
        "SWARM_TENANT": "t418",
        "SWARM_JOB_WORKER": str(wrapper),
        "SWARM_FIXTURE_SCRIPT": "epoch",
        "SWARM_FIXTURE_REMOTE": str(bare),
        "SWARM_FIXTURE_ISSUE": "418",
    }
    return subprocess.run(
        [sys.executable, "-I", str(ISSUE_WORKER / "job_launch.py")],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


class Issue418JobRunnerTests(unittest.TestCase):
    def test_the_production_image_is_pinned_non_root_and_has_one_entrypoint(self) -> None:
        docker = (ROOT / "web" / "worker" / "Dockerfile").read_text(encoding="utf-8")
        for pin in PINS:
            self.assertIn(pin, docker)
        self.assertIn("USER 1000:1000", docker)
        self.assertIn(
            'ENTRYPOINT ["python3", "-I", "/opt/swarm/issue_worker/job_launch.py"]',
            docker,
        )
        self.assertNotIn("SWARM_JOB_WORKER", docker)
        fixture = (ROOT / "web" / "worker" / "Dockerfile.fixture").read_text(encoding="utf-8")
        self.assertIn("SWARM_JOB_WORKER=/opt/swarm/fixture_worker.py", fixture)
        self.assertIn('ENTRYPOINT ["python3", "-I", "/opt/swarm/issue_worker/job_launch.py"]', fixture)
        allow = (ROOT / "web" / "worker" / "egress-allowlist.txt").read_text(encoding="utf-8")
        self.assertIn("169.254.169.254", allow)
        self.assertIn("api.github.com", allow)

    def test_installation_token_body_names_one_repository(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-uat-token.") as temporary:
            root = Path(temporary)
            key = root / "bot.pem"
            key.write_text("not used\n", encoding="utf-8")
            key.chmod(0o600)
            config = root / "apps.json"
            config.write_text(
                json.dumps(
                    {
                        "claude": {
                            "app_id": 1,
                            "installation_id": 9,
                            "private_key_path": str(key),
                            "bot_login": "swarm-claude-bot[bot]",
                            "installations": {"acme": 555},
                        }
                    }
                ),
                encoding="utf-8",
            )
            seen: list[dict[str, object]] = []

            def urlopen(request, timeout=30):
                del timeout
                seen.append(json.loads(request.data.decode("utf-8")))
                return io.BytesIO(json.dumps({"token": f"ghs_uat{len(seen)}tokenvalue"}).encode())

            with mock.patch.object(auth_module.GitHubAppAuth, "_jwt", return_value="jwt"):
                with mock.patch.object(auth_module.urllib.request, "urlopen", side_effect=urlopen):
                    auth = auth_module.GitHubAppAuth(config, repository="acme/demo")
                    auth.repository_scoped_token("claude", "acme/demo")
                    auth.repository_scoped_token("claude", "acme/demo")
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[0]["repositories"], ["demo"])
            self.assertEqual(seen[0]["permissions"], dict(auth_module.REPOSITORY_TOKEN_PERMISSIONS))

    def test_epoch_yield_delivers_across_two_fresh_containers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-uat-job.") as temporary:
            root = Path(temporary)
            bare = _bare(root)
            first = _launch(root, bare, "a")
            self.assertEqual(first.returncode, 13, first.stderr + first.stdout)
            durable = root / "durable" / "tenants" / "t418"
            proof = (durable / "delivery-proof").read_text(encoding="utf-8")
            self.assertTrue(proof.startswith("push "))
            self.assertNotIn("comment", proof)
            self.assertLess(proof.index("push "), proof.index("pr "))
            (root / "a" / "workspace" / "LEFTOVER.txt").write_text("secret", encoding="utf-8")
            shutil.rmtree(root / "a")
            second = _launch(root, bare, "b")
            self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
            proof = (durable / "delivery-proof").read_text(encoding="utf-8")
            self.assertLess(proof.index("push "), proof.index("comment "))
            self.assertIn("label Ready For Testing", proof)
            self.assertFalse((root / "b" / "workspace" / "LEFTOVER.txt").exists())
            present = subprocess.run(
                ["git", "--git-dir", str(bare), "rev-parse", "--verify", f"refs/heads/{BRANCH}"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(present.returncode, 0, present.stderr)


if __name__ == "__main__":
    unittest.main()
