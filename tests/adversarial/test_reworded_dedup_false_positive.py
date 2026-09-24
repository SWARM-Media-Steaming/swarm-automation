"""Issue #222: the reworded-rediscovery dedup added to
`file_adversarial_findings` treats two findings as the same rediscovered bug
whenever their title-token Jaccard similarity is >= 0.6
(`FINDING_TITLE_SIMILARITY_THRESHOLD` in `issue_worker/adversarial_uat.py`).

That threshold is meant to catch the same real bug described in different
words across rounds (see `test_adversarial_uat.py`'s
`test_reworded_rediscovery_of_a_prior_finding_is_not_refiled`). But two
*genuinely distinct* bugs that happen to share a common phrasing template
("<subject> crashes on empty <field>", "<component> crashes on empty
<format> files") clear the same 0.6 bar purely from the shared scaffolding
words, even though the actual defects (a login-form bug vs. a signup-form
bug; a JSON-parsing bug vs. an XML-parsing bug) are unrelated and both need
their own GitHub issue.

`_finding_title_similarity` confirms the exact boundary this test exercises:

    "Login form crashes on empty username field"
    "Signup form crashes on empty username field"
    -> 5/7 = 0.714... >= 0.6

Per adversarial-uat-testing.md, "Real findings outside this issue's scope
must not block delivery" -- they must still be filed as their own issues.
Silently merging a second, unrelated real bug into the first is exactly the
kind of dropped evidence that rule exists to prevent, and it is a direct,
in-scope regression introduced by #222's own fix (a false negative traded
for a false positive), not a pre-existing or unrelated defect.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
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


class RewordedDedupFalsePositiveTests(unittest.TestCase):
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

    def _file_two_rounds(self, first, second):
        created = []

        def gh(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                created.append(args[args.index("--title") + 1])
                return f"https://example.invalid/issues/{900 + len(created)}"
            return ""

        loop = self.prepare()
        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()):
            # Round 0's tester finds and files the first bug.
            self.worker.file_adversarial_findings(loop, [first])
        loop = self.worker.read_state()["adversarial"]
        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            # A later round's fresh-context tester independently finds the
            # *second*, unrelated bug and reports it.
            self.worker.file_adversarial_findings(loop, [second])
        return created, captured.getvalue()

    def test_two_distinct_bugs_sharing_a_phrasing_template_are_both_filed(self):
        first = {
            "title": "Login form crashes on empty username field",
            "body": "Repro: submit the login form with username=''. NPE in auth.py:88.",
        }
        second = {
            "title": "Signup form crashes on empty username field",
            "body": "Repro: submit the signup form with username=''. NPE in registration.py:41.",
        }
        created, log_output = self._file_two_rounds(first, second)
        self.assertEqual(
            created,
            [
                "Login form crashes on empty username field",
                "Signup form crashes on empty username field",
            ],
            "two unrelated real bugs that merely share a phrasing template "
            "must each get their own GitHub issue; the second must not be "
            "silently absorbed as a 'reworded rediscovery' of the first "
            f"(log: {log_output!r})",
        )
        loop = self.worker.read_state()["adversarial"]
        self.assertEqual(len(loop["filed_finding_details"]), 2)

    def test_distinct_parser_format_bugs_are_both_filed(self):
        first = {
            "title": "Config parser crashes on empty JSON files",
            "body": "Repro: run parse_config() with an empty .json file. Traceback at config.py:42.",
        }
        second = {
            "title": "Config parser crashes on empty XML files",
            "body": "Repro: run parse_config() with an empty .xml file. Traceback at config.py:57.",
        }
        created, log_output = self._file_two_rounds(first, second)
        self.assertEqual(
            created,
            [
                "Config parser crashes on empty JSON files",
                "Config parser crashes on empty XML files",
            ],
            "a JSON-parsing bug and an unrelated XML-parsing bug must not "
            f"be merged just because their titles overlap (log: {log_output!r})",
        )


if __name__ == "__main__":
    unittest.main()
