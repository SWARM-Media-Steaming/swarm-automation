#!/usr/bin/env python3
"""Run the SWARM issue worker continuously in the foreground.

The historical filename says "cron", but this is a foreground scheduler. It
also removes the legacy crontab block installed by older SWARM versions.
"""

from __future__ import annotations

import argparse
import atexit
import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Sequence, TextIO


ISSUE_COMPLETED_EXIT_CODE = 10
QUOTA_PAUSED_EXIT_CODE = 11
PROVIDER_UNAVAILABLE_EXIT_CODE = 12
BEGIN_MARKER = "# BEGIN SWARM ISSUE WORKER"
END_MARKER = "# END SWARM ISSUE WORKER"
WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# "Run now" pressed while this scheduler is already running: the desktop app
# drops this file in the state directory instead of starting a second
# scheduler (the runner lock would refuse it). Seeing it, the scheduler stops
# waiting, scans every repository immediately, and restarts its timer from the
# end of that cycle.
RUN_NOW_REQUEST_FILE = "run-now.request"


def saved_routing_overrides(worker_args: Sequence[object]) -> list[str]:
    """Routing flags from a saved repos.json entry, in the order they appear.

    Repeated at the end of the worker command so a save while the scheduler is
    already running wins over the copies captured on the scheduler command line
    at startup. argparse keeps the last BooleanOptionalAction / store value.
    """
    flags: list[str] = []
    arguments = [str(arg) for arg in worker_args]
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"--dynamic-model-routing", "--no-dynamic-model-routing"}:
            flags.append(argument)
            index += 1
            continue
        if argument == "--routing-optimization" and index + 1 < len(arguments):
            flags.extend([argument, arguments[index + 1]])
            index += 2
            continue
        index += 1
    return flags


def timestamp() -> str:
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def env_value(name: str, fallback: str) -> str:
    return os.environ.get(name, fallback)


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def schedule_time(value: str) -> tuple[int, int]:
    try:
        parsed = dt.datetime.strptime(value, "%H:%M")
    except ValueError as error:
        raise argparse.ArgumentTypeError("must use 24-hour HH:MM format") from error
    return parsed.hour, parsed.minute


def schedule_days(value: str) -> frozenset[int]:
    names = [part.strip().lower()[:3] for part in value.split(",") if part.strip()]
    invalid = [name for name in names if name not in WEEKDAY_NAMES]
    if invalid or not names:
        raise argparse.ArgumentTypeError("must be a comma-separated list such as mon,tue,wed")
    return frozenset(WEEKDAY_NAMES.index(name) for name in names)


class Runner:
    # A transient network/GitHub failure shouldn't cost a whole cycle.
    FETCH_ATTEMPTS = 3
    FETCH_RETRY_SECONDS = 3

    def __init__(self, args: argparse.Namespace, worker_arguments: Sequence[str]) -> None:
        self.args = args
        self.worker_arguments = list(worker_arguments)
        self.script_dir = Path(__file__).resolve().parent
        self.state_dir = Path(args.state_dir).expanduser().resolve()
        self.log_path = (
            Path(args.log_path).expanduser().resolve()
            if args.log_path
            else self.state_dir / "cron.log"
        )
        self.lock_dir = self.state_dir / "runner.lock"
        self.run_now_path = self.state_dir / RUN_NOW_REQUEST_FILE
        self.worker = Path(args.worker).expanduser().resolve()
        self.acquired_lock = False
        self.stop_requested = False
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        # A single scheduler services every configured repo. By default the
        # repos are worked one at a time; with --parallel-repos each repo gets
        # its own worker in the same cycle (faster, but AI credits burn faster).
        self.repos = self._load_repos()
        self.parallel_repos = self._parallel_for(self.repos)

    def _parallel_for(self, repos: list[dict[str, object]]) -> bool:
        return bool(getattr(self.args, "parallel_repos", False)) and len(repos) > 1

    def reload_repos(self) -> None:
        """Pick up repository changes the desktop app saved since this scheduler
        started (it rewrites --repos-file on every save). An unreadable file
        keeps the current list rather than stopping the scheduler."""
        if not getattr(self.args, "repos_file", ""):
            return
        try:
            repos = self._load_repos()
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            self.log(
                f"WARNING: could not reload {self.args.repos_file}; "
                f"keeping the current repository list: {error}"
            )
            return
        if not any(Path(str(repo["workspace_dir"])).is_dir() for repo in repos):
            # A list none of whose checkouts exist cannot be work the app meant
            # to schedule (a stale or foreign file); keep working what we have.
            self.log(
                f"WARNING: none of the repositories in {self.args.repos_file} has a checkout on "
                "disk; keeping the current repository list."
            )
            return
        before = [str(repo["label"]) for repo in self.repos]
        after = [str(repo["label"]) for repo in repos]
        # Always adopt the reloaded entries: per-repo worker arguments can change
        # even when the set of repositories does not.
        self.repos = repos
        self.parallel_repos = self._parallel_for(repos)
        if before != after:
            added = [label for label in after if label not in before]
            removed = [label for label in before if label not in after]
            changes = [f"added {', '.join(added)}"] if added else []
            changes += [f"removed {', '.join(removed)}"] if removed else []
            self.log(
                f"Repository list changed ({'; '.join(changes) or 'reordered'}); "
                f"now working {len(repos)} repository(ies)."
            )

    def _load_repos(self) -> list[dict[str, object]]:
        if getattr(self.args, "repos_file", ""):
            entries = json.loads(Path(self.args.repos_file).expanduser().read_text(encoding="utf-8"))
            if not isinstance(entries, list) or not entries:
                raise RuntimeError(f"--repos-file has no repositories: {self.args.repos_file}")
            return [self._normalize_repo(entry) for entry in entries]
        workspace = str(Path(self.args.repo_dir).expanduser().resolve())
        return [
            self._normalize_repo(
                {
                    "label": self.args.repo_dir,
                    "workspace_dir": workspace,
                    "state_dir": str(self.state_dir),
                    "base_branch": self.args.base_branch,
                    "remote_name": self.args.remote_name,
                    "integration_branch": getattr(self.args, "integration_branch", "ai-main"),
                    "worker_args": [],
                }
            )
        ]

    @staticmethod
    def _normalize_repo(entry: dict[str, object]) -> dict[str, object]:
        entry = dict(entry)
        entry["state_dir"] = str(Path(str(entry["state_dir"])).expanduser().resolve())
        entry["workspace_dir"] = str(Path(str(entry["workspace_dir"])).expanduser().resolve())
        entry.setdefault("worker_args", [])
        entry.setdefault("label", entry["workspace_dir"])
        return entry

    def in_progress_file(self, repo: dict[str, object]) -> Path:
        return Path(str(repo["state_dir"])) / "in-progress-issue.json"

    def log(self, message: str) -> None:
        line = f"[{timestamp()}] {message}"
        print(line, flush=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def remove_legacy_cron(self) -> None:
        if not self.args.crontab_bin:
            return
        current = subprocess.run(
            [self.args.crontab_bin, "-l"], text=True, capture_output=True, check=False
        ).stdout
        if BEGIN_MARKER not in current.splitlines():
            return
        filtered: list[str] = []
        removing = False
        for line in current.splitlines():
            if line == BEGIN_MARKER:
                removing = True
                continue
            if line == END_MARKER:
                removing = False
                continue
            if not removing:
                filtered.append(line)
        payload = "\n".join(filtered) + ("\n" if filtered else "")
        result = subprocess.run(
            [self.args.crontab_bin, "-"], input=payload, text=True, capture_output=True, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(f"Could not remove legacy crontab block: {result.stderr.strip()}")
        self.log("Removed the legacy SWARM issue worker crontab entry.")

    def acquire_lock(self) -> bool:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.lock_dir.mkdir()
        except FileExistsError:
            pid_path = self.lock_dir / "pid"
            try:
                owner = int(pid_path.read_text().strip())
                os.kill(owner, 0)
            except (OSError, ValueError):
                pid_path.unlink(missing_ok=True)
                try:
                    self.lock_dir.rmdir()
                except OSError:
                    pass
                try:
                    self.lock_dir.mkdir()
                except FileExistsError:
                    self.log("Another foreground runner acquired the lock during stale-lock recovery; exiting.")
                    return False
            else:
                self.log(f"Another foreground SWARM issue runner is already active as pid {owner}; exiting.")
                return False
        (self.lock_dir / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.acquired_lock = True
        return True

    def release_lock(self) -> None:
        if not self.acquired_lock:
            return
        (self.lock_dir / "pid").unlink(missing_ok=True)
        try:
            self.lock_dir.rmdir()
        except OSError:
            pass
        self.acquired_lock = False

    def transcode_active(self) -> bool:
        if not self.args.pgrep_bin:
            return False
        result = subprocess.run(
            [
                self.args.pgrep_bin,
                "-f",
                self.args.transcode_pattern,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def prune_cargo_target(self, repo: dict[str, object]) -> None:
        target = (
            Path(self.args.cargo_target_dir).expanduser().resolve()
            if self.args.cargo_target_dir
            else Path(str(repo["workspace_dir"])) / "target"
        )
        if not target.is_dir():
            return
        size = sum(path.stat().st_size for path in target.rglob("*") if path.is_file())
        limit = self.args.cargo_target_max_gib * 1024**3
        if size <= limit:
            return
        for process in ("cargo", "rustc"):
            if self.args.pgrep_bin and subprocess.run(
                [self.args.pgrep_bin, "-x", process],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode == 0:
                self.log(
                    f"Cargo target exceeds {self.args.cargo_target_max_gib} GiB, but a Rust build is active; cleanup deferred."
                )
                return
        if not self.args.cargo_bin:
            self.log(
                f"Cargo target exceeds {self.args.cargo_target_max_gib} GiB, but cargo is unavailable; cleanup deferred."
            )
            return
        self.log(
            f"Cargo target exceeds {self.args.cargo_target_max_gib} GiB; removing generated build artifacts."
        )
        result = subprocess.run(
            [self.args.cargo_bin, "clean"], cwd=str(repo["workspace_dir"]), check=False
        )
        self.log(
            "Cargo build-artifact cleanup completed."
            if result.returncode == 0
            else "Warning: Cargo build-artifact cleanup failed; it will be retried later."
        )

    def git(self, repo: dict[str, object], *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.args.git_bin, "-C", str(repo["workspace_dir"]), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def synchronize_repository(self, repo: dict[str, object]) -> bool:
        """Light pre-flight for one repo. The worker itself does the real
        branch positioning and the base -> integration parity merge; here we
        only confirm the checkout is usable and refresh remote refs."""
        if not self.args.git_bin:
            return self._defer(repo, "Git is unavailable; deferring the worker.")
        if self.git(repo, "rev-parse", "--is-inside-work-tree").returncode != 0:
            return self._defer(
                repo, f"{repo['workspace_dir']} is not a Git checkout; deferring this run."
            )
        if self.checkout_test_lock_active(repo):
            return self._defer(
                repo,
                "the repository test scheduler owns this checkout; deferring issue work "
                "until the recorded commit finishes testing.",
            )
        if self.in_progress_file(repo).exists():
            # A saved issue owns the checkout — leave it exactly as it is; the
            # worker resumes it and does its own fetching.
            return True
        status_lines = self.git(repo, "status", "--porcelain").stdout.splitlines()
        if status_lines:
            tracked = [line for line in status_lines if not line.startswith("??")]
            if tracked:
                return self._defer(
                    repo,
                    f"{self._current_branch(repo)} has uncommitted changes to tracked files "
                    f"({self._summarize_paths(tracked)}) and no saved issue owns them; "
                    "deferring synchronization and AI (left untouched for manual review).",
                )
            blocker = self._recover_harmless_untracked_checkout(repo)
            if blocker is not None:
                reason, transient = blocker
                outcome = "will retry next cycle" if transient else "left untouched for manual review"
                return self._defer(
                    repo,
                    f"{reason}; untracked files ({self._summarize_paths(status_lines)}) have no "
                    f"saved issue owner; deferring synchronization and AI ({outcome}).",
                )
        fetched = self._fetch_with_retry(repo)
        if fetched.returncode != 0:
            return self._defer(
                repo,
                f"Could not fetch {repo['remote_name']}: "
                f"{self._git_detail(fetched, 'git fetch failed')}; deferring this run.",
            )
        return True

    def _defer(self, repo: dict[str, object], reason: str) -> bool:
        """Log why this repository is skipped for the cycle -- always naming the
        repository, since parallel workers interleave their output -- and
        return False for synchronize_repository to hand straight back."""
        self.log(f"{repo['label']}: {reason}")
        return False

    def _current_branch(self, repo: dict[str, object]) -> str:
        branch = self.git(repo, "branch", "--show-current").stdout.strip()
        return f"'{branch}'" if branch else "detached HEAD"

    @staticmethod
    def _git_detail(result: subprocess.CompletedProcess[str], fallback: str) -> str:
        """First line of git's own explanation, trimmed to keep log lines short."""
        text = result.stderr.strip() or result.stdout.strip() or fallback
        return text.splitlines()[0][:160]

    @staticmethod
    def _summarize_paths(status_lines: Sequence[str], limit: int = 3) -> str:
        paths = [line[3:] for line in status_lines]
        shown = ", ".join(paths[:limit])
        return f"{shown} +{len(paths) - limit} more" if len(paths) > limit else shown

    def _fetch_with_retry(
        self, repo: dict[str, object], *refs: str
    ) -> subprocess.CompletedProcess[str]:
        """`git fetch --prune <remote> [refs]`, retried a couple of times: a
        dropped connection or a brief GitHub hiccup should not skip a cycle."""
        arguments = ("fetch", "--prune", str(repo["remote_name"]), *refs)
        result = self.git(repo, *arguments)
        for _ in range(self.FETCH_ATTEMPTS - 1):
            if result.returncode == 0 or self.stop_requested:
                break
            time.sleep(self.FETCH_RETRY_SECONDS)
            result = self.git(repo, *arguments)
        return result

    def checkout_test_lock_active(self, repo: dict[str, object]) -> bool:
        common_dir = self.git(repo, "rev-parse", "--git-common-dir").stdout.strip()
        if not common_dir:
            return False
        common_path = Path(common_dir)
        if not common_path.is_absolute():
            common_path = Path(str(repo["workspace_dir"])) / common_path
        lock = common_path / "swarm-test-run.lock"
        if not lock.exists():
            return False
        try:
            pid = int(lock.read_text(encoding="utf-8").splitlines()[0])
            os.kill(pid, 0)
        except (ValueError, IndexError, ProcessLookupError):
            lock.unlink(missing_ok=True)
            return False
        except (OSError, PermissionError):
            # If liveness cannot be disproved, preserve the lock and checkout.
            return True
        return True

    def _recover_harmless_untracked_checkout(
        self, repo: dict[str, object]
    ) -> tuple[str, bool] | None:
        """Called only once every line of `git status --porcelain` is an
        untracked (`??`) entry -- no staged or modified tracked file is
        present, so nothing tracked can be lost. Returns None when the
        checkout is fine to carry on with, otherwise `(reason, transient)`:
        a one-line explanation for the log, and whether it should clear up
        by itself on a later cycle rather than needing a human.

        Untracked files alone never change whether the worker can proceed
        (a clean checkout takes the same path), so a checkout already on
        the integration branch needs nothing here. What can block every
        future cycle is the checkout sitting on whatever branch an
        interrupted work-round left it on (see issue-branch-delivery.md for
        one way that happens).

        Repositioning onto the integration branch is only safe once that's
        *proven*, not assumed: fetch it fresh, then require that the current
        commit is either already reachable from the remote integration
        branch (everything on it has been merged) or has a tree byte-
        identical to it (a squash/rebase merge). Both are pure commit-to-
        commit checks that never look at the working directory, so the
        untracked files can't skew them. Any real difference -- a genuinely
        unmerged commit sitting here -- leaves this repository deferred for
        a human to look at, exactly as before.
        """
        label = str(repo["label"])
        integration_branch = str(repo["integration_branch"])
        integration_ref = f"{repo['remote_name']}/{integration_branch}"
        current = self._current_branch(repo)
        if current == f"'{integration_branch}'":
            return None
        fetched = self._fetch_with_retry(repo, integration_branch)
        if fetched.returncode != 0:
            return (
                f"could not fetch {integration_ref} ({self._git_detail(fetched, 'git fetch failed')})",
                True,
            )
        # `merge-base --is-ancestor` and `diff --quiet` both exit 1 for the
        # "no" answer and >1 for a genuine error, which must not read as "no".
        contained = self.git(repo, "merge-base", "--is-ancestor", "HEAD", integration_ref)
        if contained.returncode == 1:
            identical = self.git(repo, "diff", "--quiet", "HEAD", integration_ref)
            if identical.returncode == 1:
                count = self.git(repo, "rev-list", "--count", f"{integration_ref}..HEAD")
                latest = self.git(repo, "log", "-1", "--format=%h %s")
                return (
                    f"{current} has {count.stdout.strip() or 'some'} commit(s) not in "
                    f"{integration_ref} (latest {latest.stdout.strip()[:80]})",
                    False,
                )
            contained = identical
        if contained.returncode != 0:
            return (
                f"could not compare {current} with {integration_ref} "
                f"({self._git_detail(contained, 'git comparison failed')})",
                False,
            )
        local_exists = self.git(
            repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{integration_branch}"
        ).returncode == 0
        switched = (
            self.git(repo, "switch", integration_branch)
            if local_exists
            else self.git(repo, "switch", "-c", integration_branch, integration_ref)
        )
        if switched.returncode != 0:
            return (
                f"could not switch {current} to {integration_branch} "
                f"({self._git_detail(switched, 'git switch failed')})",
                # A leftover .lock file from a concurrent git process clears itself.
                ".lock" in switched.stderr,
            )
        self.log(
            f"{label}: untracked-only checkout on {current} was already contained in "
            f"{integration_ref}; repositioned onto {integration_branch} and continuing automatically."
        )
        return None

    def run_worker(self, repo: dict[str, object], prefix: str = "") -> int:
        # Each repo gets its own snapshot so parallel workers never race on a
        # half-written file.
        snapshot = Path(str(repo["state_dir"])) / "swarm_issue_worker.snapshot.py"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.worker, snapshot)
        workspace = str(repo["workspace_dir"])
        environment = os.environ.copy()
        environment["SWARM_REPO_DIR"] = workspace
        environment["SWARM_ISSUE_WORKER_STATE_DIR"] = str(repo["state_dir"])
        environment["SWARM_ISSUE_WORKER_SCRIPT_DIR"] = str(self.script_dir)
        environment["GIT_BIN"] = self.args.git_bin
        environment["SWARM_BASE_BRANCH"] = str(repo["base_branch"])
        environment["SWARM_GIT_REMOTE"] = str(repo["remote_name"])
        environment["SWARM_INTEGRATION_BRANCH"] = str(repo["integration_branch"])
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(self.script_dir), environment.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        worker_args = [str(arg) for arg in repo["worker_args"]]
        command = [
            self.args.python_bin,
            str(snapshot),
            *worker_args,
            *self.worker_arguments,
            *saved_routing_overrides(worker_args),
        ]
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout and process.stderr
        threads = [
            threading.Thread(
                target=self.forward_worker_stream,
                args=(process.stdout, sys.stdout, prefix),
                daemon=True,
            ),
            threading.Thread(
                target=self.forward_worker_stream,
                args=(process.stderr, sys.stderr, prefix),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        status = process.wait()
        for thread in threads:
            thread.join()
        return status

    def forward_worker_stream(self, source: TextIO, destination: TextIO, prefix: str = "") -> None:
        with source, self.log_path.open("a", encoding="utf-8") as log_stream:
            for line in source:
                print(prefix + line, end="", file=destination, flush=True)
                log_stream.write(prefix + line)
                log_stream.flush()

    def clear_run_now_request(self) -> None:
        """Drop a request left over from an earlier scheduler: the cycle this
        one is about to run already satisfies it, and honouring it later would
        spend a cycle nobody asked for."""
        try:
            self.run_now_path.unlink(missing_ok=True)
        except OSError as error:
            self.log(f"WARNING: could not clear {self.run_now_path}: {error}")

    def run_now_requested(self) -> bool:
        """True once per "Run now" the desktop app asked for while this
        scheduler was waiting. The request is removed as it is read, so one
        click cuts exactly one wait short."""
        try:
            if not self.run_now_path.exists():
                return False
            self.run_now_path.unlink(missing_ok=True)
        except OSError as error:
            self.log(f"WARNING: could not read {self.run_now_path}: {error}")
            return False
        self.log(
            "Run now requested: scanning every repository immediately; the timer for the "
            "next check restarts when this cycle finishes."
        )
        return True

    def sleep(self) -> None:
        deadline = time.monotonic() + self.args.interval_seconds
        while not self.stop_requested and time.monotonic() < deadline:
            if self.run_now_requested():
                return
            time.sleep(min(1, deadline - time.monotonic()))

    def scheduled_days(self) -> frozenset[int]:
        if self.args.schedule_mode == "weekdays":
            return frozenset(range(5))
        if self.args.schedule_mode == "custom":
            return self.args.schedule_days
        return frozenset(range(7))

    def next_scheduled_run(self, now: dt.datetime | None = None) -> dt.datetime:
        current = now or dt.datetime.now().astimezone()
        hour, minute = self.args.schedule_time
        allowed_days = self.scheduled_days()
        for offset in range(8):
            day = current.date() + dt.timedelta(days=offset)
            if day.weekday() not in allowed_days:
                continue
            # Calling astimezone() on a naive local datetime lets the host OS
            # apply the correct UTC offset even when the next run crosses a
            # daylight-saving boundary.
            candidate = dt.datetime.combine(day, dt.time(hour, minute)).astimezone()
            if candidate > current:
                return candidate
        raise RuntimeError("Could not determine the next scheduled worker run")

    def wait_for_schedule(self) -> bool:
        target = self.next_scheduled_run()
        self.log(f"Next scheduled issue-worker check: {target:%A, %Y-%m-%d at %H:%M %Z}.")
        while not self.stop_requested:
            if self.run_now_requested():
                return True
            remaining = (target - dt.datetime.now().astimezone()).total_seconds()
            if remaining <= 0:
                return True
            time.sleep(min(1, remaining))
        return False

    def work_repo(self, repo: dict[str, object]) -> int | None:
        """One repo's turn in a cycle. Returns the worker exit status, or None
        when the pre-flight deferred the repo this run."""
        if self.stop_requested:
            return None
        label = str(repo["label"])
        prefix = f"[{label}] " if self.parallel_repos else ""
        self.log(f"=== repo: {label} ===")
        if not self.synchronize_repository(repo):
            return None
        status = self.run_worker(repo, prefix)
        self.prune_cargo_target(repo)
        if status in (ISSUE_COMPLETED_EXIT_CODE, QUOTA_PAUSED_EXIT_CODE):
            self.log(f"{label}: made progress; will re-check on the next cycle.")
        elif status == PROVIDER_UNAVAILABLE_EXIT_CODE:
            self.log(
                f"{label}: an issue is queued, but no enabled AI provider has "
                "enough verified capacity; will retry on schedule."
            )
        elif status:
            self.log(f"{label}: worker exited with status {status}; will retry.")
        else:
            self.log(f"{label}: no issue to work right now.")
        return status

    @staticmethod
    def _cycle_exit_status(statuses: Sequence[int | None]) -> int:
        """Fold one cycle's per-repo worker statuses into a single exit code
        for --once. A lone repo keeps its exact status; across several repos
        the most significant outcome wins (error, then progress, then queued)."""
        real = [status for status in statuses if status is not None]
        if not real:
            return 0
        if len(real) == 1:
            return real[0]
        expected = (
            ISSUE_COMPLETED_EXIT_CODE,
            QUOTA_PAUSED_EXIT_CODE,
            PROVIDER_UNAVAILABLE_EXIT_CODE,
        )
        errors = [status for status in real if status != 0 and status not in expected]
        if errors:
            return errors[0]
        for code in expected:
            if code in real:
                return code
        return 0

    def run_cycle(self) -> list[int | None]:
        """Work every configured repo once. Sequentially by default; with
        --parallel-repos, one worker per repository runs at the same time."""
        if self.parallel_repos:
            self.log(
                f"Working {len(self.repos)} repositories in parallel "
                "(one worker each); AI credits are consumed faster this way."
            )
            with ThreadPoolExecutor(max_workers=len(self.repos)) as executor:
                return list(
                    executor.map(self.work_repo, self.repos)
                )
        results: list[int | None] = []
        for repo in self.repos:
            if self.stop_requested:
                break
            results.append(self.work_repo(repo))
        return results

    def run(self) -> int:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.remove_legacy_cron()
        if self.args.remove:
            self.log("The SWARM issue worker is not running from this terminal.")
            return 0
        if self.args.check_transcode_active:
            return 0 if self.transcode_active() else 1
        if not self.worker.is_file():
            raise RuntimeError(f"Worker was not found: {self.worker}")
        if not self.acquire_lock():
            return 0
        atexit.register(self.release_lock)
        self.clear_run_now_request()

        def stop(_signum: int, _frame: object) -> None:
            self.stop_requested = True

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        self.log(
            "Running the SWARM issue worker in this terminal. Queued issues run back to back; "
            + (
                f"idle checks occur every {self.args.interval_seconds} seconds."
                if self.args.schedule_mode == "continuous"
                else f"queue checks use the {self.args.schedule_mode} schedule."
            )
        )
        self.log(f"Live output is also appended to {self.log_path}. Press Ctrl+C to stop.")
        scheduled_tick_active = self.args.once or self.args.schedule_mode == "continuous"
        try:
            while not self.stop_requested:
                if not scheduled_tick_active:
                    if not self.wait_for_schedule():
                        break
                    scheduled_tick_active = True
                self.reload_repos()
                self.log(f"Starting a cycle over {len(self.repos)} repository(ies).")
                if self.transcode_active():
                    self.log(
                        f"A SWARM media transcode is active; deferring AI and build work for "
                        f"{self.args.interval_seconds} seconds."
                    )
                    if self.args.once:
                        return 0
                    self.sleep()
                    continue

                statuses = self.run_cycle()
                progressed = any(
                    status in (ISSUE_COMPLETED_EXIT_CODE, QUOTA_PAUSED_EXIT_CODE)
                    for status in statuses
                )
                queued = any(status == PROVIDER_UNAVAILABLE_EXIT_CODE for status in statuses)
                errored = any(
                    status
                    not in (
                        None,
                        0,
                        ISSUE_COMPLETED_EXIT_CODE,
                        QUOTA_PAUSED_EXIT_CODE,
                        PROVIDER_UNAVAILABLE_EXIT_CODE,
                    )
                    for status in statuses
                )
                last_status = self._cycle_exit_status(statuses)

                if queued:
                    self.log("Cycle complete: queued issue work is waiting for AI capacity.")
                elif not progressed and not errored:
                    self.log("Cycle complete: no ready issues were found in enabled repositories.")

                if self.args.once:
                    return last_status
                if progressed and self.args.schedule_mode == "continuous":
                    # Drain more ready work across the repos without waiting.
                    continue
                if self.args.schedule_mode == "continuous":
                    self.sleep()
                else:
                    scheduled_tick_active = False
        finally:
            self.release_lock()
        self.log("Ctrl+C received; stopped the SWARM issue worker.")
        return 130 if self.stop_requested else 0


def build_parser() -> argparse.ArgumentParser:
    script_dir = Path(__file__).resolve().parent
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--check-transcode-active", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--interval-seconds",
        type=positive_integer,
        default=positive_integer(env_value("SWARM_ISSUE_WORKER_INTERVAL_SECONDS", "600")),
    )
    parser.add_argument(
        "--schedule-mode",
        choices=("continuous", "daily", "weekdays", "custom"),
        default=env_value("SWARM_ISSUE_WORKER_SCHEDULE_MODE", "continuous"),
        help="continuous polling, daily, weekdays, or selected custom days",
    )
    parser.add_argument(
        "--schedule-time",
        type=schedule_time,
        default=schedule_time(env_value("SWARM_ISSUE_WORKER_SCHEDULE_TIME", "09:00")),
        metavar="HH:MM",
        help="local start time for daily/weekday/custom schedules",
    )
    parser.add_argument(
        "--schedule-days",
        type=schedule_days,
        default=schedule_days(env_value("SWARM_ISSUE_WORKER_SCHEDULE_DAYS", "mon,tue,wed,thu,fri")),
        metavar="DAYS",
        help="comma-separated weekdays used by --schedule-mode custom",
    )
    parser.add_argument(
        "--cargo-target-max-gib",
        type=positive_integer,
        default=positive_integer(env_value("SWARM_CARGO_TARGET_MAX_GIB", "5")),
    )
    parser.add_argument("--repo-dir", default=env_value("SWARM_REPO_DIR", str(script_dir.parent.parent)))
    parser.add_argument(
        "--repos-file",
        default=env_value("SWARM_REPOS_FILE", ""),
        help="JSON array of per-repo objects to cycle over (multi-repo mode)",
    )
    parser.add_argument(
        "--parallel-repos",
        action="store_true",
        default=env_value("SWARM_ISSUE_WORKER_PARALLEL_REPOS", "") not in ("", "0", "false"),
        help="run one worker per repository at the same time instead of one at a time",
    )
    parser.add_argument(
        "--integration-branch",
        default=env_value("SWARM_INTEGRATION_BRANCH", "ai-main"),
    )
    parser.add_argument(
        "--state-dir",
        default=env_value("SWARM_ISSUE_WORKER_STATE_DIR", str(home / ".local/state/swarm-issue-worker")),
    )
    parser.add_argument(
        "--worker", default=env_value("SWARM_ISSUE_WORKER_PATH", str(script_dir / "swarm_issue_worker.py"))
    )
    parser.add_argument("--python-bin", default=env_value("PYTHON_BIN", shutil.which("python3") or "python3"))
    parser.add_argument("--crontab-bin", default=env_value("CRONTAB_BIN", shutil.which("crontab") or ""))
    parser.add_argument("--pgrep-bin", default=env_value("PGREP_BIN", shutil.which("pgrep") or ""))
    parser.add_argument("--cargo-bin", default=env_value("CARGO_BIN", shutil.which("cargo") or ""))
    parser.add_argument("--git-bin", default=env_value("GIT_BIN", shutil.which("git") or ""))
    parser.add_argument("--base-branch", default=env_value("SWARM_BASE_BRANCH", "main"))
    parser.add_argument("--remote-name", default=env_value("SWARM_GIT_REMOTE", "origin"))
    parser.add_argument("--log-path", default=env_value("SWARM_ISSUE_WORKER_LOG_PATH", ""))
    parser.add_argument("--cargo-target-dir", default=env_value("SWARM_CARGO_TARGET_DIR", ""))
    parser.add_argument(
        "--transcode-pattern",
        default=env_value(
            "SWARM_TRANSCODE_PROCESS_PATTERN",
            r"[f]fmpeg .* -f hls .*app[.]swarm[.]server/transcodes/",
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, worker_arguments = parser.parse_known_args(argv)
    if args.remove and args.check_transcode_active:
        parser.error("--remove and --check-transcode-active are mutually exclusive")
    return Runner(args, worker_arguments).run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
