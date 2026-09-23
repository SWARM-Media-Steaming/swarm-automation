"""Adversarial coverage for issue #210: the title an out-of-scope adversarial
UAT finding is persisted and surfaced under must match the title of the
GitHub issue it actually links to.

`file_adversarial_findings` (`issue_worker/adversarial_uat.py`) files the
GitHub issue with a truncated title -- `file_labelled_issue(finding["title"][:120],
...)`, matching `gh issue create`'s own title-length limits and the sibling
CI-failure-issue path's convention -- but then records the *untruncated*
`finding["title"]` into `filed_finding_details` and, from there, into
`adversarial_filed_findings` execution history
(`issue_worker/adversarial_uat.py` ~line 372). A tester's `out_of_scope`
`title` field has no length cap enforced anywhere upstream
(`result_payload` in the same module only requires a non-empty string), so
this is directly reachable: a verbose tester model can easily emit a title
far longer than 120 characters.

The result is a receipt that lies about what it links to: the Execution
History panel (`ui/app.js`, the `adversarialFiledFindings` rendering added by
this same issue) renders `finding.title` directly as the clickable link's
label (`externalLink(finding.title || ..., finding.url, ...)`), so a user
sees one (possibly very long, unbounded) string as the link text while the
GitHub issue at the other end of that link actually has a different,
truncated title. That is a correctness bug introduced by this issue's own
diff, not a pre-existing, unrelated one -- so it belongs in a blocking
adversarial suite, not an out-of-scope follow-up issue.
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
from swarm_issue_worker import IssueContext, ProviderChoice  # noqa: E402


class FiledFindingTitleTruncationTests(unittest.TestCase):
    """`file_adversarial_findings` in isolation, mirroring the harness used by
    `issue_worker/test_adversarial_uat.py`'s own out-of-scope tests."""

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

    def test_persisted_title_matches_the_title_the_filed_issue_actually_has(self):
        self.prepare()
        loop = self.worker.read_state()["adversarial"]
        long_title = "Separate parser bug: " + ("crashes on a deeply nested malformed payload " * 5)
        self.assertGreater(len(long_title), 120, "fixture must actually exercise gh's title cap")
        finding = {"title": long_title, "body": "Reproduction details, unrelated to #180's spec."}

        captured_titles: list[str] = []

        def gh(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                captured_titles.append(args[args.index("--title") + 1])
                return "https://example.invalid/issues/182"
            return ""

        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()):
            self.worker.file_adversarial_findings(loop, [finding])

        self.assertEqual(len(captured_titles), 1)
        actual_issue_title = captured_titles[0]
        self.assertLessEqual(
            len(actual_issue_title), 120,
            "file_labelled_issue is always called with finding['title'][:120]; if this "
            "fixture's own truncation assumption is wrong the rest of the assertion is moot",
        )

        details = self.worker.read_state()["adversarial"]["filed_finding_details"]
        self.assertEqual(len(details), 1)
        self.assertEqual(
            details[0]["title"], actual_issue_title,
            "filed_finding_details recorded the untruncated tester-supplied title "
            f"({details[0]['title']!r}) instead of the title the GitHub issue at "
            f"{details[0]['url']!r} actually has ({actual_issue_title!r}) -- the app "
            "will show a link labelled with text that doesn't match what it points to",
        )

        row = self.worker.history.repository.for_repository(
            self.worker.config.github_repository
        )[0]
        stored = json.loads(row["adversarial_filed_findings"])
        self.assertEqual(len(stored), 1)
        self.assertEqual(
            stored[0]["title"], actual_issue_title,
            "execution history persisted the untruncated title too, so the mismatch "
            "reaches the Execution History panel's rendering, not just in-memory state",
        )


if __name__ == "__main__":
    unittest.main()
