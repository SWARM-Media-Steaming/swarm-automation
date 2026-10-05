"""Real-git regressions for the issue #381 / PR #385 delivery retry loop."""
import dataclasses
import json
import sys
import unittest
from unittest import mock
from types import SimpleNamespace

from adversarial_core import read_definition
from delivery_recovery import DeliveryRecoveryYield
from swarm_issue_worker import (
    IssueContext, ProviderChoice, ProviderUsage, Worker, WorkerError, UAT_STAGE, SECURITY_STAGE,
)
import test_swarm_issue_worker as fixtures


class DeliveryRecoveryTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self, conflict=True, reviews=True):
        self.worker.config = dataclasses.replace(
            self.worker.config, auto_approve=True, adversarial_uat_enabled=reviews,
            adversarial_security_enabled=reviews,
        )
        self.worker.issue = IssueContext(381, "Intelligent Prompt Caching", "Keep both features.", [], "https://example.invalid/issues/381")
        self.worker.choice = ProviderChoice("Codex", "test-model", "medium")
        self.git("switch", "-c", "ai/codex/issue-381")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        (self.repo / "tracked.txt").write_text("prompt caching\n")
        self.git("commit", "-am", "[codex] Prompt caching (#381)")
        self.original = self.git("rev-parse", "HEAD")
        for stage in (UAT_STAGE, SECURITY_STAGE):
            self.worker.initialize_stage(stage, self.original, "Original implementation summary")
            loop = self.worker.read_state()[stage.key]
            loop.update(phase="done", status="PASS", outcome="clean_first_pass")
            self.worker.save_stage(stage, loop)
        if not reviews:
            state = self.worker.read_state()
            for stage in (UAT_STAGE, SECURITY_STAGE):
                state.pop(stage.key)
            self.worker.write_state(state)
        self.git("switch", "ai-main")
        path = "tracked.txt" if conflict else "integration.txt"
        (self.repo / path).write_text("new integration feature\n")
        self.git("add", path)
        self.git("commit", "-m", "Integration advances")
        self.target = self.git("rev-parse", "HEAD")
        self.git("push", "origin", "ai-main")
        self.git("switch", "ai/codex/issue-381")
        self.choice_patch = mock.patch.object(self.worker, "choose_stage_provider", return_value=ProviderChoice("Codex", "test-model", "medium"))
        self.choice_patch.start()
        self.addCleanup(self.choice_patch.stop)
        auth = mock.patch.object(self.worker, "ensure_bot_auth")
        auth.start()
        self.addCleanup(auth.stop)

    def resolve(self, prompt, activity=""):
        self.assertIn("Preserve", prompt)
        self.assertEqual(activity, "resolving integration merge conflicts")
        (self.repo / "tracked.txt").write_text("prompt caching\nnew integration feature\n")
        self.git("add", "tracked.txt")
        self.worker.ai_output_file.write_text("Preserved both features.")
        return 0

    def recover(self):
        with mock.patch.object(self.worker, "run_ai", side_effect=self.resolve) as ai:
            with self.assertRaises(DeliveryRecoveryYield) as result:
                self.worker.synchronize_issue_delivery(self.original, "Original implementation summary")
        self.assertEqual(result.exception.status, 13)
        return ai

    def test_exact_retry_shape_repairs_once_and_invalidates_both_verdicts_before_push(self):
        self.prepare()
        with mock.patch.object(self.worker, "deliver_pull_request") as deliver:
            with mock.patch.object(self.worker, "run_ai", side_effect=self.resolve):
                with self.assertRaises(DeliveryRecoveryYield):
                    self.worker.finalize_issue(self.original, "Original implementation summary")
        deliver.assert_not_called()
        self.assertEqual((self.repo / "tracked.txt").read_text(), "prompt caching\nnew integration feature\n")
        self.assertFalse(self.worker.worktree_status())
        self.git("merge-base", "--is-ancestor", self.original, "HEAD")
        self.git("merge-base", "--is-ancestor", self.target, "HEAD")
        state = self.worker.read_state()
        self.assertEqual(state[UAT_STAGE.key]["phase"], "test")
        self.assertEqual(state[UAT_STAGE.key]["completion"], self.git("rev-parse", "HEAD"))
        self.assertNotIn(SECURITY_STAGE.key, state)
        self.assertEqual(state["delivery_recovery"]["previous_reviews"][SECURITY_STAGE.key]["status"], "PASS")
        self.assertFalse(self.worker.pending_file.exists())
        # Once the merge is reviewed, the same target requires no more AI calls.
        with mock.patch.object(self.worker, "run_ai") as ai:
            self.worker.synchronize_issue_delivery(self.git("rev-parse", "HEAD"), "summary")
        ai.assert_not_called()

    def test_clean_integration_update_also_requires_fresh_reviews(self):
        self.prepare(conflict=False)
        ai = self.recover()
        ai.assert_not_called()
        self.assertEqual((self.repo / "tracked.txt").read_text(), "prompt caching\n")
        self.assertEqual((self.repo / "integration.txt").read_text(), "new integration feature\n")
        self.assertEqual(self.worker.read_state()[UAT_STAGE.key]["phase"], "test")

    def test_failed_resolver_preserves_merge_and_restart_retries_the_same_checkpoint(self):
        self.prepare()
        with mock.patch.object(self.worker, "run_ai", return_value=1), mock.patch.object(self.worker, "ai_failure_is_quota", return_value=False):
            with self.assertRaisesRegex(WorkerError, "checkpoint preserved"):
                self.worker.synchronize_issue_delivery(self.original, "summary")
        self.assertEqual(self.git("rev-parse", "MERGE_HEAD"), self.target)
        restarted = Worker(self.worker.config)
        restarted.issue = self.worker.issue
        restarted.choice = self.worker.choice
        with mock.patch.object(restarted, "ensure_bot_auth"), mock.patch.object(restarted, "run_ai", side_effect=self.resolve):
            with self.assertRaises(DeliveryRecoveryYield):
                restarted.resume_delivery_recovery()
        self.assertEqual(restarted.read_state()[UAT_STAGE.key]["phase"], "test")

    def test_unresolved_conflicts_cannot_be_committed_or_delivered(self):
        self.prepare()
        def incomplete(prompt, activity=""):
            self.worker.ai_output_file.write_text("Done")
            return 0
        with mock.patch.object(self.worker, "run_ai", side_effect=incomplete):
            with self.assertRaisesRegex(WorkerError, "unresolved"):
                self.worker.synchronize_issue_delivery(self.original, "summary")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.original)
        self.assertFalse(self.worker.pending_file.exists())

    def test_staged_conflict_markers_cannot_be_committed(self):
        self.prepare()
        def stage_markers(prompt, activity=""):
            self.git("add", "tracked.txt")
            self.worker.ai_output_file.write_text("Done")
            return 0
        with mock.patch.object(self.worker, "run_ai", side_effect=stage_markers):
            with self.assertRaises(WorkerError):
                self.worker.synchronize_issue_delivery(self.original, "summary")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.original)

    def test_crash_after_merge_commit_rebuilds_reviews_without_rerunning_resolver(self):
        self.prepare()
        with mock.patch.object(self.worker, "run_ai", side_effect=self.resolve), mock.patch.object(self.worker, "initialize_stage", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.worker.synchronize_issue_delivery(self.original, "summary")
        self.assertEqual(self.worker.read_state()["delivery_recovery"]["phase"], "review")
        with mock.patch.object(self.worker, "run_ai") as ai:
            with self.assertRaises(DeliveryRecoveryYield):
                self.worker.resume_delivery_recovery()
        ai.assert_not_called()
        self.assertEqual(self.worker.read_state()[UAT_STAGE.key]["phase"], "test")

    def test_quota_pause_uses_existing_checkpoint_and_does_not_deliver(self):
        self.prepare()
        with mock.patch.object(self.worker, "run_ai", return_value=1), mock.patch.object(self.worker, "ai_failure_is_quota", return_value=True), mock.patch.object(self.worker, "post_quota_comment"):
            with self.assertRaises(DeliveryRecoveryYield) as result:
                self.worker.synchronize_issue_delivery(self.original, "summary")
        self.assertEqual(result.exception.status, 11)
        self.assertEqual(self.worker.read_state()["delivery_recovery"]["phase"], "resolve")
        self.assertEqual(self.worker.read_state()["status"], "quota_paused")
        self.assertFalse((self.worker.paused_dir / "381.json").exists())
        self.assertEqual(self.git("rev-parse", "MERGE_HEAD"), self.target)
        with mock.patch.object(self.worker, "issue_is_closed", return_value=False), mock.patch.object(self.worker, "provider_capacity", return_value=1), mock.patch.object(self.worker, "choose_handoff_provider", return_value=None), mock.patch.object(self.worker, "post_quota_comment"):
            with self.assertRaises(SystemExit) as waiting:
                self.worker.prepare_paused_resume()
        self.assertEqual(waiting.exception.code, 11)
        self.assertEqual(self.git("rev-parse", "MERGE_HEAD"), self.target)

    def test_manual_delivery_does_not_start_automatic_conflict_repair(self):
        self.prepare()
        self.worker.config = dataclasses.replace(self.worker.config, auto_approve=False)
        with mock.patch.object(self.worker, "synchronize_issue_delivery") as sync, mock.patch.object(self.worker, "deliver_pull_request", side_effect=WorkerError("stop before posting")):
            with self.assertRaisesRegex(WorkerError, "stop before posting"):
                self.worker.finalize_issue(self.original, "summary")
        sync.assert_not_called()

    def test_reconciliation_cannot_merge_older_remote_tip_while_recovery_owns_delivery(self):
        self.prepare()
        self.recover()
        listing = [{"headRefName": "ai/codex/issue-381", "state": "OPEN",
                    "url": "https://example.invalid/pull/385", "headRefOid": self.original,
                    "mergeable": "MERGEABLE", "reviewDecision": "APPROVED"}]
        with mock.patch.object(self.worker.github, "gh", return_value=json.dumps(listing)), mock.patch.object(self.worker, "approve_pull_request") as approve, mock.patch.object(self.worker, "merge_pull_request") as merge, mock.patch.object(self.worker, "auto_promote_integration_branch"):
            self.worker.reconcile_issue_pull_requests()
        approve.assert_not_called()
        merge.assert_not_called()

    def test_repaired_commit_runs_uat_then_security_then_pushes_and_merges_existing_pr(self):
        self.prepare()
        self.recover()
        events = []
        def reviewer(prompt, activity=""):
            state = self.worker.read_state()
            if state[UAT_STAGE.key].get("active"):
                events.append("uat")
                folder = self.repo / "tests/adversarial"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "test_both.py").write_text(
                    "import unittest\nfrom pathlib import Path\nclass Both(unittest.TestCase):\n"
                    " def test_features(self):\n"
                    "  self.assertEqual(Path('tracked.txt').read_text(), 'prompt caching\\nnew integration feature\\n')\n"
                )
                definition = read_definition(self.repo)
                definition["suites"].append({
                    "id": "adversarial-381", "origin": "adversarial", "name": "Both features",
                    "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests/adversarial"],
                    "timeoutSeconds": 20,
                })
                (self.repo / ".swarm/tests.json").write_text(json.dumps(definition))
                output = 'SWARM_ADVERSARIAL_RESULT: {"out_of_scope": [], "dispute_resolution": ""}'
            else:
                events.append("security")
                output = 'SWARM_SECURITY_RESULT: {"summary": "Both features reviewed", "findings": [], "out_of_scope": [], "dispute_resolution": ""}'
            self.worker.ai_output_file.write_text(output)
            return 0
        def gh(args, provider=None, body=None):
            if args[:2] == ["pr", "list"]:
                return json.dumps([{"url": "https://example.invalid/pull/385", "state": "OPEN", "body": ""}])
            if args[:2] == ["issue", "list"]:
                return "[]"
            return ""
        def merge(url, head, provider, issue):
            events.append("merge")
            self.assertEqual(url, "https://example.invalid/pull/385")
            self.assertEqual(events, ["uat", "security", "merge"])
            self.assertEqual(self.git("rev-parse", "origin/ai/codex/issue-381"), head)
            self.git("merge-base", "--is-ancestor", self.target, head)
            self.assertEqual(self.worker.read_state()[UAT_STAGE.key]["phase"], "done")
            self.assertEqual(self.worker.read_state()[SECURITY_STAGE.key]["status"], "PASS")
            return head
        # The only remote boundary mocked is GitHub. Suites and branch push use real processes.
        with mock.patch.object(self.worker, "run_ai", side_effect=reviewer), mock.patch.object(self.worker, "provider_usage", return_value=ProviderUsage(0, 80)), mock.patch.object(self.worker, "comments", return_value=[]), mock.patch.object(self.worker.github, "gh", side_effect=gh), mock.patch.object(self.worker, "merge_pull_request", side_effect=merge), mock.patch.object(self.worker, "minor_bump_requested_by_trusted_user", return_value=False):
            self.assertEqual(self.worker.run_adversarial_pipeline(), 10)
        self.assertEqual(events, ["uat", "security", "merge"])
        self.assertIn(381, self.worker.completed_numbers())
        self.assertFalse(self.worker.in_progress_file.exists())

    def test_recovered_reused_pr_approval_avoids_its_original_author_after_provider_handoff(self):
        self.prepare()
        pr = {"state": "OPEN", "url": "https://example.invalid/pull/385",
              "author": {"login": "app/swarm-claude-bot"}}
        def definition(key):
            return SimpleNamespace(bot_login=f"swarm-{key}-bot[bot]")
        with mock.patch.object(self.worker.apps, "configured", return_value=True), mock.patch.object(self.worker.apps, "definition", side_effect=definition), mock.patch.object(self.worker, "provider_environment", return_value={}), mock.patch.object(self.worker, "push_ref", return_value=SimpleNamespace(returncode=0)), mock.patch.object(self.worker.github, "gh", side_effect=[json.dumps([pr]), ""]) as gh, mock.patch.object(self.worker, "merge_pull_request", side_effect=WorkerError("stop after approval")):
            with self.assertRaisesRegex(WorkerError, "stop after approval"):
                self.worker.deliver_pull_request(self.original)
        self.assertEqual(gh.call_args_list[1].args[1], "codex")

    def test_new_pr_does_not_reuse_the_author_from_a_previous_merged_round(self):
        self.prepare()
        old_pr = {"state": "MERGED", "url": "https://example.invalid/pull/380",
                  "baseRefName": "ai-main", "mergeCommit": {"oid": self.base_sha},
                  "author": {"login": "app/swarm-claude-bot"}}
        with mock.patch.object(self.worker, "provider_environment", return_value={}), mock.patch.object(self.worker, "push_ref", return_value=SimpleNamespace(returncode=0)), mock.patch.object(self.worker, "pull_request_author_provider") as old_author, mock.patch.object(self.worker.github, "gh", side_effect=[json.dumps([old_pr]), "https://example.invalid/pull/385", ""]) as gh, mock.patch.object(self.worker, "merge_pull_request", side_effect=WorkerError("stop after approval")):
            with self.assertRaisesRegex(WorkerError, "stop after approval"):
                self.worker.deliver_pull_request(self.original)
        old_author.assert_not_called()
        self.assertEqual(gh.call_args_list[2].args[1], "claude")


if __name__ == "__main__":
    unittest.main()
