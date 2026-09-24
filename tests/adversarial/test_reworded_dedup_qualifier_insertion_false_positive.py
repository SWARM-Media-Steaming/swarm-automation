"""Issue #222: `_finding_title_is_reworded_duplicate` in
`issue_worker/adversarial_uat.py` requires high Jaccard similarity AND a
token symmetric difference <= 1 before treating two out-of-scope findings as
the same rediscovered bug. The module's own comment explains this guards
against *substitution* ("Login vs. Signup form, JSON vs. XML parser"),
because swapping one content word for another removes one token and adds a
different one, so the symmetric difference is at least 2.

That reasoning only covers substitution. It does not cover *insertion*: a
second, genuinely distinct bug whose title is the first bug's title plus one
extra qualifying word has a symmetric difference of exactly 1 (one token
added, none removed), so it clears both the similarity bar and the
symmetric-difference bar and is wrongly treated as a reworded rediscovery of
the first finding -- even though the inserted word is exactly what makes it a
different bug:

    "Export fails silently"       (generic: could be any export path)
    "CSV export fails silently"   (specific: the CSV export path)

    tokens: {export, fails, silently} vs {csv, export, fails, silently}
    similarity = 3/4 = 0.75 >= 0.6 ; symmetric difference = {csv} = 1 <= 1
    -> _finding_title_is_reworded_duplicate(...) is True

If a fresh-context tester in one round reports the generic bug and a later
round's fresh-context tester independently reports the specific one (a real,
different defect -- e.g. only the CSV export path is broken, not the whole
export feature, or vice versa), `file_adversarial_findings` silently drops
the second finding instead of filing it. That is a direct, in-scope
regression in #222's own fix: it trades the original bug (duplicate filings
for the same rediscovered defect) for the opposite failure (silently losing
a distinct, real, reproducible defect that should have its own GitHub
issue), which contradicts "A stable finding marker prevents duplicate
auto-filing" -- the marker is supposed to prevent *duplicate* filing, not
suppress unrelated findings.
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


class RewordedDedupQualifierInsertionFalsePositiveTests(unittest.TestCase):
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
            181, "Require fixed output",
            "tracked.txt must contain fixed.", [], "https://example.invalid/issues/181",
        )
        self.worker.choice = ProviderChoice("Claude", "fixer-model", "high", "implementer-session")
        self.git("switch", "-c", "ai/claude/issue-181")
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
            # Round 0's tester finds and files the generic bug.
            self.worker.file_adversarial_findings(loop, [first])
        loop = self.worker.read_state()["adversarial"]
        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            # A later round's fresh-context tester independently finds a
            # *different* bug whose title happens to be the first title
            # plus one qualifying word.
            self.worker.file_adversarial_findings(loop, [second])
        return created, captured.getvalue()

    def test_qualifier_insertion_does_not_suppress_a_distinct_finding(self):
        first = {
            "title": "Export fails silently",
            "body": "Repro: click Export, the dialog closes, no file appears anywhere, no "
                     "error is surfaced. exporter.py:80.",
        }
        second = {
            "title": "CSV export fails silently",
            "body": "Repro: click the CSV export button specifically (other formats work); "
                     "no file is written and no error is shown. exporter.py:120.",
        }
        created, log_output = self._file_two_rounds(first, second)
        self.assertEqual(
            created,
            ["Export fails silently", "CSV export fails silently"],
            "a generic finding and a distinct, more specific finding whose title "
            "merely adds one qualifying word must not be collapsed into a single "
            f"filed issue (log: {log_output!r})",
        )
        loop = self.worker.read_state()["adversarial"]
        self.assertEqual(len(loop["filed_finding_details"]), 2)

    def test_qualifier_insertion_the_other_direction_also_not_suppressed(self):
        first = {
            "title": "Sidebar icon misaligned",
            "body": "Repro: open the main navigation sidebar; the collapse icon sits two "
                     "pixels high of the label. style.css .sidebar .icon.",
        }
        second = {
            "title": "Settings sidebar icon misaligned",
            "body": "Repro: open Settings; the panel's own nested sidebar (a distinct "
                     "component, #view-settings .settings-sidebar) has its icon rendered "
                     "off-baseline. Unrelated to the main nav sidebar.",
        }
        created, log_output = self._file_two_rounds(first, second)
        self.assertEqual(
            created,
            ["Sidebar icon misaligned", "Settings sidebar icon misaligned"],
            "two icon-misalignment bugs in two different sidebar components must "
            f"each get their own GitHub issue (log: {log_output!r})",
        )


if __name__ == "__main__":
    unittest.main()
