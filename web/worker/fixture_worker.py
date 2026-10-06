#!/usr/bin/env python3
"""Stand-in for the issue worker inside the job-runner acceptance image.

It performs the delivery sequence from ``issue-branch-delivery.md`` against a
local git remote and records the steps in the ``delivery-proof`` artifact the
entrypoint publishes. It does not call a provider CLI. The real image runs
``worker_entrypoint.py`` instead (``SWARM_JOB_WORKER`` is unset there).

``SWARM_FIXTURE_SCRIPT``:

* ``epoch`` — push and open the PR, write the in-progress checkpoint, exit 13.
  The next container (which hydrated that checkpoint) posts the comment and label.
* ``quota`` — exit 11 with a quota-paused checkpoint, then on the next
  container finish the comment and label.
* ``deliver`` — push, PR, comment and label in one container, exit 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ISSUE = int(os.environ.get("SWARM_FIXTURE_ISSUE", "418"))
BRANCH = f"ai/claude/issue-{ISSUE}"
MARKER = f"<!-- swarm-issue-worker:commit:fixture{ISSUE} -->"
PROOF_NAME = "delivery-proof"


def _run(command: list[str], cwd: Path) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise SystemExit(f"{command[0]} failed: {detail[:500]}")


def _proof_path(state: Path) -> Path:
    return state / PROOF_NAME


def _read_proof(state: Path) -> str:
    path = _proof_path(state)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _write_proof(state: Path, text: str) -> None:
    _proof_path(state).write_text(text, encoding="utf-8")


def _push(state: Path) -> str:
    repo = Path(os.environ["SWARM_REPO_DIR"])
    remote = os.environ["SWARM_FIXTURE_REMOTE"]
    _run(["git", "checkout", "-B", BRANCH], repo)
    note = repo / "DELIVERY.txt"
    note.write_text(f"issue {ISSUE}\n", encoding="utf-8")
    _run(["git", "add", "DELIVERY.txt"], repo)
    _run(
        [
            "git",
            "-c",
            "user.name=Swarm Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-m",
            f"Deliver issue {ISSUE}",
        ],
        repo,
    )
    _run(["git", "push", remote, BRANCH], repo)
    # The comment is not written here. Delivery order is push, then PR, then
    # comment and label, and a later step must not run before push succeeds.
    text = _read_proof(state)
    text += f"push {BRANCH}\npr https://github.com/acme/demo/pull/{ISSUE}\n"
    _write_proof(state, text)
    return text


def _comment(state: Path) -> None:
    text = _read_proof(state)
    if not text.startswith("push "):
        raise SystemExit("refusing to comment before the branch was pushed")
    text += f"comment {MARKER}\nlabel Ready For Testing\n"
    _write_proof(state, text)


def main() -> int:
    mode = os.environ.get("SWARM_FIXTURE_SCRIPT", "deliver")
    state = Path(os.environ["SWARM_ISSUE_WORKER_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True)
    progress = state / "in-progress-issue.json"
    quota_dir = state / "quota-paused-issues"
    quota = quota_dir / f"{ISSUE}.json"

    if mode == "quota":
        if not quota.is_file():
            quota_dir.mkdir(parents=True, exist_ok=True)
            quota.write_text(
                json.dumps({"issue_number": ISSUE, "reason": "quota"}),
                encoding="utf-8",
            )
            return 11
        _push(state)
        _comment(state)
        quota.unlink()
        return 0

    if progress.is_file():
        _comment(state)
        progress.unlink()
        return 0

    _push(state)
    progress.write_text(
        json.dumps(
            {
                "issue_number": ISSUE,
                "branch_name": BRANCH,
                "adversarial": {"phase": "test", "epoch": 1, "round": 0},
            }
        ),
        encoding="utf-8",
    )
    if mode == "epoch":
        return 13
    _comment(state)
    progress.unlink()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
