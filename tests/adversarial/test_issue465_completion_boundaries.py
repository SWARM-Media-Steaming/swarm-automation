"""Independent Issue #465 acceptance tests across actual completion boundaries.

Contract: adversarial-created bugs need no human testing after completion,
including an accepted no-code resolution. Ordinary delivered issues stay open
and ready for testing; paused work and failed delivery are never completion.
Git repositories and checkpoints are real; GitHub and quota probes are local
fixtures. No provider CLI or network service is invoked.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

WORKER_DIR = Path(__file__).resolve().parents[2] / "issue_worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

import test_swarm_issue_worker as fixtures
from swarm_issue_worker import IssueContext, ProviderChoice, Worker, WorkerError


class CompletionBoundaryTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self, labels, *, provider="Claude", followup=False, commit=True):
        self.counter = getattr(self, "counter", 0) + 1
        number = 46500 + self.counter
        self.worker.issue = IssueContext(
            number, "Resolve tracked bug", "Deterministic acceptance fixture.",
            labels, f"https://example.invalid/issues/{number}",
            work_type="followup" if followup else "new",
            trigger_comment_id=987 if followup else 0,
        )
        self.worker.choice = ProviderChoice(provider, "fixture-model", "high", "fixture-session")
        self.git("switch", "main")
        self.branch = self.worker.expected_branch()
        self.git("switch", "-c", self.branch)
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        if commit:
            (self.repo / "tracked.txt").write_text("resolved\n", encoding="utf-8")
            self.git("add", "tracked.txt")
            self.git("commit", "-qm", f"[{provider.lower()}] Resolve bug (#{number})")
        self.sha = self.git("rev-parse", "HEAD")
        self.calls = []
        self.issue_state = "OPEN"
        self.labels = set(labels)
        self.comment_bodies = []
        self.fail_action = None
        self.pr_url = f"https://example.invalid/pull/{number + 1}"
        return number

    def gh(self, args, provider=None, body=None):
        self.calls.append((list(args), provider, body))
        action = tuple(args[:2])
        if action == self.fail_action:
            raise WorkerError("fixture GitHub failure")
        if action == ("pr", "list"):
            return "[]"
        if action == ("pr", "create"):
            # A completion must never be based only on an unpushed local SHA.
            remote_sha = subprocess.run(
                ["git", "--git-dir", str(self.remote), "rev-parse", f"refs/heads/{self.branch}"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(remote_sha, self.sha)
            return self.pr_url
        if action == ("issue", "comment"):
            self.comment_bodies.append(body)
            return ""
        if action == ("issue", "close"):
            self.assertEqual(args[2], str(self.worker.issue.number))
            self.assertEqual(args[args.index("--repo") + 1], self.worker.config.github_repository)
            self.assertTrue(self.comment_bodies, "Publish a completion receipt before closing")
            self.issue_state = "CLOSED"
            return ""
        if action == ("issue", "edit"):
            if "--add-label" in args:
                self.labels.add(args[args.index("--add-label") + 1])
            if "--remove-label" in args:
                self.labels.discard(args[args.index("--remove-label") + 1])
            return ""
        if action == ("label", "create"):
            return ""
        raise AssertionError(f"Unexpected GitHub operation: {args!r}")

    def isolated_services(self, worker=None):
        worker = worker or self.worker
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(worker.github, "gh", side_effect=self.gh))
        stack.enter_context(mock.patch.object(worker, "comments", return_value=[]))
        stack.enter_context(mock.patch.object(worker, "usage_snapshot", return_value=None))
        return stack

    def assert_adversarial_complete(self, number):
        self.assertEqual(self.issue_state, "CLOSED", "Completed adversarial findings need automatic closure")
        self.assertNotIn(self.worker.config.ready_label, self.labels)
        self.assertIn(number, self.worker.completed_numbers())
        self.assertFalse(self.worker.in_progress_file.exists())
        self.assertFalse(self.worker.pending_file.exists())

    def test_full_delivery_closes_both_finding_origins_with_every_worker_provider(self):
        for label in ("adversarial-uat", "ADVERSARIAL-SECURITY"):
            for provider in ("Claude", "Codex", "Grok"):
                with self.subTest(label=label, provider=provider):
                    number = self.prepare(["bug", label], provider=provider)
                    with self.isolated_services():
                        self.worker.finalize_issue(self.sha, "Resolved and verified.")
                    self.assert_adversarial_complete(number)
                    actions = [call[0][:2] for call in self.calls]
                    self.assertLess(actions.index(["pr", "create"]), actions.index(["issue", "comment"]))
                    self.assertLess(actions.index(["issue", "comment"]), actions.index(["issue", "close"]))
                    self.assertFalse(any("--add-label" in call[0] for call in self.calls))
                    self.assertEqual(self.calls[-1][1], provider.lower())
                    self.assertIn(self.sha, self.comment_bodies[0])
                    self.assertIn(self.pr_url, self.comment_bodies[0])

    def test_ordinary_protected_and_lookalike_labels_preserve_human_testing(self):
        for labels in ([], ["bug"], ["adversarial-protected"], ["adversarial"],
                       ["adversarial-uat-extra"], ["prefix-adversarial-security"], ["ci-failure"]):
            with self.subTest(labels=labels):
                number = self.prepare(labels)
                # Mentions in prose are not evidence that an agent filed it.
                self.worker.issue.body = "adversarial-uat and adversarial-security are discussed here."
                with self.isolated_services():
                    self.worker.finalize_issue(self.sha, "Verified.")
                self.assertEqual(self.issue_state, "OPEN")
                self.assertIn(self.worker.config.ready_label, self.labels)
                self.assertIn(number, self.worker.completed_numbers())
                self.assertFalse(any(call[0][:2] == ["issue", "close"] for call in self.calls))

    def test_reopened_adversarial_followup_also_closes_after_rework(self):
        number = self.prepare(["bug", "adversarial-uat"], provider="Codex", followup=True)
        with self.isolated_services():
            self.worker.finalize_issue(self.sha, "Follow-up fixed.")
        self.assert_adversarial_complete(number)
        self.assertIn("Reworked by **Codex**", self.comment_bodies[0])
        self.assertIn("through-comment:987", self.comment_bodies[0])

    def test_push_failure_cannot_close_or_mark_completed(self):
        number = self.prepare(["adversarial-uat"])
        with self.isolated_services(), mock.patch.object(
            self.worker, "push_ref", return_value=subprocess.CompletedProcess([], 1, "", "rejected push")
        ):
            with self.assertRaisesRegex(WorkerError, "rejected push"):
                self.worker.finalize_issue(self.sha, "Verified locally.")
        self.assertEqual(self.issue_state, "OPEN")
        self.assertFalse(self.comment_bodies)
        self.assertNotIn(number, self.worker.completed_numbers())
        self.assertTrue(self.worker.in_progress_file.exists())
        self.assertFalse(self.worker.pending_file.exists())

    def test_pr_failure_after_push_cannot_close_or_mark_completed(self):
        number = self.prepare(["adversarial-security"])
        self.fail_action = ("pr", "create")
        with self.isolated_services():
            with self.assertRaisesRegex(WorkerError, "fixture GitHub failure"):
                self.worker.finalize_issue(self.sha, "Verified locally.")
        self.assertEqual(self.issue_state, "OPEN")
        self.assertFalse(self.comment_bodies)
        self.assertNotIn(number, self.worker.completed_numbers())
        self.assertTrue(self.worker.in_progress_file.exists())
        self.assertFalse(self.worker.pending_file.exists())

    def test_completion_comment_failure_retries_from_real_persisted_payload(self):
        number = self.prepare(["adversarial-uat"])
        self.fail_action = ("issue", "comment")
        with self.isolated_services():
            with self.assertRaisesRegex(WorkerError, "fixture GitHub failure"):
                self.worker.finalize_issue(self.sha, "Verified.")
        self.assertEqual(self.issue_state, "OPEN")
        self.assertNotIn(number, self.worker.completed_numbers())
        self.assertTrue(self.worker.in_progress_file.exists())
        self.assertFalse(json.loads(self.worker.pending_file.read_text())["github_comment_posted"])

        # A fresh process has no IssueContext or ProviderChoice; pending data
        # must retain the issue's origin and the actual delivering provider.
        fresh = Worker(self.worker.config)
        self.fail_action = None
        with self.isolated_services(fresh):
            fresh.deliver_pending()
        self.assert_adversarial_complete(number)
        self.assertEqual(len(self.comment_bodies), 1)
        self.assertEqual(self.calls[-1][0][:2], ["issue", "close"])

    def test_close_failure_then_provider_handoff_restart_keeps_origin_and_receipt(self):
        number = self.prepare(["adversarial-security"], provider="Grok")
        self.fail_action = ("issue", "close")
        with self.isolated_services():
            with self.assertRaisesRegex(WorkerError, "fixture GitHub failure"):
                self.worker.finalize_issue(self.sha, "Verified.")
        self.assertNotIn(number, self.worker.completed_numbers())
        self.assertTrue(self.worker.in_progress_file.exists())
        fresh = Worker(self.worker.config)
        # Selection may now refer to a different issue and worker. It must not
        # change the disposition or credentials of the saved delivery.
        fresh.issue = IssueContext(999, "Human issue", "Body", [], "https://example.invalid/999")
        fresh.choice = ProviderChoice("Claude", "other-model", "high", "other-session")
        self.fail_action = None
        with self.isolated_services(fresh):
            fresh.deliver_pending()
        self.assert_adversarial_complete(number)
        self.assertEqual(len(self.comment_bodies), 1)
        self.assertEqual(self.calls[-1][1], "grok")
        self.assertEqual(sum(call[0][:2] == ["pr", "create"] for call in self.calls), 1)

    def test_dry_run_leaves_pending_delivery_untouched(self):
        self.prepare(["adversarial-uat"])
        self.fail_action = ("issue", "close")
        with self.isolated_services():
            with self.assertRaises(WorkerError):
                self.worker.finalize_issue(self.sha, "Verified.")
        before = self.worker.pending_file.read_bytes()
        self.worker.config = dataclasses.replace(self.worker.config, dry_run=True)
        with mock.patch.object(self.worker.github, "gh") as gh:
            with self.assertRaises(SystemExit) as exit_info:
                self.worker.deliver_pending()
        self.assertEqual(exit_info.exception.code, 0)
        gh.assert_not_called()
        self.assertEqual(before, self.worker.pending_file.read_bytes())

    def test_needs_external_input_is_not_completed_work_and_stays_open(self):
        self.prepare(["adversarial-uat"], commit=False)
        with self.isolated_services():
            self.worker.finalize_needs_input("## Action required\nConfigure missing credentials.")
        self.assertEqual(self.issue_state, "OPEN")
        self.assertIn("AI Needs Input", self.labels)
        self.assertNotIn(self.worker.config.ready_label, self.labels)
        self.assertFalse(any(call[0][:2] == ["issue", "close"] for call in self.calls))

    def check_environment_completion(self, origin):
        number = self.prepare(["bug", origin], commit=False)
        with self.isolated_services():
            self.worker.finalize_environment_only("The reported failure no longer reproduces; no code change needed.")
        self.assert_adversarial_complete(number)
        self.assertTrue(any("environment-only:issue:" in body for body in self.comment_bodies))

    def test_accepted_no_code_uat_resolution_closes_the_finding(self):
        self.check_environment_completion("adversarial-uat")

    def test_accepted_no_code_security_resolution_closes_the_finding(self):
        self.check_environment_completion("adversarial-security")

    def test_no_code_close_failure_must_preserve_retryable_state(self):
        number = self.prepare(["adversarial-uat"], commit=False)
        self.fail_action = ("issue", "close")
        with self.isolated_services():
            with self.assertRaisesRegex(WorkerError, "fixture GitHub failure"):
                self.worker.finalize_environment_only("Reported failure no longer reproduces.")
        self.assertNotIn(number, self.worker.completed_numbers())
        self.assertTrue(self.worker.in_progress_file.exists() or self.worker.pending_file.exists())


if __name__ == "__main__":
    unittest.main()
