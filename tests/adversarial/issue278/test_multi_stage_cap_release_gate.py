"""Issue #278 AC 15: the cap-hit PR release gate must generalize to two stages.

Independent oracle, derived from the issue and `issue-branch-delivery.md` before
reading the diff: a reused pull request marked with the adversarial cap-hit
notice must stay held until *every* adversarial stage that ran on it is clean.
Before #278 there was only one stage (UAT), so "the UAT loop is clean" and
"every stage that ran is clean" were the same statement. #278 introduces a
second, independently capped stage sharing the same PR and the same marker;
the release check in `deliver_pull_request` must not regress to "at least one
stage is clean" or "the most recently updated stage is clone" when a second
loop enters the picture.

This is a real, reachable production shape: a repository can enable both
Adversarial UAT and Adversarial Cybersecurity, UAT can finish clean while the
security review is still working through its own fix/re-test rounds, and a
scheduler tick (or a concurrent worker invocation) can call
`deliver_pull_request` while that mixed state is on disk.

Only `deliver_pull_request`'s own gate is exercised here (git and a stubbed
`gh` are real/local); no coding provider or adversarial loop is actually run.
"""
import dataclasses
import json
import unittest

from harness import uat  # inserts issue_worker/ onto sys.path as a side effect
import test_swarm_issue_worker as fixtures
from swarm_issue_worker import IssueContext, ProviderChoice, WorkerError


class MultiStageCapReleaseGateTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self):
        self.worker.config = dataclasses.replace(
            self.worker.config, adversarial_uat_enabled=True, adversarial_security_enabled=True,
        )
        self.worker.issue = IssueContext(
            278, "Add a service client",
            "The client must authenticate without embedding credentials in the repository.",
            [], "https://example.invalid/issues/278")
        self.worker.choice = ProviderChoice("Claude", "fixer-model", "high", "delivery-session")
        self.git("switch", "-c", "ai/claude/issue-278")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        (self.repo / "service.py").write_text("# hardened\n")
        self.git("add", ".")
        self.git("commit", "-qm", "[claude] Implementation (#278)")
        self.head = self.git("rev-parse", "HEAD")
        self.existing = {
            "url": "https://example.invalid/pull/279", "state": "OPEN",
            "body": uat.CAP_HIT_PR_MARKER + "\nStill failing.",
        }
        self.events = []

    def set_loops(self, *, uat_outcome, security_outcome):
        """Only `outcome` is read by the gate; every other field is deliberately
        omitted so a real bug can't hide behind an accidentally-populated key
        the gate does not actually consult."""
        state = self.worker.read_state()
        if uat_outcome is None:
            state.pop("adversarial", None)
        else:
            state["adversarial"] = {"outcome": uat_outcome}
        if security_outcome is None:
            state.pop("adversarial_security", None)
        else:
            state["adversarial_security"] = {"outcome": security_outcome}
        self.worker.write_state(state)

    def gh(self, args, provider=None, body=None):
        if args[:2] == ["pr", "list"]:
            return json.dumps([self.existing])
        if args[:2] == ["pr", "edit"]:
            self.events.append("release")
            self.existing["body"] = body
        return ""

    def attempt_release(self):
        from unittest import mock
        with mock.patch.object(self.worker.github, "gh", side_effect=self.gh), \
             mock.patch.object(self.worker, "push_ref") as push:
            import subprocess
            push.return_value = subprocess.CompletedProcess([], 0, "", "")
            self.worker.deliver_pull_request(self.head)

    # -- both stages ran: neither one alone may authorize release ------------------

    def test_a_clean_uat_does_not_release_a_pr_still_capped_by_security(self):
        self.prepare()
        self.set_loops(uat_outcome="resolved_after_n", security_outcome="cap_hit")
        with self.assertRaisesRegex(WorkerError, "requires a passing UAT"):
            self.attempt_release()
        self.assertEqual(self.events, [])
        self.assertIn(uat.CAP_HIT_PR_MARKER, self.existing["body"])

    def test_a_clean_security_review_does_not_release_a_pr_still_capped_by_uat(self):
        self.prepare()
        self.set_loops(uat_outcome="cap_hit", security_outcome="resolved_after_n")
        with self.assertRaisesRegex(WorkerError, "requires a passing UAT"):
            self.attempt_release()
        self.assertEqual(self.events, [])
        self.assertIn(uat.CAP_HIT_PR_MARKER, self.existing["body"])

    def test_both_stages_clean_releases_the_held_pr(self):
        self.prepare()
        self.set_loops(uat_outcome="clean_first_pass", security_outcome="resolved_after_n")
        self.attempt_release()
        self.assertEqual(self.events, ["release"])
        self.assertNotIn(uat.CAP_HIT_PR_MARKER, self.existing["body"])

    # -- only one stage ran on this repository: the historical single-stage shape --

    def test_security_alone_enabled_and_capped_blocks_its_own_release(self):
        self.prepare()
        self.set_loops(uat_outcome=None, security_outcome="cap_hit")
        with self.assertRaisesRegex(WorkerError, "requires a passing UAT"):
            self.attempt_release()

    def test_security_alone_enabled_and_clean_releases(self):
        self.prepare()
        self.set_loops(uat_outcome=None, security_outcome="resolved_after_n")
        self.attempt_release()
        self.assertEqual(self.events, ["release"])


if __name__ == "__main__":
    unittest.main()
