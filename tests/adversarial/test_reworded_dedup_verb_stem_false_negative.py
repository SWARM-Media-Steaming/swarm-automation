"""Issue #222: `_finding_title_stem` in `issue_worker/adversarial_uat.py`
normalizes plurals/verb-forms by blindly stripping a single trailing "s"
(when the word is longer than 3 characters and doesn't end "ss"):

    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]

That is correct for regular plurals ("uploads" -> "upload") but wrong for
any verb whose third-person singular is formed with "-es" onto a stem that
already ends in a sibilant ("crash" -> "crashes", "fix" -> "fixes", "catch"
-> "catches", "stash" -> "stashes"). Stripping only the final "s" leaves a
trailing "e"/consonant that never matches the base form:

    _finding_title_stem("crashes") == "crashe"
    _finding_title_stem("crash")   == "crash"
    # "crashe" != "crash" -- the same verb stems to two different tokens
    # depending only on which grammatical form a tester happened to write.

This is exactly the failure mode issue #222 exists to fix: a later round's
fresh-context tester rediscovering an already-filed out-of-scope bug almost
never reuses the exact wording (see adversarial-uat-testing.md: "fresh
context ... never the implementer's transcript"), and swapping "crashes" for
"crash" (or "fixes" for "fix") between rounds is a completely ordinary
rewording, not a different bug. But because the two verb forms stem to
different tokens, the token-set symmetric difference is 2 (one token
removed, a different one added) instead of 0, which fails
`FINDING_TITLE_MAX_SYMMETRIC_DIFFERENCE` (<=1) -- the exact same rejection
path the module's own comment reserves for genuine *content-word*
substitutions ("Login vs. Signup form, JSON vs. XML parser"). The rediscovery
is wrongly treated as a new, distinct bug and filed as a second GitHub issue
for the identical defect -- reproducing #222's own "duplicate GitHub issues
for the sam[e]" title defect through a very ordinary verb-form rewording,
not through anything exotic.

Confirmed directly against `_finding_title_is_reworded_duplicate`:

    _finding_title_is_reworded_duplicate(
        "Upload endpoint crashes on large files",
        "Large file uploads crash the endpoint",
    )  # -> False (should be True: identical bug, only the verb form differs)

This is in scope for #222 (the fix's own stemming helper fails to recognize
an ordinary verb-form rewording of the identical bug), not an unrelated
defect, so it is a blocking adversarial test rather than an out-of-scope
finding.
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


class RewordedDedupVerbStemFalseNegativeTests(unittest.TestCase):
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
            183, "Require fixed output",
            "tracked.txt must contain fixed.", [], "https://example.invalid/issues/183",
        )
        self.worker.choice = ProviderChoice("Claude", "fixer-model", "high", "implementer-session")
        self.git("switch", "-c", "ai/claude/issue-183")
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
            # Round 0's tester finds and files the bug, using the "-es"
            # verb form in its title.
            self.worker.file_adversarial_findings(loop, [first])
        loop = self.worker.read_state()["adversarial"]
        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            # A later round's fresh-context tester independently rediscovers
            # the *same* bug and happens to phrase the verb differently.
            self.worker.file_adversarial_findings(loop, [second])
        return created, captured.getvalue()

    def test_singular_verb_form_rediscovery_is_not_refiled(self):
        first = {
            "title": "Upload endpoint crashes on large files",
            "body": "Repro: PUT a 2GB file to /upload; the process dies with no error. "
                     "uploader.py:140.",
        }
        second = {
            "title": "Large file uploads crash the endpoint",
            "body": "Steps: upload a multi-gigabyte file via /upload and observe the "
                     "process die. uploader.py:150.",
        }
        created, log_output = self._file_two_rounds(first, second)
        self.assertEqual(
            created,
            ["Upload endpoint crashes on large files"],
            "a later round's fresh-context tester independently rediscovering "
            "the identical bug with the verb written as \"crash\" instead of "
            "\"crashes\" must be recognized as the same finding, not filed as "
            f"a second GitHub issue (log: {log_output!r})",
        )
        loop = self.worker.read_state()["adversarial"]
        self.assertEqual(len(loop["filed_finding_details"]), 1)

    def test_other_es_verb_rediscovery_is_not_refiled(self):
        first = {
            "title": "Search index fixes stale results",
            "body": "Repro: reindex, query immediately; results reflect the pre-reindex "
                     "state for several seconds. search.py:90.",
        }
        second = {
            "title": "Stale results fix broken by search index",
            "body": "Steps to reproduce: reindex then query right away; stale results "
                     "are returned. search.py:95.",
        }
        created, log_output = self._file_two_rounds(first, second)
        self.assertEqual(
            created,
            ["Search index fixes stale results"],
            "a later round's fresh-context tester rediscovering the same bug "
            "with \"fix\" instead of \"fixes\" must dedup against the earlier "
            f"filing (log: {log_output!r})",
        )


if __name__ == "__main__":
    unittest.main()
