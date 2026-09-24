"""Issue #222: `_finding_title_is_reworded_duplicate` in
`issue_worker/adversarial_uat.py` special-cases *where* a single extra token
lands to tell a genuinely distinct, more specific finding apart from a
reworded rediscovery of the same bug:

    extra_token = next(iter(symmetric_difference))
    extra_sequence = tokens_a if extra_token in set_a else tokens_b
    return extra_sequence[0] != extra_token

That is: if the one differing token is the *first* token of the title that
contains it, treat the titles as distinct (a prepended qualifier narrows the
sentence's own subject -- "CSV export...", "Settings sidebar..."); otherwise
treat them as the same bug reworded. `test_reworded_dedup_qualifier_
insertion_false_positive.py` locks in the leading-position half of that
rule.

But a distinguishing content word does not only ever get prepended. A
fresh-context tester restating a bug just as often inserts the distinguishing
word in the *middle* of the sentence (before the trailing noun it qualifies)
or appends it at the *end* -- and the current rule treats every non-leading
position as "must be the same bug," with no regard for whether the inserted
word is actually the kind of distinguishing content word the fix exists to
protect (a format, a component, a platform), because it never revisits
`extra_sequence` at any index other than 0. That contradicts the fix's own
stated goal ("the actual defects... are unrelated" for "JSON vs. XML
parser") whenever the second word is inserted anywhere but the front:

    "Export crashes on large files"
    "Export crashes on large XML files"
    -> tokens: {export, crashe, large, file} vs {export, crashe, large, xml, file}
    -> similarity = 4/5 = 0.8 >= 0.6 ; symmetric difference = {xml}, len 1
    -> extra_token "xml" is at index 3 of its sequence, not index 0
    -> _finding_title_is_reworded_duplicate(...) is True (wrongly merged)

    "Video playback stutters"
    "Video playback stutters on Safari"
    -> symmetric difference = {safari} (appended at the end)
    -> _finding_title_is_reworded_duplicate(...) is True (wrongly merged)

If round 0's tester files the generic finding first, a later round's
fresh-context tester independently finding the format-specific or
platform-specific bug is silently dropped by `file_adversarial_findings`
instead of filed as its own GitHub issue -- the exact "real findings...
must not block delivery [but] must still be filed" requirement this fix was
supposed to satisfy for insertions, just at a different token position than
the one case the existing regression test already covers. This is a direct,
in-scope gap in #222's own fix (the same false-negative failure mode as the
leading-qualifier case, merely at a different insertion point), not an
unrelated defect.
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


class RewordedDedupNonleadingQualifierInsertionFalsePositiveTests(unittest.TestCase):
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
            182, "Require fixed output",
            "tracked.txt must contain fixed.", [], "https://example.invalid/issues/182",
        )
        self.worker.choice = ProviderChoice("Claude", "fixer-model", "high", "implementer-session")
        self.git("switch", "-c", "ai/claude/issue-182")
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
            # *different*, more specific bug whose title inserts one
            # distinguishing word somewhere other than the very front.
            self.worker.file_adversarial_findings(loop, [second])
        return created, captured.getvalue()

    def test_mid_sentence_format_qualifier_is_not_suppressed(self):
        first = {
            "title": "Export crashes on large files",
            "body": "Repro: export any file over 500MB; the process dies with no error. "
                     "exporter.py:200.",
        }
        second = {
            "title": "Export crashes on large XML files",
            "body": "Repro: export a large .xml file specifically (large CSV/JSON export "
                     "both work fine); the XML serializer runs out of memory. "
                     "xml_exporter.py:64.",
        }
        created, log_output = self._file_two_rounds(first, second)
        self.assertEqual(
            created,
            ["Export crashes on large files", "Export crashes on large XML files"],
            "a generic finding and a distinct, format-specific finding whose "
            "title inserts the distinguishing word before the trailing noun "
            f"must not be collapsed into a single filed issue (log: {log_output!r})",
        )
        loop = self.worker.read_state()["adversarial"]
        self.assertEqual(len(loop["filed_finding_details"]), 2)

    def test_trailing_platform_qualifier_is_not_suppressed(self):
        first = {
            "title": "Video playback stutters",
            "body": "Repro: play any video longer than a minute; frames drop steadily "
                     "across Chrome, Firefox and Safari. player.js:310.",
        }
        second = {
            "title": "Video playback stutters on Safari",
            "body": "Repro: Chrome and Firefox play smoothly; only Safari's "
                     "hardware-decode path drops frames. player.js:412, Safari-only "
                     "codepath.",
        }
        created, log_output = self._file_two_rounds(first, second)
        self.assertEqual(
            created,
            ["Video playback stutters", "Video playback stutters on Safari"],
            "a generic finding and a distinct, platform-specific finding whose "
            "title merely appends the distinguishing word at the end must not "
            f"be collapsed into a single filed issue (log: {log_output!r})",
        )


if __name__ == "__main__":
    unittest.main()
