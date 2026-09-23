"""Issue #210: out-of-scope finding log lines are worker stdout that the app
splits on newlines and then allowlists. Titles interpolated into those lines
must not create a second log event, and a duplicated finding in one report
must log both the create and the already-filed cases without opening a
second GitHub issue.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
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


class FiledFindingLogIntegrityTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self):
        self.worker.config = dataclasses.replace(
            self.worker.config,
            adversarial_uat_enabled=True,
            ai_execution_history_enabled=True,
            execution_history_db=self.state / "history.sqlite3",
        )
        self.worker.history = ExecutionHistoryService(
            True, self.state / "history.sqlite3"
        )
        self.worker.issue = IssueContext(
            180,
            "Require fixed output",
            "tracked.txt must contain fixed.",
            [],
            "https://example.invalid/issues/180",
        )
        self.worker.choice = ProviderChoice(
            "Claude", "fixer-model", "high", "implementer-session"
        )
        self.git("switch", "-c", "ai/claude/issue-180")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()
        self.worker.initialize_adversarial(
            self.git("rev-parse", "HEAD"), "## Summary\nImplementation"
        )

    def marker_for(self, finding):
        digest = hashlib.sha256(json.dumps(finding, sort_keys=True).encode()).hexdigest()[:20]
        return f"<!-- swarm-issue-worker:adversarial-finding:issue:180;id:{digest} -->"

    def test_multiline_title_stays_on_one_log_line(self):
        # emit_log / parseAutomationLog are line-oriented. A tester title with
        # an embedded newline would otherwise become a second automation.log
        # event, and a title that starts "ERROR:" on that second line would
        # show up as a fake worker failure in Info & Debug.
        self.prepare()
        finding = {
            "title": "Parser bug\nERROR: GitHub authentication failed",
            "body": "Reproduction details, unrelated to #180.",
        }
        loop = self.worker.read_state()["adversarial"]
        loop["filed_findings"] = [self.marker_for(finding)]
        self.worker.save_adversarial(loop)

        def gh(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                self.fail("local-marker retry must not create a second GitHub issue")
            return ""

        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()) as captured, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.worker.file_adversarial_findings(loop, [finding])

        stdout = captured.getvalue()
        stderr = err.getvalue()
        combined_lines = [line for line in (stdout + stderr).splitlines() if line.strip()]
        self.assertEqual(
            len(combined_lines),
            1,
            "a newline in the tester title must not become a second automation.log event; "
            f"got {combined_lines!r}",
        )
        self.assertIn("already filed for #180:", combined_lines[0])
        self.assertFalse(
            any(line.lstrip().startswith("ERROR:") for line in (stdout + stderr).splitlines()),
            "the title's second line must not be emitted as its own ERROR log event",
        )

    def test_duplicate_findings_in_one_report_create_once_and_log_both_paths(self):
        self.prepare()
        finding = {"title": "Separate parser bug", "body": "Reproduction details."}
        created = []

        def gh(args, provider=None, body=None):
            if args[:1] == ["label"]:
                return ""
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                created.append(args[args.index("--title") + 1])
                return "https://example.invalid/issues/182"
            return ""

        loop = self.worker.read_state()["adversarial"]
        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            self.worker.file_adversarial_findings(loop, [finding, dict(finding)])

        log_output = captured.getvalue()
        self.assertEqual(created, ["Separate parser bug"])
        self.assertIn(
            "Filed out-of-scope adversarial UAT finding for #180: "
            "https://example.invalid/issues/182",
            log_output,
        )
        self.assertIn("already filed for #180: Separate parser bug", log_output)
        stored = json.loads(
            self.worker.history.repository.for_repository(
                self.worker.config.github_repository
            )[0]["adversarial_filed_findings"]
        )
        self.assertEqual(
            stored,
            [{"title": "Separate parser bug", "url": "https://example.invalid/issues/182"}],
        )


if __name__ == "__main__":
    unittest.main()
