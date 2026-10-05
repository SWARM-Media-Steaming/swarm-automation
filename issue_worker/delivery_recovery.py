"""Durable integration updates before issue delivery, including AI conflict repair."""
from __future__ import annotations

import dataclasses


class DeliveryRecoveryYield(Exception):
    """Return control to the scheduler without treating recovery as a failure."""

    def __init__(self, status: int):
        self.status = status


class DeliveryRecoveryMixin:
    def pull_request_author_provider(self, pull_request: dict, fallback: str) -> str:
        """A provider handoff does not change who created the existing PR."""
        from swarm_issue_worker import normalize_author

        author = normalize_author(str((pull_request.get("author") or {}).get("login") or "")).removeprefix("app/")
        for spec in self.config.enabled_specs:
            if self.apps.configured(spec.key):
                login = normalize_author(self.apps.definition(spec.key).bot_login).removeprefix("app/")
                if login == author:
                    return spec.key
        return fallback

    def synchronize_issue_delivery(self, completion: str, output: str) -> None:
        """Merge a pinned integration revision locally; never push unreviewed repairs."""
        from swarm_issue_worker import WorkerError, iso_timestamp, log

        checkpoint = self.read_state().get("delivery_recovery") or {}
        if checkpoint.get("phase") in {"merge", "resolve", "review"}:
            self.resume_delivery_recovery()
            return
        if self.worktree_status():
            raise WorkerError("Cannot update issue delivery with uncommitted changes")
        if self.git("branch", "--show-current") != self.expected_branch():
            raise WorkerError("Cannot update delivery outside the saved issue branch")
        if self.git("rev-parse", "HEAD") != completion:
            raise WorkerError("Delivery commit is not the current issue branch tip")
        remote = self.config.remote_name
        branch = self.config.integration_branch
        self.git("fetch", remote, f"refs/heads/{branch}:refs/remotes/{remote}/{branch}")
        target = self.git("rev-parse", f"refs/remotes/{remote}/{branch}")
        if self.git_ok("merge-base", "--is-ancestor", target, completion):
            return
        # Save before git changes the index: an interrupted merge belongs to this
        # checkpoint even when the process dies before its first conflict is logged.
        checkpoint = {
            "phase": "merge", "original": completion, "target": target,
            "implementation_output": output, "started_at": iso_timestamp(),
            "delivery_choice": dataclasses.asdict(self.choice), "active": False,
        }
        self.update_state(delivery_recovery=checkpoint)
        log(f"Issue #{self.issue.number}: updating its branch with {branch} at {target[:12]} before delivery.")
        self.resume_delivery_recovery()

    def resume_delivery_recovery(self) -> None:
        from swarm_issue_worker import (
            ADVERSARIAL_STAGES, ADVERSARIAL_EPOCH_YIELD_EXIT_CODE,
            PROVIDER_UNAVAILABLE_EXIT_CODE, UAT_STAGE, WorkerError, iso_timestamp, log,
        )

        checkpoint = self.read_state().get("delivery_recovery") or {}
        if checkpoint.get("phase") not in {"merge", "resolve", "review"}:
            return
        original, target = checkpoint["original"], checkpoint["target"]
        if self.git("branch", "--show-current") != self.expected_branch():
            raise WorkerError("Conflict recovery left the saved issue branch")
        if checkpoint["phase"] == "merge":
            if not self.git_ok("rev-parse", "--verify", "MERGE_HEAD"):
                # The merge may already have committed before a crash.
                if not self.git_ok("merge-base", "--is-ancestor", target, "HEAD"):
                    if self.worktree_status() or self.git("rev-parse", "HEAD") != original:
                        raise WorkerError("Unexpected checkout changes before delivery recovery merge")
                    from swarm_issue_worker import run_command
                    result = run_command([
                        self.config.git_bin, "-C", self.config.repo_dir, "merge",
                        "--no-ff", "--no-commit", target,
                    ], check=False)
                    if result.returncode and not self.git_ok("rev-parse", "--verify", "MERGE_HEAD"):
                        raise WorkerError(f"Could not start delivery recovery merge: {result.stderr or result.stdout}")
            checkpoint["phase"] = "resolve"
            checkpoint["conflicts"] = self.git("diff", "--name-only", "--diff-filter=U").splitlines()
            self.update_state(delivery_recovery=checkpoint)
        if checkpoint["phase"] == "resolve":
            conflicts = self.git("diff", "--name-only", "--diff-filter=U").splitlines()
            if conflicts or (checkpoint.get("active") and self.worktree_status()):
                if not checkpoint.get("active"):
                    loop = {"phase": "fix", "capacity_start": {}, "capacity_end": {}, "capacity_used": []}
                    choice = self.choose_stage_provider(UAT_STAGE, loop)
                    if choice is None:
                        raise DeliveryRecoveryYield(PROVIDER_UNAVAILABLE_EXIT_CODE)
                    self.choice = choice
                    self.update_state_for_choice(choice)
                    checkpoint["active"] = True
                    self.update_state(delivery_recovery=checkpoint)
                self.ensure_bot_auth()
                self.issue_images = []
                prompt = (
                    f"Resolve the saved git merge for issue #{self.issue.number}: {self.issue.title}.\n"
                    f"Issue specification:\n{self.issue.body}\n\n"
                    f"Issue implementation: {original}\nIntegration revision: {target}\n"
                    f"Conflicted files: {checkpoint.get('conflicts', conflicts)!r}\n"
                    "Read repository conventions. Preserve the issue's functionality AND the latest integration changes. "
                    "Resolve conflict markers and stage the resolutions with git add. Preserve all existing test "
                    "coverage and registered suites from both sides; do not weaken, disable, or delete tests. "
                    "Make only changes needed to reconcile this merge. Run relevant checks in the foreground. "
                    "Stay on the current branch. Do not abort the merge, reset, rebase, rewrite history, commit, "
                    "push, open PRs, post comments, or edit VERSION. The worker commits the merge and runs fresh "
                    "independent reviews before delivery. Return a summary of resolutions and checks.\n"
                )
                log(f"Issue #{self.issue.number}: {self.choice.name} resolving integration merge conflicts.")
                self.history.update(iso_timestamp(), effective_prompt=prompt, final_status="running")
                status = self.run_ai(prompt, activity="resolving integration merge conflicts")
                if status or not self.ai_output_file.exists() or not self.ai_output_file.stat().st_size:
                    if self.ai_failure_is_quota():
                        raise DeliveryRecoveryYield(self.pause_adversarial())
                    raise WorkerError("Integration conflict resolution failed; merge and checkpoint preserved")
                checkpoint["resolution_output"] = self.ai_output_file.read_text(encoding="utf-8", errors="replace")
                self.update_state(delivery_recovery=checkpoint)
            if self.git("branch", "--show-current") != self.expected_branch():
                raise WorkerError("Conflict resolver changed the saved issue branch")
            if self.git("diff", "--name-only", "--diff-filter=U"):
                raise WorkerError("Conflict resolver left unresolved integration conflicts")
            if self.git_ok("rev-parse", "--verify", "MERGE_HEAD"):
                if self.git("rev-parse", "MERGE_HEAD") != target or self.git("rev-parse", "HEAD") != original:
                    raise WorkerError("Conflict resolver changed the pinned merge revisions")
                self.git("add", "-u")
                self.git("diff", "--cached", "--check")
                self.git("commit", "-m", f"[{self.choice.key}] Reconcile integration changes (#{self.issue.number})")
            head = self.git("rev-parse", "HEAD")
            if self.worktree_status() or not all(
                self.git_ok("merge-base", "--is-ancestor", parent, head) for parent in (original, target)
            ):
                raise WorkerError("Conflict recovery did not preserve both merge histories in a clean commit")
            checkpoint.update(phase="review", completion=head, active=False)
            self.update_state(delivery_recovery=checkpoint, candidate_sha=head)
        if checkpoint["phase"] == "review":
            # One atomic write invalidates every old verdict. A restart must never
            # deliver the repair with a PASS belonging to the pre-merge commit.
            state = self.read_state()
            if "previous_reviews" not in checkpoint:
                checkpoint["previous_reviews"] = {
                    stage.key: state.get(stage.key) for stage in ADVERSARIAL_STAGES if stage.key in state
                }
            for stage in ADVERSARIAL_STAGES:
                state.pop(stage.key, None)
            state["delivery_recovery"] = checkpoint
            state.pop("adversarial_best_effort", None)
            self.write_state(state)
            stages = self.adversarial_stages()
            if stages:
                self.initialize_stage(stages[0], checkpoint["completion"], checkpoint["implementation_output"])
            checkpoint["phase"] = "done"
            self.update_state(delivery_recovery=checkpoint)
            log(f"Issue #{self.issue.number}: integration update committed as {checkpoint['completion'][:12]}; "
                "prior review results invalidated; restarting enabled reviews.")
            self.history.note("Integration delivery recovery completed; prior reviews invalidated", iso_timestamp())
            raise DeliveryRecoveryYield(ADVERSARIAL_EPOCH_YIELD_EXIT_CODE)
