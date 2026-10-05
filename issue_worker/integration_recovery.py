"""Durable AI recovery for conflicts while synchronizing ai-main with main."""
from __future__ import annotations


class IntegrationRecoveryMixin:
    def recover_integration_merge(self, checkpoint: dict) -> str:
        """Resolve a pinned main-to-integration merge and keep its two-parent history."""
        from swarm_issue_worker import WorkerError, atomic_write_json, iso_timestamp, log

        branch = self.config.integration_branch
        original, target = checkpoint["original"], checkpoint["target"]
        if self.git("branch", "--show-current") != branch:
            raise WorkerError(f"Integration recovery must run on {branch}")

        if checkpoint.get("phase") == "committed":
            head = checkpoint["commit"]
            if self.git("rev-parse", "HEAD") != head:
                raise WorkerError("Integration recovery commit changed before it was pushed")
            return head

        if not self.git_ok("rev-parse", "--verify", "MERGE_HEAD"):
            if self.git_ok("merge-base", "--is-ancestor", target, "HEAD"):
                # A previous attempt completed and committed the merge before
                # it could update its durable checkpoint.
                head = self.git("rev-parse", "HEAD")
                parents = self.git("show", "-s", "--format=%P", head).split()
                if original not in parents or target not in parents:
                    raise WorkerError("Recovered integration merge does not preserve both pinned parents")
                checkpoint.update(phase="committed", commit=head)
                atomic_write_json(self.integration_recovery_file, checkpoint)
                return head
            if self.git("rev-parse", "HEAD") != original or self.worktree_status():
                raise WorkerError("Integration checkout changed before its pinned recovery merge")
            from swarm_issue_worker import run_command
            result = run_command([
                self.config.git_bin, "-C", str(self.config.repo_dir), "merge",
                "--no-ff", "--no-commit", target,
            ], check=False)
            if result.returncode and not self.git_ok("rev-parse", "--verify", "MERGE_HEAD"):
                raise WorkerError(f"Could not start integration recovery merge: {result.stderr or result.stdout}")

        if self.git("rev-parse", "MERGE_HEAD") != target or self.git("rev-parse", "HEAD") != original:
            raise WorkerError("Integration merge revisions changed during recovery")
        conflicts = self.git("diff", "--name-only", "--diff-filter=U").splitlines()
        if conflicts and not checkpoint.get("resolved"):
            checkpoint.update(phase="resolving", conflicts=conflicts, attempts=int(checkpoint.get("attempts", 0)) + 1)
            atomic_write_json(self.integration_recovery_file, checkpoint)
            self.ensure_bot_auth()
            prompt = (
                f"Resolve the active merge of {self.config.base_branch} into {branch}.\n"
                f"Base commit: {target}\nIntegration commit: {original}\n"
                f"Conflicted files: {conflicts!r}\n\n"
                "Preserve the intended behavior from both branches. Read repository guidance and inspect the full "
                "changes on both sides before editing. Resolve every conflict and stage the resolutions with git add. "
                "Keep the current branch and merge in progress. Do not abort, reset, rebase, rewrite history, commit, "
                "push, open or edit pull requests, or change VERSION. Run relevant checks in the foreground and "
                "return a concise summary. The worker will verify and commit the merge.\n"
            )
            log(f"{branch} conflicts with {self.config.base_branch}; {self.choice.name} is resolving the merge.")
            self.ai_prompt_file.write_text(prompt, encoding="utf-8")
            status = self.run_ai(prompt, activity="resolving integration merge conflicts")
            if status or not self.ai_output_file.exists() or not self.ai_output_file.stat().st_size:
                raise WorkerError("AI integration conflict resolution failed; recovery checkpoint was preserved")
            if self.git("diff", "--name-only", "--diff-filter=U"):
                raise WorkerError("AI integration conflict resolver left unresolved files; checkpoint was preserved")
            checkpoint["resolution"] = self.ai_output_file.read_text(encoding="utf-8", errors="replace")
            checkpoint["resolved"] = True
            atomic_write_json(self.integration_recovery_file, checkpoint)

        if self.git("branch", "--show-current") != branch:
            raise WorkerError("AI integration conflict resolver changed branches")
        if self.git("diff", "--name-only", "--diff-filter=U"):
            raise WorkerError("AI integration conflict resolver left unresolved files")
        self.git("add", "-u")
        self.git("diff", "--cached", "--check")
        if not self.git_ok("diff", "--quiet"):
            raise WorkerError("Integration conflict resolution left unstaged changes")
        if any(line.startswith("?? ") for line in self.git("status", "--porcelain").splitlines()):
            raise WorkerError("Integration conflict resolution left untracked files")
        self.git("commit", "-m", f"[{branch}] reconcile {self.config.base_branch}")
        head = self.git("rev-parse", "HEAD")
        parents = self.git("show", "-s", "--format=%P", head).split()
        if original not in parents or target not in parents or self.worktree_status():
            raise WorkerError("Integration recovery commit failed its clean two-parent history check")
        checkpoint.update(phase="committed", commit=head, completed_at=iso_timestamp())
        atomic_write_json(self.integration_recovery_file, checkpoint)
        log(f"AI reconciled {branch} with {self.config.base_branch} in {head[:12]}.")
        return head
