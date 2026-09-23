"""Issue #210: a filed out-of-scope finding whose URL capture failed once
must not stay permanently broken.

`file_adversarial_findings` records a finding's marker in
`loop["filed_findings"]` the very first time it is processed, regardless of
whether `gh issue create`'s stdout actually yielded a usable issue URL (see
`test_filed_finding_url_validation.py` for that validation itself). On every
later call for the *same* finding (the tester can legitimately report the
same out-of-scope bug again on a following adversarial round), the function
short-circuits on `marker in loop["filed_findings"]` and `continue`s before
ever reaching the `gh issue list --search` dedup lookup that would confirm
the real, now-discoverable issue URL.

That means a single transient hiccup in `gh issue create`'s stdout (a stray
warning line before the URL -- exactly the realistic case the malformed-
output test already exercises) permanently locks the finding's persisted
`url` at `""` for the rest of the issue's lifetime, even though the GitHub
issue was in fact created and every subsequent round's `gh issue list`
search would find it. The Execution History panel and the actionable log
then show a dead/missing link for a finding that really was filed --
precisely the kind of invisible-to-the-user gap issue #210 asks the app to
stop having.
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


class FiledFindingUrlNeverBackfilledTests(unittest.TestCase):
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

    def test_a_later_round_recovers_the_real_url_once_the_issue_is_discoverable(self):
        self.prepare()
        loop = self.worker.read_state()["adversarial"]
        finding = {
            "title": "Separate parser bug",
            "body": "Reproduction: malformed sibling endpoint crashes; unrelated to tracked.txt spec.",
        }

        # Round N: gh issue create succeeds (the issue really is created) but
        # its stdout is noisy, so the URL can't be captured -- the same shape
        # `test_filed_finding_url_validation.py` exercises in isolation.
        def gh_round_one(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                return "Warning: could not add label to issue\n(no url returned)"
            return ""

        with mock.patch.object(self.worker.github, "gh", side_effect=gh_round_one):
            self.worker.file_adversarial_findings(loop, [finding])

        loop = self.worker.read_state()["adversarial"]
        details = loop["filed_finding_details"]
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["url"], "")
        marker = details[0]["marker"]

        # Round N+1: the same out-of-scope bug is reported again (a fresh
        # tester invocation re-finding the same real issue). The GitHub issue
        # from round N is now genuinely discoverable via search, with a
        # proper URL, exactly like the cross-session dedup case that
        # `test_filed_finding_surfaces.py`'s
        # `test_github_search_dedup_logs_existing_url_and_does_not_create_again`
        # covers for a *first-time* local call -- the only difference here is
        # this finding's marker is already present in `loop["filed_findings"]`
        # from round N.
        def gh_round_two(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return json.dumps([{
                    "body": f"{marker}\nFound while testing #180; outside that delivery's scope.\n",
                    "url": "https://example.invalid/issues/999",
                }])
            if args[:2] == ["issue", "create"]:
                raise AssertionError(
                    "the issue was already filed in round one; a second "
                    "gh issue create would duplicate it on GitHub"
                )
            return ""

        with mock.patch.object(self.worker.github, "gh", side_effect=gh_round_two), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            self.worker.file_adversarial_findings(loop, [finding])

        loop = self.worker.read_state()["adversarial"]
        details = loop["filed_finding_details"]
        self.assertEqual(len(details), 1)
        self.assertEqual(
            details[0]["url"], "https://example.invalid/issues/999",
            "the finding's real, now-discoverable GitHub issue URL was never "
            "recovered: file_adversarial_findings short-circuits on the local "
            "marker before ever re-checking `gh issue list`, so a one-time "
            "stdout hiccup during the first filing permanently locks the "
            "persisted url at '' even though the issue genuinely exists and "
            "is searchable -- the Execution History panel and actionable log "
            "will show a dead link for this finding forever",
        )

        row = self.worker.history.repository.for_repository(
            self.worker.config.github_repository
        )[0]
        stored = json.loads(row["adversarial_filed_findings"])
        self.assertEqual(len(stored), 1)
        self.assertEqual(
            stored[0]["url"], "https://example.invalid/issues/999",
            "execution history must also reflect the recovered URL, not the "
            "empty one persisted by the first, malformed-stdout attempt",
        )

        self.assertIn(
            "https://example.invalid/issues/999", captured.getvalue(),
            "the recovered URL should be logged once it's found, the same "
            "way a normal cross-session dedup hit is logged",
        )


if __name__ == "__main__":
    unittest.main()
