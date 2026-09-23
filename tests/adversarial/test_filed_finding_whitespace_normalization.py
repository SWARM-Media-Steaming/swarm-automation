"""Adversarial coverage for issue #210: the title recorded for an out-of-scope
adversarial UAT finding must match the title of the GitHub issue it actually
links to -- not just after length truncation (already covered by
`test_filed_finding_title_truncation.py`), but also when the tester's raw
title contains irregular whitespace.

`file_adversarial_findings` (`issue_worker/adversarial_uat.py` ~line 353)
computes the *stored* title as:

    title = " ".join(finding["title"][:120].split())

which collapses runs of whitespace (newlines, tabs, repeated spaces) down to
single spaces. But the title actually sent to GitHub, a few lines later, is
the raw, un-normalized value:

    output = self.file_labelled_issue(finding["title"][:120], ...)

`file_labelled_issue` (`issue_worker/swarm_issue_worker.py` ~line 5463) passes
that string straight through as `gh issue create --title <title>` with no
whitespace collapsing of its own. A tester's `out_of_scope` `title` field is
free-form AI output with no whitespace constraint enforced anywhere upstream
(`result_payload` only requires a non-empty string), so a title containing an
embedded newline or doubled space is directly reachable -- exactly the same
reachability argument the title-truncation test already established for
length.

The result is the same class of bug the truncation test targets: the receipt
recorded into `filed_finding_details` / `adversarial_filed_findings` execution
history, and rendered as the Execution History panel's link label
(`ui/app.js`, `externalLink(finding.title || ..., finding.url, ...)`), can
read differently than the `--title` argument actually used to create the
GitHub issue -- a correctness bug in this issue's own diff, not a pre-existing
unrelated one, so it belongs in a blocking adversarial suite.
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


class FiledFindingWhitespaceNormalizationTests(unittest.TestCase):
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
        # An embedded newline plus a doubled space: well under the 120-char
        # cap, so this exercises whitespace normalization in isolation from
        # the already-covered length-truncation case.
        raw_title = "Separate parser bug:\ncrashes on a  deeply nested payload"
        finding = {"title": raw_title, "body": "Reproduction details, unrelated to #180's spec."}

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
        self.assertEqual(
            actual_issue_title, raw_title[:120],
            "file_labelled_issue is always called with finding['title'][:120] verbatim; "
            "if this fixture's own assumption is wrong the rest of the assertion is moot",
        )

        details = self.worker.read_state()["adversarial"]["filed_finding_details"]
        self.assertEqual(len(details), 1)
        self.assertEqual(
            details[0]["title"], actual_issue_title,
            "filed_finding_details recorded a whitespace-normalized title "
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
            "execution history persisted the normalized title too, so the mismatch "
            "reaches the Execution History panel's rendering, not just in-memory state",
        )


if __name__ == "__main__":
    unittest.main()
