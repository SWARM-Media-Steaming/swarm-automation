"""Issue #217: a GitHub error while filing an out-of-scope UAT finding must
never abort the original issue's own adversarial round.

`file_adversarial_findings` files independent, non-blocking out-of-scope
findings discovered by the tester alongside the in-scope issue under test.
The spec is explicit that these findings "must not block delivery" of the
original issue. Before the fix, any `WorkerError` raised from `self.github.gh`
inside this function (`gh issue list` during dedup search, `gh label create`
during `ensure_label`, or `gh issue create` itself -- a 403, a rate limit, a
transient network failure) propagated straight up through
`run_adversarial_delivery`, aborting the whole round and leaving the original
issue undelivered over a problem that was never its own.

These tests probe boundaries the existing fix-and-regression tests
(`issue_worker/test_adversarial_uat.py`) do not: multiple findings in one
call where only one fails, a failure specifically from label creation rather
than issue creation, and a fail-then-retry sequence that must end with the
finding filed exactly once (no duplicate `gh issue create`).
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import test_swarm_issue_worker as fixtures  # noqa: E402
from ai_execution_history import ExecutionHistoryService  # noqa: E402
from swarm_issue_worker import IssueContext, ProviderChoice, WorkerError  # noqa: E402


class FiledFindingGitHubFailureResilienceTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self):
        self.worker.config = dataclasses.replace(
            self.worker.config, adversarial_uat_enabled=True,
            ai_execution_history_enabled=True,
            execution_history_db=self.state / "history.sqlite3",
        )
        self.worker.history = ExecutionHistoryService(True, self.state / "history.sqlite3")
        self.worker.issue = IssueContext(
            180, "Require fixed output",
            "tracked.txt must contain fixed.", [], "https://example.invalid/issues/180",
        )
        self.worker.choice = ProviderChoice("Claude", "fixer-model", "high", "implementer-session")
        self.git("switch", "-c", "ai/claude/issue-180")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()
        self.worker.initialize_adversarial(self.git("rev-parse", "HEAD"), "## Summary\nImplementation")
        return self.worker.read_state()["adversarial"]

    def test_one_finding_failing_does_not_stop_a_sibling_finding_from_being_filed(self):
        # Two independent out-of-scope findings surface in the same tester
        # report. GitHub rejects the first `gh issue create` (rate limited)
        # but would happily accept the second. Both findings are equally
        # out-of-scope and equally non-blocking -- one GitHub hiccup on the
        # first must not prevent the second from being filed in the same pass.
        loop = self.prepare()
        finding_fails = {"title": "Rate limited finding", "body": "Reproduction A."}
        finding_succeeds = {"title": "Separate parser bug", "body": "Reproduction B."}

        def gh(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                if "Rate limited finding" in args:
                    raise WorkerError("gh: HTTP 403: API rate limit exceeded")
                return "https://example.invalid/issues/900"
            return ""

        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            self.worker.file_adversarial_findings(loop, [finding_fails, finding_succeeds])

        log_output = captured.getvalue()
        self.assertIn("Could not file out-of-scope adversarial UAT finding for #180", log_output)
        self.assertIn(
            "Filed out-of-scope adversarial UAT finding for #180: "
            "https://example.invalid/issues/900",
            log_output,
        )

        loop = self.worker.read_state()["adversarial"]
        details = loop["filed_finding_details"]
        self.assertEqual(
            len(details), 1,
            "the failing finding must not leave a stub/blank detail entry "
            "behind; only the finding that genuinely made it to GitHub "
            "should be recorded",
        )
        self.assertEqual(details[0]["title"], "Separate parser bug")
        self.assertEqual(details[0]["url"], "https://example.invalid/issues/900")
        self.assertEqual(
            len(loop["filed_findings"]), 1,
            "only the successfully filed finding's marker should be "
            "recorded as filed",
        )

    def test_label_creation_failure_is_absorbed_the_same_as_issue_create_failure(self):
        # ensure_label re-raises WorkerError for any failure other than
        # "already exists" (swarm_issue_worker.py's ensure_label). That
        # WorkerError surfaces from inside file_labelled_issue, before
        # `gh issue create` is ever attempted, and must be absorbed exactly
        # like a failure from issue creation itself.
        loop = self.prepare()
        finding = {"title": "Separate parser bug", "body": "Reproduction details."}

        def gh(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["label", "create"]:
                raise WorkerError("gh: HTTP 403: Resource not accessible by integration")
            if args[:2] == ["issue", "create"]:
                raise AssertionError("issue create must not run if label creation failed")
            return ""

        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            self.worker.file_adversarial_findings(loop, [finding])

        self.assertIn(
            "Could not file out-of-scope adversarial UAT finding for #180",
            captured.getvalue(),
        )
        loop = self.worker.read_state()["adversarial"]
        self.assertEqual(loop["filed_findings"], [])
        self.assertEqual(loop["filed_finding_details"], [])

    def test_failed_filing_can_be_retried_on_a_later_round_without_duplicating_the_issue(self):
        # Round N: a network blip fails `gh issue create` outright (not a
        # malformed-stdout case -- see test_filed_finding_url_never_backfilled.py
        # for that distinct failure mode). The finding must be left entirely
        # unfiled, not half-recorded, so round N+1 retries it as if for the
        # first time.
        loop = self.prepare()
        finding = {"title": "Separate parser bug", "body": "Reproduction details."}
        create_calls = []

        def gh_round_one(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                create_calls.append(args)
                raise WorkerError("gh: connection reset by peer")
            return ""

        with mock.patch.object(self.worker.github, "gh", side_effect=gh_round_one):
            self.worker.file_adversarial_findings(loop, [finding])

        loop = self.worker.read_state()["adversarial"]
        self.assertEqual(loop["filed_findings"], [])
        self.assertEqual(loop["filed_finding_details"], [])
        self.assertEqual(len(create_calls), 1)

        def gh_round_two(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                create_calls.append(args)
                return "https://example.invalid/issues/901"
            return ""

        with mock.patch.object(self.worker.github, "gh", side_effect=gh_round_two), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            self.worker.file_adversarial_findings(loop, [finding])

        self.assertEqual(
            len(create_calls), 2,
            "the retried finding must attempt exactly one more "
            "`gh issue create`, not zero (stuck) and not more than one "
            "(duplicated)",
        )
        loop = self.worker.read_state()["adversarial"]
        self.assertEqual(len(loop["filed_findings"]), 1)
        self.assertEqual(len(loop["filed_finding_details"]), 1)
        self.assertEqual(
            loop["filed_finding_details"][0]["url"], "https://example.invalid/issues/901"
        )
        self.assertIn("https://example.invalid/issues/901", captured.getvalue())

        row = self.worker.history.repository.for_repository(
            self.worker.config.github_repository
        )[0]
        stored = json.loads(row["adversarial_filed_findings"])
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["url"], "https://example.invalid/issues/901")


if __name__ == "__main__":
    unittest.main()
