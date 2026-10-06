#!/usr/bin/env python3
"""Container entrypoint for one hosted issue-worker job.

The desktop runs ``worker_entrypoint.py`` against a checkout that already
exists. A hosted job has nowhere to start from: the container's filesystem is
empty, read-only except for per-job tmpfs mounts, and destroyed when the job
ends. This wrapper does only that plumbing, then runs the same entrypoint and
returns its exit code (13 epoch yield, 11 quota pause, 14 automation hold, and
the rest unchanged):

* fresh ``git clone`` into an empty workspace, optionally sped up by a
  read-only object cache that is dissociated before the worker starts, so the
  workspace does not keep a dependency on a shared disk;
* an empty ``HOME``, so Claude/Codex/Grok session files cannot survive into
  another issue or tenant (``docs/prompt-caching.md``);
* checkpoints copied in from the hosted store before the worker, and back out
  after it exits, including after ``SIGTERM``.

``SWARM_JOB_REPO_URL`` unset skips the clone (local use of this file as a thin
wrapper). ``SWARM_JOB_WORKER`` overrides the program that runs after setup; the
image leaves it unset, so the program is ``worker_entrypoint.py``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


class JobLaunchError(RuntimeError):
    """The container could not be prepared. The worker was not started."""


def _fresh_dir(path: Path, what: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise JobLaunchError(
            f"{what} at {path} is not empty; refusing to share it between jobs"
        )


def _git_env() -> dict[str, str]:
    """Askpass for an https clone so the token is not in the URL or argv."""
    environment = os.environ.copy()
    token = environment.get("GH_TOKEN") or environment.get("GITHUB_TOKEN") or ""
    if not token or not environment.get("SWARM_JOB_REPO_URL", "").startswith("https://"):
        return environment
    askpass_dir = Path(environment.get("SWARM_JOB_HOME") or "/workspace/home")
    askpass_dir.mkdir(parents=True, exist_ok=True)
    askpass = askpass_dir / "git-askpass.sh"
    askpass.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *Username*) printf '%s\\n' x-access-token ;;\n"
        "  *) printf '%s\\n' \"$GH_TOKEN\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    askpass.chmod(0o700)
    environment["GIT_ASKPASS"] = str(askpass)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def clone_fresh(url: str, dest: Path, cache: str | None, environment: dict[str, str]) -> None:
    """Clone ``url`` into an empty ``dest``. A cache is a dissociated reference."""
    if dest.exists() and any(dest.iterdir()):
        raise JobLaunchError(f"workspace {dest} is not empty; refusing to reuse it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.rmdir()
    command = ["git", "clone"]
    if cache:
        cache_path = Path(cache)
        if not cache_path.is_dir():
            raise JobLaunchError(f"git cache {cache_path} does not exist")
        try:
            if dest.resolve().is_relative_to(cache_path.resolve()):
                raise JobLaunchError("the workspace must not live inside the git cache")
        except AttributeError:
            pass
        command += ["--reference-if-able", str(cache_path), "--dissociate"]
    command += [url, str(dest)]
    completed = subprocess.run(command, env=environment, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise JobLaunchError(f"git clone failed: {detail[:500]}")
    alternates = dest / ".git" / "objects" / "info" / "alternates"
    if alternates.is_file() and alternates.read_text(encoding="utf-8").strip():
        raise JobLaunchError("clone kept a shared object store; --dissociate did not take effect")


def _durable():
    target = os.environ.get("SWARM_JOB_STORAGE", "").strip()
    if not target:
        return None
    from storage_factory import open_storage

    return open_storage(target)


def _run_worker() -> int:
    override = os.environ.get("SWARM_JOB_WORKER", "").strip()
    if override:
        command = [override]
    else:
        command = [sys.executable, "-I", str(HERE / "worker_entrypoint.py")]
    child = subprocess.Popen(command)

    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except OSError:
            pass

    previous = signal.signal(signal.SIGTERM, forward)
    try:
        return child.wait()
    finally:
        signal.signal(signal.SIGTERM, previous)


def run() -> int:
    workspace = Path(os.environ.get("SWARM_JOB_WORKSPACE", "/workspace/repo"))
    home = Path(os.environ.get("SWARM_JOB_HOME", "/workspace/home"))
    state_dir = Path(os.environ.get("SWARM_ISSUE_WORKER_STATE_DIR", "/workspace/state"))
    _fresh_dir(home, "HOME")
    os.environ["HOME"] = str(home)
    url = os.environ.get("SWARM_JOB_REPO_URL", "").strip()
    if url:
        clone_fresh(url, workspace, os.environ.get("SWARM_GIT_CACHE") or None, _git_env())
        os.environ["SWARM_REPO_DIR"] = str(workspace)
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ["SWARM_ISSUE_WORKER_STATE_DIR"] = str(state_dir)

    tenant = os.environ.get("SWARM_TENANT", "default").strip() or "default"
    durable = _durable()
    hydrated = False
    if durable is not None:
        from job_checkpoint_sync import hydrate

        hydrate(durable, tenant, state_dir)
        hydrated = True
    try:
        return _run_worker()
    finally:
        if hydrated and durable is not None:
            from job_checkpoint_sync import publish

            publish(durable, tenant, state_dir)


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except (JobLaunchError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
