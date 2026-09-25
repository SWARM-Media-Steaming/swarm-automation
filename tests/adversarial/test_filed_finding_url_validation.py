"""Adversarial coverage for issue #210: out-of-scope adversarial UAT findings
must be visibly logged, with a filed issue's real URL, not a fabricated one.

The CI-failure filing path (`monitor_repository_actions` in
swarm_issue_worker.py) validates `gh issue create`'s stdout against
`/issues/<n>$` before trusting it as a URL, and logs a distinct warning when
that validation fails instead of pretending the title is a URL. Issue #210's
suggested fix explicitly asks the new `file_adversarial_findings` logging to
mirror that CI-monitor logging convention. If `gh issue create` prints
anything other than a bare issue URL as its last stdout line (a realistic
case: gh prints warnings before the URL for a brand-new label, or a
misconfigured host wrapper prints extra diagnostic lines to stdout),
`file_adversarial_findings` must not report success with a bogus/missing URL
that then gets persisted into execution history and shown to the user in the
Execution History panel as if it were a real link.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import re
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
from swarm_issue_worker import IssueContext, ProviderChoice, ProviderUsage  # noqa: E402


class FiledFindingUrlValidationTests(unittest.TestCase):
    """Exercises `file_adversarial_findings` in isolation, the same way
    `issue_worker/test_adversarial_uat.py`'s own out-of-scope test does, but
    with a `gh issue create` stand-in that returns non-URL stdout."""

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

    def gh_returns(self, create_output):
        def respond(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                return create_output
            return ""
        return respond

    def test_malformed_gh_output_is_not_reported_as_a_successful_filing(self):
        self.prepare()
        loop = self.worker.read_state()["adversarial"]
        finding = {
            "title": "Separate parser bug",
            "body": "Reproduction: malformed sibling endpoint crashes; unrelated to tracked.txt spec.",
        }
        # A realistic `gh issue create` stdout that does not end in a bare
        # issue URL, e.g. a warning about label creation printed to stdout by
        # a wrapped/mocked `gh`, or a transient API hiccup that still exits 0.
        # `monitor_repository_actions`'s sibling filing path treats exactly
        # this shape of output as a failure to capture the URL (it requires
        # the last stdout line to match `/issues/<n>$` before trusting it),
        # and logs a distinct "GitHub did not return an issue URL" warning
        # rather than logging anything resembling a success line.
        malformed_output = "Warning: could not add label to issue\n(no url returned)"
        with mock.patch.object(self.worker.github, "gh", side_effect=self.gh_returns(malformed_output)), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            self.worker.file_adversarial_findings(loop, [finding])

        log_output = captured.getvalue()
        self.assertNotIn(
            "Filed out-of-scope adversarial UAT finding for #180",
            log_output,
            "gh's stdout did not end in a valid GitHub issue URL, so this must not "
            "be logged as a confirmed successful filing -- issue #210's own fix "
            "explicitly asks this path to mirror monitor_repository_actions, which "
            "validates the URL before ever logging success",
        )

        details = self.worker.read_state()["adversarial"]["filed_finding_details"]
        self.assertEqual(len(details), 1)
        url_pattern = re.compile(r"/issues/[0-9]+$")
        self.assertFalse(
            url_pattern.match(details[0]["url"] or ""),
            f"details[0]['url'] = {details[0]['url']!r} was accepted as a real issue "
            "URL despite not matching a GitHub issue URL shape -- no validation is "
            "applied to gh's stdout before it's trusted and persisted",
        )

        if self.worker.config.ai_execution_history_enabled:
            row = self.worker.history.repository.for_repository(
                self.worker.config.github_repository
            )[0]
            stored = json.loads(row["adversarial_filed_findings"])
            self.assertEqual(len(stored), 1)
            self.assertFalse(
                url_pattern.match(stored[0]["url"] or ""),
                "execution history must not persist an unvalidated, non-issue-URL "
                "string as if it were the filed finding's url -- the Execution "
                "History panel renders it as a clickable link",
            )

    def test_valid_gh_url_is_still_logged_and_persisted_normally(self):
        # Control case: confirms the assertions above are meaningful and this
        # suite isn't just failing on any output shape.
        self.prepare()
        loop = self.worker.read_state()["adversarial"]
        finding = {"title": "Separate parser bug", "body": "Reproduction details."}
        with mock.patch.object(
            self.worker.github, "gh",
            side_effect=self.gh_returns("https://example.invalid/issues/182"),
        ), contextlib.redirect_stdout(io.StringIO()) as captured:
            self.worker.file_adversarial_findings(loop, [finding])

        self.assertIn(
            "Filed out-of-scope adversarial UAT finding for #180: "
            "https://example.invalid/issues/182",
            captured.getvalue(),
        )
        details = self.worker.read_state()["adversarial"]["filed_finding_details"]
        self.assertEqual(details[0]["url"], "https://example.invalid/issues/182")


if __name__ == "__main__":
    unittest.main()
