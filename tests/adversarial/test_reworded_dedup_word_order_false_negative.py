"""Issue #222: the reworded-rediscovery dedup added to
`file_adversarial_findings` requires, in addition to unigram Jaccard
similarity and a small symmetric difference, that the two titles' *bigrams*
(adjacent-token pairs, in title order -- see `_finding_title_bigrams` in
`issue_worker/adversarial_uat.py`) overlap by at least
`FINDING_TITLE_BIGRAM_SIMILARITY_THRESHOLD` (0.75).

That bigram gate was added to stop a qualifier *insertion* ("Export fails
silently" -> "CSV export fails silently") from being treated as a reword of
a distinct finding, since inserting a word next to the head noun it
qualifies breaks most adjacent-word pairings. But the same mechanism throws
out the exact case #222 exists to fix: a genuine, same-bug rediscovery
whose fresh-context tester wrote the sentence in a different word order.
Issue #222 requires "Each tester invocation has fresh context ... never the
implementer's transcript or reasoning" (adversarial-uat-testing.md), and
real testers restating an identical defect very often front-load a
different clause ("Empty YAML file crashes the config parser" instead of
"Config parser crashes on empty YAML") -- not just append one qualifying
word. Reordering the same tokens collapses bigram overlap to near zero even
though the unigram set is nearly identical (Jaccard 0.83, symmetric
difference 1), so `_finding_title_is_reworded_duplicate` returns False and
`file_adversarial_findings` files a second GitHub issue for the identical
bug -- the exact "unstable across rounds" duplicate-filing defect #222's
own title names, now reproduced *through* the round-2 fix rather than
before it.

Confirmed directly against `_finding_title_is_reworded_duplicate`:

    _finding_title_is_reworded_duplicate(
        "Config parser crashes on empty YAML",
        "Empty YAML file crashes config parser",
    )  # -> False (unigram Jaccard 0.833, symmetric difference 1,
       #     bigram Jaccard 0.286 < 0.75)

This is in scope for #222 (it is #222's own fix failing to fix the bug the
issue describes), not an unrelated defect, so it is a blocking adversarial
test rather than an out-of-scope finding.
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


class RewordedDedupWordOrderFalseNegativeTests(unittest.TestCase):
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
            # Round 0's tester finds and files the bug.
            self.worker.file_adversarial_findings(loop, [first])
        loop = self.worker.read_state()["adversarial"]
        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            # A later round's fresh-context tester independently rediscovers
            # the *same* bug and phrases the title in a different word order.
            self.worker.file_adversarial_findings(loop, [second])
        return created, captured.getvalue()

    def test_reordered_rediscovery_of_the_same_bug_is_not_refiled(self):
        first = {
            "title": "Config parser crashes on empty YAML",
            "body": "Repro: run parse_config() with an empty file. Traceback at line 42.",
        }
        second = {
            "title": "Empty YAML file crashes config parser",
            "body": "Steps to reproduce: parse_config() raises on a zero-byte YAML input.",
        }
        created, log_output = self._file_two_rounds(first, second)
        self.assertEqual(
            created,
            ["Config parser crashes on empty YAML"],
            "a later round's fresh-context tester independently rediscovering "
            "the identical bug in a different word order must be recognized "
            "as the same finding, not filed as a second GitHub issue "
            f"(log: {log_output!r})",
        )
        loop = self.worker.read_state()["adversarial"]
        self.assertEqual(len(loop["filed_finding_details"]), 1)

    def test_reordered_rediscovery_with_reworded_verb_is_not_refiled(self):
        first = {
            "title": "Upload endpoint times out on large files",
            "body": "Repro: PUT a 2GB file to /upload; the request hangs past the 30s timeout.",
        }
        second = {
            "title": "Large file uploads cause the endpoint to time out",
            "body": "Steps: upload a multi-gigabyte file via /upload and observe the request hang.",
        }
        created, log_output = self._file_two_rounds(first, second)
        self.assertEqual(
            created,
            ["Upload endpoint times out on large files"],
            "reordering plus light rewording of the same finding must still "
            f"dedup against the earlier filing (log: {log_output!r})",
        )


if __name__ == "__main__":
    unittest.main()
